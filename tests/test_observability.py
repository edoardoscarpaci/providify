"""Failing (red) tests for Plan 009 — container observability hooks (F6).

These tests describe behaviour that does NOT exist yet:
    - ``providify/observability.py`` — the four frozen dataclass events
      (``InstanceCreated``, ``InstanceDisposed``, ``ScopeEntered``,
      ``ScopeExited``) and the ``ContainerEvent`` type alias.
    - ``DIContainer.add_hook()`` / ``remove_hook()`` / ``_emit()`` — hook
      registration and dispatch, keyed by exact event type.
    - Instrumentation of singleton/request/session/dependent creation,
      shutdown/ashutdown disposal, scoped pre-destroy disposal, and the
      ``request()``/``arequest()``/``session()``/``asession()`` façades.

See plans/009-observability-hooks.md, Steps 1, 3, 6, 7, 10, 13.

Every test in this file is expected to fail right now with an ImportError
(the ``providify.observability`` module does not exist) or, for tests that
only need the container, an AttributeError (``add_hook``/``remove_hook``
are not yet defined on ``DIContainer``) — never a logic/assertion failure
that would suggest the feature is partially built.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from unittest import mock

import pytest

from providify.container import DIContainer
from providify.decorator.lifecycle import PreDestroy
from providify.decorator.scope import Provider, RequestScoped, Singleton
from providify.metadata import Scope

# Importing the event dataclasses is expected to fail with ImportError until
# providify/observability.py exists (Step 2 of the plan).
from providify.observability import (
    ContainerEvent,
    InstanceCreated,
    InstanceDisposed,
    ScopeEntered,
    ScopeExited,
)


class _Slow:
    """Module-level target for a @Provider return-type annotation.

    Must live at module scope (not nested inside a test method) — a
    function-local class annotation cannot be resolved by
    `get_type_hints()` at runtime (see `binding.py`'s `ProviderBinding`
    return-type resolution), which is exactly what
    `test_duration_ns_reflects_slow_provider` needs `make_slow`'s `-> _Slow`
    annotation to survive.
    """


# ─────────────────────────────────────────────────────────────────
#  Step 1 — event dataclasses in isolation (no container)
# ─────────────────────────────────────────────────────────────────


class TestEventDataclassesInIsolation:
    """Frozen, kw_only, hashable/equatable value objects — no container involved."""

    def test_instance_created_is_frozen(self) -> None:
        """Mutating a field after construction must raise FrozenInstanceError."""
        event = InstanceCreated(
            interface=str,
            implementation=str,
            scope=Scope.SINGLETON,
            qualifier=None,
            duration_ns=1,
            is_async=False,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.duration_ns = 2  # type: ignore[misc]

    def test_instance_disposed_is_frozen(self) -> None:
        """Mutating a field after construction must raise FrozenInstanceError."""
        event = InstanceDisposed(
            interface=str,
            implementation=str,
            scope=Scope.SINGLETON,
            owner="str.teardown",
            duration_ns=1,
            error=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.owner = "other"  # type: ignore[misc]

    def test_scope_entered_is_frozen(self) -> None:
        """Mutating a field after construction must raise FrozenInstanceError."""
        event = ScopeEntered(kind="request", scope_id="abc")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.scope_id = "def"  # type: ignore[misc]

    def test_scope_exited_is_frozen(self) -> None:
        """Mutating a field after construction must raise FrozenInstanceError."""
        event = ScopeExited(kind="request", scope_id="abc", duration_ns=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.duration_ns = 2  # type: ignore[misc]

    def test_instance_created_is_kw_only(self) -> None:
        """Positional construction must raise TypeError — kw_only=True."""
        with pytest.raises(TypeError):
            InstanceCreated(str, str, Scope.SINGLETON, None, 1, False)  # type: ignore[misc]

    def test_instance_disposed_is_kw_only(self) -> None:
        """Positional construction must raise TypeError — kw_only=True."""
        with pytest.raises(TypeError):
            InstanceDisposed(str, str, Scope.SINGLETON, "owner", 1, None)  # type: ignore[misc]

    def test_scope_entered_is_kw_only(self) -> None:
        """Positional construction must raise TypeError — kw_only=True."""
        with pytest.raises(TypeError):
            ScopeEntered("request", "abc")  # type: ignore[misc]

    def test_scope_exited_is_kw_only(self) -> None:
        """Positional construction must raise TypeError — kw_only=True."""
        with pytest.raises(TypeError):
            ScopeExited("request", "abc", 1)  # type: ignore[misc]

    def test_instance_created_hashable_and_equatable_by_value(self) -> None:
        """Two events with identical field values must hash and compare equal."""
        kwargs = dict(
            interface=str,
            implementation=str,
            scope=Scope.SINGLETON,
            qualifier=None,
            duration_ns=1,
            is_async=False,
        )
        a = InstanceCreated(**kwargs)
        b = InstanceCreated(**kwargs)
        assert a == b
        assert hash(a) == hash(b)

    def test_scope_entered_hashable_and_equatable_by_value(self) -> None:
        """Two events with identical field values must hash and compare equal."""
        a = ScopeEntered(kind="session", scope_id="s1")
        b = ScopeEntered(kind="session", scope_id="s1")
        assert a == b
        assert hash(a) == hash(b)

    def test_container_event_alias_covers_exactly_the_four_types(self) -> None:
        """ContainerEvent must be a union of exactly the four event dataclasses."""
        import typing

        args = set(typing.get_args(ContainerEvent))
        assert args == {InstanceCreated, InstanceDisposed, ScopeEntered, ScopeExited}


# ─────────────────────────────────────────────────────────────────
#  Step 3 — hook registration semantics
# ─────────────────────────────────────────────────────────────────


class TestHookRegistration:
    """add_hook / remove_hook contract, independent of instrumentation sites."""

    def test_add_hook_returns_unsubscribe_callable(
        self, container: DIContainer
    ) -> None:
        """add_hook must return a zero-arg callable that removes the registration."""
        calls: list[InstanceCreated] = []
        unsubscribe = container.add_hook(InstanceCreated, calls.append)
        assert callable(unsubscribe)

        unsubscribe()

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)
        assert calls == []

    def test_remove_hook_returns_true_when_present(
        self, container: DIContainer
    ) -> None:
        """remove_hook must return True when the callback was actually registered."""
        cb = lambda e: None  # noqa: E731
        container.add_hook(InstanceCreated, cb)
        assert container.remove_hook(InstanceCreated, cb) is True

    def test_remove_hook_returns_false_when_absent(
        self, container: DIContainer
    ) -> None:
        """remove_hook for an unregistered pair must return False, not raise."""
        cb = lambda e: None  # noqa: E731
        assert container.remove_hook(InstanceCreated, cb) is False

    def test_same_callback_registered_twice_fires_twice(
        self, container: DIContainer
    ) -> None:
        """Registering one callback twice for the same event type fires it twice per event."""
        calls: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, calls.append)
        container.add_hook(InstanceCreated, calls.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)
        assert len(calls) == 2

    def test_hook_for_unemitted_event_type_never_called(
        self, container: DIContainer
    ) -> None:
        """A hook registered for an event type nobody emits must never fire."""
        calls: list[ScopeEntered] = []
        container.add_hook(ScopeEntered, calls.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)
        assert calls == []

    def test_raising_hook_does_not_break_get_and_is_logged(
        self, container: DIContainer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hook that raises must be swallowed, logged at WARNING, and get() must still work."""

        def boom(event: InstanceCreated) -> None:
            raise RuntimeError("telemetry hook exploded")

        container.add_hook(InstanceCreated, boom)

        @Singleton
        class Foo: ...

        container.register(Foo)
        with caplog.at_level(logging.WARNING):
            instance = container.get(Foo)
        assert isinstance(instance, Foo)
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_hook_registered_for_supertype_is_not_called(
        self, container: DIContainer
    ) -> None:
        """Exact-type dispatch: a hook for `object` must not receive InstanceCreated events."""
        calls: list[object] = []
        container.add_hook(object, calls.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)
        assert calls == []


