"""Red tests for Plan 001, Phase 7 — per-parameter annotation resolution (release B).

`get_type_hints()` is all-or-nothing over an entire signature. Phase 7 replaces
whole-signature resolution with per-parameter / per-class-attribute resolution
so that one unresolvable annotation (a `TYPE_CHECKING`-only import, a
function-local type on a defaulted, non-injected parameter) can no longer
destroy the hints of every OTHER parameter on the same callable — and so a
failure that DOES matter (an unresolvable annotation on something that IS an
injection point) is attributed to the exact parameter that produced it.

These tests target the new pure-function module `providify._annotations`
(`_eval_annotation`, `_raw_annotations`, `_annotation_namespaces`,
`_sniff_injection_marker`, `resolve_params`, `resolve_class_annotations`) and
the two new thin `DIContainer` methods that cache their results
(`_resolve_params`, `_resolve_class_annotations`). None of this exists yet —
every test here is expected to fail on import (`ModuleNotFoundError`) until
Phase 7 lands, and then on `AttributeError` for the container methods once the
module exists but the container hasn't been wired up yet.

Nothing here touches Phase 1-6 (already shipped, `_resolve_hints_or_warn` /
`_resolve_hints_or_raise`); see `tests/test_annotation_resolution.py` and
`tests/test_forward_ref_provider.py` for that release's coverage.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, ClassVar, Optional, get_type_hints  # noqa: F401

import pytest

from providify._annotations import (
    _annotation_namespaces,
    _eval_annotation,
    resolve_class_annotations,
    resolve_params,
)
from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton
from providify.exceptions import AnnotationResolutionError
from providify.type import (
    Inject,
    InjectInstances,  # noqa: F401
    InjectMeta,  # noqa: F401
    Live,  # noqa: F401
    LiveMeta,
)
from tests._annotations_no_future import AnnotatedBase, Thing, make_thing

# NOTE: Annotated / ClassVar / Optional / InjectInstances / InjectMeta / Live are
# imported but never referenced as bare Python names in this module. They appear
# ONLY inside annotation-STRING literals that `_eval_annotation` / `get_type_hints`
# evaluate against this module's `globals()` — so removing them as "unused" breaks
# those tests with `NameError` at evaluation time, not at import time.
#
# The suppressions must be PER-NAME and inline. A bare noqa comment sitting on its
# own line suppresses nothing, and `ruff check --fix` will then delete these imports.

# ─────────────────────────────────────────────────────────────────
#  Module-level domain types
#
#  These live at module level (present in this module's __globals__) so the
#  "resolvable" half of every test genuinely resolves without help from
#  localns, isolating the behaviour under test from container plumbing.
# ─────────────────────────────────────────────────────────────────


class Foo:
    """Trivially resolvable dependency type used across the primitive tests."""


class User:
    """Type argument for the PEP-695 generic-alias parity case."""


class Repository[T]:
    """PEP-695 generic — exercises `__type_params__` seeding (plan §7.1)."""

    def get(self, item: T) -> T:  # pragma: no cover - signature only
        return item


@Singleton
class TracerX:
    """Always-resolvable module-level dependency, injected via `Inject[T]`.

    SINGLETON (not DEPENDENT/@Component): several tests below inject this
    via `Inject[TracerX]` into `@Provider(singleton=True)` factories —
    a SINGLETON capturing a DEPENDENT-scoped dep through `Inject[T]` (rather
    than `Live[T]`/`Instance[T]`) is a genuine scope leak the container's
    (pre-existing, Phase 1-6) validator correctly flags. Matching TracerX's
    scope to its consumers' avoids tripping that unrelated validation while
    testing something else entirely (per-parameter annotation isolation).
    """


class WidgetX:
    """Plain object capturing whatever a provider injects, for assertions."""

    def __init__(self, dep: TracerX | None) -> None:
        self.dep = dep


class Combo:
    """Plain object capturing a provider's before/after params, for assertions.

    Module-level (not function-local): `ProviderBinding.__init__` resolves a
    provider's RETURN annotation eagerly, at `container.provide()` time,
    using only `fn.__globals__` (no container `localns` exists yet — there is
    no container reference at that point) — a function-local return type
    would make registration itself fail with a `TypeError`, which is not the
    scenario `test_unresolvable_param_in_middle_of_signature` is testing.
    `Combo.__init__`'s own `LocalOnly` parameter annotation is never resolved
    in that test (the container only inspects the PROVIDER function's
    signature, never `Combo`'s own constructor, since `Combo` is registered
    via `provide()` rather than `bind()`/`register()`), so referencing a
    parameter type that only exists inside the test function is harmless —
    it is a lazy (PEP-563) string that nothing ever evaluates.
    """

    def __init__(self, before: TracerX, junk: object, after: TracerX) -> None:
        self.before = before
        self.after = after


# ─────────────────────────────────────────────────────────────────
#  7A — the evaluation primitive
# ─────────────────────────────────────────────────────────────────


class TestEvalAnnotationPrimitive:
    """`_eval_annotation` must match `get_type_hints`' exact semantics, one name at a time."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Inject[Foo]",
            "Annotated[Foo, InjectMeta(qualifier='x')]",
            "Foo | None",
            "Optional[Foo]",
            "list[Foo]",
            "InjectInstances[Foo]",
            "ClassVar[Inject[Foo]]",
            "Repository[User]",
            "'Foo'",
        ],
    )
    def test_matches_get_type_hints_for_a_single_annotation(self, raw: str) -> None:
        """For every resolvable shape, the isolated evaluator must equal the whole-class one.

        This is the load-bearing guarantee of the whole design (plan §7.1):
        evaluating one annotation via a throwaway single-attribute holder
        class must be indistinguishable from evaluating it as part of a real
        signature — otherwise Phase 7 silently changes resolved hints, which
        the plan's Non-goals section forbids.
        """
        holder = type("_AnnotationHolder", (), {"__annotations__": {"x": raw}})
        expected = get_type_hints(holder, globals(), {}, include_extras=True)["x"]

        actual = _eval_annotation(raw, globals(), {})

        assert actual == expected

    def test_live_optional_union_reports_optional_candidate(self) -> None:
        """`Live[Foo | None]` must resolve to an Annotated[Foo | None, LiveMeta()].

        WHY this shape specifically: the container's existing union-unwrapping
        helper (`DIContainer._unwrap_union`) is what turns this annotation into
        "inject None if nothing is bound" — Phase 7 must feed it the exact same
        shape `get_type_hints` would have, not a pre-collapsed/simplified one.
        """
        holder = type(
            "_AnnotationHolder", (), {"__annotations__": {"x": "Live[Foo | None]"}}
        )
        expected = get_type_hints(holder, globals(), {}, include_extras=True)["x"]

        actual = _eval_annotation("Live[Foo | None]", globals(), {})

        assert actual == expected
        underlying, meta = actual.__metadata__ and (
            actual.__origin__,
            actual.__metadata__[0],
        )
        assert isinstance(meta, LiveMeta)
        candidates, is_optional = DIContainer._unwrap_union(underlying)
        assert is_optional is True
        assert candidates == [Foo]

    def test_non_string_annotation_passes_through_unchanged(self) -> None:
        """Step 1 of `resolve_one`: a non-str annotation (no PEP 563) is already an object.

        Modules without `from __future__ import annotations` never reach the
        eval machinery at all — the raw annotation IS the resolved hint.
        """
        assert _eval_annotation(Foo, {}, {}) is Foo


