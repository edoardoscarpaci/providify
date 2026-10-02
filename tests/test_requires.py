"""Failing tests (RED mode) for Plan 015 — `@Requires(condition=…, env=…, value=…)`.

These tests encode the design in
`plans/015-requires-conditional-registration.md` before any of `Requires`,
`RequiresMarker`, `ConditionEvaluationError`, `.conditions` on bindings, the
third `_binding_is_active()` conjunct, or `Severity.INFO`/
`IssueKind.CONDITION_INACTIVE` exist. Every test is expected to fail with an
ImportError/AttributeError until the corresponding implementation steps land.

Covered (grouped by plan Step number):
    Step 1  — gap-report reproduction + lazy toggling + non-eviction
    Step 2  — `@Requires` decoration-time contract
    Step 3  — `env=`/`value=` sugar truth table
    Step 4  — every resolution path + composition with @Profile/@Alternative
    Step 5  — `validate()` CONDITION_INACTIVE / Severity.INFO surface
"""

from __future__ import annotations

from abc import ABC

import pytest

from providify.container import DIContainer

# ─────────────────────────────────────────────────────────────────
#  Module-level sentinels — @Provider return types must be resolvable
#  via fn.__globals__, see tests/test_profiles.py:33-40.
# ─────────────────────────────────────────────────────────────────


class _ReqBase(ABC):
    """Module-level interface for the gap-report reproduction (Step 1)."""


class _ReqWidget:
    """Module-level sentinel — see module docstring."""


class _ReqGadget:
    """Module-level sentinel used by resolution tests."""


class _ReqService:
    """Module-level sentinel — depends on _ReqBase for the MISSING_BINDING test."""


# ─────────────────────────────────────────────────────────────────
#  Step 1 — gap-report reproduction, lazy toggling, non-eviction
# ─────────────────────────────────────────────────────────────────


class TestRequiresReproduction:
    def test_gap_report_reproduction(self, container: DIContainer) -> None:
        """[GAP:111-133] ported verbatim except OnImpl/OffImpl get @Component.

        Locks in: a flag closed over by `condition=` gates get(Base).
        """
        from providify import Component, Requires

        flag = {"on": True}

        @Component
        class OnImpl(_ReqBase):
            pass

        @Requires(condition=lambda: flag["on"])
        @Component
        class OffImpl(_ReqBase):
            pass

        container.bind(_ReqBase, OnImpl)
        container.bind(_ReqBase, OffImpl)

        # Both candidates tied unless OffImpl's condition is toggled off.
        flag["on"] = False
        assert isinstance(container.get(_ReqBase), OnImpl)

    def test_condition_toggles_at_resolve_time(self, container: DIContainer) -> None:
        """Lazy, not memoised — get() flips back and forth twice."""
        from providify import Component, Requires

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)

        assert isinstance(container.get(_ReqBase), Impl)
        flag["on"] = False
        with pytest.raises(LookupError):
            container.get(_ReqBase)
        flag["on"] = True
        assert isinstance(container.get(_ReqBase), Impl)

    def test_condition_true_then_false_does_not_evict_cached_singleton(
        self, container: DIContainer
    ) -> None:
        """E12 — cached singleton stays in the cache after condition flips False."""
        from providify import Requires, Singleton

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Singleton
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        instance = container.get(_ReqBase)

        flag["on"] = False
        with pytest.raises(LookupError):
            container.get(_ReqBase)

        # Non-eviction: the instance built while active is still cached.
        assert instance in container._singleton_cache.values()


# ─────────────────────────────────────────────────────────────────
#  Step 2 — @Requires decoration-time contract
# ─────────────────────────────────────────────────────────────────


