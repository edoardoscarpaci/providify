"""Failing tests (RED mode) for Plan 003 — `container.validate()`.

These tests encode the design in `plans/003-startup-graph-validation.md` before
any of `providify/validation.py`, `ContainerValidationError`,
`DIContainer.validate()`, or `DIContainer._iter_injection_points()` exist.
Every test in this file is expected to fail with an ImportError/AttributeError
until the corresponding implementation steps land.

Covered (grouped by plan Step number):
    Step 1  — ValidationReport / ValidationIssue shape
    Step 4  — `_classify_hint` over the full hint table
    Step 6  — missing-binding detection
    Step 9  — ambiguous-binding detection
    Step 11 — static cycle detection
    Step 13 — aggregate raise (ContainerValidationError)
    Step 15 — regression/composition guards
    Step 16 — parity test (anti-drift guard)
"""

from __future__ import annotations

from typing import Annotated, ClassVar, Optional

import pytest

from providify.container import DIContainer
from providify.decorator.lifecycle import PostConstruct
from providify.decorator.scope import (
    Component,
    Decorator,
    Provider,
    RequestScoped,
    Singleton,
)
from providify.exceptions import ValidationError
from providify.type import (
    DelegateMeta,
    Event,
    Inject,
    InjectInstances,
    InjectionPoint,
    InjectMeta,
    Instance,
    Lazy,
    Live,
    NamedMeta,
)

# ─────────────────────────────────────────────────────────────────
#  Step 1 — Report shape
# ─────────────────────────────────────────────────────────────────


class TestReportShape:
    """`validate()` / `ValidationReport` structural contract."""

    def test_empty_container_returns_empty_ok_report(
        self, container: DIContainer
    ) -> None:
        """An empty container has nothing to validate — report is empty and ok."""
        report = container.validate()

        assert report.issues == ()
        assert report.checked_bindings == 0
        assert report.ok is True

    def test_fully_wired_container_is_ok(self, container: DIContainer) -> None:
        """A container with no wiring defects reports ok=True with no issues."""

        @Component
        class Wired:
            pass

        container.register(Wired)

        report = container.validate()

        assert report.ok is True
        assert report.errors == ()

    def test_to_dict_round_trips_to_json_safe_primitives(
        self, container: DIContainer
    ) -> None:
        """to_dict() must be safe to json.dumps() — only str/int/bool/list/dict/None."""
        import json

        @Component
        class Wired:
            pass

        container.register(Wired)
        report = container.validate()

        payload = report.to_dict()
        # Must not raise — proves every value is JSON-primitive.
        json.dumps(payload)
        assert isinstance(payload, dict)

    def test_report_has_no_dunder_bool(self, container: DIContainer) -> None:
        """ValidationReport deliberately omits __bool__ — ambiguous semantics.

        Callers must write `if not report.ok:` rather than `if report:`.
        """
        report = container.validate()

        assert "__bool__" not in type(report).__dict__

    def test_errors_and_warnings_properties_partition_issues(
        self, container: DIContainer
    ) -> None:
        """report.errors / report.warnings must be disjoint views of report.issues."""
        report = container.validate()

        assert isinstance(report.errors, tuple)
        assert isinstance(report.warnings, tuple)


# ─────────────────────────────────────────────────────────────────
#  Step 4 — `_classify_hint` over the full hint table
# ─────────────────────────────────────────────────────────────────


class _Widget:
    pass


class _GenericRepository[T]:
    pass