# ─────────────────────────────────────────────────────────────────
#  Step 6 — zero-overhead test (executable form of the zero-cost rule)
# ─────────────────────────────────────────────────────────────────


class TestZeroOverheadRule:
    """No hooks registered → no timer call at all; one hook → timer is called."""

    def test_no_hooks_means_perf_counter_never_called(
        self, container: DIContainer
    ) -> None:
        """With zero hooks registered, resolving 50 singletons must never call perf_counter_ns."""

        @Singleton
        class Foo: ...

        container.register(Foo)

        with mock.patch("providify.container.perf_counter_ns") as patched:
            for _ in range(50):
                container.get(Foo)
            patched.assert_not_called()

    def test_one_hook_means_perf_counter_is_called(
        self, container: DIContainer
    ) -> None:
        """Registering a single hook must cause the timer to be used on creation."""

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.add_hook(InstanceCreated, lambda e: None)

        with mock.patch(
            "providify.container.perf_counter_ns", wraps=time.perf_counter_ns
        ) as patched:
            container.get(Foo)
            patched.assert_called()


# ─────────────────────────────────────────────────────────────────
#  Step 7 — InstanceCreated instrumentation
# ─────────────────────────────────────────────────────────────────


class TestInstanceCreatedEvents:
    def test_singleton_emits_once_cache_hit_emits_nothing(
        self, container: DIContainer
    ) -> None:
        """One InstanceCreated per singleton key; a second get() (cache hit) emits nothing."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)
        container.get(Foo)

        assert len(events) == 1
        assert events[0].interface is Foo
        assert events[0].scope == Scope.SINGLETON

    def test_request_scoped_emits_once_per_scope_frame(
        self, container: DIContainer
    ) -> None:
        """REQUEST scope: one event per request() frame, not per get() call within it."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        @RequestScoped
        class Foo: ...

        container.register(Foo)
        with container.request():
            container.get(Foo)
            container.get(Foo)

        assert len(events) == 1

    def test_dependent_scope_emits_once_per_get(self, container: DIContainer) -> None:
        """DEPENDENT scope: every get() call creates a fresh instance and emits one event."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        from providify.decorator.scope import Component

        @Component
        class Foo: ...

        container.register(Foo)
        container.get(Foo)
        container.get(Foo)

        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_aget_reports_is_async_true(self, container: DIContainer) -> None:
        """aget() must produce InstanceCreated(is_async=True)."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        await container.aget(Foo)

        assert len(events) == 1
        assert events[0].is_async is True

    def test_get_reports_is_async_false(self, container: DIContainer) -> None:
        """Sync get() must produce InstanceCreated(is_async=False)."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)

        assert len(events) == 1
        assert events[0].is_async is False

    def test_duration_ns_reflects_slow_provider(self, container: DIContainer) -> None:
        """A provider that sleeps 10ms must report duration_ns >= 10_000_000."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        @Provider(singleton=True)
        def make_slow() -> _Slow:
            time.sleep(0.01)
            return _Slow()

        container.provide(make_slow)
        container.get(_Slow)

        assert len(events) == 1
        assert events[0].duration_ns > 0
        assert events[0].duration_ns >= 10_000_000

    def test_implementation_qualifier_scope_match_binding(
        self, container: DIContainer
    ) -> None:
        """implementation is the class for ClassBinding, the factory for ProviderBinding."""
        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        @Singleton
        class Foo: ...

        container.register(Foo)
        container.get(Foo)

        assert events[0].implementation is Foo
        assert events[0].scope == Scope.SINGLETON

    def test_reentrant_hook_resolving_same_singleton_does_not_deadlock(
        self, container: DIContainer
    ) -> None:
        """A hook that re-resolves the same singleton must get the cached instance, no deadlock."""

        @Singleton
        class Foo: ...

        container.register(Foo)

        reentrant_results: list[Foo] = []

        def on_created(event: InstanceCreated) -> None:
            reentrant_results.append(container.get(Foo))

        container.add_hook(InstanceCreated, on_created)

        first = container.get(Foo)
        assert reentrant_results == [first]