# ─────────────────────────────────────────────────────────────────
#  7A — namespace selection
# ─────────────────────────────────────────────────────────────────


class TestNamespaceSelection:
    """`_annotation_namespaces` picks the right globalns/localns per target kind."""

    def test_class_with_no_explicit_init_yields_empty_result_no_crash(self) -> None:
        """`object.__init__` is a slot wrapper with no `__globals__` — must not crash.

        Per the namespace table (plan §7.1): a slot wrapper falls back to
        `vars(sys.modules[cls.__module__])`. A class with no explicit
        `__init__` inherits `object.__init__`, which has no injectable
        parameters — resolving it must yield `{}`, not an exception.
        """

        class NoInit:
            """No __init__ of its own — inherits object.__init__."""

        globalns, localns = _annotation_namespaces(NoInit.__init__, {})

        result = resolve_params(NoInit.__init__, "NoInit.__init__", globalns, localns)

        assert result == {}

    def test_wrapped_provider_reads_annotations_from_unwrapped_function(self) -> None:
        """A `functools.wraps`-decorated provider must resolve the REAL signature.

        `inspect.unwrap` must be applied before annotations are read, or the
        wrapper's `(*args, **kwargs)` signature (carrying no annotations at
        all) would shadow the wrapped function's real parameters.
        """
        import functools

        def real_provider(dep: Foo) -> Foo:
            return dep

        @functools.wraps(real_provider)
        def wrapper(*args: Any, **kwargs: Any) -> Foo:
            return real_provider(*args, **kwargs)

        globalns, localns = _annotation_namespaces(wrapper, {})

        result = resolve_params(wrapper, "wrapper", globalns, localns)

        assert result == {"dep": Foo}

    def test_generic_owner_type_param_resolves_via_type_params_seeding(self) -> None:
        """`class Repository[T]` method annotated `T` must resolve via `__type_params__`.

        The holder-class trick loses PEP-695 type params because the holder
        is not the generic owner — `_annotation_namespaces` must reseed
        `localns` with `{tp.__name__: tp for tp in owner.__type_params__}`.
        """
        globalns, localns = _annotation_namespaces(Repository.get, {}, owner=Repository)

        result = resolve_params(Repository.get, "Repository.get", globalns, localns)

        assert result == {"item": Repository.__type_params__[0]}


