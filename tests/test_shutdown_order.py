"""Failing (red) tests for Plan 004 — graceful shutdown / reverse-dependency-order
teardown (F9).

These tests describe behaviour that does NOT exist yet:
    - Reverse-*creation*-order teardown for singletons (dependents destroyed
      before their dependencies), replacing today's registration-order walk.
    - ``ShutdownError`` / ``ShutdownFailure`` — failure aggregation instead of
      raise-on-first, with caches ALWAYS cleared.
    - Eviction of ``_singleton_order`` entries on ``override()`` /
      ``reset_binding()`` / ``copy()``.

See plans/004-graceful-shutdown.md, Steps 1-4.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.lifecycle import Disposes, PreDestroy
from providify.decorator.module import Configuration
from providify.decorator.scope import Provider, Singleton

# ShutdownError / ShutdownFailure do not exist yet (Step 5/6) — importing them
# is expected to fail with ImportError until the feature lands.
from providify.exceptions import ShutdownError, ShutdownFailure  # noqa: E402
from providify.metadata import Scope as _Scope


class _MixedBindingConnection:
    """Module-level so `get_type_hints()` can resolve it under `from __future__ import annotations`."""


class _AsyncDisposesConnection:
    """Module-level so `get_type_hints()` can resolve it under `from __future__ import annotations`."""


# ─────────────────────────────────────────────────────────────────
#  Shared fixtures / helper graph builders
# ─────────────────────────────────────────────────────────────────


def _build_chain(container: DIContainer, order: list[str]) -> type:
    """Config -> Db -> Repo -> Service, each appending its name on teardown."""

    @Singleton
    class Config:
        @PreDestroy
        def teardown(self) -> None:
            order.append("Config")

    @Singleton
    class Db:
        def __init__(self, config: Config) -> None:
            self.config = config

        @PreDestroy
        def teardown(self) -> None:
            order.append("Db")

    @Singleton
    class Repo:
        def __init__(self, db: Db) -> None:
            self.db = db

        @PreDestroy
        def teardown(self) -> None:
            order.append("Repo")

    @Singleton
    class Service:
        def __init__(self, repo: Repo) -> None:
            self.repo = repo

        @PreDestroy
        def teardown(self) -> None:
            order.append("Service")

    container.register(Config)
    container.register(Db)
    container.register(Repo)
    container.register(Service)
    return Service


# ─────────────────────────────────────────────────────────────────
#  Step 1 — sync ordering
# ─────────────────────────────────────────────────────────────────


class TestSyncOrdering:
    def test_chain_teardown_order_is_reverse_dependency_order(
        self, container: DIContainer
    ) -> None:
        """Dependents must be destroyed before the dependencies they hold."""
        order: list[str] = []
        Service = _build_chain(container, order)

        container.get(Service)
        container.shutdown()

        assert order == ["Service", "Repo", "Db", "Config"]

    def test_warm_up_produces_same_teardown_order(self, container: DIContainer) -> None:
        """warm_up() drives the same instantiation path as get() — same order."""
        order: list[str] = []
        _build_chain(container, order)

        container.warm_up()
        container.shutdown()

        assert order == ["Service", "Repo", "Db", "Config"]

    def test_diamond_dependent_torn_down_first_and_shared_dep_last(
        self, container: DIContainer
    ) -> None:
        """Diamond A(B,C), B(D), C(D): A must be first, D must be last.

        B vs C relative order is creation-dependent (not asserted exactly).
        """
        order: list[str] = []

        @Singleton
        class D:
            @PreDestroy
            def teardown(self) -> None:
                order.append("D")

        @Singleton
        class B:
            def __init__(self, d: D) -> None:
                self.d = d

            @PreDestroy
            def teardown(self) -> None:
                order.append("B")

        @Singleton
        class C:
            def __init__(self, d: D) -> None:
                self.d = d

            @PreDestroy
            def teardown(self) -> None:
                order.append("C")

        @Singleton
        class A:
            def __init__(self, b: B, c: C) -> None:
                self.b = b
                self.c = c

            @PreDestroy
            def teardown(self) -> None:
                order.append("A")

        container.register(D)
        container.register(B)
        container.register(C)
        container.register(A)

        container.get(A)
        container.shutdown()

        assert order[0] == "A", "the topmost dependent must be torn down first"
        assert order[-1] == "D", "the shared leaf dependency must be torn down last"
        assert set(order) == {"A", "B", "C", "D"}

    def test_teardown_order_ignores_reversed_registration_order(
        self, container: DIContainer
    ) -> None:
        """Registering Service before its dependencies must not affect teardown
        order — only creation (dependency) order matters, proving _bindings
        registration order is no longer the source of truth.
        """
        order: list[str] = []

        @Singleton
        class Config:
            @PreDestroy
            def teardown(self) -> None:
                order.append("Config")

        @Singleton
        class Db:
            def __init__(self, config: Config) -> None:
                self.config = config

            @PreDestroy
            def teardown(self) -> None:
                order.append("Db")

        @Singleton
        class Service:
            def __init__(self, db: Db) -> None:
                self.db = db

            @PreDestroy
            def teardown(self) -> None:
                order.append("Service")

        # Deliberately reversed registration order vs dependency order.
        container.register(Service)
        container.register(Db)
        container.register(Config)

        container.get(Service)
        container.shutdown()

        assert order == ["Service", "Db", "Config"]

    def test_never_resolved_singleton_has_no_pre_destroy_call(
        self, container: DIContainer
    ) -> None:
        """A @Singleton that was registered but never get()'d contributes nothing."""
        order: list[str] = []

        @Singleton
        class Unused:
            @PreDestroy
            def teardown(self) -> None:
                order.append("Unused")

        container.register(Unused)
        container.shutdown()

        assert order == []

    def test_mixed_class_and_provider_binding_share_teardown_order(
        self, container: DIContainer
    ) -> None:
        """A @Provider-produced singleton with @Disposes participates in the same
        reverse-dependency-order teardown as ClassBinding singletons.
        """
        order: list[str] = []

        @Singleton
        class Config:
            @PreDestroy
            def teardown(self) -> None:
                order.append("Config")

        @Configuration
        class InfraModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self, config: Config) -> _MixedBindingConnection:
                return _MixedBindingConnection()

            @Disposes(_MixedBindingConnection)
            def close_conn(self, conn: _MixedBindingConnection) -> None:
                order.append("_MixedBindingConnection")

        container.register(Config)
        container.install(InfraModule)

        container.get(_MixedBindingConnection)
        container.shutdown()

        assert order == ["_MixedBindingConnection", "Config"]


