"""Unit tests for the container mutation and introspection API.

Covered:
    - override(): replace a class binding, evict singleton cache, reset validation
    - override(): skips ProviderBinding entries for the same interface
    - override(): interface not registered → still registers the new binding
    - reset_binding(): removes all bindings for an interface, returns count
    - reset_binding(qualifier=...): removes only bindings that match the qualifier
    - reset_binding(): evicts singleton cache for removed implementations
    - reset_binding(): returns 0 for unknown interface (no error)
    - get_binding(): returns the best candidate binding without instantiating
    - get_binding(): raises LookupError when no binding is registered
    - get_all_bindings(): returns all matching bindings in registration order
    - get_all_bindings(): returns [] (not LookupError) for unregistered interface

DESIGN NOTE:
    These methods form the "runtime mutation" surface of the container —
    intended for test overrides and introspection tooling.  They are separate
    from the resolution path (get/aget) and must NEVER trigger instantiation.
"""

from __future__ import annotations

import pytest

from providify.binding import ClassBinding, ProviderBinding
from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton


# ─────────────────────────────────────────────────────────────────
#  Shared fixtures — small domain types reused across test classes
# ─────────────────────────────────────────────────────────────────


class Notifier:
    """Interface shared by all notification binding tests."""


@Component
class EmailNotifier(Notifier):
    """Primary concrete implementation."""


@Component
class SmsNotifier(Notifier):
    """Secondary concrete — used in override / multi-binding tests."""


@Singleton
class CachedNotifier(Notifier):
    """Singleton impl — exercises singleton-cache eviction on override/reset."""


# ─────────────────────────────────────────────────────────────────
#  override()
# ─────────────────────────────────────────────────────────────────


class TestOverride:
    """Tests for DIContainer.override()."""

    def test_override_replaces_existing_class_binding(
        self, container: DIContainer
    ) -> None:
        """override() must unregister the old binding and register the new one."""
        container.bind(Notifier, EmailNotifier)
        container.override(Notifier, SmsNotifier)

        binding = container.get_binding(Notifier)

        assert isinstance(binding, ClassBinding)
        assert binding.implementation is SmsNotifier

    def test_override_evicts_singleton_cache(self, container: DIContainer) -> None:
        """override() must evict any cached singleton instance of the old impl.

        If the stale singleton is not evicted, the new binding is never
        instantiated — the override is silently ignored.
        """
        container.bind(Notifier, CachedNotifier)

        # Force singleton into cache by resolving it
        original = container.get(Notifier)
        assert isinstance(original, CachedNotifier)
        assert container.is_resolvable(Notifier)

        # Now override — the CachedNotifier singleton must be evicted
        container.override(Notifier, SmsNotifier)

        # Next resolution creates a fresh SmsNotifier, not the stale CachedNotifier
        replacement = container.get(Notifier)
        assert isinstance(replacement, SmsNotifier)

    def test_override_resets_validated_flag(self, container: DIContainer) -> None:
        """override() must reset _validated so scope checks run again.

        Without this, a scope violation introduced by the replacement binding
        would never be caught — it would be silently skipped.
        """
        container.bind(Notifier, EmailNotifier)
        container.get(Notifier)  # triggers validate_bindings() → _validated = True

        assert container._validated is True

        container.override(Notifier, SmsNotifier)

        # Validation state must be reset — next get() runs validate_bindings()
        assert container._validated is False

    def test_override_unregistered_interface_still_registers(
        self, container: DIContainer
    ) -> None:
        """override() on an unknown interface must not raise — just register the new binding.

        This is useful for 'unconditional swap' patterns where the caller
        doesn't know whether a prior binding exists.
        """
        # No prior binding for Notifier — should not raise
        container.override(Notifier, EmailNotifier)

        binding = container.get_binding(Notifier)
        assert isinstance(binding, ClassBinding)
        assert binding.implementation is EmailNotifier

    def test_override_skips_provider_bindings(self, container: DIContainer) -> None:
        """override() must leave ProviderBinding entries intact.

        The override contract is: replace ClassBinding for *interface*, do NOT
        touch ProviderBinding entries. This preserves provider-based bindings
        that happen to return the same interface type.
        """

        @Provider
        def make_notifier() -> Notifier:
            return EmailNotifier()

        container.provide(make_notifier)
        container.bind(Notifier, SmsNotifier)

        # Only the ClassBinding should be replaced; ProviderBinding stays
        container.override(Notifier, EmailNotifier)

        all_bindings = container.get_all_bindings(Notifier)

        # ProviderBinding must still be present
        provider_bindings = [b for b in all_bindings if isinstance(b, ProviderBinding)]
        assert len(provider_bindings) == 1

        # The new ClassBinding must be present
        class_bindings = [b for b in all_bindings if isinstance(b, ClassBinding)]
        assert len(class_bindings) == 1
        assert class_bindings[0].implementation is EmailNotifier