# ─────────────────────────────────────────────────────────────────
#  7B — the resolvers: headline per-parameter isolation
# ─────────────────────────────────────────────────────────────────


class TestPerParamIsolation:
    """The headline behaviour: one unresolvable annotation cannot poison siblings.

    All no-monkeypatch, all using genuinely (function-locally) unresolvable
    types, exercised end-to-end through the container so that "injected" means
    what it says: a real instance ends up in the constructed object.
    """

    def test_unresolvable_defaulted_param_does_not_break_sibling_sync(
        self, container: DIContainer
    ) -> None:
        """`(dep: Inject[Tracer], junk: LocalOnly | None = None)` → `dep` IS injected.

        Today (pre-Phase-7): `get_type_hints` raises `NameError` for the whole
        signature, `_resolve_hints_or_warn` returns `{}`, and NOTHING gets
        injected — `dep` ends up `None` too. This is the exact bug Phase 7
        removes.
        """

        class LocalOnly:
            """Function-local — absent from the provider's __globals__ by construction."""

        @Provider(singleton=True)
        def make_widget(dep: Inject[TracerX], junk: LocalOnly | None = None) -> WidgetX:
            return WidgetX(dep)

        container.bind(TracerX, TracerX)
        container.provide(make_widget)

        widget = container.get(WidgetX)

        assert isinstance(widget.dep, TracerX)

    async def test_unresolvable_defaulted_param_does_not_break_sibling_async(
        self, container: DIContainer
    ) -> None:
        """Async mirror of the sync isolation guarantee."""

        class LocalOnly:
            """Function-local — absent from the provider's __globals__ by construction."""

        @Provider(singleton=True)
        async def make_widget(
            dep: Inject[TracerX], junk: LocalOnly | None = None
        ) -> WidgetX:
            return WidgetX(dep)

        container.bind(TracerX, TracerX)
        container.provide(make_widget)

        widget = await container.aget(WidgetX)

        assert isinstance(widget.dep, TracerX)

    def test_unresolvable_param_in_middle_of_signature(
        self, container: DIContainer
    ) -> None:
        """Params BEFORE and AFTER an unresolvable one must both still be injected."""

        class LocalOnly:
            """Function-local — the poison parameter, sandwiched between two good ones."""

        @Provider(singleton=True)
        def make_combo(
            before: TracerX,
            junk: LocalOnly | None = None,
            after: TracerX = None,  # type: ignore[assignment]
        ) -> Combo:
            return Combo(before, junk, after)

        container.bind(TracerX, TracerX)
        container.provide(make_combo)

        combo = container.get(Combo)

        assert isinstance(combo.before, TracerX)
        assert isinstance(combo.after, TracerX)

    def test_unresolvable_return_annotation_does_not_block_params(
        self, container: DIContainer
    ) -> None:
        """A provider's unresolvable RETURN annotation must not block its param injection.

        `_get_provider_return_type` (Tier 3) is evaluated independently of the
        parameter path per the migration table (plan §7.5) — its failure is a
        warn-and-`None`, never a reason to skip param resolution.
        """

        class LocalReturn:
            """Function-local return type — unresolvable, deliberately unused as interface."""

        @Provider(singleton=True)
        def make_widget(dep: Inject[TracerX]) -> WidgetX:
            return WidgetX(dep)

        container.bind(TracerX, TracerX)
        container.provide(make_widget)

        # Attach a bogus, unresolvable-looking return annotation string directly
        # to __annotations__ AFTER registration — to simulate the quoted-
        # forward-ref failure mode purely for `_get_provider_return_type`
        # (Tier 3, consulted again at resolution time for cycle detection)
        # without perturbing what `container.provide()` already recorded as
        # the binding's interface (``ProviderBinding.__init__`` resolves the
        # return annotation eagerly, at registration time, to determine that
        # key — mutating it beforehand would make registration itself fail,
        # which is not the scenario under test here).
        make_widget.__annotations__["return"] = "LocalReturn"

        widget = container.get(WidgetX)

        assert isinstance(widget.dep, TracerX)

    def test_class_var_isolation_sync(self, container: DIContainer) -> None:
        """`good: Inject[Tracer]` and `junk: LocalOnly` on the same class → only `good` is set."""

        class LocalOnly:
            """Function-local class-var annotation — unmarked, unresolvable, not injected."""

        @Component
        class Consumer:
            good: Inject[TracerX]
            junk: LocalOnly

        container.bind(TracerX, TracerX)
        container.bind(Consumer, Consumer)

        instance = container.get(Consumer)

        assert isinstance(instance.good, TracerX)
        assert not hasattr(instance, "junk")

    async def test_class_var_isolation_async(self, container: DIContainer) -> None:
        """Async mirror of `test_class_var_isolation_sync`."""

        class LocalOnly:
            """Function-local class-var annotation — unmarked, unresolvable, not injected."""

        @Component
        class Consumer:
            good: Inject[TracerX]
            junk: LocalOnly

        container.bind(TracerX, TracerX)
        container.bind(Consumer, Consumer)

        instance = await container.aget(Consumer)

        assert isinstance(instance.good, TracerX)
        assert not hasattr(instance, "junk")

    def test_no_future_annotations_module_still_works(
        self, container: DIContainer
    ) -> None:
        """Cross-module positive control: annotations that are real objects (no PEP 563).

        Exercises step 1 of `resolve_one` end to end via `tests/_annotations_no_future`,
        whose provider/class carry live annotation objects, not strings.
        """
        container.bind(Thing, Thing)
        container.provide(make_thing)

        result = container.get(Thing)

        assert isinstance(result, Thing)


