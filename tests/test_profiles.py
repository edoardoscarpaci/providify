"""Failing tests (RED mode) for Plan 005 — profile-based provider activation.

These tests encode the design in `plans/005-profile-based-activation.md` before
any of `providify/profiles.py`, the `@Profile` decorator, `.profiles` on
bindings, or the container profile API (`profiles=`, `activate_profile()`,
`deactivate_profile()`, `active_profiles`) exist.
Every test in this file is expected to fail with an ImportError/AttributeError/
NameError until the corresponding implementation steps land.

Covered (grouped by plan Step number):
    Step 1  — pure matcher: matches() / parse_profiles() / resolve_active_profiles()
    Step 3  — @Profile marker behaviour
    Step 6  — binding.profiles attribute
    Step 8  — container profile state
    Step 11 — resolution through _filter()/get()/get_all()/is_resolvable()
    Step 13 — composition with @Alternative
    Step 15 — end-to-end env-driven scan + container.copy() inheritance
    Step 16 — interaction with validate()
"""

from __future__ import annotations

import sys
import types
import uuid
from abc import ABC

import pytest

from providify.container import DIContainer


class _ProfileWidget:
    """Module-level sentinel — @Provider return types must be resolvable via
    fn.__globals__, so locally-scoped classes cannot be used once a binding
    is actually constructed (ProviderBinding / container.provide/install)."""


class _ProfileClock:
    """Module-level sentinel, see _ProfileWidget."""


# ─────────────────────────────────────────────────────────────────
#  Step 1 — pure matcher tests: providify/profiles.py
# ─────────────────────────────────────────────────────────────────


class TestMatches:
    """`matches(expressions, active)` — OR-of-literals with leading '!' negation.

    Every row of the plan's §Design truth table, one test each.
    """

    def test_empty_expressions_always_match(self) -> None:
        """Unprofiled binding (`()`) is eligible regardless of the active set."""
        from providify.profiles import matches

        assert matches((), frozenset()) is True
        assert matches((), frozenset({"prod"})) is True

    def test_single_literal_matches_active_profile(self) -> None:
        from providify.profiles import matches

        assert matches(("prod",), frozenset({"prod"})) is True

    def test_single_literal_does_not_match_other_active_profile(self) -> None:
        from providify.profiles import matches

        assert matches(("prod",), frozenset({"dev"})) is False

    def test_multiple_literals_are_ored(self) -> None:
        from providify.profiles import matches

        assert matches(("dev", "test"), frozenset({"test"})) is True

    def test_negation_matches_when_negated_profile_inactive(self) -> None:
        from providify.profiles import matches

        assert matches(("!prod",), frozenset({"dev"})) is True

    def test_negation_fails_when_negated_profile_active(self) -> None:
        from providify.profiles import matches

        assert matches(("!prod",), frozenset({"prod"})) is False

    def test_negation_matches_against_empty_active_set(self) -> None:
        """Nothing is active, so "!prod" is vacuously true (nothing is prod)."""
        from providify.profiles import matches

        assert matches(("!prod",), frozenset()) is True

    def test_positive_literal_fails_against_empty_active_set(self) -> None:
        from providify.profiles import matches

        assert matches(("prod",), frozenset()) is False

    def test_mixed_positive_and_negative_both_fail(self) -> None:
        """dev fails (not active), !prod fails (prod IS active) -> OR of two falses."""
        from providify.profiles import matches

        assert matches(("dev", "!prod"), frozenset({"prod"})) is False


