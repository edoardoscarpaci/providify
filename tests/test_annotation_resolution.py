"""Failing (red) regression tests for annotation-resolution failure policy — release A.

`get_type_hints()` can fail for reasons that fall into exactly three tiers, and
each tier has a DIFFERENT correct response, because "the annotations didn't
resolve" means something different depending on what the resolution result was
going to be used FOR:

    TIER 1 — injection (what gets constructed):
        `_inject_class_vars_sync` / `_inject_class_vars_async`.
        An unresolvable NAME (``NameError``) is tolerated — logged and treated
        as "nothing to inject" — because `get_type_hints` is all-or-nothing
        over the whole class and one unrelated, non-injected annotation must
        not break construction of an otherwise-fine class. Anything OTHER than
        ``NameError`` is a genuine defect (e.g. a malformed interface produced
        the ``AttributeError`` from the historical forward-ref bug) and must
        propagate, never be swallowed into a silent "no class vars set".

    TIER 2 — validation (is the dependency graph legal?):
        `_collect_class_var_hints`, `_check_scope_violation`,
        `_check_provider_scope_violation`. A validator's entire job is to
        prove a binding is safe. If it cannot even READ the annotations, it
        has no evidence either way — returning "no leaks found" in that case
        is not "lenient", it is a false "all clear". So ANY exception here
        (including ``NameError``) raises `AnnotationResolutionError`, a
        `ValidationError` subclass, naming the exact binding.

    TIER 3 — enrichment (dependency-graph reporting, `describe()`):
        `_collect_dependencies`, `_get_provider_return_type`, and the
        `_get_dependencies` call site that reads class-var hints for the
        graph. These are advisory / informational — a partial or missing
        graph node is a worse debugging experience, not a wiring bug — so
        failures are swallowed but MUST be logged, never silent.

These tests pin the CURRENT (pre-fix) behaviour where the plan calls for one,
and describe the TARGET behaviour (currently absent, hence red) everywhere
else. Each test's docstring says which side of that line it is on.

Plan 001 Phase 7 (per-parameter resolution) update: the Tier 1 / Tier 2
distinction this module documents above no longer exists as a MECHANISM —
`_inject_class_vars_sync/async` and the validators
(`_collect_class_var_hints` / `_check_scope_violation` /
`_check_provider_scope_violation`) all now delegate to the SAME resolver
(`DIContainer._resolve_params` / `_resolve_class_annotations`), which always
raises `AnnotationResolutionError` for a resolution failure that IS (or
plausibly is) an injection point, and silently skips one that ISN'T —
regardless of which caller asked. The POLICY difference documented above
(Tier 1 tolerates `NameError`, Tier 2 never does) is superseded: Phase 7
tolerates far less, more precisely (per-parameter, not per-signature), and
raises the SAME exception type everywhere. Consequently:
  - `_boom_for` no longer intercepts `providify.container.get_type_hints`
    (nothing in `container.py` calls it any more — every evaluation happens
    inside `providify._annotations._eval_annotation`, one annotation at a
    time, via a disposable holder class that never receives the ORIGINAL
    target object). It now matches on the target's own RAW (unevaluated)
    annotation strings instead — see its docstring.
  - The two `test_unexpected_hint_error_propagates_*` tests now expect
    `AnnotationResolutionError` (chained via `__cause__`) rather than a raw,
    unwrapped `AttributeError` — uniform wrapping is the point of merging
    Tier 1 and Tier 2 into one resolver.
  - `test_unresolvable_name_is_tolerated_and_logged_*` are replaced (Step 37)
    with a raise variant (marked class var) and a silent-skip variant
    (unmarked class var) — the "tolerated and logged" middle ground Phase 7
    removes entirely.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton
from providify.exceptions import AnnotationResolutionError
from providify.type import Inject

# ─────────────────────────────────────────────────────────────────
#  Module-level domain types
#
#  These live at module level so they ARE present in a class/function's
#  __globals__ — required for the "positive control, no monkeypatch"
#  tests, and for `_boom_for` to have a real, resolvable target to compare
#  the faked behaviour against.
# ─────────────────────────────────────────────────────────────────


@Component
class Dep:
    """A simple, always-resolvable dependency type."""


@Component
class Consumer:
    """Class-var injection target — Tier 1 subject."""

    var: Inject[Dep]


@Component
class Impl:
    """Trivial constructor-based class — Tier 2 ``__init__`` subject."""

    def __init__(self, dep: Dep) -> None:
        self.dep = dep


def _boom_for(monkeypatch: pytest.MonkeyPatch, target: object, exc: Exception) -> None:
    """Make annotation evaluation raise *exc* for *target*'s own annotations only.

    Plan 001 Phase 7 (per-parameter resolution) replaced the whole-signature
    ``get_type_hints(target, ...)`` call this helper used to intercept with a
    per-annotation evaluator (``providify._annotations._eval_annotation``)
    that never receives *target* itself — each annotation is evaluated in
    isolation against a disposable holder class (plan §7.1). Selectivity
    instead matches on *target*'s own RAW (unevaluated) annotation strings:
    only an ``_eval_annotation`` call whose ``raw`` argument is one of
    *target*'s own annotations is boomed; every other evaluation — including
    another binding's, which may be resolved during the SAME ``get()`` /
    ``validate_bindings()`` call — proceeds through the real function
    unaffected.

    Patches THREE name bindings, not one:
      - ``providify._annotations._eval_annotation`` — looked up by
        ``resolve_params``/``resolve_class_annotations``/``_resolve_one``,
        which live in (and call the bare name from) that module.
      - ``providify.container._eval_annotation`` — that module does ``from
        ._annotations import _eval_annotation`` and therefore holds its OWN
        copy of the reference (e.g. inside ``_get_provider_return_type``) —
        rebinding the origin module's attribute alone would not affect an
        already-imported copy elsewhere.
      - ``providify._annotations.eval`` (the builtin, shadowed at module
        scope) — the bootstrap classifier (``_sniff_head``) does its OWN,
        independent ``eval(name, ...)`` call to resolve a marker HEAD name
        when ``_eval_annotation`` has already failed with a ``NameError``.
        If the boomed name is otherwise genuinely resolvable (e.g. a
        module-level class, as `Dep` is here), that SECOND call would
        succeed unboomed and reclassify the annotation as "not an injection
        point" — silently skipping it instead of raising. Shadowing ``eval``
        too makes the simulated failure consistent everywhere the name is
        (re-)evaluated, matching what an ACTUALLY unresolvable name does in
        production (where both calls are the same failure, not two
        independent ones).
    """
    import builtins

    import providify._annotations as annotations_module
    import providify.container as container_module

    raw_annotations = set(inspect.get_annotations(target, eval_str=False).values())
    real_eval_annotation = annotations_module._eval_annotation
    real_eval = builtins.eval

    def fake_eval_annotation(raw: object, globalns: object, localns: object) -> object:
        if raw in raw_annotations:
            raise exc
        return real_eval_annotation(raw, globalns, localns)

    def fake_eval(source: object, *args: object, **kwargs: object) -> object:
        if source in raw_annotations:
            raise exc
        return real_eval(source, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(annotations_module, "_eval_annotation", fake_eval_annotation)
    monkeypatch.setattr(container_module, "_eval_annotation", fake_eval_annotation)
    monkeypatch.setattr(annotations_module, "eval", fake_eval, raising=False)


# NOTE: release A's `_pre_validate` helper (which ran `validate_bindings()`
# to completion, unboomed, before installing a boom — isolating "Tier 1" from
# "Tier 2") is deleted. Phase 7 merges both into ONE resolver sharing ONE
# cache (`_hints_cache`) per target, so there is no longer a tier to isolate
# from — pre-validating first would simply cache the (unboomed) result
# before the boom is installed, making the boom a no-op either way.


# ─────────────────────────────────────────────────────────────────
#  Tier 1 — class-var injection: NameError tolerated, everything else propagates
# ─────────────────────────────────────────────────────────────────


class TestClassVarInjectionHintFailure:
    """`_inject_class_vars_sync` / `_async` — the injection tier.

    Target behaviour (Phase 3): only ``NameError`` is tolerated (logged, no
    class-var injection); anything else propagates.
    """

    def test_unexpected_hint_error_propagates_sync(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-NameError failure must not be swallowed into a silent no-op.

        Phase 7: `_resolve_class_annotations` wraps ANY non-``NameError``
        failure in `AnnotationResolutionError` (never a bare, unwrapped
        exception) — the original cause is still inspectable via
        ``__cause__``, so it is provably "not swallowed" without asserting
        on the exact exception TYPE release A propagated raw.

        No `_pre_validate` here (unlike release A): Phase 7 shares ONE cache
        (`_hints_cache`) between the injection and validation call sites for
        the SAME class — pre-validating first would cache Consumer's
        (unboomed) class-var hints before the boom is installed below, and
        the boom would then never fire.
        """
        container.register(Consumer)
        container.bind(Dep, Dep)

        boom = AttributeError("'str' object has no attribute '__name__'")
        _boom_for(monkeypatch, Consumer, boom)

        with pytest.raises(AnnotationResolutionError) as exc_info:
            container.get(Consumer)
        assert exc_info.value.__cause__ is boom

    async def test_unexpected_hint_error_propagates_async(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Async mirror of the propagation guarantee."""
        container.register(Consumer)
        container.bind(Dep, Dep)

        boom = AttributeError("'str' object has no attribute '__name__'")
        _boom_for(monkeypatch, Consumer, boom)

        with pytest.raises(AnnotationResolutionError) as exc_info:
            await container.aget(Consumer)
        assert exc_info.value.__cause__ is boom

    def test_unresolvable_marked_class_var_raises_sync(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 7 replacement (Step 37) for the old "tolerated and logged" test.

        `var: Inject[Dep]` carries a providify marker — an unresolvable name
        on it is unambiguously an injection point, so it now RAISES rather
        than being tolerated-and-logged. The tolerated middle ground release
        A occupied (silently unset class var, warning only) is gone.
        """
        container.register(Consumer)
        container.bind(Dep, Dep)

        _boom_for(
            monkeypatch,
            Consumer,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with pytest.raises(AnnotationResolutionError) as exc_info:
            container.get(Consumer)
        assert "var" in str(exc_info.value)

    async def test_unresolvable_marked_class_var_raises_async(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Async mirror of the marked-class-var-raises case."""
        container.register(Consumer)
        container.bind(Dep, Dep)

        _boom_for(
            monkeypatch,
            Consumer,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with pytest.raises(AnnotationResolutionError) as exc_info:
            await container.aget(Consumer)
        assert "var" in str(exc_info.value)

    def test_unresolvable_unmarked_class_var_skips_silently_sync(
        self, container: DIContainer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other half of Step 37: an UNMARKED, unresolvable class var is a non-event.

        No monkeypatch needed — `junk` genuinely cannot resolve (function-
        local type, absent from every namespace). Nothing to configure, so
        nothing warns and nothing raises.
        """

        class LocallyDefined:
            """Function-local — genuinely unresolvable, and NOT a providify marker."""

        @Component
        class PlainConsumer:
            junk: LocallyDefined

        container.register(PlainConsumer)

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            instance = container.get(PlainConsumer)

        assert isinstance(instance, PlainConsumer)
        assert hasattr(instance, "junk") is False
        assert caplog.records == []

    async def test_unresolvable_unmarked_class_var_skips_silently_async(
        self, container: DIContainer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Async mirror of the unmarked-class-var-skips-silently case."""

        class LocallyDefined:
            """Function-local — genuinely unresolvable, and NOT a providify marker."""

        @Component
        class PlainConsumer:
            junk: LocallyDefined

        container.register(PlainConsumer)

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            instance = await container.aget(PlainConsumer)

        assert isinstance(instance, PlainConsumer)
        assert hasattr(instance, "junk") is False
        assert caplog.records == []

    def test_class_var_injection_still_works_sync(self, container: DIContainer) -> None:
        """Positive control, no monkeypatch — the ordinary case must keep working."""
        container.register(Consumer)
        container.bind(Dep, Dep)

        instance = container.get(Consumer)

        assert isinstance(instance.var, Dep)

    async def test_class_var_injection_still_works_async(
        self, container: DIContainer
    ) -> None:
        """Async positive control — no monkeypatch."""
        container.register(Consumer)
        container.bind(Dep, Dep)

        instance = await container.aget(Consumer)

        assert isinstance(instance.var, Dep)


# ─────────────────────────────────────────────────────────────────
#  Tier 2 — validators must never silently report a clean container
# ─────────────────────────────────────────────────────────────────


class TestValidatorNeverSilentlyPasses:
    """`_check_scope_violation` / `_check_provider_scope_violation` /
    `_collect_class_var_hints` — the validation tier.

    Target behaviour (Phase 4): ANY ``get_type_hints`` failure raises
    ``AnnotationResolutionError`` naming the owner — never "no leaks found".
    """

    def test_class_init_hint_failure_fails_validation(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable ``__init__`` must fail validation, not pass it clean.

        Today: `_check_scope_violation` catches bare ``Exception`` and
        returns ``[]`` — the binding validates as if it had no dependencies
        at all. Target: raises `AnnotationResolutionError` naming ``Impl``.

        Deliberately NOT bound: the bootstrap classifier's ambiguous-name
        branch (plan §7.2 step 4c) re-attempts resolving the bare name
        itself while sniffing — if ``Dep`` were bound, that second,
        UNBOOMED attempt would succeed (it only patches
        ``_eval_annotation``, not a second independent ``eval`` used for
        classification) and the annotation would be classified "not an
        injection point" and silently skipped instead of raising. Leaving
        it unbound makes the simulated failure genuine everywhere it is
        (re-)attempted, matching what an ACTUALLY unresolvable name does.
        """
        from providify import AnnotationResolutionError

        container.register(Impl)

        _boom_for(
            monkeypatch,
            Impl.__init__,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with pytest.raises(AnnotationResolutionError, match="Impl"):
            container.validate_bindings()

    def test_class_var_hint_failure_fails_validation(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A class whose class-var annotations can't be read must not validate clean.

        Boom targets the class object itself — hits `_collect_class_var_hints`
        (called from `_check_scope_violation`), not the ``__init__`` path.
        """
        from providify import AnnotationResolutionError

        container.bind(Dep, Dep)
        container.register(Consumer)

        _boom_for(
            monkeypatch,
            Consumer,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with pytest.raises(AnnotationResolutionError):
            container.validate_bindings()

    def test_provider_hint_failure_fails_validation(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A singleton ``@Provider`` whose params can't be read must not validate clean.

        ``Dep`` deliberately NOT bound — see
        ``test_class_init_hint_failure_fails_validation`` for why.
        """
        from providify import AnnotationResolutionError

        @Provider(singleton=True)
        def make_impl(dep: Dep) -> Impl:
            return Impl(dep)

        container.provide(make_impl)

        _boom_for(
            monkeypatch,
            make_impl,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with pytest.raises(AnnotationResolutionError, match="make_impl"):
            container.validate_bindings()

    def test_dependent_provider_short_circuits_before_hint_resolution(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A DEPENDENT-scoped provider validates clean regardless of boom.

        `_check_provider_scope_violation` returns before hint resolution for
        DEPENDENT-scoped bindings (`container.py:3177`) — locking in the
        deliberate exemption: a fresh-every-call provider can never leak
        scope, so there is nothing to validate.
        """
        container.bind(Dep, Dep)

        @Provider(singleton=False)
        def make_impl(dep: Dep) -> Impl:
            return Impl(dep)

        container.provide(make_impl)

        _boom_for(
            monkeypatch,
            make_impl,
            NameError("name 'LocallyDefined' is not defined"),
        )

        container.validate_bindings()  # must not raise

    def test_validate_all_reports_annotation_failure_as_violation(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`validate_all()` must surface the failure as a violation string, never swallow it.

        ``Dep`` deliberately NOT bound — see
        ``test_class_init_hint_failure_fails_validation`` for why.
        """
        container.register(Impl)

        _boom_for(
            monkeypatch,
            Impl.__init__,
            NameError("name 'LocallyDefined' is not defined"),
        )

        violations = container.validate_all()

        assert any("Impl" in v for v in violations)
        assert container.is_valid is False

    def test_regression_locally_defined_init_annotation_is_now_validated(
        self, container: DIContainer
    ) -> None:
        """The headline case — no monkeypatch: a real, previously-hidden scope leak.

        Both classes are defined INSIDE the test function so their ``__init__``
        annotations are unresolvable without ``localns`` — which
        `_check_scope_violation` today omits (``container.py:3072``), so
        `get_type_hints` raises, is swallowed, and the binding validates
        clean even though it is a genuine ``SINGLETON`` → ``REQUEST`` leak.

        Target: passing ``localns=self._build_localns()`` resolves the names
        (both classes are bound, so `_build_localns` registers them) and the
        real leak is caught, raising `LiveInjectionRequiredError`.
        """
        from providify.decorator.scope import RequestScoped

        @RequestScoped
        class LocalReqScoped:
            pass

        @Singleton
        class LocalBadSingleton:
            def __init__(self, dep: Inject[LocalReqScoped]) -> None:
                self.dep = dep

        container.register(LocalReqScoped)
        container.register(LocalBadSingleton)

        from providify.exceptions import LiveInjectionRequiredError

        # Wrapped in a request context so that, under today's bug, the
        # (wrongly) clean-validated binding can actually be constructed —
        # isolating this test's failure to "no LiveInjectionRequiredError was
        # raised" rather than an unrelated RuntimeError about a missing scope.
        with container.request(), pytest.raises(LiveInjectionRequiredError):
            container.get(LocalBadSingleton)


# ─────────────────────────────────────────────────────────────────
#  Tier 3 — optional enrichment warns instead of swallowing in silence
# ─────────────────────────────────────────────────────────────────


class TestOptionalEnrichmentWarns:
    """`_collect_dependencies` / `_get_provider_return_type` / the
    `_get_dependencies` class-var call site — the reporting tier.

    Target behaviour (Phase 5): the broad catch stays (these are advisory,
    graph/`describe()` paths), but every swallow now logs a WARNING.
    """

    def test_graph_survives_class_var_hint_failure(
        self,
        container: DIContainer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`_get_dependencies` must not crash when class-var hints can't be read.

        Today `_collect_class_var_hints` already swallows the failure and
        returns ``{}`` (so the graph already "survives"), but nothing is
        logged — this is the part that is currently missing.
        """
        container.bind(Dep, Dep)
        container.register(Consumer)

        _boom_for(
            monkeypatch,
            Consumer,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            container.describe()

        assert any("Consumer" in record.message for record in caplog.records)

    def test_collect_dependencies_failure_warns(
        self,
        container: DIContainer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`_collect_dependencies` must log a WARNING when it swallows a hint failure.

        ``Dep`` deliberately NOT bound — see
        ``test_class_init_hint_failure_fails_validation`` for why.
        """

        def make_impl(dep: Dep) -> Impl:
            return Impl(dep)

        _boom_for(
            monkeypatch,
            make_impl,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            deps = container._collect_dependencies(make_impl)

        assert deps == []
        assert any("make_impl" in record.message for record in caplog.records)

    def test_provider_return_type_failure_warns(
        self,
        container: DIContainer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """`_get_provider_return_type` must log a WARNING when it swallows a hint failure."""

        def make_impl() -> Impl:
            return Impl(Dep())

        _boom_for(
            monkeypatch,
            make_impl,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            result = container._get_provider_return_type(make_impl)

        assert result is None
        assert any("make_impl" in record.message for record in caplog.records)