# ─────────────────────────────────────────────────────────────────
#  7B — injection-point failures must be loud
# ─────────────────────────────────────────────────────────────────


class TestInjectionPointFailuresRaise:
    """An unresolvable annotation on something that IS an injection point must raise."""

    def test_marked_injection_point_with_default_raises(self) -> None:
        """`store: Inject[LocalOnly] = None` must raise even though it has a default.

        This is the case the `has_default` tie-break ALONE would wrongly skip
        — proves the AST head-sniff (step 4a) overrides the default-based
        tie-break (step 4c), because `Inject[...]` unambiguously marks an
        injection point regardless of default-ness.
        """

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        def make_thing(store: Inject[LocalOnly] = None) -> Any:  # type: ignore[assignment]
            return store

        globalns = getattr(make_thing, "__globals__", {})

        with pytest.raises(AnnotationResolutionError) as exc_info:
            resolve_params(make_thing, "make_thing", globalns, {})

        message = str(exc_info.value)
        assert "store" in message
        assert "LocalOnly" in message

    def test_marked_injection_point_without_default_raises(self) -> None:
        """Same marker, no default — must also raise, naming the same culprits."""

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        def make_thing(store: Inject[LocalOnly]) -> Any:
            return store

        globalns = getattr(make_thing, "__globals__", {})

        with pytest.raises(AnnotationResolutionError) as exc_info:
            resolve_params(make_thing, "make_thing", globalns, {})

        message = str(exc_info.value)
        assert "store" in message
        assert "LocalOnly" in message

    def test_bare_unresolvable_required_param_raises(self) -> None:
        """`dep: LocalOnly` (no marker, no default) → raises, replacing a misleading TypeError.

        Today this surfaces as `TypeError: missing 1 required positional
        argument` three layers away — Phase 7 must name the actual cause.
        """

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        def make_thing(dep: LocalOnly) -> Any:
            return dep

        globalns = getattr(make_thing, "__globals__", {})

        with pytest.raises(AnnotationResolutionError) as exc_info:
            resolve_params(make_thing, "make_thing", globalns, {})

        assert "dep" in str(exc_info.value)

    def test_non_name_error_always_raises(self) -> None:
        """A defaulted, non-injected param whose annotation raises something OTHER than NameError.

        Step 3 of `resolve_one`: any non-`NameError` failure is a genuine
        defect regardless of injection-point status and must always raise,
        with the original exception chained via `__cause__`.
        """

        def make_thing(junk: 1 / 0 = None) -> Any:  # type: ignore[assignment,valid-type]
            return junk

        globalns = getattr(make_thing, "__globals__", {})

        with pytest.raises(AnnotationResolutionError) as exc_info:
            resolve_params(make_thing, "make_thing", globalns, {})

        assert isinstance(exc_info.value.__cause__, ZeroDivisionError)

    def test_class_var_injection_point_failure_raises_sync(self) -> None:
        """`dep: ClassVar[Inject[LocalOnly]]` on a class must raise, not skip silently."""

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        class Marked:
            dep: ClassVar[Inject[LocalOnly]]  # type: ignore[valid-type]

        with pytest.raises(AnnotationResolutionError) as exc_info:
            resolve_class_annotations(Marked, {})

        message = str(exc_info.value)
        assert "dep" in message
        assert "LocalOnly" in message

    async def test_class_var_injection_point_failure_raises_async(self) -> None:
        """Async mirror — the resolver itself has no async variant, so this pins parity."""

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        class Marked:
            dep: ClassVar[Inject[LocalOnly]]  # type: ignore[valid-type]

        with pytest.raises(AnnotationResolutionError):
            resolve_class_annotations(Marked, {})

    def test_renamed_alias_import_is_still_detected(self) -> None:
        """`from providify import Inject as I`; `dep: I[LocalOnly] = None` must still raise.

        The head name `I` does not resolve (it is not actually imported at
        runtime in this synthetic annotation string), so the AST head-sniff
        falls through to the textual last resort (plan §7.2, step 4c) which
        regex-matches `Inject` (and its aliases) in the raw source text.

        The annotation is built from a raw string (not a real quoted
        annotation on the function) precisely so ruff/pytest collection never
        needs `I` or `LocalOnly` to exist as real names — only
        ``resolve_params`` ever evaluates the string, and only after a
        deliberate ``NameError``.
        """

        def make_thing(dep: object = None) -> Any:
            return dep

        make_thing.__annotations__["dep"] = "I[LocalOnly]"
        globalns: dict[str, Any] = (
            {}
        )  # deliberately empty — "I" and "LocalOnly" both unbound

        with pytest.raises(AnnotationResolutionError) as exc_info:
            resolve_params(make_thing, "make_thing", globalns, {})

        assert "dep" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────
