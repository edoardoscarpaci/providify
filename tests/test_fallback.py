"""Failing tests (RED mode) for Plan 017 — `@Fallback` marker binding.

Encodes upstream gap **P27-FALLBACK-BINDING**
(`/home/edoardo/projects/varco/design/upstream-gaps/providify-fallback-binding.md`)
and `plans/017-fallback-binding.md`.

Every test in this file is expected to fail with an ``ImportError`` on
``from providify import Fallback`` (Steps 1-3, before Step 13 lands the
export), and — once that import succeeds during implementation — with an
``AttributeError`` on ``IssueKind.FALLBACK_SHADOWED`` (before Step 5 lands
the enum member).

Covered (grouped by plan class / edge row):
    TestGuardPort          — the two [GAP §3] guard bodies, ported verbatim
    TestControlsStillPass  — the three [GAP §3] controls (today's behaviour,
                              unaffected by this plan)
    TestFallbackResolution — edge rows E1-E25
    TestFallbackValidation — E12, E13, E26-E30, plus marker-export and
                              raise_on_error=True regression tests
"""

from __future__ import annotations

import sys

import pytest

from providify import DIContainer, Inject, IssueKind, Profile, Singleton

FLOOR = -sys.maxsize - 1


class Base:
    """Shared interface used across the fallback resolution tests."""


# ─────────────────────────────────────────────────────────────────
#  TestGuardPort — [GAP §3] verbatim reproduction
# ─────────────────────────────────────────────────────────────────


class TestGuardPort:
    """Byte-for-byte port of varco's ``TestP27FallbackBinding`` guard bodies.

    Downstream: if these are green here, varco's own ``xfail(strict=True)``
    guards for this gap flip to passing against this build (plan §Downstream).
    """

    def test_fallback_decorator_is_exported(self) -> None:
        from providify import Fallback  # raises ImportError today

        @Fallback
        @Singleton
        class A(Base):
            pass

        @Singleton
        class B(Base):
            pass

        container = DIContainer()
        container.bind(Base, A)
        container.bind(Base, B)

        assert isinstance(container.get(Base), B)
        assert [type(x) for x in container.get_all(Base)] == [B]

        lone_container = DIContainer()
        lone_container.bind(Base, A)

        assert isinstance(lone_container.get(Base), A)

    def test_validate_reports_shadowed_fallback(self) -> None:
        shadowed_kind = IssueKind.FALLBACK_SHADOWED  # raises AttributeError today

        from providify import Fallback

        @Fallback
        @Singleton
        class A(Base):
            pass

        @Singleton
        class B(Base):
            pass

        container = DIContainer()
        container.bind(Base, A)
        container.bind(Base, B)

        report = container.validate(raise_on_error=False)
        shadow_issues = [issue for issue in report.issues if issue.kind == shadowed_kind]

        assert len(shadow_issues) == 1
        assert "B" in str(shadow_issues[0])

        # E26 strengthening — [GAP §3]/plan step 1: "B" in str(issue) alone
        # would pass trivially because IssueKind.FALLBACK_SHADOWED's own
        # repr contains a capital B; the message assert below is the real one.
        issue = shadow_issues[0]
        from providify.validation import Severity

        assert issue.severity is Severity.INFO
        assert issue.shadowed_by == "B"
        assert "B" in issue.message


# ─────────────────────────────────────────────────────────────────
#  TestControlsStillPass — [GAP §3] controls, unaffected by this plan
# ─────────────────────────────────────────────────────────────────