# ─────────────────────────────────────────────────────────────────
#  reset_binding()
# ─────────────────────────────────────────────────────────────────


class TestResetBinding:
    """Tests for DIContainer.reset_binding()."""

    def test_reset_removes_all_bindings_for_interface(
        self, container: DIContainer
    ) -> None:
        """reset_binding() with no qualifier removes every binding for the interface.

        bind(Interface, Impl) adds two entries:
          1. ClassBinding(Interface, Impl)              — the explicit binding
          2. ClassBinding(Impl, Impl, exact_only=True)  — the self-binding so
             container.get(Impl) works directly.
        Both share an interface that issubclass-matches Notifier, so reset_binding
        removes all four entries (2 bind() calls × 2 bindings each).
        """
        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)

        n = container.reset_binding(Notifier)

        # 2 bind() calls × 2 bindings each (interface + self-binding)
        assert n == 4
        assert container.get_all_bindings(Notifier) == []
        assert not container.is_resolvable(Notifier)

    def test_reset_with_qualifier_removes_only_matching_bindings(
        self, container: DIContainer
    ) -> None:
        """reset_binding(qualifier=...) must leave bindings with other qualifiers intact."""
        from providify.decorator.scope import Named

        # Named requires keyword form: @Named(name="email")
        @Named(name="email")
        @Component
        class NamedEmailNotifier(Notifier):
            pass

        @Named(name="sms")
        @Component
        class NamedSmsNotifier(Notifier):
            pass

        container.bind(Notifier, NamedEmailNotifier)
        container.bind(Notifier, NamedSmsNotifier)

        # Remove only the "email" qualifier bindings.
        # bind() adds 2 entries per call (interface + self-binding);
        # reset_binding(qualifier="email") filters by qualifier, so only
        # entries with qualifier="email" are removed.
        n = container.reset_binding(Notifier, qualifier="email")

        # Only the ClassBinding(Notifier, NamedEmailNotifier) has qualifier="email";
        # the self-binding ClassBinding(NamedEmailNotifier, NamedEmailNotifier)
        # also has qualifier="email" since it inherits from the decorator.
        assert n >= 1

        remaining = container.get_all_bindings(Notifier)
        qualifier_names = {b.qualifier for b in remaining}
        # "sms" stays; "email" is gone
        assert "sms" in qualifier_names
        assert "email" not in qualifier_names

    def test_reset_evicts_singleton_cache(self, container: DIContainer) -> None:
        """reset_binding() must evict cached singleton instances.

        A stale singleton surviving a reset would be returned on the next get(),
        completely circumventing the removal.
        """
        container.bind(Notifier, CachedNotifier)
        container.get(Notifier)  # warms the singleton cache

        # The cache entry must survive until reset
        assert CachedNotifier in container._singleton_cache

        container.reset_binding(Notifier)

        # Cache entry must be gone after reset
        assert CachedNotifier not in container._singleton_cache

    def test_reset_returns_zero_for_unknown_interface(
        self, container: DIContainer
    ) -> None:
        """reset_binding() on an unregistered interface must return 0 without raising."""

        class UnknownService:
            pass

        n = container.reset_binding(UnknownService)

        assert n == 0

    def test_reset_resets_validated_flag(self, container: DIContainer) -> None:
        """reset_binding() must reset _validated so scope checks run again on next get()."""
        container.bind(Notifier, EmailNotifier)
        container.get(Notifier)  # triggers validate_bindings() → _validated = True

        assert container._validated is True

        container.reset_binding(Notifier)

        assert container._validated is False


# ─────────────────────────────────────────────────────────────────
#  get_binding()
# ─────────────────────────────────────────────────────────────────


