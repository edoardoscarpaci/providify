"""Startup-time full dependency graph validation (Plan 003).

Pure data + a pure classifier — mirroring `descriptor.py`'s role for
`describe()`. Nothing in this module touches a `DIContainer` instance or
performs I/O; the walk itself (`DIContainer.validate()`) lives in
`container.py` because it needs `_filter`, `_resolve_params`, and
`_collect_class_var_hints` — internals this module deliberately does not
reach into (see plan 003 §Alternatives, "a separate GraphValidator class").

Import direction: `container.py` -> `validation.py` -> `type.py`. This module
must NEVER import from `container.py` — that would create a cycle, since
`container.py` imports `ValidationReport` / `ValidationIssue` / `_classify_hint`
from here.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Union, get_args, get_origin

from .type import (
    DelegateMeta,
    EventMeta,
    InjectionPoint,
    InjectMeta,
    InstanceMeta,
    LazyMeta,
    LiveMeta,
    NamedMeta,
    _unwrap_classvar,
)

# ─────────────────────────────────────────────────────────────────
#  Severity / IssueKind — the vocabulary of a validation report
# ─────────────────────────────────────────────────────────────────


class Severity(StrEnum):
    """Severity tier of a single :class:`ValidationIssue`.

    ``ERROR`` issues are guaranteed to fail at resolution time — they are
    what makes :meth:`~providify.container.DIContainer.validate` raise by
    default. ``WARNING`` issues describe a legal-but-notable pattern (a
    defaulted parameter with no binding, a caller-parameterised proxy with
    nothing registered today) that never raises on its own. ``INFO`` issues
    (plan 015) describe the container doing exactly what it was told —
    recorded purely so a wiring report can explain *why* a candidate is not
    the live one (e.g. an inactive ``@Requires`` condition); they never
    raise and never affect :attr:`ValidationReport.ok`.

    Thread safety:  ✅ Safe — ``StrEnum`` members are immutable singletons.
    Async safety:   ✅ Safe — same reason.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class IssueKind(StrEnum):
    """Discriminator for what kind of wiring defect a :class:`ValidationIssue` reports.

    See plan 003 §Design for the full missing/ambiguous/cycle decision table
    that produces each kind.

    Thread safety:  ✅ Safe — ``StrEnum`` members are immutable singletons.
    Async safety:   ✅ Safe — same reason.
    """

    #: Eager, single-valued injection point with no default and no binding —
    #: `_collect_kwargs_sync` would raise `LookupError` for it today.
    MISSING_BINDING = "missing_binding"
    #: Same as MISSING_BINDING but the parameter has a default value, so the
    #: runtime silently falls back to it instead of raising.
    MISSING_BINDING_DEFAULTED = "missing_binding_defaulted"
    #: `Instance[T]` / `Event[T]` with no binding today — legal because both
    #: proxies defer resolution/qualification to call time.
    MISSING_BINDING_DEFERRED = "missing_binding_deferred"
    #: >=2 candidates tied at the max priority for a single-valued point —
    #: `max()` picks the first in registration order, silently and arbitrarily.
    AMBIGUOUS_BINDING = "ambiguous_binding"
    #: A closed loop in the static (non-deferred) dependency graph.
    CIRCULAR_DEPENDENCY = "circular_dependency"
    #: A wider-scoped binding holds a direct (non-Live/Instance) reference to
    #: a narrower-scoped one — mirrors `ScopeViolationDetectedError`.
    SCOPE_LEAK = "scope_leak"
    #: A REQUEST/SESSION-scoped dependency injected without `Live[T]`/`Instance[T]`
    #: — mirrors `LiveInjectionRequiredError`.
    LIVE_REQUIRED = "live_required"
    #: A binding's annotations could not be evaluated at all — mirrors
    #: `AnnotationResolutionError`. The binding's edges are omitted from the
    #: graph walk (partial graph, never silently "no dependencies").
    UNRESOLVED_ANNOTATION = "unresolved_annotation"
    #: A `SINGLETON`-scoped `ProviderBinding` has no `@Disposes` disposer and
    #: the type it produces carries a `@PreDestroy` hook that will therefore
    #: never run. Mirrors Jakarta CDI: producer-returned objects receive no
    #: lifecycle callbacks, so `@Disposes` is the only teardown path for them.
    UNREACHABLE_PRE_DESTROY = "unreachable_pre_destroy"
    #: Two `@Disposes` methods on ONE @Configuration resolved to the same
    #: ProviderBinding it registered; the later one (definition order)
    #: replaced the earlier, which will therefore never run.
    DISPOSER_OVERWRITTEN = "disposer_overwritten"
    #: A `@Disposes(X)` on a @Configuration matches none of the bindings
    #: that @Configuration itself registered — it is attached to nothing.
    #: A disposer only ever tears down its own module's providers.
    UNMATCHED_DISPOSER = "unmatched_disposer"
    #: A binding carries `@Requires` whose condition currently evaluates
    #: `False` — excluded from resolution, working as declared.
    #: Informational (`Severity.INFO`); exists so wiring reports can
    #: explain why a candidate is not the live one.
    CONDITION_INACTIVE = "condition_inactive"
    #: An active `@Fallback` binding that `get(interface, qualifier=...)`
    #: would not return because an active non-fallback binding matches the
    #: same request. `Severity.INFO` — expected, not a defect: the fallback
    #: is doing exactly what a default is supposed to do (plan 017 §Design).
    FALLBACK_SHADOWED = "fallback_shadowed"