#  7B — non-injection-point failures must be silent
# ─────────────────────────────────────────────────────────────────


class TestNonInjectionPointsAreSilent:
    """Unresolvable annotations on non-injection-points: nothing to configure, nothing to say."""

    def test_unresolvable_defaulted_param_emits_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not just "no exception" — no WARNING log record either.

        Release A logged a warning for this exact shape (`_resolve_hints_or_warn`'s
        `NameError` branch). Phase 7 deletes that branch entirely: a defaulted,
        non-injected parameter that cannot resolve is not a configuration
        problem at all, so there is nothing left to warn about.
        """

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        def make_thing(junk: LocalOnly | None = None) -> Any:
            return junk

        globalns = getattr(make_thing, "__globals__", {})

        with (
            caplog.at_level(logging.WARNING, logger="providify.container"),
            caplog.at_level(logging.WARNING, logger="providify._annotations"),
        ):
            result = resolve_params(make_thing, "make_thing", globalns, {})

        assert result == {}
        assert caplog.records == []

    def test_type_checking_only_import_on_defaulted_param_works(self) -> None:
        """The canonical downstream shape: a `TYPE_CHECKING`-only import, defaulted param.

        This is the false positive Phase 7 exists to remove (plan Goal /
        "Why this is the root fix").
        """

        def make_thing(hint_only: "NeverImported" = None) -> Any:  # type: ignore[assignment,name-defined] # noqa: F821, UP037
            return hint_only

        globalns = getattr(make_thing, "__globals__", {})

        result = resolve_params(make_thing, "make_thing", globalns, {})

        assert result == {}

    def test_plain_unresolvable_class_var_is_skipped_silently(self) -> None:
        """An unmarked, unresolvable class attribute is skipped — no exception, no warning."""

        class LocalOnly:
            """Unresolvable by construction — function-local."""

        class Plain:
            junk: LocalOnly

        result = resolve_class_annotations(Plain, {})

        assert result == {}


# ─────────────────────────────────────────────────────────────────
#  7C — the container-level cache
# ─────────────────────────────────────────────────────────────────


class TestHintsCache:
    """`DIContainer._resolve_params` / `_resolve_class_annotations` cache their results."""

    def test_second_resolution_is_cached(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second `container.get(X)` must perform zero further `_eval_annotation` calls."""
        import providify._annotations as annotations_module

        calls = {"n": 0}
        real_eval = annotations_module._eval_annotation

        def counting_eval(*args: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            return real_eval(*args, **kwargs)

        monkeypatch.setattr(annotations_module, "_eval_annotation", counting_eval)

        @Component
        class Cached:
            def __init__(self, dep: TracerX) -> None:
                self.dep = dep

        container.bind(TracerX, TracerX)
        container.bind(Cached, Cached)

        container.get(Cached)
        first_call_count = calls["n"]
        assert first_call_count > 0

        container.get(Cached)

        assert calls["n"] == first_call_count

    def test_binding_mutation_invalidates_cache(self, container: DIContainer) -> None:
        """Resolving before AND after a new binding must observe the new binding.

        Proves the hints cache dies together with `_localns_cache`, not
        independently of it (plan §7.4 "Invalidation").
        """

        @Component
        class Dep:
            """Not yet bound — resolution must skip it (it is defaulted)."""

        @Component
        class Consumer:
            def __init__(self, dep: Dep = None) -> None:  # type: ignore[assignment]
                self.dep = dep

        container.bind(Consumer, Consumer)
        before = container._resolve_params(Consumer.__init__, "Consumer.__init__")
        assert "dep" not in before or before.get("dep") is None

        container.bind(Dep, Dep)

        after = container._resolve_params(Consumer.__init__, "Consumer.__init__")
        assert after.get("dep") is Dep

    def test_cache_is_per_container(self) -> None:
        """Two containers with different bindings resolve the same class differently."""

        @Component
        class Dep:
            """Bound in one container only."""

        @Component
        class Consumer:
            def __init__(self, dep: Dep = None) -> None:  # type: ignore[assignment]
                self.dep = dep

        container_a = DIContainer()
        container_b = DIContainer()
        container_a.bind(Consumer, Consumer)
        container_b.bind(Consumer, Consumer)
        container_b.bind(Dep, Dep)

        hints_a = container_a._resolve_params(Consumer.__init__, "Consumer.__init__")
        hints_b = container_b._resolve_params(Consumer.__init__, "Consumer.__init__")

        assert hints_a.get("dep") is not Dep
        assert hints_b.get("dep") is Dep

    def test_returned_dict_is_a_copy(self, container: DIContainer) -> None:
        """Mutating the returned dict must not corrupt the cache for the next caller."""

        @Component
        class Consumer:
            def __init__(self, dep: TracerX) -> None:
                self.dep = dep

        container.bind(TracerX, TracerX)
        container.bind(Consumer, Consumer)

        first = container._resolve_params(Consumer.__init__, "Consumer.__init__")
        first["dep"] = "poisoned"

        second = container._resolve_params(Consumer.__init__, "Consumer.__init__")

        assert second["dep"] is TracerX

    def test_concurrent_resolution_is_consistent(self, container: DIContainer) -> None:
        """8 threads resolving the same class concurrently must all succeed identically."""

        @Component
        class Consumer:
            def __init__(self, dep: TracerX) -> None:
                self.dep = dep

        container.bind(TracerX, TracerX)
        container.bind(Consumer, Consumer)

        def resolve() -> dict[str, Any]:
            return container._resolve_params(Consumer.__init__, "Consumer.__init__")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: resolve(), range(8)))

        assert all(r == {"dep": TracerX} for r in results)