class TestRequiresDecorator:
    def test_requires_is_exported(self) -> None:
        from providify import ConditionEvaluationError, Requires, RequiresMarker

        assert Requires is not None
        assert RequiresMarker is not None
        assert ConditionEvaluationError is not None

    def test_marker_is_frozen_dataclass_with_own_dict_storage(self) -> None:
        from providify import Requires
        from providify.metadata import RequiresMarker, _get_requires_markers

        @Requires(condition=lambda: True)
        class Foo:
            pass

        markers = _get_requires_markers(Foo)
        assert isinstance(markers, tuple)
        assert len(markers) == 1
        assert isinstance(markers[0], RequiresMarker)

        class Child(Foo):
            pass

        # Own __dict__ only — matches @Profile's non-inherited lookup (E21).
        assert _get_requires_markers(Child) == ()

    def test_stacked_requires_append_markers_and_and_together(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Requires
        from providify.metadata import _get_requires_markers

        flags = {"a": True, "b": True}

        @Requires(condition=lambda: flags["a"])
        @Requires(condition=lambda: flags["b"])
        class Foo:
            pass

        markers = _get_requires_markers(Foo)
        assert len(markers) == 2

        for a, b, expected in [
            (True, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, False),
        ]:
            flags["a"], flags["b"] = a, b
            assert all(m.is_satisfied() for m in markers) is expected

    def test_no_condition_and_no_env_raises_value_error(self) -> None:
        from providify import Requires

        with pytest.raises(ValueError):
            Requires()

    def test_value_without_env_raises_value_error(self) -> None:
        from providify import Requires

        with pytest.raises(ValueError):
            Requires(value="x")

    def test_empty_env_name_raises_value_error(self) -> None:
        from providify import Requires

        with pytest.raises(ValueError):
            Requires(env="")

    def test_non_callable_condition_raises_type_error(self) -> None:
        from providify import Requires

        with pytest.raises(TypeError):
            Requires(condition="not-callable")  # type: ignore[arg-type]

    def test_condition_and_env_combine_by_and(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.setenv("REQUIRES_TEST_AND", "yes")

        @Requires(condition=lambda: False, env="REQUIRES_TEST_AND")
        class Foo:
            pass

        marker = _get_requires_markers(Foo)[0]
        # env is satisfied but condition is False -> AND is False (E11).
        assert marker.is_satisfied() is False


# ─────────────────────────────────────────────────────────────────
#  Step 3 — env=/value= sugar truth table
# ─────────────────────────────────────────────────────────────────


class TestRequiresEnvSugar:
    def test_env_unset_is_inactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.delenv("REQUIRES_TEST_UNSET", raising=False)

        @Requires(env="REQUIRES_TEST_UNSET")
        class Foo:
            pass

        assert _get_requires_markers(Foo)[0].is_satisfied() is False

    def test_env_empty_string_is_inactive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.setenv("REQUIRES_TEST_EMPTY", "")

        @Requires(env="REQUIRES_TEST_EMPTY")
        class Foo:
            pass

        assert _get_requires_markers(Foo)[0].is_satisfied() is False

    def test_env_non_empty_is_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.setenv("REQUIRES_TEST_NONEMPTY", "anything")

        @Requires(env="REQUIRES_TEST_NONEMPTY")
        class Foo:
            pass

        assert _get_requires_markers(Foo)[0].is_satisfied() is True

    def test_env_value_exact_match_is_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.setenv("REQUIRES_TEST_VALUE", "redis")

        @Requires(env="REQUIRES_TEST_VALUE", value="redis")
        class Foo:
            pass

        assert _get_requires_markers(Foo)[0].is_satisfied() is True

    @pytest.mark.parametrize(
        "env_value",
        ["Redis", "other", ""],
        ids=["case-difference", "mismatch", "empty-vs-nonempty"],
    )
    def test_env_value_mismatch_is_inactive(
        self, monkeypatch: pytest.MonkeyPatch, env_value: str
    ) -> None:
        """E8 — exact, case-sensitive equality; "Redis" != "redis" locks case-sensitivity."""
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.setenv("REQUIRES_TEST_MISMATCH", env_value)

        @Requires(env="REQUIRES_TEST_MISMATCH", value="redis")
        class Foo:
            pass

        assert _get_requires_markers(Foo)[0].is_satisfied() is False

    def test_env_value_empty_string_matches_set_but_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E9 — value="" means "set and exactly empty"; unset stays inactive."""
        from providify import Requires
        from providify.metadata import _get_requires_markers

        @Requires(env="REQUIRES_TEST_E9", value="")
        class Foo:
            pass

        marker = _get_requires_markers(Foo)[0]

        monkeypatch.setenv("REQUIRES_TEST_E9", "")
        assert marker.is_satisfied() is True

        monkeypatch.delenv("REQUIRES_TEST_E9", raising=False)
        assert marker.is_satisfied() is False

    def test_env_is_read_lazily_after_decoration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Decorate first, setenv afterwards, resolve -> active (never cached at decoration time)."""
        from providify import Requires
        from providify.metadata import _get_requires_markers

        monkeypatch.delenv("REQUIRES_TEST_LAZY", raising=False)

        @Requires(env="REQUIRES_TEST_LAZY")
        class Foo:
            pass

        marker = _get_requires_markers(Foo)[0]
        assert marker.is_satisfied() is False

        monkeypatch.setenv("REQUIRES_TEST_LAZY", "now-set")
        assert marker.is_satisfied() is True


# ─────────────────────────────────────────────────────────────────
#  Step 4 — every resolution path + composition
# ─────────────────────────────────────────────────────────────────


class TestRequiresResolution:
    def test_get_all_excludes_inactive(self, container: DIContainer) -> None:
        from providify import Component, Requires

        @Component
        class Active(_ReqBase):
            pass

        @Requires(condition=lambda: False)
        @Component
        class Inactive(_ReqBase):
            pass

        container.bind(_ReqBase, Active)
        container.bind(_ReqBase, Inactive)

        results = container.get_all(_ReqBase)
        assert all(not isinstance(r, Inactive) for r in results)

    def test_is_resolvable_reflects_condition(self, container: DIContainer) -> None:
        from providify import Component, Requires

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        assert container.is_resolvable(_ReqBase) is True

        flag["on"] = False
        assert container.is_resolvable(_ReqBase) is False

    async def test_aget_and_aget_all_respect_condition(self, container: DIContainer) -> None:
        from providify import Component, Requires

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        assert isinstance(await container.aget(_ReqBase), Impl)

        flag["on"] = False
        with pytest.raises(LookupError):
            await container.aget(_ReqBase)
        # aget_all() mirrors get_all(): an empty candidate list raises, whether
        # nothing was ever bound or every binding is condition-inactive.
        with pytest.raises(LookupError):
            await container.aget_all(_ReqBase)

    @pytest.mark.parametrize("profile_active", [True, False])
    @pytest.mark.parametrize("condition_true", [True, False])
    def test_and_with_profile_matching_and_not_matching(
        self, container: DIContainer, profile_active: bool, condition_true: bool
    ) -> None:
        from providify import Component, Profile, Requires

        @Requires(condition=lambda: condition_true)
        @Profile("prod")
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        if profile_active:
            container.activate_profile("prod")

        expect_active = profile_active and condition_true
        assert container.is_resolvable(_ReqBase) is expect_active

    @pytest.mark.parametrize("alt_enabled", [True, False])
    @pytest.mark.parametrize("condition_true", [True, False])
    def test_and_with_alternative_enabled_and_not_enabled(
        self, container: DIContainer, alt_enabled: bool, condition_true: bool
    ) -> None:
        from providify import Alternative, Component, Requires, Singleton

        @Singleton
        class Real(_ReqBase):
            pass

        # priority=10: _get_best_candidate() breaks a priority tie in favour of
        # the first-registered binding (Real), so the alternative needs a higher
        # priority to win once enabled — same setup as tests/test_alternative.py.
        @Requires(condition=lambda: condition_true)
        @Alternative
        @Component(priority=10)
        class Mock(_ReqBase):
            pass

        container.bind(_ReqBase, Real)
        container.bind(_ReqBase, Mock)
        if alt_enabled:
            container.enable_alternative(Mock)

        got = container.get(_ReqBase)
        expect_mock = alt_enabled and condition_true
        assert isinstance(got, Mock) is expect_mock

    def test_on_provider_function(self, container: DIContainer) -> None:
        from providify import Provider, Requires

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Provider(singleton=True)
        def make_gadget() -> _ReqGadget:
            return _ReqGadget()

        container.provide(make_gadget)
        assert isinstance(container.get(_ReqGadget), _ReqGadget)

        flag["on"] = False
        with pytest.raises(LookupError):
            container.get(_ReqGadget)

    def test_on_configuration_provider_method(self, container: DIContainer) -> None:
        """Proves the bound-method __func__ fallback (metadata.py accessors)."""
        from providify import Configuration, Provider, Requires

        flag = {"on": False}

        @Configuration
        class Config:
            @Requires(condition=lambda: flag["on"])
            @Provider(singleton=True)
            def make_gadget(self) -> _ReqGadget:
                return _ReqGadget()

        container.install(Config)
        with pytest.raises(LookupError):
            container.get(_ReqGadget)

        flag["on"] = True
        assert isinstance(container.get(_ReqGadget), _ReqGadget)

    def test_self_binding_from_bind_is_gated_identically(self, container: DIContainer) -> None:
        """container.get(OnImpl) and get(Base) agree (container.py:1023-1024)."""
        from providify import Component, Requires

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        flag["on"] = False
        with pytest.raises(LookupError):
            container.get(_ReqBase)
        with pytest.raises(LookupError):
            container.get(Impl)

    def test_raising_predicate_propagates_condition_evaluation_error(
        self, container: DIContainer
    ) -> None:
        from providify import Component, ConditionEvaluationError, Requires

        def boom() -> bool:
            raise RuntimeError("broken predicate")

        @Requires(condition=boom)
        @Component
        class OnImpl(_ReqBase):
            pass

        container.bind(_ReqBase, OnImpl)

        with pytest.raises(ConditionEvaluationError, match="OnImpl") as exc:
            container.get(_ReqBase)

        assert isinstance(exc.value.__cause__, RuntimeError)
        assert "cheap, pure" in str(exc.value)

    def test_raising_predicate_is_not_evaluated_when_profile_excludes_binding(
        self, container: DIContainer
    ) -> None:
        """E14 — condition evaluated last; a prod-only raising predicate never runs in dev."""
        from providify import Component, Profile, Requires

        def boom() -> bool:
            raise RuntimeError("should never run")

        @Component
        class Other(_ReqBase):
            pass

        @Requires(condition=boom)
        @Profile("prod")
        @Component
        class ProdOnly(_ReqBase):
            pass

        container.bind(_ReqBase, Other)
        container.bind(_ReqBase, ProdOnly)

        # No profile active -> ProdOnly excluded by profile before condition runs.
        assert isinstance(container.get(_ReqBase), Other)

    def test_copy_and_snapshot_keep_conditions(self, container: DIContainer) -> None:
        from providify import Component, Requires

        flag = {"on": True}

        @Requires(condition=lambda: flag["on"])
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        copied = container.copy()

        assert isinstance(copied.get(_ReqBase), Impl)
        flag["on"] = False
        with pytest.raises(LookupError):
            copied.get(_ReqBase)


# ─────────────────────────────────────────────────────────────────
#  Step 5 — validate() CONDITION_INACTIVE / Severity.INFO surface
# ─────────────────────────────────────────────────────────────────


class TestConditionInactiveValidation:
    def test_inactive_condition_reports_info_issue(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Component, Requires
        from providify.validation import IssueKind, Severity

        monkeypatch.delenv("REQUIRES_TEST_VALIDATE_INFO", raising=False)

        @Requires(env="REQUIRES_TEST_VALIDATE_INFO")
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        report = container.validate(raise_on_error=False)

        info_issues = [i for i in report.issues if i.kind is IssueKind.CONDITION_INACTIVE]
        assert len(info_issues) == 1
        issue = info_issues[0]
        assert issue.severity is Severity.INFO
        assert "Impl" in issue.owner
        assert "_ReqBase" in issue.requested or "ReqBase" in issue.requested
        assert "REQUIRES_TEST_VALIDATE_INFO" in issue.message

    def test_active_condition_reports_nothing(self, container: DIContainer) -> None:
        from providify import Component, Requires
        from providify.validation import IssueKind

        @Requires(condition=lambda: True)
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        report = container.validate(raise_on_error=False)

        assert not any(i.kind is IssueKind.CONDITION_INACTIVE for i in report.issues)

    def test_validate_raise_on_error_true_never_raises_on_info(
        self, container: DIContainer
    ) -> None:
        from providify import Component, Requires

        @Requires(condition=lambda: False)
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)

        # default raise_on_error=True must not raise for an INFO-only report.
        report = container.validate()
        assert report.ok is True
        assert report.errors == ()
        assert report.warnings == ()
        assert len(report.infos) == 1

    def test_repr_renders_info_tier(self, container: DIContainer) -> None:
        from providify import Component, Requires

        @Requires(condition=lambda: False)
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        report = container.validate(raise_on_error=False)

        assert "[INFO]" in repr(report)

    def test_to_dict_severity_is_info_string(self, container: DIContainer) -> None:
        from providify import Component, Requires
        from providify.validation import IssueKind

        @Requires(condition=lambda: False)
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        report = container.validate(raise_on_error=False)

        issue = next(i for i in report.issues if i.kind is IssueKind.CONDITION_INACTIVE)
        assert issue.to_dict()["severity"] == "info"

    def test_inactive_condition_plus_dependent_reports_missing_binding_error(
        self, container: DIContainer
    ) -> None:
        """The only provider of _ReqBase is condition-inactive and _ReqService
        injects it -> one MISSING_BINDING ERROR and one CONDITION_INACTIVE INFO."""
        from providify import Component, Requires, Singleton
        from providify.validation import IssueKind, Severity

        @Requires(condition=lambda: False)
        @Component
        class Impl(_ReqBase):
            pass

        @Singleton
        class Wired:
            def __init__(self, base: _ReqBase) -> None:
                self.base = base

        container.bind(_ReqBase, Impl)
        container.register(Wired)

        report = container.validate(raise_on_error=False)

        assert any(
            i.kind is IssueKind.MISSING_BINDING and i.severity is Severity.ERROR
            for i in report.issues
        )
        assert any(i.kind is IssueKind.CONDITION_INACTIVE for i in report.issues)

    def test_profile_inactive_binding_still_reports_condition_inactive(
        self, container: DIContainer
    ) -> None:
        """E13 — pass 1 is unfiltered; @Profile-inactive binding still gets CONDITION_INACTIVE."""
        from providify import Component, Profile, Requires
        from providify.validation import IssueKind

        @Requires(condition=lambda: False)
        @Profile("prod")
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        report = container.validate(raise_on_error=False)

        assert any(i.kind is IssueKind.CONDITION_INACTIVE for i in report.issues)

    def test_raising_predicate_propagates_from_validate(self, container: DIContainer) -> None:
        """E14 — validate() raises ConditionEvaluationError for a broken predicate."""
        from providify import Component, ConditionEvaluationError, Requires

        def boom() -> bool:
            raise RuntimeError("broken")

        @Requires(condition=boom)
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)

        with pytest.raises(ConditionEvaluationError):
            container.validate(raise_on_error=False)

    def test_validated_flag_not_set_when_only_info_present(self, container: DIContainer) -> None:
        """E17 — _validated stays False when only INFO issues exist (conservative rule)."""
        from providify import Component, Requires

        @Requires(condition=lambda: False)
        @Component
        class Impl(_ReqBase):
            pass

        container.bind(_ReqBase, Impl)
        container.validate(raise_on_error=False)

        assert container._validated is False


# ─────────────────────────────────────────────────────────────────
#  P34 (varco) — validate() must not graph-check an INACTIVE owner's
#  own dependencies. Module-level sentinels: __init__/@Provider
#  annotations are resolved via __globals__ under PEP 563.
# ─────────────────────────────────────────────────────────────────


from providify import Inject  # noqa: E402 — module scope so PEP 563 hints resolve


class _OwnerMissing:
    """Never bound — the unsatisfied dependency of an inactive owner."""


class _OwnerPort(ABC):
    """Interface an inactive owner is bound to."""


class _OwnerProduct:
    """Return type of a @Requires-gated @Provider."""


class _OwnerDependent:
    """Sentinel — an active class depending on _OwnerPort."""


class TestInactiveOwnerDependencies:
    """An inactive binding's OWN injection points are not graph-checked.

    User reports (varco P34): validate() emits CONDITION_INACTIVE (INFO) for
    a binding gated off by @Requires/@Profile and, in the same report, a
    MISSING_BINDING ERROR for that binding's own unbound dependency. Correct
    behaviour: no MISSING_BINDING for the inactive owner, because get() can
    never construct it — validate()'s contract is "the graph as it will
    actually be wired" (plans 005/015). Dependents of an inactive binding
    keep reporting MISSING_BINDING exactly as before (candidate side).
    """

    @staticmethod
    def _missing(report: object) -> list[object]:
        from providify.validation import IssueKind

        return [i for i in report.issues if i.kind is IssueKind.MISSING_BINDING]  # type: ignore[attr-defined]

    def test_regression_validate_inactive_owner_deps_requires_class(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Requires, Singleton
        from providify.validation import IssueKind

        monkeypatch.delenv("P34_GATE_CLASS", raising=False)

        @Singleton
        @Requires(env="P34_GATE_CLASS", value="on")
        class Gated(_OwnerPort):
            def __init__(self, m: Inject[_OwnerMissing]) -> None:
                self.m = m

        container.bind(_OwnerPort, Gated)
        report = container.validate(raise_on_error=False)

        assert self._missing(report) == []
        assert any(i.kind is IssueKind.CONDITION_INACTIVE for i in report.issues)
        assert report.ok is True

    def test_regression_validate_inactive_owner_deps_requires_provider(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Configuration, Provider, Requires
        from providify.validation import IssueKind

        monkeypatch.delenv("P34_GATE_PROVIDER", raising=False)

        @Configuration
        class Config:
            @Requires(env="P34_GATE_PROVIDER", value="on")
            @Provider(singleton=True)
            def product(self, m: _OwnerMissing) -> _OwnerProduct:
                return _OwnerProduct()

        container.install(Config)
        report = container.validate(raise_on_error=False)

        assert self._missing(report) == []
        assert any(i.kind is IssueKind.CONDITION_INACTIVE for i in report.issues)
        assert report.ok is True

    def test_regression_validate_inactive_owner_deps_profile_excluded_class(
        self, container: DIContainer
    ) -> None:
        from providify import Profile, Singleton

        @Profile("prod")
        @Singleton
        class ProdOnly(_OwnerPort):
            def __init__(self, m: Inject[_OwnerMissing]) -> None:
                self.m = m

        container.bind(_OwnerPort, ProdOnly)
        report = container.validate(raise_on_error=False)

        assert self._missing(report) == []
        assert report.ok is True

    def test_not_enabled_alternative_owner_deps_not_checked(self, container: DIContainer) -> None:
        """Same predicate (alternative_ok) — a not-enabled @Alternative owner."""
        from providify import Alternative, Singleton

        @Alternative
        @Singleton
        class Alt(_OwnerPort):
            def __init__(self, m: Inject[_OwnerMissing]) -> None:
                self.m = m

        container.bind(_OwnerPort, Alt)
        report = container.validate(raise_on_error=False)

        assert self._missing(report) == []

    def test_active_requires_class_with_missing_dep_still_errors(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Requires, Singleton
        from providify.validation import IssueKind, Severity

        monkeypatch.setenv("P34_GATE_ON", "on")

        @Singleton
        @Requires(env="P34_GATE_ON", value="on")
        class Gated(_OwnerPort):
            def __init__(self, m: Inject[_OwnerMissing]) -> None:
                self.m = m

        container.bind(_OwnerPort, Gated)
        report = container.validate(raise_on_error=False)

        missing = self._missing(report)
        assert missing and all(i.severity is Severity.ERROR for i in missing)  # type: ignore[attr-defined]
        assert not any(i.kind is IssueKind.CONDITION_INACTIVE for i in report.issues)
        assert report.ok is False

    def test_active_requires_provider_with_missing_dep_still_errors(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from providify import Configuration, Provider, Requires

        monkeypatch.setenv("P34_GATE_PROVIDER_ON", "on")

        @Configuration
        class Config:
            @Requires(env="P34_GATE_PROVIDER_ON", value="on")
            @Provider(singleton=True)
            def product(self, m: _OwnerMissing) -> _OwnerProduct:
                return _OwnerProduct()

        container.install(Config)
        report = container.validate(raise_on_error=False)

        assert len(self._missing(report)) == 1
        assert report.ok is False

    def test_condition_toggle_is_reflected_on_next_validate(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E2 — lazy: the same container reports per its CURRENT condition state."""
        from providify import Requires, Singleton

        monkeypatch.delenv("P34_GATE_TOGGLE", raising=False)

        @Singleton
        @Requires(env="P34_GATE_TOGGLE", value="on")
        class Gated(_OwnerPort):
            def __init__(self, m: Inject[_OwnerMissing]) -> None:
                self.m = m

        container.bind(_OwnerPort, Gated)
        assert container.validate(raise_on_error=False).ok is True

        monkeypatch.setenv("P34_GATE_TOGGLE", "on")
        assert self._missing(container.validate(raise_on_error=False))

    def test_dependent_of_inactive_owner_still_reports_missing_binding(
        self, container: DIContainer
    ) -> None:
        """Candidate side unchanged (plan 015): an ACTIVE dependent of the
        inactive owner gets exactly one MISSING_BINDING; the inactive owner's
        own unbound dependency contributes none."""
        from providify import Component, Requires, Singleton
        from providify.validation import Severity

        @Requires(condition=lambda: False)
        @Component
        class Gated(_OwnerPort):
            def __init__(self, m: Inject[_OwnerMissing]) -> None:
                self.m = m

        @Singleton
        class Dependent(_OwnerDependent):
            def __init__(self, port: _OwnerPort) -> None:
                self.port = port

        container.bind(_OwnerPort, Gated)
        container.register(Dependent)
        report = container.validate(raise_on_error=False)

        missing = self._missing(report)
        assert len(missing) == 1
        assert missing[0].severity is Severity.ERROR  # type: ignore[attr-defined]
        assert missing[0].requested == "_OwnerPort"  # type: ignore[attr-defined]

    def test_inactive_owner_scope_tier_still_runs(self, container: DIContainer) -> None:
        """Pass 1 stays unfiltered (plan 005): an inactive owner's scope leak
        is still reported — only the pass-2 graph walk is skipped."""
        from providify import Component, Requires, Singleton
        from providify.validation import IssueKind

        @Component
        class Shortlived(_OwnerMissing):
            pass

        @Requires(condition=lambda: False)
        @Singleton
        class Gated(_OwnerPort):
            def __init__(self, m: _OwnerMissing) -> None:
                self.m = m

        container.bind(_OwnerMissing, Shortlived)
        container.bind(_OwnerPort, Gated)
        report = container.validate(raise_on_error=False)

        assert any(i.kind in (IssueKind.SCOPE_LEAK, IssueKind.LIVE_REQUIRED) for i in report.issues)