# ─────────────────────────────────────────────────────────────────
#  Step 10 — InstanceDisposed instrumentation
# ─────────────────────────────────────────────────────────────────


class TestInstanceDisposedEvents:
    def test_shutdown_emits_one_event_per_torn_down_singleton_in_teardown_order(
        self, container: DIContainer
    ) -> None:
        """shutdown() must emit one InstanceDisposed per singleton, reverse-creation order."""
        events: list[InstanceDisposed] = []
        container.add_hook(InstanceDisposed, events.append)

        @Singleton
        class Config:
            @PreDestroy
            def teardown(self) -> None: ...

        @Singleton
        class Db:
            def __init__(self, config: Config) -> None:
                self.config = config

            @PreDestroy
            def teardown(self) -> None: ...

        container.register(Config)
        container.register(Db)
        container.get(Db)
        container.shutdown()

        assert [e.interface for e in events] == [Db, Config]
        assert all(e.error is None for e in events)

    def test_predestroy_raising_sets_error_and_still_raises_shutdown_error(
        self, container: DIContainer
    ) -> None:
        """A raising @PreDestroy must carry `error` on its event; ShutdownError still raised."""
        from providify.exceptions import ShutdownError

        events: list[InstanceDisposed] = []
        container.add_hook(InstanceDisposed, events.append)

        @Singleton
        class Bad:
            @PreDestroy
            def teardown(self) -> None:
                raise ValueError("boom")

        container.register(Bad)
        container.get(Bad)

        with pytest.raises(ShutdownError):
            container.shutdown()

        assert len(events) == 1
        assert isinstance(events[0].error, ValueError)

    def test_owner_matches_shutdown_failure_owner_format(
        self, container: DIContainer
    ) -> None:
        """InstanceDisposed.owner must match ShutdownFailure.owner's `ClassName.hook_name` format."""
        events: list[InstanceDisposed] = []
        container.add_hook(InstanceDisposed, events.append)

        @Singleton
        class Foo:
            @PreDestroy
            def teardown(self) -> None: ...

        container.register(Foo)
        container.get(Foo)
        container.shutdown()

        assert events[0].owner == "Foo.teardown"

    @pytest.mark.asyncio
    async def test_ashutdown_mirror(self, container: DIContainer) -> None:
        """await ashutdown() must mirror shutdown()'s InstanceDisposed emission."""
        events: list[InstanceDisposed] = []
        container.add_hook(InstanceDisposed, events.append)

        @Singleton
        class Foo:
            @PreDestroy
            def teardown(self) -> None: ...

        container.register(Foo)
        await container.aget(Foo)
        await container.ashutdown()

        assert len(events) == 1
        assert events[0].interface is Foo

    def test_request_scope_exit_emits_instance_disposed_with_request_scope(
        self, container: DIContainer
    ) -> None:
        """Exiting a request() frame must emit InstanceDisposed(scope=Scope.REQUEST)."""
        events: list[InstanceDisposed] = []
        container.add_hook(InstanceDisposed, events.append)

        @RequestScoped
        class Foo:
            @PreDestroy
            def teardown(self) -> None: ...

        container.register(Foo)
        with container.request():
            container.get(Foo)

        assert len(events) == 1
        assert events[0].scope == Scope.REQUEST

    def test_binding_with_no_teardown_hook_emits_no_event(
        self, container: DIContainer
    ) -> None:
        """A singleton with no @PreDestroy/@Disposes must not emit InstanceDisposed."""
        events: list[InstanceDisposed] = []
        container.add_hook(InstanceDisposed, events.append)

        @Singleton
        class Plain: ...

        container.register(Plain)
        container.get(Plain)
        container.shutdown()

        assert events == []