# ─────────────────────────────────────────────────────────────────
#  7D — parity guard: Phase 7 changes no resolvable outcome
# ─────────────────────────────────────────────────────────────────


class TestParityWithGetTypeHints:
    """Table-driven guard: for resolvable shapes, per-param resolution equals `get_type_hints`.

    This is the "Phase 7 changes no resolvable outcome" contract from the
    plan's Non-goals section — if this table ever fails, something changed
    the SHAPE of a hint that already resolved today, which the plan expressly
    forbids (see Risks → Phase 7 → "Semantic drift").
    """

    @pytest.mark.parametrize(
        "annotation_source",
        [
            "def fn(x: Foo) -> None: ...",
            "def fn(x: Inject[Foo]) -> None: ...",
            "def fn(x: Annotated[Foo, InjectMeta(qualifier='q', priority=1)]) -> None: ...",
            "def fn(x: Foo | None) -> None: ...",
            "def fn(x: list[Foo]) -> None: ...",
            "def fn(x: InjectInstances[Foo]) -> None: ...",
            "def fn(x: Live[Foo]) -> None: ...",
            "def fn(x: Repository[User]) -> None: ...",
            "def fn(x: 'Foo') -> None: ...",
        ],
    )
    def test_resolve_params_matches_get_type_hints_for_resolvable_shapes(
        self, container: DIContainer, annotation_source: str
    ) -> None:
        """`container._resolve_params(fn, ...)` must equal `get_type_hints`, minus `"return"`."""
        namespace: dict[str, Any] = dict(globals())
        exec(
            annotation_source, namespace
        )  # noqa: S102 - controlled, module-level test data
        fn = namespace["fn"]

        expected = {
            k: v
            for k, v in get_type_hints(
                fn, include_extras=True, localns=container._build_localns()
            ).items()
            if k != "return"
        }

        actual = container._resolve_params(fn, "fn")

        assert actual == expected

    def test_resolve_class_annotations_matches_get_type_hints_for_inherited_var(
        self, container: DIContainer
    ) -> None:
        """Inherited class-var annotations must match `get_type_hints`' MRO order too."""

        class Sub(AnnotatedBase):
            """Adds nothing — pure inheritance-order check."""

        expected = {
            k: v
            for k, v in get_type_hints(Sub, include_extras=True).items()
            if k != "return"
        }

        actual = container._resolve_class_annotations(Sub)

        assert actual == expected