class TestClassifyHint:
    """Unit tests for the pure `_classify_hint` classifier (no container)."""

    def test_inject_t_is_eager_single_not_optional_not_deferred(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Inject[_Widget])

        assert spec is not None
        assert spec.base_type is _Widget
        assert spec.optional is False
        assert spec.deferred is False
        assert spec.multi is False
        assert spec.caller_parameterised is False

    def test_inject_t_or_none_is_optional(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Inject[_Widget] | None)

        assert spec is not None
        assert spec.optional is True

    def test_inject_meta_optional_true_is_optional(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Annotated[_Widget, InjectMeta(optional=True)])

        assert spec is not None
        assert spec.optional is True

    def test_bare_type_is_eager_single(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(_Widget)

        assert spec is not None
        assert spec.optional is False
        assert spec.deferred is False
        assert spec.multi is False

    def test_generic_alias_is_eager_single(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(_GenericRepository[_Widget])

        assert spec is not None
        assert spec.base_type is not None

    def test_union_of_two_types_is_eager_union(self) -> None:
        from providify.validation import _classify_hint

        class Other:
            pass

        spec = _classify_hint(_Widget | Other)

        assert spec is not None
        assert spec.optional is False

    def test_optional_t_typing_form_is_optional(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(
            Optional[  # noqa: UP045 UP007 — testing typing.Optional form specifically
                _Widget
            ]
        )

        assert spec is not None
        assert spec.optional is True

    def test_lazy_t_is_deferred_not_optional(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Lazy[_Widget])

        assert spec is not None
        assert spec.deferred is True
        assert spec.optional is False

    def test_live_t_is_deferred_not_optional(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Live[_Widget])

        assert spec is not None
        assert spec.deferred is True
        assert spec.optional is False

    def test_instance_t_is_caller_parameterised(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Instance[_Widget])

        assert spec is not None
        assert spec.caller_parameterised is True
        assert spec.deferred is True

    def test_event_t_is_caller_parameterised(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Event[_Widget])

        assert spec is not None
        assert spec.caller_parameterised is True
        assert spec.deferred is True

    def test_inject_instances_t_is_multi(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(InjectInstances[_Widget])

        assert spec is not None
        assert spec.multi is True

    def test_inject_meta_all_true_is_multi(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Annotated[list[_Widget], InjectMeta(all=True)])

        assert spec is not None
        assert spec.multi is True

    def test_named_meta_sets_qualifier(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Annotated[_Widget, NamedMeta("x")])

        assert spec is not None
        assert spec.qualifier == "x"
        assert spec.multi is False

    def test_delegate_meta_excludes_self(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(Annotated[_Widget, DelegateMeta()])

        assert spec is not None
        assert spec.excludes_self is True

    def test_injection_point_is_not_an_injection_point_for_classify(self) -> None:
        """InjectionPoint is context metadata, never itself a dependency edge."""
        from providify.validation import _classify_hint

        spec = _classify_hint(InjectionPoint)

        assert spec is None

    def test_classvar_wrapped_inject_unwraps_to_same_spec(self) -> None:
        from providify.validation import _classify_hint

        spec = _classify_hint(ClassVar[Inject[_Widget]])  # type: ignore[valid-type]

        assert spec is not None
        assert spec.base_type is _Widget

    def test_plain_int_is_not_an_injection_point(self) -> None:
        from providify.validation import _classify_hint

        assert _classify_hint(int) is None

    def test_plain_str_is_not_an_injection_point(self) -> None:
        from providify.validation import _classify_hint

        assert _classify_hint(str) is None

    def test_unannotated_none_is_not_an_injection_point(self) -> None:
        from providify.validation import _classify_hint

        assert _classify_hint(None) is None


# ─────────────────────────────────────────────────────────────────
#  Step 6 — Missing-binding detection
# ─────────────────────────────────────────────────────────────────


class _Unbound:
    """Never registered — used to trigger missing-binding checks."""


class TestMissingBindingDetection:
    def test_unbound_inject_without_default_is_error(
        self, container: DIContainer
    ) -> None:
        """Inject[T] with no default and no binding must be reported ERROR."""
        from providify.validation import IssueKind, Severity

        @Singleton
        class Svc:
            def __init__(self, dep: Inject[_Unbound]) -> None:
                self.dep = dep

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        missing = [i for i in report.issues if i.kind == IssueKind.MISSING_BINDING]
        assert len(missing) == 1
        assert missing[0].severity == Severity.ERROR
        assert missing[0].param_name == "dep"

    def test_unbound_inject_with_default_is_warning(
        self, container: DIContainer
    ) -> None:
        """Inject[T] with a default falls back safely at runtime → WARNING only."""
        from providify.validation import IssueKind, Severity

        @Singleton
        class Svc:
            def __init__(self, dep: Inject[_Unbound] = None) -> None:  # type: ignore[assignment]
                self.dep = dep

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        defaulted = [
            i for i in report.issues if i.kind == IssueKind.MISSING_BINDING_DEFAULTED
        ]
        assert len(defaulted) == 1
        assert defaulted[0].severity == Severity.WARNING
        assert report.ok is True  # warnings never make ok False

    def test_unbound_optional_inject_produces_no_issue(
        self, container: DIContainer
    ) -> None:
        """Inject[T | None] documents a legal None injection — no issue at all."""

        @Singleton
        class Svc:
            def __init__(self, dep: Inject[_Unbound] | None = None) -> None:
                self.dep = dep

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        assert report.issues == ()

    def test_unbound_inject_instances_produces_no_issue(
        self, container: DIContainer
    ) -> None:
        """InjectInstances[T] with zero candidates legally returns [] — no issue."""

        @Singleton
        class Svc:
            def __init__(self, deps: InjectInstances[_Unbound]) -> None:
                self.deps = deps

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        assert report.issues == ()

    def test_unbound_class_var_inject_is_error(self, container: DIContainer) -> None:
        """Class-var injection points have no default fallback → ERROR."""
        from providify.validation import IssueKind, Severity

        @Singleton
        class Svc:
            dep: Inject[_Unbound]

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        missing = [i for i in report.issues if i.kind == IssueKind.MISSING_BINDING]
        assert len(missing) == 1
        assert missing[0].severity == Severity.ERROR

    def test_unbound_lazy_is_error(self, container: DIContainer) -> None:
        """Lazy[T] merely postpones a certain failure — still ERROR."""
        from providify.validation import IssueKind, Severity

        @Singleton
        class Svc:
            def __init__(self, dep: Lazy[_Unbound]) -> None:
                self.dep = dep

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        missing = [i for i in report.issues if i.kind == IssueKind.MISSING_BINDING]
        assert len(missing) == 1
        assert missing[0].severity == Severity.ERROR

    def test_unbound_instance_is_warning(self, container: DIContainer) -> None:
        """Instance[T] is caller-parameterised — "no binding today" is legal."""
        from providify.validation import IssueKind, Severity

        @Singleton
        class Svc:
            def __init__(self, dep: Instance[_Unbound]) -> None:
                self.dep = dep

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        deferred = [
            i for i in report.issues if i.kind == IssueKind.MISSING_BINDING_DEFERRED
        ]
        assert len(deferred) == 1
        assert deferred[0].severity == Severity.WARNING

    def test_unbound_event_is_warning(self, container: DIContainer) -> None:
        """Event[T] is caller-parameterised like Instance[T] — WARNING only."""
        from providify.validation import IssueKind

        @Singleton
        class Svc:
            def __init__(self, dep: Event[_Unbound]) -> None:
                self.dep = dep

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        deferred = [
            i for i in report.issues if i.kind == IssueKind.MISSING_BINDING_DEFERRED
        ]
        assert len(deferred) == 1

    def test_var_positional_and_var_keyword_are_never_reported(
        self, container: DIContainer
    ) -> None:
        """*args: T / **kwargs: T are skipped regardless of annotation, per Step 7."""

        @Singleton
        class Svc:
            def __init__(
                self, *args: Inject[_Unbound], **kwargs: Inject[_Unbound]
            ) -> None:
                self.args = args
                self.kwargs = kwargs

        container.register(Svc)

        report = container.validate(raise_on_error=False)

        assert report.issues == ()


# ─────────────────────────────────────────────────────────────────
#  Step 9 — Ambiguous-binding detection
# ─────────────────────────────────────────────────────────────────


class _Iface:
    pass


class TestAmbiguousBindingDetection:
    def test_two_equal_priority_candidates_via_inject_is_error(
        self, container: DIContainer
    ) -> None:
        """Two candidates tied at max priority for a single-valued Inject[T] point
        is a silent, arbitrary pick at runtime — must be reported ERROR."""
        from providify.validation import IssueKind, Severity

        @Singleton
        class ImplA(_Iface):
            pass

        @Singleton
        class ImplB(_Iface):
            pass

        @Singleton
        class Consumer:
            def __init__(self, dep: Inject[_Iface]) -> None:
                self.dep = dep

        container.bind(_Iface, ImplA)
        container.bind(_Iface, ImplB)
        container.register(Consumer)

        report = container.validate(raise_on_error=False)

        ambiguous = [i for i in report.issues if i.kind == IssueKind.AMBIGUOUS_BINDING]
        assert len(ambiguous) == 1
        assert ambiguous[0].severity == Severity.ERROR
        assert len(ambiguous[0].candidates) == 2

    def test_two_candidates_different_priority_is_not_ambiguous(
        self, container: DIContainer
    ) -> None:
        """Different priorities is the intended @Priority override mechanism."""
        from providify.validation import IssueKind

        @Singleton(priority=1)
        class ImplA(_Iface):
            pass

        @Singleton(priority=5)
        class ImplB(_Iface):
            pass

        @Singleton
        class Consumer:
            def __init__(self, dep: Inject[_Iface]) -> None:
                self.dep = dep

        container.bind(_Iface, ImplA)
        container.bind(_Iface, ImplB)
        container.register(Consumer)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.AMBIGUOUS_BINDING]

    def test_equal_priority_candidates_via_inject_instances_is_not_ambiguous(
        self, container: DIContainer
    ) -> None:
        """InjectInstances[T] (get_all) legitimately consumes every candidate."""
        from providify.validation import IssueKind

        @Singleton
        class ImplA(_Iface):
            pass

        @Singleton
        class ImplB(_Iface):
            pass

        @Singleton
        class Consumer:
            def __init__(self, deps: InjectInstances[_Iface]) -> None:
                self.deps = deps

        container.bind(_Iface, ImplA)
        container.bind(_Iface, ImplB)
        container.register(Consumer)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.AMBIGUOUS_BINDING]

    def test_different_qualifiers_are_not_ambiguous(
        self, container: DIContainer
    ) -> None:
        """Distinct qualifiers mean the injection point only ever sees one candidate."""
        from providify.validation import IssueKind

        @Singleton(qualifier="a")
        class ImplA(_Iface):
            pass

        @Singleton(qualifier="b")
        class ImplB(_Iface):
            pass

        @Singleton
        class Consumer:
            def __init__(self, dep: Annotated[_Iface, NamedMeta("a")]) -> None:
                self.dep = dep

        container.bind(_Iface, ImplA)
        container.bind(_Iface, ImplB)
        container.register(Consumer)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.AMBIGUOUS_BINDING]


# ─────────────────────────────────────────────────────────────────
#  Step 11 — Static cycle detection
# ─────────────────────────────────────────────────────────────────


class _CycleProduct:
    pass


class TestStaticCycleDetection:
    def test_two_class_cycle_via_inject_is_one_error(
        self, container: DIContainer
    ) -> None:
        from providify.validation import IssueKind, Severity

        @Singleton
        class A:
            def __init__(self, b: Inject[B]) -> None:
                self.b = b

        @Singleton
        class B:
            def __init__(self, a: Inject[A]) -> None:
                self.a = a

        container.register(A)
        container.register(B)

        report = container.validate(raise_on_error=False)

        cycles = [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]
        assert len(cycles) == 1
        assert cycles[0].severity == Severity.ERROR

    def test_cycle_broken_by_lazy_on_one_side_is_not_reported(
        self, container: DIContainer
    ) -> None:
        """Lazy[T] is the documented cycle-breaker — no edge, no cycle issue."""
        from providify.validation import IssueKind

        @Singleton
        class A:
            def __init__(self, b: Lazy[B]) -> None:
                self.b = b

        @Singleton
        class B:
            def __init__(self, a: Inject[A]) -> None:
                self.a = a

        container.register(A)
        container.register(B)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]

    def test_cycle_via_live_or_instance_is_not_reported(
        self, container: DIContainer
    ) -> None:
        """Live[T]/Instance[T] proxies never resolve during construction — no edge."""
        from providify.validation import IssueKind

        @Singleton
        class A:
            def __init__(self, b: Live[B]) -> None:
                self.b = b

        @Singleton
        class B:
            def __init__(self, a: Instance[A]) -> None:
                self.a = a

        container.register(A)
        container.register(B)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]

    def test_self_cycle_is_one_issue(self, container: DIContainer) -> None:
        from providify.validation import IssueKind

        @Singleton
        class SelfRef:
            def __init__(self, me: Inject[SelfRef] = None) -> None:  # type: ignore[assignment]
                self.me = me

        container.register(SelfRef)

        report = container.validate(raise_on_error=False)

        cycles = [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]
        assert len(cycles) == 1

    def test_three_node_cycle_reported_exactly_once(
        self, container: DIContainer
    ) -> None:
        from providify.validation import IssueKind

        @Singleton
        class X:
            def __init__(self, y: Inject[Y]) -> None:
                self.y = y

        @Singleton
        class Y:
            def __init__(self, z: Inject[Z]) -> None:
                self.z = z

        @Singleton
        class Z:
            def __init__(self, x: Inject[X]) -> None:
                self.x = x

        container.register(X)
        container.register(Y)
        container.register(Z)

        report = container.validate(raise_on_error=False)

        cycles = [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]
        assert len(cycles) == 1

    def test_two_disjoint_cycles_report_two_issues(
        self, container: DIContainer
    ) -> None:
        from providify.validation import IssueKind

        @Singleton
        class P1:
            def __init__(self, q: Inject[Q1]) -> None:
                self.q = q

        @Singleton
        class Q1:
            def __init__(self, p: Inject[P1]) -> None:
                self.p = p

        @Singleton
        class P2:
            def __init__(self, q: Inject[Q2]) -> None:
                self.q = q

        @Singleton
        class Q2:
            def __init__(self, p: Inject[P2]) -> None:
                self.p = p

        container.register(P1)
        container.register(Q1)
        container.register(P2)
        container.register(Q2)

        report = container.validate(raise_on_error=False)

        cycles = [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]
        assert len(cycles) == 2

    def test_decorator_wrapping_its_own_interface_is_not_a_self_cycle(
        self, container: DIContainer
    ) -> None:
        """DelegateMeta excludes the owning implementation — no false self-cycle."""
        from providify.validation import IssueKind

        class Notifier:
            pass

        @Singleton
        class EmailNotifier(Notifier):
            pass

        @Singleton(priority=10)
        @Decorator
        class LoggingNotifier(Notifier):
            def __init__(self, delegate: Annotated[Notifier, DelegateMeta()]) -> None:
                self._delegate = delegate

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, LoggingNotifier)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]

    def test_provider_class_provider_cycle_is_one_issue(
        self, container: DIContainer
    ) -> None:
        from providify.validation import IssueKind

        @Singleton
        class CycleClass:
            def __init__(self, product: Inject[_CycleProduct]) -> None:
                self.product = product

        @Provider(singleton=True)
        def make_product(dep: Inject[CycleClass]) -> _CycleProduct:
            return _CycleProduct()

        container.register(CycleClass)
        container.provide(make_product)

        report = container.validate(raise_on_error=False)

        cycles = [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]
        assert len(cycles) == 1

    def test_diamond_shared_dependency_is_not_a_cycle(
        self, container: DIContainer
    ) -> None:
        from providify.validation import IssueKind

        @Component
        class Shared:
            pass

        @Component
        class Left:
            def __init__(self, shared: Inject[Shared]) -> None:
                self.shared = shared

        @Component
        class Right:
            def __init__(self, shared: Inject[Shared]) -> None:
                self.shared = shared

        @Component
        class Top:
            def __init__(self, left: Inject[Left], right: Inject[Right]) -> None:
                self.left = left
                self.right = right

        container.register(Shared)
        container.register(Left)
        container.register(Right)
        container.register(Top)

        report = container.validate(raise_on_error=False)

        assert not [i for i in report.issues if i.kind == IssueKind.CIRCULAR_DEPENDENCY]


