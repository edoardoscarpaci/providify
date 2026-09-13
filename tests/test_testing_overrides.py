"""Failing (red) tests for providify.testing.ContainerOverrides.

Plan: plans/007-pytest-integration.md, step 6.

`providify.testing` does not exist yet — every test here is expected to
fail with ImportError until step 7 is implemented.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component, Singleton

# This import is expected to fail (ModuleNotFoundError) until providify/testing.py exists.
from providify.testing import ContainerOverrides


# NOTE: bind()/override() require the implementation to carry DI metadata
# (raises ClassBindingNotDecoratedError otherwise) — @Component added to the
# classes used as direct bind()/override() targets below. This is a test
# fix, not a production behaviour change.
@Component
class Clock:
    pass


class FrozenClock(Clock):
    pass


@Component
class Notifier:
    pass


@Component
class FakeNotifier(Notifier):
    pass


class TestInstanceOverride:
    def test_instance_returns_exact_object_by_identity_on_every_get(
        self, container: DIContainer
    ) -> None:
        # instance() must bind DEPENDENT scope: same object every resolution.
        double = FrozenClock()
        container.bind(Clock, Clock)
        with ContainerOverrides(container) as ov:
            ov.instance(Clock, double)
            first = container.get(Clock)
            second = container.get(Clock)
        assert first is double
        assert second is double

    def test_instance_overrides_resolved_singleton_and_restores_original_on_exit(
        self, container: DIContainer
    ) -> None:
        @Singleton
        class RealClock(Clock):
            pass

        container.bind(Clock, RealClock)
        original = container.get(Clock)

        double = FrozenClock()
        with ContainerOverrides(container) as ov:
            ov.instance(Clock, double)
            assert container.get(Clock) is double

        # After exit: original binding AND its cached instance are back.
        restored = container.get(Clock)
        assert restored is original


class TestBindOverride:
    def test_bind_swaps_implementation_class(self, container: DIContainer) -> None:
        @Component
        class RealNotifier(Notifier):
            pass

        container.bind(Notifier, RealNotifier)
        with ContainerOverrides(container) as ov:
            ov.bind(Notifier, FakeNotifier)
            assert isinstance(container.get(Notifier), FakeNotifier)


class TestFactoryOverride:
    def test_factory_is_called_once_per_get(self, container: DIContainer) -> None:
        calls = []

        def make_notifier() -> Notifier:
            calls.append(1)
            return FakeNotifier()

        container.bind(Notifier, Notifier)
        with ContainerOverrides(container) as ov:
            ov.factory(Notifier, make_notifier)
            container.get(Notifier)
            container.get(Notifier)
        assert len(calls) == 2


class TestRemoveOverride:
    def test_remove_makes_interface_unresolvable_inside_block_and_restores_after(
        self, container: DIContainer
    ) -> None:
        @Component
        class RealNotifier(Notifier):
            pass

        container.bind(Notifier, RealNotifier)
        with ContainerOverrides(container) as ov:
            ov.remove(Notifier)
            assert container.is_resolvable(Notifier) is False
        assert container.is_resolvable(Notifier) is True


class TestProfileAndAlternativeOverride:
    def test_profiles_active_inside_block_and_restored_after(self, container: DIContainer) -> None:
        with ContainerOverrides(container) as ov:
            ov.profiles("test")
            assert "test" in container._active_profiles
        assert "test" not in container._active_profiles

    def test_alternative_enabled_inside_block_and_restored_after(
        self, container: DIContainer
    ) -> None:
        from providify.decorator.scope import Alternative

        @Alternative
        @Component
        class AltImpl:
            pass

        with ContainerOverrides(container) as ov:
            ov.alternative(AltImpl)
            assert AltImpl in container._enabled_alternatives
        assert AltImpl not in container._enabled_alternatives


class TestExceptionSafety:
    def test_overrides_are_undone_when_block_raises(self, container: DIContainer) -> None:
        container.bind(Notifier, Notifier)
        with pytest.raises(ValueError):
            with ContainerOverrides(container) as ov:
                ov.remove(Notifier)
                raise ValueError("boom")
        assert container.is_resolvable(Notifier) is True


class TestNestedOverrides:
    def test_nested_overrides_lifo_restore(self, container: DIContainer) -> None:
        container.bind(Notifier, Notifier)
        with ContainerOverrides(container) as outer:
            outer.remove(Notifier)
            with ContainerOverrides(container) as inner:
                inner.bind(Notifier, FakeNotifier)
                assert isinstance(container.get(Notifier), FakeNotifier)
            # inner exit restores outer's state: Notifier removed again.
            assert container.is_resolvable(Notifier) is False
        assert container.is_resolvable(Notifier) is True


class TestExplicitReset:
    def test_explicit_reset_then_exit_does_not_double_restore(self, container: DIContainer) -> None:
        container.bind(Notifier, Notifier)
        ov = ContainerOverrides(container)
        ov.__enter__()
        ov.remove(Notifier)
        ov.reset()
        assert container.is_resolvable(Notifier) is True
        ov.__exit__(None, None, None)  # must not raise or double-restore
        assert container.is_resolvable(Notifier) is True


class TestNoMutationNoOp:
    def test_exit_without_any_mutation_is_a_noop(self, container: DIContainer) -> None:
        container.bind(Notifier, Notifier)
        with ContainerOverrides(container):
            pass  # no override calls at all — snapshot must never be taken
        assert container.is_resolvable(Notifier) is True