# ─────────────────────────────────────────────────────────────────
#  Step 13 — ScopeEntered / ScopeExited instrumentation
# ─────────────────────────────────────────────────────────────────


class TestScopeEvents:
    def test_request_emits_entered_then_exited_with_matching_id(
        self, container: DIContainer
    ) -> None:
        """`with container.request():` emits ScopeEntered then ScopeExited, matching scope_id."""
        events: list[object] = []
        container.add_hook(ScopeEntered, events.append)
        container.add_hook(ScopeExited, events.append)

        with container.request():
            pass

        assert len(events) == 2
        entered, exited = events
        assert isinstance(entered, ScopeEntered)
        assert isinstance(exited, ScopeExited)
        assert entered.kind == "request"
        assert exited.kind == "request"
        assert entered.scope_id == exited.scope_id
        assert exited.duration_ns > 0

    def test_nested_request_frames_produce_distinct_ids_in_lifo_order(
        self, container: DIContainer
    ) -> None:
        """Nested request() frames must produce two ScopeEntered/ScopeExited pairs, LIFO."""
        entered_ids: list[str] = []
        exited_ids: list[str] = []
        container.add_hook(ScopeEntered, lambda e: entered_ids.append(e.scope_id))
        container.add_hook(ScopeExited, lambda e: exited_ids.append(e.scope_id))

        with container.request() as outer_id:
            with container.request() as inner_id:
                pass

        assert entered_ids == [outer_id, inner_id]
        assert exited_ids == [inner_id, outer_id]
        assert outer_id != inner_id

    def test_scope_exited_still_emitted_when_block_raises(
        self, container: DIContainer
    ) -> None:
        """A request() block that raises must still emit ScopeExited."""
        events: list[ScopeExited] = []
        container.add_hook(ScopeExited, events.append)

        with pytest.raises(ValueError):
            with container.request():
                raise ValueError("boom")

        assert len(events) == 1

    def test_session_reports_scope_id_equal_to_given_session_id(
        self, container: DIContainer
    ) -> None:
        """session("abc") must report scope_id == "abc" on both events."""
        events: list[object] = []
        container.add_hook(ScopeEntered, events.append)
        container.add_hook(ScopeExited, events.append)

        with container.session("abc"):
            pass

        assert all(e.scope_id == "abc" for e in events)
        assert [e.kind for e in events] == ["session", "session"]

    @pytest.mark.asyncio
    async def test_arequest_mirror(self, container: DIContainer) -> None:
        """`async with container.arequest():` must mirror the sync request() emission."""
        events: list[object] = []
        container.add_hook(ScopeEntered, events.append)
        container.add_hook(ScopeExited, events.append)

        async with container.arequest():
            pass

        assert len(events) == 2
        assert events[0].kind == "request"
        assert events[1].kind == "request"

    def test_no_hooks_registered_request_returns_raw_scope_context(
        self, container: DIContainer
    ) -> None:
        """With no hooks registered, request() must return the underlying ScopeContext CM unwrapped."""
        cm = container.request()
        raw_cm = container.scope_context.request()

        assert type(cm) is type(raw_cm)