# ─────────────────────────────────────────────────────────────────
#  Step 2 — async ordering
# ─────────────────────────────────────────────────────────────────


class TestAsyncOrdering:
    async def test_async_chain_teardown_order_via_ashutdown(
        self, container: DIContainer
    ) -> None:
        """4-node chain with async @PreDestroy hooks resolved via aget() and torn
        down by ashutdown() must yield reverse-dependency order.
        """
        order: list[str] = []

        @Singleton
        class Config:
            @PreDestroy
            async def teardown(self) -> None:
                order.append("Config")

        @Singleton
        class Db:
            def __init__(self, config: Config) -> None:
                self.config = config

            @PreDestroy
            async def teardown(self) -> None:
                order.append("Db")

        @Singleton
        class Repo:
            def __init__(self, db: Db) -> None:
                self.db = db

            @PreDestroy
            async def teardown(self) -> None:
                order.append("Repo")

        @Singleton
        class Service:
            def __init__(self, repo: Repo) -> None:
                self.repo = repo

            @PreDestroy
            async def teardown(self) -> None:
                order.append("Service")

        container.register(Config)
        container.register(Db)
        container.register(Repo)
        container.register(Service)

        await container.aget(Service)
        await container.ashutdown()

        assert order == ["Service", "Repo", "Db", "Config"]

    async def test_mixed_sync_and_async_chain_teardown_order(
        self, container: DIContainer
    ) -> None:
        """A chain mixing sync and async @PreDestroy hooks still tears down in
        reverse-dependency order under ashutdown().
        """
        order: list[str] = []

        @Singleton
        class Config:
            @PreDestroy
            def teardown(self) -> None:
                order.append("Config")

        @Singleton
        class Db:
            def __init__(self, config: Config) -> None:
                self.config = config

            @PreDestroy
            async def teardown(self) -> None:
                order.append("Db")

        @Singleton
        class Service:
            def __init__(self, db: Db) -> None:
                self.db = db

            @PreDestroy
            def teardown(self) -> None:
                order.append("Service")

        container.register(Config)
        container.register(Db)
        container.register(Service)

        await container.aget(Service)
        await container.ashutdown()

        assert order == ["Service", "Db", "Config"]

    async def test_async_disposes_disposer_is_awaited(
        self, container: DIContainer
    ) -> None:
        """An async @Disposes disposer must be awaited by ashutdown()."""
        order: list[str] = []

        @Configuration
        class InfraModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _AsyncDisposesConnection:
                return _AsyncDisposesConnection()

            @Disposes(_AsyncDisposesConnection)
            async def close_conn(self, conn: _AsyncDisposesConnection) -> None:
                order.append("_AsyncDisposesConnection")

        container.install(InfraModule)
        await container.aget(_AsyncDisposesConnection)
        await container.ashutdown()

        assert order == ["_AsyncDisposesConnection"]