class TestParseProfiles:
    """`parse_profiles(raw)` — comma/whitespace-separated env value parsing."""

    def test_none_returns_empty_set(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles(None) == frozenset()

    def test_empty_string_returns_empty_set(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles("") == frozenset()

    def test_single_profile(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles("prod") == frozenset({"prod"})

    def test_comma_separated_profiles(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles("prod,eu") == frozenset({"prod", "eu"})

    def test_comma_separated_profiles_with_spaces(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles("prod, eu") == frozenset({"prod", "eu"})

    def test_profiles_are_stripped_and_lowercased(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles(" PROD , Eu ") == frozenset({"prod", "eu"})

    def test_empty_segments_are_dropped(self) -> None:
        from providify.profiles import parse_profiles

        assert parse_profiles("a,,b") == frozenset({"a", "b"})


class TestResolveActiveProfiles:
    """`resolve_active_profiles(explicit)` — explicit-vs-env precedence."""

    def test_none_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from providify.profiles import ENV_VAR, resolve_active_profiles

        monkeypatch.setenv(ENV_VAR, "prod,eu")
        assert resolve_active_profiles(None) == frozenset({"prod", "eu"})

    def test_empty_tuple_returns_empty_set_without_reading_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """explicit=() must win even though PROVIDIFY_PROFILES is set."""
        from providify.profiles import ENV_VAR, resolve_active_profiles

        monkeypatch.setenv(ENV_VAR, "prod,eu")
        assert resolve_active_profiles(()) == frozenset()

    def test_explicit_iterable_is_normalised(self) -> None:
        from providify.profiles import resolve_active_profiles

        assert resolve_active_profiles(["Prod"]) == frozenset({"prod"})

    def test_no_explicit_and_no_env_returns_empty_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify.profiles import ENV_VAR, resolve_active_profiles

        monkeypatch.delenv(ENV_VAR, raising=False)
        assert resolve_active_profiles(None) == frozenset()


# ─────────────────────────────────────────────────────────────────
#  Step 3 — @Profile marker
# ─────────────────────────────────────────────────────────────────


class TestProfileMarker:
    """`@Profile(*expressions)` decorator behaviour."""

    def test_returns_same_class_object(self) -> None:
        from providify import Profile

        @Profile("prod")
        class Foo:
            pass

        assert Foo.__name__ == "Foo"

    def test_marker_readable_via_get_profile_expressions(self) -> None:
        from providify import Profile
        from providify.metadata import _get_profile_expressions

        @Profile("prod")
        class Foo:
            pass

        assert _get_profile_expressions(Foo) == ("prod",)

    def test_expressions_are_normalised_stripped_and_lowercased(self) -> None:
        from providify import Profile
        from providify.metadata import _get_profile_expressions

        @Profile("A", "b")
        class Foo:
            pass

        assert _get_profile_expressions(Foo) == ("a", "b")

    def test_no_arguments_raises_value_error(self) -> None:
        from providify import Profile

        with pytest.raises(ValueError):
            Profile()

    @pytest.mark.parametrize("bad", ["", "!", "  "])
    def test_empty_or_bang_only_literal_raises_value_error(self, bad: str) -> None:
        from providify import Profile

        with pytest.raises(ValueError):
            Profile(bad)

    def test_subclass_does_not_inherit_expressions(self) -> None:
        """__dict__-only lookup — matches @Alternative's non-inheritance."""
        from providify import Profile
        from providify.metadata import _get_profile_expressions

        @Profile("prod")
        class Base:
            pass

        class Child(Base):
            pass

        assert _get_profile_expressions(Child) == ()

    def test_stacked_on_provider_function_preserves_provider_metadata(self) -> None:
        from providify import Profile, Provider
        from providify.metadata import _get_profile_expressions

        class Widget:
            pass

        @Profile("prod")
        @Provider
        def make_widget() -> Widget:
            return Widget()

        assert _get_profile_expressions(make_widget) == ("prod",)
        assert hasattr(make_widget, "__di_provider__") or hasattr(
            make_widget, "__di_scope__"
        )

    def test_applied_twice_merges_expressions_order_preserving_deduped(self) -> None:
        from providify import Profile
        from providify.metadata import _get_profile_expressions

        @Profile("dev")
        @Profile("prod")
        class Foo:
            pass

        assert _get_profile_expressions(Foo) == ("prod", "dev")


# ─────────────────────────────────────────────────────────────────
#  Step 6 — binding.profiles
# ─────────────────────────────────────────────────────────────────


class TestBindingProfilesAttribute:
    """Every ClassBinding / ProviderBinding exposes `.profiles`."""

    def test_unprofiled_class_binding_has_empty_profiles(self) -> None:
        from providify import Component
        from providify.binding import ClassBinding

        @Component
        class Plain:
            pass

        binding = ClassBinding(Plain, Plain)
        assert binding.profiles == ()

    def test_profiled_class_carries_expressions_via_bind(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class Impl(Iface):
            pass

        container.bind(Iface, Impl)
        binding = next(
            b for b in container._bindings if getattr(b, "implementation", None) is Impl
        )
        assert binding.profiles == ("prod",)

    def test_profiled_class_carries_expressions_via_register(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        @Profile("prod")
        @Singleton
        class SelfBound:
            pass

        container.register(SelfBound)
        binding = next(
            b
            for b in container._bindings
            if getattr(b, "implementation", None) is SelfBound
        )
        assert binding.profiles == ("prod",)

    def test_provider_binding_reads_marker_off_function(self) -> None:
        from providify import Profile, Provider
        from providify.binding import ProviderBinding

        @Profile("prod")
        @Provider
        def make_widget() -> _ProfileWidget:
            return _ProfileWidget()

        binding = ProviderBinding(make_widget)
        assert binding.profiles == ("prod",)

    def test_configuration_bound_method_carries_marker(
        self, container: DIContainer
    ) -> None:
        from providify import Configuration, Profile, Provider

        @Configuration
        class Config:
            @Profile("prod")
            @Provider(singleton=True)
            def make_widget(self) -> _ProfileWidget:
                return _ProfileWidget()

        container.install(Config)
        binding = next(
            b for b in container._bindings if getattr(b, "fn", None) is not None
        )
        assert binding.profiles == ("prod",)

    def test_provider_property_carries_marker(self, container: DIContainer) -> None:
        from providify import Configuration, Profile, Provider

        @Configuration
        class Config:
            @property
            @Profile("prod")
            @Provider(singleton=True)
            def widget(self) -> _ProfileWidget:
                return _ProfileWidget()

        container.install(Config)
        binding = next(
            b for b in container._bindings if getattr(b, "fn", None) is not None
        )
        assert binding.profiles == ("prod",)


# ─────────────────────────────────────────────────────────────────
#  Step 8 — container profile state
# ─────────────────────────────────────────────────────────────────


class TestContainerProfileState:
    def test_default_container_has_no_active_profiles(self) -> None:
        c = DIContainer()
        assert c.active_profiles == frozenset()

    def test_explicit_profiles_kwarg_sets_active_profiles(self) -> None:
        c = DIContainer(profiles=["prod"])
        assert c.active_profiles == frozenset({"prod"})

    def test_env_var_used_when_no_explicit_profiles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROVIDIFY_PROFILES", "prod,eu")
        c = DIContainer()
        assert c.active_profiles == frozenset({"prod", "eu"})

    def test_explicit_empty_tuple_wins_over_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROVIDIFY_PROFILES", "prod,eu")
        c = DIContainer(profiles=())
        assert c.active_profiles == frozenset()

    def test_activate_profile_normalises_and_invalidates_validated_flag(
        self, container: DIContainer
    ) -> None:
        container._validated = True
        container.activate_profile("Dev")
        assert container.active_profiles == frozenset({"dev"})
        assert container._validated is False

    def test_deactivate_profile_on_inactive_name_is_silent_noop(
        self, container: DIContainer
    ) -> None:
        container.deactivate_profile("dev")
        assert container.active_profiles == frozenset()

    def test_active_profiles_is_a_frozenset(self, container: DIContainer) -> None:
        assert isinstance(container.active_profiles, frozenset)

    def test_active_profiles_snapshot_is_immutable(
        self, container: DIContainer
    ) -> None:
        container.activate_profile("prod")
        snapshot = container.active_profiles
        with pytest.raises(AttributeError):
            snapshot.add("dev")  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────
#  Step 11 — resolution through _filter()/get()/get_all()/is_resolvable()
# ─────────────────────────────────────────────────────────────────


class TestResolutionThroughFilter:
    def test_unprofiled_binding_resolves_under_any_active_set(
        self, container: DIContainer
    ) -> None:
        from providify import Singleton

        class Iface(ABC):
            pass

        @Singleton
        class Impl(Iface):
            pass

        container.bind(Iface, Impl)
        container.activate_profile("anything")
        assert isinstance(container.get(Iface), Impl)

    def test_profiled_class_unresolvable_under_non_matching_profile(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class Impl(Iface):
            pass

        container.bind(Iface, Impl)
        container.activate_profile("dev")
        with pytest.raises(LookupError):
            container.get(Iface)

    def test_profiled_class_resolvable_under_matching_profile(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class Impl(Iface):
            pass

        container.bind(Iface, Impl)
        container.activate_profile("prod")
        assert isinstance(container.get(Iface), Impl)

    def test_get_picks_the_single_active_candidate_no_ambiguity(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class ProdImpl(Iface):
            pass

        @Profile("dev")
        @Singleton
        class DevImpl(Iface):
            pass

        container.bind(Iface, ProdImpl)
        container.bind(Iface, DevImpl)

        container.activate_profile("prod")
        assert isinstance(container.get(Iface), ProdImpl)

        container2 = DIContainer(profiles=("dev",))
        container2.bind(Iface, ProdImpl)
        container2.bind(Iface, DevImpl)
        assert isinstance(container2.get(Iface), DevImpl)

    def test_get_all_returns_only_active_candidates(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class ProdImpl(Iface):
            pass

        @Profile("dev")
        @Singleton
        class DevImpl(Iface):
            pass

        container.bind(Iface, ProdImpl)
        container.bind(Iface, DevImpl)
        container.activate_profile("prod")

        results = container.get_all(Iface)
        assert len(results) == 1
        assert isinstance(results[0], ProdImpl)

    def test_is_resolvable_tracks_the_active_set(self, container: DIContainer) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class Impl(Iface):
            pass

        container.bind(Iface, Impl)
        assert container.is_resolvable(Iface) is False

        container.activate_profile("prod")
        assert container.is_resolvable(Iface) is True

    def test_negated_profile_provider_function_filtered_out(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Provider

        @Profile("!prod")
        @Provider(singleton=True)
        def fake_clock() -> _ProfileClock:
            return _ProfileClock()

        container.provide(fake_clock)
        container.activate_profile("prod")
        with pytest.raises(LookupError):
            container.get(_ProfileClock)

    def test_activate_profile_after_get_changes_next_get_answer(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("dev")
        @Singleton
        class DevImpl(Iface):
            pass

        @Profile("prod")
        @Singleton
        class ProdImpl(Iface):
            pass

        container.bind(Iface, DevImpl)
        container.bind(Iface, ProdImpl)

        container.activate_profile("dev")
        first = container.get(Iface)
        assert isinstance(first, DevImpl)

        container.activate_profile("prod")
        container.deactivate_profile("dev")
        second = container.get(Iface)
        assert isinstance(second, ProdImpl)

    def test_cached_singleton_survives_profile_change(
        self, container: DIContainer
    ) -> None:
        """A singleton created under an old profile is NOT evicted on profile change."""
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("dev", "prod")
        @Singleton
        class Impl(Iface):
            pass

        container.bind(Iface, Impl)
        container.activate_profile("dev")
        first = container.get(Iface)

        container.activate_profile("prod")
        container.deactivate_profile("dev")
        second = container.get(Iface)

        assert first is second


# ─────────────────────────────────────────────────────────────────
#  Step 13 — composition with @Alternative (see also test_alternative.py)
# ─────────────────────────────────────────────────────────────────


class TestAlternativeProfileComposition:
    def test_alternative_with_profile_resolves_without_enable_call(
        self, container: DIContainer
    ) -> None:
        from providify import Alternative, Profile, Singleton

        class Gateway:
            pass

        @Singleton
        class RealGateway(Gateway):
            pass

        # priority=10: without an explicit tiebreaker both bindings are
        # equally eligible once "test" is active (@Priority rules apply to
        # ties per plan 005 §Edge cases) — same convention test_alternative.py
        # uses for its pre-existing MockGateway fixture.
        @Profile("test")
        @Alternative
        @Singleton(priority=10)
        class MockGateway(Gateway):
            pass

        container.bind(Gateway, RealGateway)
        container.bind(Gateway, MockGateway)
        container.activate_profile("test")

        assert isinstance(container.get(Gateway), MockGateway)

    def test_alternative_with_profile_filtered_out_under_non_matching_profile(
        self, container: DIContainer
    ) -> None:
        from providify import Alternative, Profile, Singleton

        class Gateway:
            pass

        @Singleton
        class RealGateway(Gateway):
            pass

        @Profile("test")
        @Alternative
        @Singleton
        class MockGateway(Gateway):
            pass

        container.bind(Gateway, RealGateway)
        container.bind(Gateway, MockGateway)
        container.activate_profile("prod")

        assert isinstance(container.get(Gateway), RealGateway)

    def test_enable_alternative_does_not_override_non_matching_profile(
        self, container: DIContainer
    ) -> None:
        """Profile is a hard gate — enable_alternative() cannot override a mismatch."""
        from providify import Alternative, Profile, Singleton

        class Gateway:
            pass

        @Singleton
        class RealGateway(Gateway):
            pass

        @Profile("test")
        @Alternative
        @Singleton
        class MockGateway(Gateway):
            pass

        container.bind(Gateway, RealGateway)
        container.bind(Gateway, MockGateway)
        container.enable_alternative(MockGateway)
        container.activate_profile("prod")

        assert isinstance(container.get(Gateway), RealGateway)

    def test_alternative_marked_provider_function_now_inactive_by_default(
        self, container: DIContainer
    ) -> None:
        """Behaviour change: @Alternative on a @Provider fn is now really disabled."""
        from providify import Alternative, Provider

        @Alternative
        @Provider(singleton=True)
        def mock_widget() -> _ProfileWidget:
            return _ProfileWidget()

        container.provide(mock_widget)
        with pytest.raises(LookupError):
            container.get(_ProfileWidget)


# ─────────────────────────────────────────────────────────────────
#  Step 15 — end-to-end env-driven scan + copy() inheritance
# ─────────────────────────────────────────────────────────────────


def _fresh_module_name() -> str:
    return f"_providify_test_profiles_{uuid.uuid4().hex}"


def _add(mod: types.ModuleType, obj: object) -> object:
    name = getattr(obj, "__name__", None) or getattr(obj, "__qualname__", "unknown")
    obj.__module__ = mod.__name__  # type: ignore[union-attr]
    setattr(mod, name, obj)
    return obj


@pytest.fixture
def fake_mod():
    name = _fresh_module_name()
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    yield mod
    sys.modules.pop(name, None)


class _Mailer(ABC):
    """Module-level interface for the env-driven scan test."""


class TestEndToEndEnvDrivenActivation:
    def test_scan_resolves_differently_under_two_env_profiles(
        self, fake_mod: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Profile, Singleton

        @Profile("prod")
        @Singleton
        class RealMailer(_Mailer):
            pass

        @Profile("dev")
        @Singleton
        class ConsoleMailer(_Mailer):
            pass

        _add(fake_mod, RealMailer)
        _add(fake_mod, ConsoleMailer)

        monkeypatch.setenv("PROVIDIFY_PROFILES", "dev")
        dev_container = DIContainer()
        dev_container.scan(fake_mod)
        assert isinstance(dev_container.get(_Mailer), ConsoleMailer)

        monkeypatch.setenv("PROVIDIFY_PROFILES", "prod")
        prod_container = DIContainer()
        prod_container.scan(fake_mod)
        assert isinstance(prod_container.get(_Mailer), RealMailer)

    def test_copy_inherits_active_profile_set(self, container: DIContainer) -> None:
        container.activate_profile("prod")
        clone = container.copy()
        assert clone.active_profiles == frozenset({"prod"})

    def test_copy_profile_set_is_independent_of_original(
        self, container: DIContainer
    ) -> None:
        container.activate_profile("prod")
        clone = container.copy()
        clone.activate_profile("dev")

        assert clone.active_profiles == frozenset({"prod", "dev"})
        assert container.active_profiles == frozenset({"prod"})


# ─────────────────────────────────────────────────────────────────
#  Step 16 — interaction with validate()
# ─────────────────────────────────────────────────────────────────


class TestValidateInteraction:
    def test_profile_gated_only_provider_reports_missing_binding_when_inactive(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton
        from providify.validation import IssueKind

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class Impl(Iface):
            pass

        @Singleton
        class Needs:
            def __init__(self, dep: Iface) -> None:
                self.dep = dep

        container.bind(Iface, Impl)
        container.register(Needs)

        report = container.validate(raise_on_error=False)
        assert not report.ok
        assert any(i.kind is IssueKind.MISSING_BINDING for i in report.errors)

    def test_validates_clean_when_profile_active(self, container: DIContainer) -> None:
        from providify import Profile, Singleton

        class Iface(ABC):
            pass

        @Profile("prod")
        @Singleton
        class Impl(Iface):
            pass

        @Singleton
        class Needs:
            def __init__(self, dep: Iface) -> None:
                self.dep = dep

        container.bind(Iface, Impl)
        container.register(Needs)
        container.activate_profile("prod")

        report = container.validate(raise_on_error=False)
        assert report.ok