class TestControlsStillPass:
    """Today's behaviour, pinned so plan 017 cannot silently change it."""

    def test_equal_priority_tie_breaks_to_first_registered_today(
        self, container: DIContainer
    ) -> None:
        """bind(Base, A) then bind(Base, B), both priority 0 -> get(Base) is A."""

        @Singleton
        class A(Base):
            pass

        @Singleton
        class B(Base):
            pass

        container.bind(Base, A)
        container.bind(Base, B)

        assert isinstance(container.get(Base), A)

    def test_higher_priority_already_wins_regardless_of_order_today(
        self, container: DIContainer
    ) -> None:
        """A at priority 0 first, B at priority=1 second -> get(Base) is B."""

        @Singleton
        class A(Base):
            pass

        @Singleton(priority=1)
        class B(Base):
            pass

        container.bind(Base, A)
        container.bind(Base, B)

        assert isinstance(container.get(Base), B)

    def test_inactive_sibling_is_not_a_candidate_today(self) -> None:
        """A plain, B @Profile("x")-gated; DIContainer(profiles=()) -> get_all(Base) is [A].

        After activate_profile("x") -> {A, B}.
        """

        @Singleton
        class A(Base):
            pass

        @Profile("x")
        @Singleton
        class B(Base):
            pass

        container = DIContainer(profiles=())
        container.bind(Base, A)
        container.bind(Base, B)

        assert [type(x) for x in container.get_all(Base)] == [A]

        container.activate_profile("x")

        assert {type(x) for x in container.get_all(Base)} == {A, B}


# ─────────────────────────────────────────────────────────────────
#  TestFallbackResolution — edge rows E1-E25
# ─────────────────────────────────────────────────────────────────