# ─────────────────────────────────────────────────────────────────
#  Step 3 — failure aggregation
# ─────────────────────────────────────────────────────────────────


class TestFailureAggregation:
    def test_shutdown_error_aggregates_multiple_failures_and_runs_non_failing_hooks(
        self, container: DIContainer
    ) -> None:
        """Two failing @PreDestroy hooks -> ShutdownError with 2 failures; a third
        non-failing hook still runs.
        """
        ran: list[str] = []

        @Singleton
        class Good:
            @PreDestroy
            def teardown(self) -> None:
                ran.append("Good")

        @Singleton
        class Bad1:
            @PreDestroy
            def teardown(self) -> None:
                raise ValueError("bad1 boom")

        @Singleton
        class Bad2:
            @PreDestroy
            def teardown(self) -> None:
                raise RuntimeError("bad2 boom")

        container.register(Good)
        container.register(Bad1)
        container.register(Bad2)
        container.get(Good)
        container.get(Bad1)
        container.get(Bad2)

        with pytest.raises(ShutdownError) as exc_info:
            container.shutdown()

        exc = exc_info.value
        assert len(exc.failures) == 2
        for failure in exc.failures:
            assert isinstance(failure, ShutdownFailure)
            assert isinstance(failure.owner, str)
            assert isinstance(failure.exception, BaseException)
        assert "Good" in ran

    def test_singleton_cache_is_cleared_after_shutdown_error(
        self, container: DIContainer
    ) -> None:
        """Caches must always be cleared, even when ShutdownError is raised."""

        @Singleton
        class Bad:
            @PreDestroy
            def teardown(self) -> None:
                raise ValueError("boom")

        container.register(Bad)
        container.get(Bad)

        with pytest.raises(ShutdownError):
            container.shutdown()

        assert container._singleton_cache == {}

    def test_shutdown_error_cause_is_first_raised_exception(
        self, container: DIContainer
    ) -> None:
        """exc.__cause__ must be the first captured exception (chained via `from`)."""
        first_error = ValueError("first boom")

        @Singleton
        class Bad1:
            @PreDestroy
            def teardown(self) -> None:
                raise first_error

        @Singleton
        class Bad2:
            @PreDestroy
            def teardown(self) -> None:
                raise RuntimeError("second boom")

        container.register(Bad1)
        container.register(Bad2)
        container.get(Bad1)
        container.get(Bad2)

        with pytest.raises(ShutdownError) as exc_info:
            container.shutdown()

        assert exc_info.value.__cause__ is first_error

    async def test_async_shutdown_error_aggregates_failures(
        self, container: DIContainer
    ) -> None:
        """ashutdown() must aggregate failures the same way as shutdown()."""

        @Singleton
        class Bad1:
            @PreDestroy
            async def teardown(self) -> None:
                raise ValueError("bad1 boom")

        @Singleton
        class Bad2:
            @PreDestroy
            async def teardown(self) -> None:
                raise RuntimeError("bad2 boom")

        container.register(Bad1)
        container.register(Bad2)
        await container.aget(Bad1)
        await container.aget(Bad2)

        with pytest.raises(ShutdownError) as exc_info:
            await container.ashutdown()

        assert len(exc_info.value.failures) == 2
        assert container._singleton_cache == {}

    def test_context_manager_exit_propagates_shutdown_error_and_clears_caches(
        self, container: DIContainer
    ) -> None:
        """`with DIContainer() as c:` where a @PreDestroy raises must propagate
        ShutdownError out of __exit__, with caches still cleared.
        """

        @Singleton
        class Bad:
            @PreDestroy
            def teardown(self) -> None:
                raise ValueError("boom")

        container.register(Bad)
        container.get(Bad)

        with pytest.raises(ShutdownError):
            with container:
                pass

        assert container._singleton_cache == {}