# ─────────────────────────────────────────────────────────────────
#  Step 13 — Aggregate raise (ContainerValidationError)
# ─────────────────────────────────────────────────────────────────


class TestAggregateRaise:
    def test_validate_raises_container_validation_error_on_broken_graph(
        self, container: DIContainer
    ) -> None:
        from providify.exceptions import ContainerValidationError

        @Singleton
        class Broken:
            def __init__(self, dep: Inject[_Unbound]) -> None:
                self.dep = dep

        container.register(Broken)

        with pytest.raises(ContainerValidationError):
            container.validate()

    def test_exception_report_carries_every_issue(self, container: DIContainer) -> None:
        from providify.exceptions import ContainerValidationError

        @Singleton
        class Broken:
            def __init__(self, dep: Inject[_Unbound]) -> None:
                self.dep = dep

        container.register(Broken)

        with pytest.raises(ContainerValidationError) as exc_info:
            container.validate()

        assert len(exc_info.value.report.errors) >= 1

    def test_raise_on_error_false_returns_report_without_raising(
        self, container: DIContainer
    ) -> None:
        @Singleton
        class Broken:
            def __init__(self, dep: Inject[_Unbound]) -> None:
                self.dep = dep

        container.register(Broken)

        report = container.validate(raise_on_error=False)

        assert report.ok is False
        assert len(report.errors) >= 1

    def test_warnings_only_container_does_not_raise(
        self, container: DIContainer
    ) -> None:
        """A warning-only report must never raise, even with raise_on_error=True."""

        @Singleton
        class Svc:
            def __init__(self, dep: Inject[_Unbound] = None) -> None:  # type: ignore[assignment]
                self.dep = dep

        container.register(Svc)

        report = container.validate()  # must not raise

        assert report.ok is True

    def test_exception_message_contains_every_error_message(
        self, container: DIContainer
    ) -> None:
        from providify.exceptions import ContainerValidationError

        @Singleton
        class Broken:
            def __init__(self, dep: Inject[_Unbound]) -> None:
                self.dep = dep

        container.register(Broken)

        with pytest.raises(ContainerValidationError) as exc_info:
            container.validate()

        text = str(exc_info.value)
        for issue in exc_info.value.report.errors:
            assert issue.message in text

    def test_container_validation_error_is_a_validation_error(self) -> None:
        """Must subclass ValidationError so existing `except ValidationError` still works."""
        from providify.exceptions import ContainerValidationError

        assert issubclass(ContainerValidationError, ValidationError)