class TestFallbackResolution:
    """One test per edge row E1-E25 of plan 017's edge table."""

    def test_e01_inactive_profile_shadower_does_not_shadow_until_activated(self) -> None:
        # E1: X gated by an inactive @Profile is not a candidate, so F wins
        # until the profile is activated (Quarkus/Micronaut "active-only"
        # model, not Spring's, per plan §Design).
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Profile("x")
        @Singleton
        class X(Base):
            pass

        container = DIContainer(profiles=())
        container.bind(Base, F)
        container.bind(Base, X)

        assert isinstance(container.get(Base), F)
        assert [type(b) for b in container.get_all(Base)] == [F]

        container.activate_profile("x")

        assert isinstance(container.get(Base), X)
        assert [type(b) for b in container.get_all(Base)] == [X]

    def test_e02_inactive_alternative_shadower_does_not_shadow_until_enabled(
        self, container: DIContainer
    ) -> None:
        # E2: same predicate as E1, exercised through @Alternative instead
        # of @Profile.
        from providify import Alternative, Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Alternative
        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert isinstance(container.get(Base), F)

        container.enable_alternative(X)
        assert isinstance(container.get(Base), X)

        container.disable_alternative(X)
        assert isinstance(container.get(Base), F)

    def test_e03_unqualified_fallback_shadowed_by_qualified_non_fallback(
        self, container: DIContainer
    ) -> None:
        # E3: an unqualified request matches any qualifier (:1729), so both
        # are candidates -> F is dropped -> X wins. Deliberate deviation
        # from CDI identity matching.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton(qualifier="x")
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert isinstance(container.get(Base), X)

    def test_e04_qualified_fallback_survives_matching_qualifier_request(
        self, container: DIContainer
    ) -> None:
        # E4: the "in_memory" escape hatch (varco §2 bucket 2) keeps working
        # — a request for F's own qualifier only ever sees F as a candidate.
        from providify import Fallback

        @Fallback
        @Singleton(qualifier="in_memory")
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert isinstance(container.get(Base, qualifier="in_memory"), F)
        assert isinstance(container.get(Base), X)

    def test_e05_no_match_at_all_raises_lookup_error_unchanged(
        self, container: DIContainer
    ) -> None:
        # E5: nothing matches the request before the fallback post-step
        # ever runs -> LookupError, unchanged.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton(qualifier="x")
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        with pytest.raises(LookupError):
            container.get(Base, qualifier="y")

    def test_e06_generic_alias_mismatch_leaves_fallback_unshadowed(
        self, container: DIContainer
    ) -> None:
        # E6: Repo[Post] never matches a Repo[User] request, so F is the
        # only candidate for that alias — baseline for plan 016.
        from typing import Generic, TypeVar

        from providify import Fallback

        T = TypeVar("T")

        class User:
            pass

        class Post:
            pass

        class Repo(Generic[T]):
            pass

        @Fallback
        @Singleton
        class FRepo(Repo[User]):
            pass

        @Singleton
        class XRepo(Repo[Post]):
            pass

        container.bind(Repo[User], FRepo)
        container.bind(Repo[Post], XRepo)

        assert isinstance(container.get(Repo[User]), FRepo)

    def test_e07_same_generic_alias_shadows_normally(self, container: DIContainer) -> None:
        # E7: F and X bound to the identical alias share one candidate set.
        from typing import Generic, TypeVar

        from providify import Fallback

        T = TypeVar("T")

        class User:
            pass

        class Repo(Generic[T]):
            pass

        @Fallback
        @Singleton
        class FRepo(Repo[User]):
            pass

        @Singleton
        class XRepo(Repo[User]):
            pass

        container.bind(Repo[User], FRepo)
        container.bind(Repo[User], XRepo)

        assert isinstance(container.get(Repo[User]), XRepo)

    def test_e08_bare_origin_sweep_is_one_request_and_fallback_yields(
        self, container: DIContainer
    ) -> None:
        # E8: get(Repo) / get_all(Repo) is a single bare-origin request that
        # both aliased bindings match -> F yields, documented not special-cased.
        from typing import Generic, TypeVar

        from providify import Fallback

        T = TypeVar("T")

        class User:
            pass

        class Post:
            pass

        class Repo(Generic[T]):
            pass

        @Fallback
        @Singleton
        class FRepo(Repo[User]):
            pass

        @Singleton
        class XRepo(Repo[Post]):
            pass

        container.bind(Repo[User], FRepo)
        container.bind(Repo[Post], XRepo)

        assert isinstance(container.get(Repo), XRepo)
        assert [type(b) for b in container.get_all(Repo)] == [XRepo]

    def test_e09_exact_only_self_binding_is_sole_candidate_for_own_type(
        self, container: DIContainer
    ) -> None:
        # E9: get(A) only ever sees A's own exact_only self-binding, which
        # is the sole candidate regardless of B's presence.
        from providify import Fallback

        @Fallback
        @Singleton
        class A(Base):
            pass

        @Singleton
        class B(Base):
            pass

        container.bind(Base, A)
        container.bind(Base, B)

        assert isinstance(container.get(A), A)

    def test_e10_self_binding_of_fallback_class_still_yields_to_subclass(
        self, container: DIContainer
    ) -> None:
        # E10: A's self-binding carries the marker at the class level, so
        # even a request for A's own concrete key yields to a non-fallback
        # SubA bound to A.
        from providify import Fallback

        @Fallback
        @Singleton
        class A(Base):
            pass

        @Singleton
        class B(Base):
            pass

        @Singleton
        class SubA(A):
            pass

        container.bind(Base, A)
        container.bind(Base, B)
        container.bind(A, SubA)

        assert isinstance(container.get(A), SubA)

    def test_e11_two_fallbacks_no_non_fallback_uses_todays_max_rule(
        self, container: DIContainer
    ) -> None:
        # E11: an all-fallback candidate set is unchanged by the post-step
        # -> today's max()-by-priority rule still applies.
        from providify import Fallback

        @Fallback
        @Singleton
        class F1(Base):
            pass

        @Fallback
        @Singleton(priority=1)
        class F2(Base):
            pass

        container.bind(Base, F1)
        container.bind(Base, F2)

        assert isinstance(container.get(Base), F2)
        assert [type(b) for b in container.get_all(Base)] == [F1, F2]

    def test_e14_priority_narrowing_returns_the_fallback_explicitly(
        self, container: DIContainer
    ) -> None:
        # E14: requesting the exact FLOOR priority narrows to an all-fallback
        # set -- an explicit "give me the default" request.
        from providify import Fallback

        @Fallback
        @Singleton(priority=FLOOR)
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert isinstance(container.get(Base, priority=FLOOR), F)
        assert container.is_resolvable(Base, priority=FLOOR) is True

    def test_e15_get_all_and_aget_all_both_exclude_shadowed_fallback(
        self, container: DIContainer
    ) -> None:
        # E15: :1400 and :1593 both route through the same _filter() step.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert [type(b) for b in container.get_all(Base)] == [X]

        import asyncio

        result = asyncio.run(container.aget_all(Base))
        assert [type(b) for b in result] == [X]

    async def test_e16_multibind_list_collection_excludes_shadowed_fallback(
        self, container: DIContainer
    ) -> None:
        # E16: get(list[T]), aget(list[T]), and a class injecting list[T]
        # all route through _collect_sync/_collect_async -> _filter().
        from providify import Fallback

        container.multibind(Base)

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert [type(b) for b in container.get(list[Base])] == [X]
        assert [type(b) for b in await container.aget(list[Base])] == [X]

        @Singleton
        class Consumer:
            def __init__(self, items: Inject[list[Base]]) -> None:
                self.items = items

        # register() is required before get() for a locally-defined class
        # that was never bind()/provide()'d — matches
        # tests/test_multibinding.py's Dispatcher pattern.
        container.register(Consumer)
        consumer = container.get(Consumer)
        assert [type(b) for b in consumer.items] == [X]

    def test_e17_multibind_with_lone_fallback_returns_it(self, container: DIContainer) -> None:
        # E17: a lone fallback is a real binding -> the collection point
        # returns it, since there is nothing to shadow it.
        from providify import Fallback

        container.multibind(Base)

        @Fallback
        @Singleton
        class F(Base):
            pass

        container.bind(Base, F)

        assert [type(b) for b in container.get(list[Base])] == [F]

    def test_e18_fallback_provider_method_in_configuration_yields_to_bind(
        self, container: DIContainer
    ) -> None:
        # E18: a bound-method @Provider carries the marker via __func__; a
        # plain bind(T, X) still shadows it.
        from providify import Configuration, Fallback, Provider
        from providify import Singleton as _S

        @Configuration
        class Config:
            @Fallback
            @Provider(singleton=True)
            def make(self) -> Base:
                return Base()

        container.install(Config)

        binding = next(b for b in container._bindings if getattr(b, "fn", None) is not None)
        assert binding.fallback is True

        assert isinstance(container.get(Base), Base)

        @_S
        class X(Base):
            pass

        other = DIContainer()
        other.install(Config)
        other.bind(Base, X)
        assert isinstance(other.get(Base), X)

    def test_e19_fallback_provider_property_in_configuration_carries_marker(
        self, container: DIContainer
    ) -> None:
        # E19: the @property wrapper copies the getter's __dict__, so the
        # marker travels onto the ProviderBinding.
        from providify import Configuration, Fallback, Provider

        @Configuration
        class Config:
            @property
            @Fallback
            @Provider(singleton=True)
            def widget(self) -> Base:
                return Base()

        container.install(Config)
        binding = next(b for b in container._bindings if getattr(b, "fn", None) is not None)
        assert binding.fallback is True

    async def test_e20_async_fallback_provider_yields_to_class_binding(
        self, container: DIContainer
    ) -> None:
        # E20: aget() must resolve the winning async candidate correctly,
        # and fall back to awaiting the fallback provider when alone.
        from providify import Fallback, Provider

        @Fallback
        @Provider(singleton=True)
        async def make_f() -> Base:
            return Base()

        @Singleton
        class X(Base):
            pass

        container.provide(make_f)
        container.bind(Base, X)

        assert isinstance(await container.aget(Base), X)

        lone = DIContainer()
        lone.provide(make_f)
        result = await lone.aget(Base)
        assert isinstance(result, Base)

    def test_e21_fallback_and_profile_compose_by_and(self) -> None:
        # E21: @Fallback @Profile("dev") F composes by AND — F must be both
        # profile-active AND unshadowed to be returned.
        from providify import Fallback

        @Fallback
        @Profile("dev")
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        with_x_no_profile = DIContainer(profiles=())
        with_x_no_profile.bind(Base, F)
        with_x_no_profile.bind(Base, X)
        assert isinstance(with_x_no_profile.get(Base), X)

        with_x_dev_profile = DIContainer(profiles=("dev",))
        with_x_dev_profile.bind(Base, F)
        with_x_dev_profile.bind(Base, X)
        assert isinstance(with_x_dev_profile.get(Base), X)

        no_x_dev_profile = DIContainer(profiles=("dev",))
        no_x_dev_profile.bind(Base, F)
        assert isinstance(no_x_dev_profile.get(Base), F)

        no_x_no_profile = DIContainer(profiles=())
        no_x_no_profile.bind(Base, F)
        with pytest.raises(LookupError):
            no_x_no_profile.get(Base)

    def test_e22_decorator_order_does_not_matter(self, container: DIContainer) -> None:
        # E22: bindings are built at bind()/register() time, long after
        # decoration, so @Singleton above or below @Fallback is identical.
        from providify import Fallback
        from providify.metadata import _is_fallback

        @Fallback
        @Singleton
        class TopFallback(Base):
            pass

        @Singleton
        @Fallback
        class BottomFallback(Base):
            pass

        assert _is_fallback(TopFallback) is True
        assert _is_fallback(BottomFallback) is True

    def test_e23_subclass_of_fallback_class_does_not_inherit_marker(
        self, container: DIContainer
    ) -> None:
        # E23: __dict__-only lookup, no MRO walk -- Sub is NOT a fallback
        # even though it subclasses F, matching @Profile's rule.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class Sub(F):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, Sub)
        container.bind(Base, X)

        assert [type(b) for b in container.get_all(Base)] == [Sub, X]

    def test_e24_cached_fallback_singleton_not_evicted_by_later_shadower(
        self, container: DIContainer
    ) -> None:
        # E24: cache is keyed per binding source -- the earlier F instance
        # is not evicted when X arrives, and a dependent constructed before
        # the shadowing bind still holds the stale F reference.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class Dependent:
            def __init__(self, base: Inject[Base]) -> None:
                self.base = base

        container.bind(Base, F)
        # register() is required before get() for a locally-defined class
        # that was never bind()/provide()'d — matches
        # tests/test_multibinding.py's Dispatcher pattern.
        container.register(Dependent)

        first = container.get(Base)
        dependent = container.get(Dependent)
        assert isinstance(first, F)
        assert dependent.base is first

        @Singleton
        class X(Base):
            pass

        container.bind(Base, X)

        assert isinstance(container.get(Base), X)
        # the earlier dependent still references the stale F instance
        assert dependent.base is first

    def test_e25_get_binding_and_get_all_bindings_exclude_shadowed_fallback(
        self, container: DIContainer
    ) -> None:
        # E25: get_binding()/get_all_bindings() route through the same
        # _get_best_candidate/_filter as get()/get_all().
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        assert container.get_binding(Base).implementation is X
        assert [b.implementation for b in container.get_all_bindings(Base)] == [X]
        assert container.is_resolvable(Base) is True

        lone = DIContainer()
        lone.bind(Base, F)
        assert lone.is_resolvable(Base) is True