# ─────────────────────────────────────────────────────────────────
#  Step 4 — idempotency / eviction
# ─────────────────────────────────────────────────────────────────


class TestIdempotencyAndEviction:
    def test_double_shutdown_runs_hooks_only_once(self, container: DIContainer) -> None:
        """Calling shutdown() twice must not run any hook a second time."""
        calls: list[str] = []

        @Singleton
        class Resource:
            @PreDestroy
            def teardown(self) -> None:
                calls.append("Resource")

        container.register(Resource)
        container.get(Resource)

        container.shutdown()
        container.shutdown()  # second call: no-op

        assert calls == ["Resource"]

    def test_override_evicts_instance_from_teardown_plan(
        self, container: DIContainer
    ) -> None:
        """override() after resolving an interface must drop the evicted instance
        from the teardown plan — no double teardown, no stale-binding error.
        """
        calls: list[str] = []

        class IThing:
            pass

        @Singleton
        class Original(IThing):
            @PreDestroy
            def teardown(self) -> None:
                calls.append("Original")

        @Singleton
        class Replacement(IThing):
            @PreDestroy
            def teardown(self) -> None:
                calls.append("Replacement")

        container.bind(IThing, Original)
        container.get(IThing)

        container.override(IThing, Replacement)

        container.shutdown()

        assert "Original" not in calls

    def test_reset_binding_evicts_instance_from_teardown_plan(
        self, container: DIContainer
    ) -> None:
        """reset_binding() after resolving must drop the evicted instance from
        the teardown plan.
        """
        calls: list[str] = []

        class IThing:
            pass

        @Singleton
        class Impl(IThing):
            @PreDestroy
            def teardown(self) -> None:
                calls.append("Impl")

        container.bind(IThing, Impl)
        container.get(IThing)

        container.reset_binding(IThing)

        container.shutdown()  # must not raise, must not run Impl's hook

        assert calls == []

    def test_copy_has_independent_empty_teardown_order(
        self, container: DIContainer
    ) -> None:
        """copy() must start with an empty teardown order: shutting it down runs
        no hooks and does not touch the original container's instances.
        """
        calls: list[str] = []

        @Singleton
        class Resource:
            @PreDestroy
            def teardown(self) -> None:
                calls.append("Resource")

        container.register(Resource)
        container.get(Resource)

        copy = container.copy()
        copy.shutdown()  # copy's own cache holds its own Resource instance

        assert calls == []

        container.shutdown()
        assert calls == ["Resource"]