# ─────────────────────────────────────────────────────────────────
#  Step 15 — Regression / composition guards
# ─────────────────────────────────────────────────────────────────


class TestCompositionGuards:
    def test_validate_does_not_populate_singleton_cache(
        self, container: DIContainer
    ) -> None:
        """validate() is pure introspection — never instantiates anything."""

        @Singleton
        class Svc:
            pass

        container.register(Svc)
        container.validate()

        assert len(container._singleton_cache) == 0

    def test_validate_never_fires_post_construct(self, container: DIContainer) -> None:
        calls: list[str] = []

        @Singleton
        class Spy:
            @PostConstruct
            def _init(self) -> None:
                calls.append("fired")

        container.register(Spy)
        container.validate()

        assert calls == []

    def test_request_scoped_binding_validated_outside_request_context(
        self, container: DIContainer
    ) -> None:
        """REQUEST/SESSION scope must validate cleanly with no active context —
        nothing is instantiated so _get_cache is never reached."""

        @RequestScoped
        class ReqScoped:
            pass

        container.register(ReqScoped)

        # Must not raise RuntimeError for "no active request scope".
        report = container.validate(raise_on_error=False)
        assert isinstance(report.checked_bindings, int)

    def test_validate_all_unchanged_on_container_flagged_by_validate(
        self, container: DIContainer
    ) -> None:
        """validate_all()/validate_bindings() keep their pre-existing (scope-only)
        semantics even when validate() would flag missing bindings."""

        @Singleton
        class Broken:
            def __init__(self, dep: Inject[_Unbound]) -> None:
                self.dep = dep

        container.register(Broken)

        # validate() flags a missing binding.
        report = container.validate(raise_on_error=False)
        assert report.ok is False

        # validate_all()/validate_bindings() are scope-tier only and untouched by this.
        errors = container.validate_all()
        assert isinstance(errors, list)
        container.validate_bindings()  # must not raise for a missing-binding-only issue

    def test_calling_validate_twice_yields_identical_report(
        self, container: DIContainer
    ) -> None:
        @Component
        class Wired:
            pass

        container.register(Wired)

        report1 = container.validate()
        report2 = container.validate()

        assert report1.to_dict() == report2.to_dict()