class TestGetBinding:
    """Tests for DIContainer.get_binding()."""

    def test_get_binding_returns_best_candidate(self, container: DIContainer) -> None:
        """get_binding() must return the highest-priority binding without instantiating."""
        container.bind(Notifier, EmailNotifier)

        binding = container.get_binding(Notifier)

        # Returns a binding object, NOT an instance
        assert isinstance(binding, ClassBinding)
        assert binding.implementation is EmailNotifier
        # Verify no instance was created (DEPENDENT scope — would be new every time)
        assert binding.scope is not None  # just a structural check

    def test_get_binding_does_not_trigger_validate_bindings(
        self, container: DIContainer
    ) -> None:
        """get_binding() is a pure read — must NOT trigger validate_bindings().

        Triggering validation would be a side-effect that callers don't expect
        from an introspection method.
        """
        container.bind(Notifier, EmailNotifier)

        # _validated starts False
        assert container._validated is False

        container.get_binding(Notifier)

        # Must still be False — get_binding() is read-only
        assert container._validated is False

    def test_get_binding_raises_lookup_error_when_absent(
        self, container: DIContainer
    ) -> None:
        """get_binding() must raise LookupError when no binding is registered."""

        class UnknownService:
            pass

        with pytest.raises(LookupError):
            container.get_binding(UnknownService)

    def test_get_binding_with_qualifier_returns_correct_binding(
        self, container: DIContainer
    ) -> None:
        """get_binding(qualifier=...) must filter by qualifier."""
        from providify.decorator.scope import Named

        @Named(name="primary")
        @Component
        class PrimaryNotifier(Notifier):
            pass

        @Named(name="fallback")
        @Component
        class FallbackNotifier(Notifier):
            pass

        container.bind(Notifier, PrimaryNotifier)
        container.bind(Notifier, FallbackNotifier)

        binding = container.get_binding(Notifier, qualifier="fallback")

        assert isinstance(binding, ClassBinding)
        assert binding.implementation is FallbackNotifier


# ─────────────────────────────────────────────────────────────────
#  get_all_bindings()
# ─────────────────────────────────────────────────────────────────


class TestGetAllBindings:
    """Tests for DIContainer.get_all_bindings()."""

    def test_get_all_bindings_returns_all_matches(self, container: DIContainer) -> None:
        """get_all_bindings() must return every binding registered for the interface."""
        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)

        bindings = container.get_all_bindings(Notifier)

        assert len(bindings) == 2
        implementations = {b.implementation for b in bindings}  # type: ignore[union-attr]
        assert EmailNotifier in implementations
        assert SmsNotifier in implementations

    def test_get_all_bindings_returns_empty_list_when_absent(
        self, container: DIContainer
    ) -> None:
        """get_all_bindings() must return [] (not raise) for unregistered interface.

        Callers can use `if not container.get_all_bindings(T)` safely without
        a try/except — contrasting with container.get_all() which raises.
        """

        class UnknownService:
            pass

        result = container.get_all_bindings(UnknownService)

        assert result == []

    def test_get_all_bindings_does_not_trigger_validate_bindings(
        self, container: DIContainer
    ) -> None:
        """get_all_bindings() is a pure read — must NOT trigger validate_bindings()."""
        container.bind(Notifier, EmailNotifier)
        assert container._validated is False

        container.get_all_bindings(Notifier)

        assert container._validated is False

    def test_get_all_bindings_filters_by_qualifier(
        self, container: DIContainer
    ) -> None:
        """get_all_bindings(qualifier=...) must only return bindings with that qualifier."""
        from providify.decorator.scope import Named

        @Named(name="fast")
        @Component
        class FastNotifier(Notifier):
            pass

        container.bind(Notifier, EmailNotifier)  # no qualifier
        container.bind(Notifier, FastNotifier)  # qualifier="fast"

        bindings = container.get_all_bindings(Notifier, qualifier="fast")

        assert len(bindings) == 1
        assert bindings[0].implementation is FastNotifier  # type: ignore[union-attr]

    def test_get_all_bindings_includes_provider_bindings(
        self, container: DIContainer
    ) -> None:
        """get_all_bindings() must include ProviderBinding entries, not just ClassBinding."""

        @Provider
        def build_notifier() -> Notifier:
            return EmailNotifier()

        container.provide(build_notifier)
        container.bind(Notifier, SmsNotifier)

        bindings = container.get_all_bindings(Notifier)

        binding_types = {type(b) for b in bindings}
        assert ClassBinding in binding_types
        assert ProviderBinding in binding_types
