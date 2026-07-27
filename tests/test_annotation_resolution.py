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
"""

from __future__ import annotations

import logging

import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton
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
    """Make ``get_type_hints`` raise *exc* for *target* only, real behaviour otherwise.

    Selectivity is required: a blanket boom fires in ``_collect_kwargs_sync``
    (which receives ``cls.__init__``) before the class-var path (which
    receives the class object itself) is ever reached. Targeting the exact
    object under test isolates the tier under test from every other
    ``get_type_hints`` call the container makes along the way.
    """
    import providify.container as container_module

    real = container_module.get_type_hints

    def fake(t: object, *args: object, **kwargs: object) -> dict[str, object]:
        if t is target:
            raise exc
        return real(t, *args, **kwargs)

    monkeypatch.setattr(container_module, "get_type_hints", fake)


def _pre_validate(container: DIContainer) -> None:
    """Run ``validate_bindings()`` to completion (unboomed) and mark it done.

    WHY this exists — do not delete as "redundant": ``get()``/``aget()`` call
    ``validate_bindings()`` themselves on their first invocation
    (``container.py:799-801``), which walks EVERY registered binding, not
    just the one under test. If a test installs a ``_boom_for`` patch and
    THEN calls ``container.get(Target)`` on a fresh (never-validated)
    container, that implicit validation runs Tier 2 (the now-strict
    ``_check_scope_violation`` / ``_collect_class_var_hints``) against the
    SAME boomed target *before* the Tier-1/Tier-3 code the test actually
    wants to exercise ever runs — so the boom is intercepted by the wrong
    tier and the test observes Tier 2's behaviour instead of Tier 1's.
    Running validation to completion here, before the boom is installed,
    isolates the tier under test. ``validate_bindings()`` itself does not
    set ``_validated`` (only ``get()``/``aget()``/``get_all()``/``aget_all()``
    do, after calling it) so it must be set explicitly afterwards.
    """
    container.validate_bindings()
    container._validated = True


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

        Today: `_inject_class_vars_sync` catches bare ``Exception`` and sets
        ``hints = {}`` — the AttributeError below is currently swallowed and
        the instance is returned with ``var`` unset. Target: it propagates.
        """
        container.register(Consumer)
        container.bind(Dep, Dep)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            Consumer,
            AttributeError("'str' object has no attribute '__name__'"),
        )

        with pytest.raises(AttributeError, match="__name__"):
            container.get(Consumer)

    async def test_unexpected_hint_error_propagates_async(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Async mirror of the propagation guarantee."""
        container.register(Consumer)
        container.bind(Dep, Dep)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            Consumer,
            AttributeError("'str' object has no attribute '__name__'"),
        )

        with pytest.raises(AttributeError, match="__name__"):
            await container.aget(Consumer)

    def test_unresolvable_name_is_tolerated_and_logged_sync(
        self,
        container: DIContainer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The legitimate case (unresolvable local annotation) stays non-fatal.

        Target: construction succeeds, a WARNING names the class, and the
        class var is left unset (documents the tolerated gap that Phase 7
        closes by resolving per-attribute instead of all-or-nothing —
        rewritten in Step 37).
        """
        container.register(Consumer)
        container.bind(Dep, Dep)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            Consumer,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            instance = container.get(Consumer)

        assert isinstance(instance, Consumer)
        assert hasattr(instance, "dep") is False
        assert any("Consumer" in record.message for record in caplog.records)

    async def test_unresolvable_name_is_tolerated_and_logged_async(
        self,
        container: DIContainer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Async mirror of the tolerated-and-logged case."""
        container.register(Consumer)
        container.bind(Dep, Dep)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            Consumer,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            instance = await container.aget(Consumer)

        assert isinstance(instance, Consumer)
        assert hasattr(instance, "dep") is False
        assert any("Consumer" in record.message for record in caplog.records)

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
        """
        from providify import AnnotationResolutionError

        container.bind(Dep, Dep)
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
        """A singleton ``@Provider`` whose params can't be read must not validate clean."""
        from providify import AnnotationResolutionError

        container.bind(Dep, Dep)

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
        """`validate_all()` must surface the failure as a violation string, never swallow it."""
        container.bind(Dep, Dep)
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
        """`_collect_dependencies` must log a WARNING when it swallows a hint failure."""
        container.bind(Dep, Dep)

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
