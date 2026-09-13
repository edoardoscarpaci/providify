"""Failing (red) tests for DIContainer.snapshot()/restore() and scoped(container=...).

Plan: plans/007-pytest-integration.md, step 1 + step 5.

These tests exercise API that does not exist yet:
    - providify.container.ContainerSnapshot
    - DIContainer.snapshot() / DIContainer.restore()
    - DIContainer.scoped(container=...)

All tests are expected to fail with AttributeError/TypeError until the
plan's production code is implemented.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.interceptor import AroundInvoke, Interceptor
from providify.decorator.scope import Alternative, Component, Singleton


class TestSnapshotRestoreBindings:
    """snapshot()/restore() must undo binding-level mutations."""

    def test_restore_undoes_bind(self, container: DIContainer) -> None:
        # A binding added after the snapshot must vanish on restore.
        # NOTE: bind() requires the implementation to carry DI metadata
        # (raises ClassBindingNotDecoratedError otherwise) — @Component
        # added here; this is a test fix, not a production behaviour change.
        @Component
        class Greeter:
            pass

        snap = container.snapshot()
        container.bind(Greeter, Greeter)
        assert container.is_resolvable(Greeter) is True

        container.restore(snap)

        assert container.is_resolvable(Greeter) is False

    def test_restore_undoes_reset_binding(self, container: DIContainer) -> None:
        # reset_binding() removes bindings; restore() must bring them back.
        @Component
        class Service:
            pass

        container.bind(Service, Service)
        snap = container.snapshot()
        removed = container.reset_binding(Service)
        assert removed >= 1
        assert container.is_resolvable(Service) is False

        container.restore(snap)

        assert container.is_resolvable(Service) is True


class TestSnapshotRestoreSingletonIdentity:
    """restore() must bring back the exact original singleton instance."""

    def test_restore_returns_original_cached_singleton_by_identity(
        self, container: DIContainer
    ) -> None:
        @Singleton
        class Clock:
            pass

        # NOTE: override() requires the replacement to be a subclass of the
        # interface (raises TypeError otherwise) — FakeClock(Clock) added
        # here; this is a test fix, not a production behaviour change.
        @Singleton
        class FakeClock(Clock):
            pass

        container.bind(Clock, Clock)
        original = container.get(Clock)

        snap = container.snapshot()
        container.override(Clock, FakeClock)
        fake = container.get(Clock)
        assert fake is not original

        container.restore(snap)

        restored = container.get(Clock)
        assert restored is original


class TestSnapshotRestoreRuntimeState:
    """profiles, alternatives, and interceptors must round-trip through restore()."""

    def test_restore_undoes_profile_alternative_and_interceptor(
        self, container: DIContainer
    ) -> None:
        @Interceptor
        class LoggingInterceptor:
            @AroundInvoke
            def around(self, ctx):
                return ctx.proceed()

        @Alternative
        @Component
        class AltImpl:
            pass

        snap = container.snapshot()

        container.activate_profile("test")
        container.enable_alternative(AltImpl)
        container.add_interceptor(LoggingInterceptor)

        container.restore(snap)

        assert "test" not in container._active_profiles
        assert AltImpl not in container._enabled_alternatives
        assert LoggingInterceptor not in container._interceptor_classes


class TestSnapshotRestoreIdempotent:
    """Restoring twice from the same snapshot must not raise."""

    def test_restore_twice_is_idempotent(self, container: DIContainer) -> None:
        # NOTE: bind() requires DI metadata on the implementation — @Component
        # added here; this is a test fix, not a production behaviour change.
        @Component
        class Widget:
            pass

        snap = container.snapshot()
        container.bind(Widget, Widget)

        container.restore(snap)
        container.restore(snap)  # must not raise

        assert container.is_resolvable(Widget) is False


class TestSnapshotSingletonLockCleanup:
    """restore() must not leave stale lock entries for evicted cache keys."""

    def test_restore_drops_locks_for_keys_absent_from_restored_cache(
        self, container: DIContainer
    ) -> None:
        @Singleton
        class Thing:
            pass

        container.bind(Thing, Thing)
        snap = container.snapshot()  # no Thing cached yet
        container.get(Thing)  # populates _singleton_cache and _singleton_locks

        container.restore(snap)

        assert Thing not in container._singleton_locks


class TestScopedWithExistingContainer:
    """DIContainer.scoped(existing) must adopt *existing* as the global."""

    def test_scoped_installs_existing_container_as_global(self, container: DIContainer) -> None:
        with DIContainer.scoped(container) as c:
            assert c is container
            assert DIContainer.current() is container

    def test_scoped_restores_previous_global_after_block(self, container: DIContainer) -> None:
        previous = DIContainer.current()
        with DIContainer.scoped(container):
            pass
        assert DIContainer.current() is previous

    def test_scoped_restores_previous_global_when_block_raises(
        self, container: DIContainer
    ) -> None:
        previous = DIContainer.current()
        with pytest.raises(RuntimeError):
            with DIContainer.scoped(container):
                raise RuntimeError("boom")
        assert DIContainer.current() is previous

    def test_scoped_does_not_shut_down_adopted_container(self, container: DIContainer) -> None:
        @Singleton
        class Resource:
            pass

        container.bind(Resource, Resource)
        container.get(Resource)

        with DIContainer.scoped(container):
            pass

        # The adopted container must still hold its cached singleton —
        # scoped() only restores the global reference, it never tears
        # down the container it was given.
        assert Resource in container._singleton_cache