# ─────────────────────────────────────────────────────────────────
#  TestFallbackValidation — E12, E13, E26-E30, marker export, INFO safety
# ─────────────────────────────────────────────────────────────────


class TestFallbackValidation:
    """Validation-focused edge rows and the marker/`raise_on_error` contracts."""

    def test_e12_tied_fallbacks_report_ambiguous_binding_not_fallback_shadowed(
        self, container: DIContainer
    ) -> None:
        # E12: two equal-priority fallbacks with no non-fallback -> today's
        # AMBIGUOUS_BINDING ERROR, and zero FALLBACK_SHADOWED issues.
        from providify import Fallback

        @Fallback
        @Singleton
        class F1(Base):
            pass

        @Fallback
        @Singleton
        class F2(Base):
            pass

        @Singleton
        class Consumer:
            def __init__(self, base: Inject[Base]) -> None:
                self.base = base

        container.bind(Base, F1)
        container.bind(Base, F2)
        container.register(Consumer)

        assert isinstance(container.get(Base), F1)

        report = container.validate(raise_on_error=False)
        ambiguous = [i for i in report.issues if i.kind == IssueKind.AMBIGUOUS_BINDING]
        shadowed = [i for i in report.issues if i.kind == IssueKind.FALLBACK_SHADOWED]

        assert len(ambiguous) == 1
        assert len(shadowed) == 0

    def test_e13_shadowed_fallbacks_report_no_ambiguity_and_two_infos(
        self, container: DIContainer
    ) -> None:
        # E13: with a real X present, pass 2 sees the post-shadowing set
        # {X} for a single-valued injection point -> no false ambiguity,
        # and each of the two fallbacks gets its own FALLBACK_SHADOWED.
        from providify import Fallback

        @Fallback
        @Singleton
        class F1(Base):
            pass

        @Fallback
        @Singleton
        class F2(Base):
            pass

        @Singleton
        class X(Base):
            pass

        @Singleton
        class Consumer:
            def __init__(self, base: Inject[Base]) -> None:
                self.base = base

        container.bind(Base, F1)
        container.bind(Base, F2)
        container.bind(Base, X)
        container.register(Consumer)

        assert isinstance(container.get(Base), X)

        report = container.validate(raise_on_error=False)
        ambiguous = [i for i in report.issues if i.kind == IssueKind.AMBIGUOUS_BINDING]
        shadowed = [i for i in report.issues if i.kind == IssueKind.FALLBACK_SHADOWED]

        assert len(ambiguous) == 0
        assert len(shadowed) == 2
        assert {i.shadowed_by for i in shadowed} == {"X"}

    def test_e26_shadowed_fallback_reports_a_full_info_issue(self, container: DIContainer) -> None:
        # E26: [GAP §3] guard + strengthening — full field-by-field shape of
        # the shadowed-fallback INFO issue.
        from providify import Fallback
        from providify.validation import Severity

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        report = container.validate(raise_on_error=False)
        shadowed = [i for i in report.issues if i.kind == IssueKind.FALLBACK_SHADOWED]

        assert len(shadowed) == 1
        issue = shadowed[0]
        assert issue.severity is Severity.INFO
        assert issue.shadowed_by == "X"
        assert "X" in issue.message
        assert report.ok is True
        assert report.warnings == ()
        # raise_on_error=True must not raise on an INFO-only report
        container.validate(raise_on_error=True)
        assert issue in report.infos
        assert "[INFO]" in repr(report)

    def test_e27_inactive_fallback_is_not_reported_as_shadowed(
        self, container: DIContainer
    ) -> None:
        # E27: pass 1c's _binding_is_active gate — an inactive fallback is
        # invisible, not shadowed; that is @Profile's/plan 015's story.
        from providify import Fallback

        @Fallback
        @Profile("dev")
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        report = container.validate(raise_on_error=False)
        shadowed = [i for i in report.issues if i.kind == IssueKind.FALLBACK_SHADOWED]
        assert shadowed == []

    def test_e28_qualified_fallback_natural_request_is_not_shadowed(
        self, container: DIContainer
    ) -> None:
        # E28: F's natural request is (T, "in_memory", None); an unqualified
        # X does not shadow it -- no issue.
        from providify import Fallback

        @Fallback
        @Singleton(qualifier="in_memory")
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        report = container.validate(raise_on_error=False)
        shadowed = [i for i in report.issues if i.kind == IssueKind.FALLBACK_SHADOWED]
        assert shadowed == []

    def test_e29_lone_fallback_reports_no_issue(self, container: DIContainer) -> None:
        # E29: no non-fallback sibling -> nothing to shadow F.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        container.bind(Base, F)

        report = container.validate(raise_on_error=False)
        shadowed = [i for i in report.issues if i.kind == IssueKind.FALLBACK_SHADOWED]
        assert shadowed == []

    def test_e30_empty_container_validation_is_unchanged(self, container: DIContainer) -> None:
        # E30: regression on tests/test_validation.py::TestReportShape --
        # this plan must not alter the empty-container baseline.
        report = container.validate(raise_on_error=False)
        assert report.issues == ()
        assert report.ok is True
        assert report.checked_bindings == 0

    def test_fallback_marker_is_exported_and_is_the_stamp(self, container: DIContainer) -> None:
        # The attribute name is storage, not API -- the marker's presence
        # must be checked via the type-based helper, never __dict__ keys.
        from providify import Fallback, FallbackMarker  # noqa: F401
        from providify.metadata import _is_fallback

        @Fallback
        @Singleton
        class A(Base):
            pass

        assert _is_fallback(A) is True

        @Singleton
        class Plain(Base):
            pass

        assert _is_fallback(Plain) is False

    def test_info_never_raises_with_raise_on_error_true(self, container: DIContainer) -> None:
        # E26's raise_on_error=True half, separately named so a regression
        # (INFO accidentally treated like an error) is loud on its own.
        from providify import Fallback

        @Fallback
        @Singleton
        class F(Base):
            pass

        @Singleton
        class X(Base):
            pass

        container.bind(Base, F)
        container.bind(Base, X)

        report = container.validate(raise_on_error=True)
        assert report.ok is True
        assert any(i.kind == IssueKind.FALLBACK_SHADOWED for i in report.issues)