# ─────────────────────────────────────────────────────────────────
#  ValidationIssue / ValidationReport — the report shape
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationIssue:
    """One reported wiring defect, produced by :meth:`DIContainer.validate`.

    Immutable by design — a report is a point-in-time snapshot; mutating one
    issue after the fact would silently desync it from the container state
    it was computed against.

    Thread safety:  ✅ Frozen dataclass — safe to share across threads once built.
    Async safety:   ✅ Pure data — no shared mutable state.

    Attributes:
        kind:       Discriminator — see :class:`IssueKind`.
        severity:   :class:`Severity.ERROR`, :class:`Severity.WARNING`,
                    or :class:`Severity.INFO`.
        owner:      Human-readable owner of the injection point, e.g.
                    ``"OrderService.__init__"`` or ``"@Provider(make_db)"``.
                    For ``UNMATCHED_DISPOSER``, ``"ModuleName.method_name"``
                    (plan 014) — the disposer has no own binding to be
                    "owned by", so it names itself instead.
        message:    A complete, actionable, one-line message with a fix hint
                    — safe to print directly to a log or console.
        param_name: The specific parameter/class-attribute name, when known.
                    ``None`` for graph-level issues (e.g. cycles) that are
                    not anchored to a single parameter. For
                    ``UNREACHABLE_PRE_DESTROY``, the unreachable hook's
                    method name. For ``DISPOSER_OVERWRITTEN`` /
                    ``UNMATCHED_DISPOSER``, the `@Disposes` method name (the
                    winning one, for ``DISPOSER_OVERWRITTEN``).
        requested:  ``_type_name()`` of the type the injection point asked
                    for. ``None`` when not applicable (e.g. cycles). For
                    ``UNREACHABLE_PRE_DESTROY``, the type the provider
                    produces. For ``DISPOSER_OVERWRITTEN`` /
                    ``UNMATCHED_DISPOSER``, the `@Disposes(...)` argument. For
                    ``FALLBACK_SHADOWED``, the fallback's own interface (not
                    the shadowing winner's).
        qualifier:  The qualifier the injection point requested, if any. For
                    ``FALLBACK_SHADOWED``, the fallback binding's own
                    qualifier (its natural request, not the winner's).
        candidates: Populated only for ``AMBIGUOUS_BINDING`` — the
                    human-readable names of every tied candidate.
        shadowed_by: Populated only for ``FALLBACK_SHADOWED`` — the
                    ``owner`` label of the binding that wins the fallback's
                    natural request (plan 017 §Design).

    Example:
        ValidationIssue(
            kind=IssueKind.MISSING_BINDING,
            severity=Severity.ERROR,
            owner="OrderService.__init__",
            message="'repo' requests Repository but no binding is registered.",
            param_name="repo",
            requested="Repository",
        )
    """

    kind: IssueKind
    severity: Severity
    owner: str
    message: str
    param_name: str | None = None
    requested: str | None = None
    qualifier: str | type | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)
    # Trailing field (plan 017) — additive, so existing positional/keyword
    # construction of ValidationIssue elsewhere in the repo is unaffected.
    shadowed_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert this issue to a plain, JSON/YAML-safe dict.

        Returns:
            A dict with only ``str | int | bool | list | dict | None`` values
            — safe to pass directly to ``json.dumps``.

        Example:
            import json
            json.dumps(issue.to_dict())
        """
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "owner": self.owner,
            "message": self.message,
            "param_name": self.param_name,
            "requested": self.requested,
            # Qualifier can legitimately be a type (e.g. a marker class used
            # as a qualifier token) — normalise to its name for JSON safety,
            # matching BindingDescriptor.to_dict()'s qualifier handling.
            "qualifier": (
                getattr(self.qualifier, "__qualname__", str(self.qualifier))
                if self.qualifier
                else None
            ),
            "candidates": list(self.candidates),
            "shadowed_by": self.shadowed_by,
        }


@dataclass(frozen=True)
class ValidationReport:
    """Immutable result of one :meth:`DIContainer.validate` call.

    Deliberately does **not** define ``__bool__``: ``if report:`` reads as
    "there are issues" to one reader and "the container is fine" to another
    — an ambiguity not worth the convenience. Callers write
    ``if not report.ok:`` instead.

    Thread safety:  ✅ Frozen dataclass, immutable ``tuple`` of immutable
                    issues — safe to share across threads once built.
    Async safety:   ✅ Pure data — no shared mutable state.

    Attributes:
        issues:           Every issue found, in the order the graph walk
                           produced them (binding registration order, then
                           per-binding injection-point order; then
                           module-level disposer-wiring issues in install
                           order; cycles appended last).
        checked_bindings: Total number of bindings inspected — equal to
                           ``len(container._bindings)`` at validation time.

    Edge cases:
        - Empty container → ``issues=()``, ``checked_bindings=0``, ``ok is True``.
        - Calling ``validate()`` twice on an unchanged container yields two
          reports whose ``to_dict()`` outputs compare equal — no accumulated
          state.

    Example:
        report = container.validate(raise_on_error=False)
        if not report.ok:
            for issue in report.errors:
                log.error("%s: %s", issue.owner, issue.message)
    """

    issues: tuple[ValidationIssue, ...]
    checked_bindings: int

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        """Every ``ERROR``-severity issue, preserving report order."""
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        """Every ``WARNING``-severity issue, preserving report order."""
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)

    @property
    def infos(self) -> tuple[ValidationIssue, ...]:
        """Every ``INFO``-severity issue, preserving report order (plan 015)."""
        return tuple(i for i in self.issues if i.severity is Severity.INFO)

    @property
    def ok(self) -> bool:
        """``True`` iff there are no ``ERROR``-severity issues.

        Note: a report with warnings only is still ``ok``  — warnings never
        block startup, only errors do (see :class:`Severity`). INFO issues,
        like warnings, never make ``ok`` ``False`` either.
        """
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        """Convert the full report to a plain, JSON/YAML-safe dict.

        Returns:
            A dict with only ``str | int | bool | list | dict | None``
            values — safe to pass directly to ``json.dumps``.

        Example:
            import json
            print(json.dumps(report.to_dict(), indent=2))
        """
        return {
            "checked_bindings": self.checked_bindings,
            "ok": self.ok,
            "issues": [i.to_dict() for i in self.issues],
        }

    def __repr__(self) -> str:
        """Render a grouped, human-readable summary block.

        Format:
            ValidationReport(checked_bindings=N, ok=True/False)
              [ERROR] <owner>: <message>
              [WARNING] <owner>: <message>
              [INFO] <owner>: <message>
              ...

        Returns:
            Multi-line string; a single header line when ``issues`` is empty.
        """
        header = f"ValidationReport(checked_bindings={self.checked_bindings}, ok={self.ok})"
        if not self.issues:
            return header
        # Errors first, then warnings, then infos — the more actionable tier
        # leads. Without *self.infos here an INFO issue would still be in
        # `self.issues`/`to_dict()` but silently vanish from the printed
        # report (plan 015 §Risks, "__repr__ silent-drop risk").
        lines = [header]
        for issue in (*self.errors, *self.warnings, *self.infos):
            lines.append(f"  [{issue.severity.value.upper()}] {issue.owner}: {issue.message}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
#  _HintSpec / _classify_hint — pure static classifier
#
#  Mirrors `DIContainer._resolve_hint_sync` (container.py) branch for
#  branch, but answers a different question: not "what instance does this
#  hint resolve to right now" but "what WOULD it need at resolution time,
#  and how should a missing binding be graded". No container, no I/O — the
#  container decides what "missing" means for a *specific* candidate list
#  (see `DIContainer.validate`); this function only reads the shape of the
#  annotation itself.
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _HintSpec:
    """Static classification of a single type hint as an injection point.

    Module-private — the container's `validate()` is the only consumer.
    Not exported via `providify/__init__.py`.

    Attributes:
        base_type:            The type to look candidates up for. For eager
                               single/union/multi points this is the
                               (possibly still-union) requested type; for
                               `InjectInstances[T]` it is the *inner* `T`
                               (already unwrapped from `list[T]`).
        qualifier:             Qualifier to filter candidates by, mirroring
                               the hint's own marker — NOT the owning
                               binding's qualifier (see plan 003 §Design).
        priority:              Exact priority to filter candidates by, or
                               `None` for "best available".
        optional:              `True` when a missing binding legitimately
                               resolves to `None` at runtime (`T | None`,
                               `InjectMeta(optional=True)`) — never reported.
        deferred:              `True` for `Lazy[T]` / `Live[T]` /
                               `Instance[T]` / `Event[T]` — excluded from
                               cycle edges (the proxy defers resolution past
                               the owner's own construction).
        multi:                 `True` for `InjectInstances[T]` /
                               `InjectMeta(all=True)` — `get_all()`
                               semantics: missing is never an issue, every
                               candidate becomes a cycle edge.
        caller_parameterised:  `True` for `Instance[T]` / `Event[T]` — the
                               proxy takes its qualifier at call time, so
                               "no binding today" is graded WARNING, not ERROR.
        excludes_self:         `True` for `Annotated[T, DelegateMeta()]` —
                               the owning binding's own implementation must be
                               excluded from its candidate list, mirroring
                               the self-exclusion in `_resolve_hint_sync`'s
                               delegate branch.
    """

    base_type: Any
    qualifier: str | type | None
    priority: int | None
    optional: bool
    deferred: bool
    multi: bool
    caller_parameterised: bool
    excludes_self: bool


#: Bare types that are never treated as an injection point, even though
#: `isinstance(hint, type)` is True for all of them. `_resolve_hint_sync`'s
#: plain-type branch only resolves a bare type when *some* binding matches it
#: at runtime — but `_classify_hint` has no container to ask. Rather than
#: guess "yes" for every bare type (which would make `dep: int` a startup
#: ERROR for every class with a plain scalar constructor parameter — almost
#: certainly not what the author intended), the classifier conservatively
#: excludes the common built-in scalar/text/binary types from ever being
#: graded as an injection point, matching how every mainstream DI framework
#: (Spring, CDI, .NET DI) treats primitives as "plain data", never auto-wired.
#
# Tradeoffs:
#   ✅ `def __init__(self, name: str)` never produces a false-positive ERROR.
#   ❌ A genuine `container.bind(int, ...)` binding used as a bare `int`
#      constructor parameter would be silently skipped by `validate()` (it
#      would still work fine at runtime via `container.get()`). Accepted:
#      binding builtins directly is exotic and not a pattern this codebase's
#      own test suite or README demonstrates anywhere.
_NEVER_INJECTED_TYPES: frozenset[type] = frozenset(
    {int, str, float, bool, bytes, bytearray, complex, type(None)}
)


def _unwrap_union(hint: Any) -> tuple[list[Any], bool] | None:
    """Decompose a Union type hint into its candidate types and optionality.

    Private duplicate of `container.py`'s module-level helper of the same
    name — see that copy's docstring for the full design rationale. Kept as
    a separate copy (rather than imported) because `validation.py` must
    never import from `container.py` (see this module's header) to avoid a
    circular import — `container.py` imports `_classify_hint` from here.

    Args:
        hint: Any type annotation, already evaluated (not a string).

    Returns:
        `(candidates, is_optional)` — non-`NoneType` args in declaration
        order, and whether `NoneType` appeared in the union. `None` if
        *hint* is not a union type at all.
    """
    if isinstance(hint, types.UnionType):
        # Python 3.10+ pipe syntax: int | str | None
        args = get_args(hint)
    elif get_origin(hint) is Union:
        # typing.Union[...] and Optional[T] (== Union[T, None])
        args = get_args(hint)
    else:
        return None

    none_type = type(None)
    is_optional = none_type in args
    candidates = [a for a in args if a is not none_type]
    return candidates, is_optional


def _classify_hint(
    hint: Any, is_collection_point: Callable[[Any], bool] | None = None
) -> _HintSpec | None:
    """Statically classify a type hint as an injection point (or not).

    Pure function — mirrors `DIContainer._resolve_hint_sync`'s branch
    structure (`container.py`) but never touches the container or any
    registered binding. See plan 003 §Design's hint table for the full
    decision matrix this function implements.

    Args:
        hint: Any evaluated (non-string) type hint — bare type, generic
              alias, `Annotated[...]`, `Union[...]`/`X | Y`,
              `ClassVar[...]`-wrapped, or `InjectionPoint`.
        is_collection_point: Plan 010 (F7) — optional predicate answering
              "is T a declared multibinding collection point?" (the
              container's `_is_collection_point`, passed in because this
              module never touches the container itself — see the module
              docstring's one-way import rule). When given and `hint` is
              `list[T]`/`typing.List[T]` with a truthy predicate result, the
              hint is classified exactly like `InjectInstances[T]` (missing
              is never reported, every candidate for T becomes a cycle edge).
              This classifier is purely static and has no notion of "a
              literal `list[T]` binding also exists" — that ambiguity is a
              separate, binding-registry-level check
              (`DIContainer.validate_bindings()`, plan 010 step 12), not a
              per-hint classification concern. `None` (the default) preserves
              pre-plan-010 behaviour exactly — bare `list[T]` is classified
              like any other bare generic alias (the final branch below).

    Returns:
        A `_HintSpec` describing how the hint should be validated, or
        `None` when *hint* is not an injection point at all (plain
        scalars, `InjectionPoint`, unannotated hints with no providify
        marker).

    Edge cases:
        - `hint is None`                    → `None` (nothing to classify).
        - `InjectionPoint`                    → `None` — context metadata,
          never itself a dependency edge.
        - `ClassVar[Inject[T]]`               → unwrapped first, then
          classified identically to bare `Inject[T]`.
        - `Annotated[T, "just a docstring"]`  → `None` — no providify marker.
        - `int` / `str` / other builtin scalar → `None`, see
          `_NEVER_INJECTED_TYPES`.
        - `Inject[T | None]` (optionality nested *inside* the marker) → the
          union is unwrapped and merged into `optional` BEFORE the marker
          branch runs, mirroring `_resolve_hint_sync`'s
          `_unwrap_union`-inside-`Annotated` merge exactly.

    Example:
        >>> _classify_hint(Inject[Widget]).optional
        False
        >>> _classify_hint(Inject[Widget] | None).optional
        True
        >>> _classify_hint(int) is None
        True
    """
    if hint is None:
        return None

    # ClassVar[Inject[T]] -> Inject[T] — class-var wrapper carries no
    # semantics of its own once stripped; the inner marker decides everything.
    hint = _unwrap_classvar(hint)

    # InjectionPoint is context metadata handed TO an injection point, never
    # a dependency edge itself — must be excluded before the bare-type branch
    # below would otherwise happily classify it as "eager single".
    if hint is InjectionPoint:
        return None

    if get_origin(hint) is Annotated:
        args = get_args(hint)
        base_type = args[0]

        # ── Detect Optional[T] / T | None INSIDE the marker's type slot ──
        # Mirrors `_resolve_hint_sync` (container.py:2220-2233) exactly: only
        # a true single-candidate Optional[T] is simplified into
        # (T, optional=True) here — a genuine multi-member union
        # (Inject[T1 | T2]) is passed through unchanged to the union branch
        # below, one level up in `DIContainer.validate()`.
        _base_union = _unwrap_union(base_type)
        if _base_union is not None and len(_base_union[0]) == 1 and _base_union[1]:
            effective_base_type: Any = _base_union[0][0]
            effective_optional = True
        else:
            effective_base_type = base_type
            effective_optional = False

        # Priority order matches `_resolve_hint_sync`: Live -> Lazy -> Instance -> Inject.
        live_meta = next((a for a in args[1:] if isinstance(a, LiveMeta)), None)
        lazy_meta = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
        instance_meta = next((a for a in args[1:] if isinstance(a, InstanceMeta)), None)
        inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

        if live_meta:
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=live_meta.qualifier,
                priority=live_meta.priority,
                optional=effective_optional or live_meta.optional,
                deferred=True,  # re-resolves every access — never closes a cycle
                multi=False,
                caller_parameterised=False,
                excludes_self=False,
            )
        if lazy_meta:
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=lazy_meta.qualifier,
                priority=lazy_meta.priority,
                optional=effective_optional or lazy_meta.optional,
                deferred=True,  # documented cycle-breaker (exceptions.py:79)
                multi=False,
                caller_parameterised=False,
                excludes_self=False,
            )
        if instance_meta:
            # InstanceMeta carries no qualifier/priority — deferred to the
            # proxy's own .get()/.get_all() call site (type.py InstanceMeta).
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=None,
                priority=None,
                optional=False,  # "missing" is WARNING via caller_parameterised, not optional
                deferred=True,
                multi=False,
                caller_parameterised=True,
                excludes_self=False,
            )
        if inject_meta and inject_meta.all:
            # InjectInstances[T] / InjectMeta(all=True) expands the base type
            # to list[T] — unwrap back to T, mirroring _resolve_hint_sync.
            inner = (
                get_args(effective_base_type)[0]
                if get_origin(effective_base_type) is list
                else effective_base_type
            )
            return _HintSpec(
                base_type=inner,
                qualifier=inject_meta.qualifier,
                priority=None,  # get_all() has no priority filter at the container level
                optional=True,  # [] is a legal, unreported answer — never "missing"
                deferred=False,  # get_all() resolves every candidate eagerly
                multi=True,
                caller_parameterised=False,
                excludes_self=False,
            )
        if inject_meta:
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=inject_meta.qualifier,
                priority=inject_meta.priority,
                optional=inject_meta.optional or effective_optional,
                deferred=False,
                multi=False,
                caller_parameterised=False,
                excludes_self=False,
            )

        # ── NamedMeta / DelegateMeta / EventMeta (still inside Annotated) ──
        named_meta = next((a for a in args[1:] if isinstance(a, NamedMeta)), None)
        delegate_meta = next((a for a in args[1:] if isinstance(a, DelegateMeta)), None)
        event_meta = next((a for a in args[1:] if isinstance(a, EventMeta)), None)

        if named_meta:
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=named_meta.name,
                priority=None,
                optional=False,
                deferred=False,
                multi=False,
                caller_parameterised=False,
                excludes_self=False,
            )
        if delegate_meta:
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=None,
                priority=None,
                optional=False,
                deferred=False,
                multi=False,
                caller_parameterised=False,
                excludes_self=True,  # self-exclusion — see container.py:2316-2322
            )
        if event_meta:
            return _HintSpec(
                base_type=effective_base_type,
                qualifier=None,
                priority=None,
                optional=False,  # "missing" is WARNING via caller_parameterised
                deferred=True,  # EventProxy.fire() dispatches at call time, never
                # at construction — confirmed by reading providify/type.py's
                # EventProxy (plan 003 §Risks' open question): __init__ only
                # stores container + tp, no observer resolution happens there.
                multi=False,
                caller_parameterised=True,
                excludes_self=False,
            )

        # Annotated[T, ...] with no recognised providify marker at all
        # (e.g. Annotated[int, "just a docstring"]) — not an injection point.
        return None

    # ── Union / Optional resolution (mirrors _resolve_hint_sync) ──────────
    # Must run BEFORE the bare-type branch below: get_origin() on a Union
    # is non-None (typing.Union) or the hint is a types.UnionType instance —
    # neither is `isinstance(hint, type)`, and neither is ever a registered
    # binding's interface directly.
    union_result = _unwrap_union(hint)
    if union_result is not None:
        candidates, is_optional = union_result
        if not candidates:
            # Degenerate Union[None] — nothing to inject, nothing to validate.
            return None
        return _HintSpec(
            base_type=hint,  # kept as the WHOLE union — validate() re-unwraps
            # it member-by-member (declaration-order candidate resolution,
            # mirroring container.py:2342's "first member that resolves" rule).
            qualifier=None,
            priority=None,
            optional=is_optional,
            deferred=False,
            multi=False,
            caller_parameterised=False,
            excludes_self=False,
        )

    # ── F7 (plan 010 §Design F7.3): bare list[T], T a collection point ────
    # Must run BEFORE the generic bare-type branch below — a collection
    # point's list[T] is a "resolve every candidate" edge, not a single
    # missing-or-not lookup for the literal type list[T]. See the
    # is_collection_point Arg's docstring above for what this deliberately
    # does NOT model (a coexisting literal list[T] binding).
    if is_collection_point is not None and get_origin(hint) is list:
        list_args = get_args(hint)
        if len(list_args) == 1 and is_collection_point(list_args[0]):
            return _HintSpec(
                base_type=list_args[0],
                qualifier=None,
                priority=None,
                optional=True,  # [] is a legal, unreported answer (F7.4)
                deferred=False,
                multi=True,
                caller_parameterised=False,
                excludes_self=False,
            )

    if isinstance(hint, type) or get_origin(hint) is not None:
        # Bare type / generic alias with no Annotated wrapper — same runtime
        # rule as Inject[T]: _collect_kwargs_sync raises LookupError for it
        # when unbound and undefaulted (container.py:2354-2360).
        if hint in _NEVER_INJECTED_TYPES:
            return None
        return _HintSpec(
            base_type=hint,
            qualifier=None,
            priority=None,
            optional=False,
            deferred=False,
            multi=False,
            caller_parameterised=False,
            excludes_self=False,
        )

    return None