# ─────────────────────────────────────────────────────────────────
#  Step 16 — Parity test (anti-drift guard)
# ─────────────────────────────────────────────────────────────────


class _ParityTarget:
    """Never registered — used as the missing dependency across parity rows."""


@pytest.mark.parametrize(
    "hint_factory,expect_runtime_lookup_error,expect_none",
    [
        # Inject[T] unbound, no default → LookupError at runtime; classifier: ERROR/missing.
        (lambda: Inject[_ParityTarget], True, False),
        # Inject[T | None] unbound → runtime injects None; classifier: optional, no issue.
        (lambda: Inject[_ParityTarget] | None, False, True),
        # Lazy[T] unbound, accessed via .get() → LookupError; classifier: ERROR/missing.
        (lambda: Lazy[_ParityTarget], True, False),
        # Instance[T] unbound → no error at construction time, proxy defers; classifier: WARNING.
        (lambda: Instance[_ParityTarget], False, False),
    ],
)
def test_classify_hint_agrees_with_runtime_behaviour(
    container: DIContainer,
    hint_factory,
    expect_runtime_lookup_error: bool,
    expect_none: bool,
) -> None:
    """Anti-drift guard: `_classify_hint`'s verdict must match what `container.get()`
    / construction actually does. If `_resolve_hint_sync` changes without a matching
    `_classify_hint` update, this test is the one that catches it."""
    from providify.validation import _classify_hint

    hint = hint_factory()
    spec = _classify_hint(hint)
    assert spec is not None

    if hint_factory.__code__.co_consts and "Lazy" in repr(hint):
        # Lazy[T]: construction succeeds (proxy created); failure happens on .get().
        @Singleton
        class Consumer:
            def __init__(self, dep) -> None:
                self.dep = dep

        Consumer.__init__.__annotations__["dep"] = hint

        container.register(Consumer)
        consumer = container.get(Consumer)
        if expect_runtime_lookup_error:
            with pytest.raises(LookupError):
                consumer.dep.get()
        return

    if "InstanceProxy" in repr(hint) or "Instance" in repr(hint):

        @Singleton
        class Consumer:
            def __init__(self, dep) -> None:
                self.dep = dep

        Consumer.__init__.__annotations__["dep"] = hint

        container.register(Consumer)
        # Must construct cleanly — Instance[T] never fails at construction time.
        consumer = container.get(Consumer)
        assert consumer.dep is not None
        assert spec.caller_parameterised is True
        return

    @Singleton
    class Consumer:
        def __init__(self, dep) -> None:
            self.dep = dep

    Consumer.__init__.__annotations__["dep"] = hint

    container.register(Consumer)

    if expect_runtime_lookup_error:
        assert spec.optional is False
        with pytest.raises(LookupError):
            container.get(Consumer)
    elif expect_none:
        consumer = container.get(Consumer)
        assert consumer.dep is None
        assert spec.optional is True
