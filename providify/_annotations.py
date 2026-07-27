"""Pure per-annotation resolution primitives (Plan 001, Phase 7 — release B).

`get_type_hints()` evaluates an entire signature (or class) in one shot: if
ANY single name in ANY annotation is unresolvable, the whole call raises and
every OTHER annotation on that same callable/class is lost with it. That
all-or-nothing behaviour is the root cause of the false positives release A
(Phases 1-6) had to tolerate with a `NameError`-swallowing helper.

This module replaces "resolve everything at once" with "resolve one
annotation at a time", using `get_type_hints` itself as the evaluation engine
(via a disposable single-attribute holder class — see `_eval_annotation`) so
every quirk of PEP-563 strings, `ForwardRef`, `Annotated`, `ClassVar`, and
`X | None` unions is handled identically to today, just isolated per name.

Policy (WHO decides what a resolution failure means) intentionally does NOT
live here — this module answers only "what does this one annotation
evaluate to, and if it can't, is it plausibly an injection point?". The
container decides what a "yes, it is" or "no, it isn't" answer means for a
given call site (raise vs. inject vs. skip); see `container.py`'s
`_resolve_params` / `_resolve_class_annotations`.

Design constraint: **pure functions only** — no `DIContainer`, no I/O, no
mutable module state. This keeps the bootstrap-classification logic (7.2)
unit-testable without constructing a container, and keeps `container.py`
(already >3800 lines) from growing further. The container supplies its own
`localns` (`_build_localns()`) and caches results; this module has no cache
of its own.

Import direction: `container.py` → `_annotations.py` → `type.py`. This module
must NEVER import from `container.py` — that would create a cycle.
"""

from __future__ import annotations

import ast
import inspect
import re
import sys
from typing import Any, Literal, get_type_hints

from providify.exceptions import AnnotationResolutionError
from providify.type import (
    Inject,
    InjectInstances,
    Instance,
    Lazy,
    Live,
    _InjectedAlias,
    _InjectedInstancesAlias,
    _InstanceAlias,
    _LazyAlias,
    _LiveAlias,
    _providify,
)

# Re-exported for readability at call sites below — these are the runtime
# singleton objects `Inject`/`InjectInstances`/`Lazy`/`Live`/`Instance` are
# bound to (see providify/type.py); unused directly but documents intent.
_ = (Inject, InjectInstances, Lazy, Live, Instance)

#: Sentinel returned by `_resolve_one` to mean "this annotation was
#: classified as NOT an injection point — omit it from the result dict
#: entirely", as distinct from a legitimately resolved value of `None`
#: (e.g. `Optional[int]` resolving to `int | None`, whose runtime object is
#: not `None` itself, but nothing rules out some future hint literally being
#: the `None` singleton — using a private object avoids ANY ambiguity).
_SKIP = object()

#: The runtime alias-singleton classes from `providify.type` — the SUGAR
#: objects `Inject`, `InjectInstances`, `Lazy`, `Live`, `Instance` are each an
#: instance of exactly one of these (at runtime; `TYPE_CHECKING` swaps them
#: for TypeAlias-like stubs that never execute). `isinstance` against this
#: tuple is the authoritative "is this head an injection marker" check the
#: bootstrap classifier (7.2) needs — it does NOT depend on which name the
#: caller imported the object under (works even through `Inject as I`,
#: PROVIDED the head resolves at all; see `_sniff_head`'s "unknown" branch
#: for the case where it doesn't).
_MARKER_ALIAS_TYPES = (
    _InjectedAlias,
    _InjectedInstancesAlias,
    _LazyAlias,
    _LiveAlias,
    _InstanceAlias,
)

#: Last-resort textual heuristic (plan §7.2, "4c class-var fallback"): when a
#: bare, unresolvable identifier's raw source text literally contains one of
#: providify's marker names, treat it as an injection point even though the
#: name itself never resolved. This exists for the rare case where the
#: marker name appears in the string but is not (in this evaluation
#: context) an importable identifier — e.g. a metaclass or codegen tool that
#: stamped the literal word into an annotation string. It is DELIBERATELY
#: narrow: it only fires for un-subscripted bare names, because
#: `_sniff_head`'s wrapped-shape branch (below) already treats every
#: unresolvable `X[...]` / `X(...)` head as a probable injection point,
#: making a second, broader regex pass over subscripted forms redundant.
_MARKER_TEXT_RE = re.compile(
    r"\b(Inject|InjectInstances|Lazy|Live|Instance|"
    r"InjectMeta|LazyMeta|LiveMeta|InstanceMeta)\b"
)


def _eval_annotation(
    raw: Any, globalns: dict[str, Any], localns: dict[str, Any]
) -> Any:
    """Evaluate ONE annotation in isolation, with `get_type_hints`'s exact semantics.

    The naive approach — hand-rolled `eval()` plus manual `ForwardRef` /
    `Annotated` / `ClassVar` unwrapping — re-implements a large, fragile
    slice of `typing` internals. Instead, this builds a throwaway class
    carrying exactly one annotation and asks `get_type_hints` to resolve
    it — the exact same machinery the container already trusts, just scoped
    to a single name so ONE unresolvable annotation cannot poison any other.

    Args:
        raw:      The raw annotation. If not a `str`, PEP-563 postponed
                  evaluation is not active for this target and the value is
                  already a real object — returned unchanged (step 1 of the
                  `resolve_one` decision procedure, plan §7.2).
        globalns: Globals namespace the string is evaluated against.
        localns:  Locals namespace (typically the container's
                  `_build_localns()`, plus any PEP-695 `__type_params__`
                  seeding — see `_annotation_namespaces`).

    Returns:
        The resolved annotation object (a real type, `Annotated[...]`,
        `X | None`, etc.) — including `include_extras=True` metadata.

    Raises:
        NameError:  A name inside the annotation cannot be resolved against
                    the given namespaces. Callers use this to distinguish
                    "needs bootstrap classification" (§7.2) from "genuine
                    defect" (any other exception).
        Exception:  Anything else `get_type_hints` / the annotation's own
                    construction raises (e.g. a metadata argument whose
                    constructor divides by zero) — never swallowed here;
                    policy about what THAT means belongs to the caller.

    Example:
        >>> _eval_annotation("int | None", {}, {})
        int | None
    """
    if not isinstance(raw, str):
        return raw
    # WHY a class holder, not a function holder: ClassVar[...] is only legal
    # in a class namespace — the class-var resolution path must be able to
    # evaluate ClassVar[Inject[T]], and a function holder would reject it.
    holder = type("_AnnotationHolder", (), {"__annotations__": {"_": raw}})
    return get_type_hints(holder, globalns, localns, include_extras=True)["_"]


def _raw_annotations(target: Any) -> dict[str, Any]:
    """Return *target*'s OWN (unevaluated) annotations, without triggering evaluation.

    Args:
        target: A function, method, or class. Unwrapped via `inspect.unwrap`
                first so a `functools.wraps`-decorated callable yields the
                REAL function's annotations, not the wrapper's
                `(*args, **kwargs)` (which carries none at all).

    Returns:
        `dict[name -> raw annotation]` — strings under PEP-563, real objects
        otherwise. Never includes inherited annotations for a class (see
        Edge cases) — callers walking the MRO must do so themselves.

    Edge cases:
        - Class with no own `__annotations__`        → `{}`.
        - Class inheriting annotations from a base    → NOT included; unlike
          `cls.__annotations__` (attribute lookup, which can silently return
          a BASE class's dict when the subclass defines none of its own),
          `inspect.get_annotations` checks `__dict__` directly. This
          asymmetry is exactly why `resolve_class_annotations` must walk the
          MRO explicitly instead of reading `cls.__annotations__` once.
        - `object.__init__` (slot wrapper, no annotations at all) → `{}`.
    """
    unwrapped = inspect.unwrap(target) if callable(target) else target
    return inspect.get_annotations(unwrapped, eval_str=False)


def _annotation_namespaces(
    target: Any,
    localns: dict[str, Any],
    *,
    owner: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pick the globalns/localns pair to evaluate *target*'s annotations against.

    Per the namespace table (plan §7.1):

    | target                              | globalns                              |
    |--------------------------------------|----------------------------------------|
    | function / bound-or-unbound method   | `__globals__` (via `inspect.unwrap`)   |
    | `cls.__init__` slot wrapper          | `vars(sys.modules[cls.__module__])`    |
    | (no `__globals__` at all)           |                                        |

    Args:
        target:   The callable whose globals namespace is needed.
        localns:  Base localns (typically the container's
                  `_build_localns()`) to seed further with PEP-695 type
                  params.
        owner:    The class that declares *target* as a method, when known
                  (e.g. `Repository` for `Repository.get`). Needed because
                  the synthetic single-attribute holder in `_eval_annotation`
                  is never itself the generic owner, so PEP-695
                  `class Repository[T]`'s `T` would otherwise be unresolvable
                  even though the caller clearly meant `Repository`'s own
                  type parameter. When omitted, falls back to *target*'s own
                  `__type_params__` (present on PEP-695 generic functions).

    Returns:
        `(globalns, localns)` — `localns` is always a NEW dict (never the
        caller's `localns` object mutated in place) with `owner.__type_params__`
        (or *target*'s own) merged in under each type param's `__name__`.

    Edge cases:
        - `target` has no `__globals__` and no resolvable module (e.g. a
          dynamically-created type with a bogus `__module__`) → `{}`,
          matching `_build_localns`'s "one malformed entry costs only
          itself" precedent rather than raising.
    """
    unwrapped = inspect.unwrap(target) if callable(target) else target
    globalns = getattr(unwrapped, "__globals__", None)
    if globalns is None:
        # Slot wrappers (e.g. `object.__init__`) have no __globals__ at all —
        # fall back to the DEFINING class's module (via __objclass__ when
        # present, e.g. C-implemented methods), not the caller's module.
        objclass = getattr(unwrapped, "__objclass__", None)
        module_name = getattr(
            objclass if objclass is not None else unwrapped, "__module__", None
        )
        module = sys.modules.get(module_name) if module_name else None
        globalns = vars(module) if module is not None else {}

    seeded_localns = dict(localns)
    type_params = getattr(owner, "__type_params__", None)
    if type_params is None:
        type_params = getattr(unwrapped, "__type_params__", ())
    for tp in type_params:
        seeded_localns[tp.__name__] = tp
    return globalns, seeded_localns


def _sniff_injection_marker(
    raw: str,
    globalns: dict[str, Any],
    localns: dict[str, Any],
    *,
    allow_textual: bool,
) -> Literal["inject", "not-inject", "unknown"]:
    """Classify an UNRESOLVABLE annotation as an injection point or not (plan §7.2).

    Runs ONLY after `_eval_annotation` has already raised `NameError` for
    *raw* — it never re-attempts full evaluation, only inspects the OUTER
    shell of the parsed expression to decide what kind of thing the
    annotation was trying to say, cheaply (resolving only marker/metadata
    HEAD names, never the dependency type itself).

    Args:
        raw:           The raw (string) annotation that failed to evaluate.
        globalns:      Same namespace `_eval_annotation` was given.
        localns:       Same namespace `_eval_annotation` was given.
        allow_textual: Whether the bare-identifier textual last resort
                       (`_MARKER_TEXT_RE`) may fire. Runs only after the
                       structural (AST-shape) checks have already failed to
                       decide — see `_sniff_head`.

    Returns:
        `"inject"`     — definitely an injection-point marker (`Inject[X]`,
                         `Lazy[X]`, `Annotated[X, InjectMeta(...)]`, etc.).
        `"not-inject"` — definitely NOT one (bare type, `list[X]`, `dict[...]`,
                         `Annotated[X, "just a docstring"]`).
        `"unknown"`    — genuinely ambiguous; the caller applies its own
                         tie-break (parameter: `has_default`; class-var:
                         skip).

    Edge cases:
        - `SyntaxError` while parsing *raw* (should not happen for a real
          annotation string, but a hand-built one — e.g. in a test — might
          be malformed) → `"not-inject"`, the conservative "nothing to
          configure" answer.
    """
    try:
        tree = ast.parse(raw, mode="eval").body
    except SyntaxError:
        return "not-inject"
    return _sniff_node(tree, globalns, localns, allow_textual, raw)


def _sniff_node(
    node: ast.expr,
    globalns: dict[str, Any],
    localns: dict[str, Any],
    allow_textual: bool,
    raw_text: str,
) -> Literal["inject", "not-inject", "unknown"]:
    """Recursive AST-shape classifier — the walk described in plan §7.2's table."""
    if isinstance(node, ast.Subscript):
        return _sniff_subscript_or_call(
            node.value, node.slice, globalns, localns, allow_textual, raw_text
        )
    if isinstance(node, ast.Call):
        return _sniff_subscript_or_call(
            node.func, None, globalns, localns, allow_textual, raw_text
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # `X | None` (PEP-604 union) — recurse into the non-`None` operand(s).
        # `Live[Foo | None]`'s *inner* union is never itself the thing being
        # classified here (Live[...] is already handled as a Subscript one
        # level up) — this branch matters for a BARE union parameter like
        # `junk: LocalOnly | None`, which is never a marker regardless of
        # whether LocalOnly resolves.
        for operand in (node.left, node.right):
            if isinstance(operand, ast.Constant) and operand.value is None:
                continue
            result = _sniff_node(operand, globalns, localns, allow_textual, raw_text)
            if result != "not-inject":
                return result
        return "not-inject"
    if isinstance(node, ast.Name):
        return _sniff_head(
            ast.unparse(node), globalns, localns, allow_textual, raw_text, wrapped=False
        )
    if isinstance(node, ast.Constant) and node.value is None:
        return "not-inject"
    # Anything else (a literal, a comparison, ...) cannot possibly be a
    # providify marker — conservatively "not-inject" rather than "unknown"
    # so it never accidentally escalates a genuinely irrelevant annotation
    # into a startup failure.
    return "not-inject"


def _sniff_subscript_or_call(
    head_node: ast.expr,
    slice_node: ast.expr | None,
    globalns: dict[str, Any],
    localns: dict[str, Any],
    allow_textual: bool,
    raw_text: str,
) -> Literal["inject", "not-inject", "unknown"]:
    """Classify `head[slice]` / `head(...)` shapes — the bulk of the sniff table."""
    head_src = ast.unparse(head_node)
    # `typing.ClassVar` / `t.ClassVar` — compare only the trailing name so a
    # qualified import path doesn't defeat the special-case.
    tail_name = head_src.rsplit(".", 1)[-1]

    if tail_name == "ClassVar" and slice_node is not None:
        # ClassVar[X] → recurse into X (plan §7.2, table row 1).
        return _sniff_node(slice_node, globalns, localns, allow_textual, raw_text)

    if tail_name == "Annotated" and slice_node is not None:
        # Annotated[X, m1, m2, ...] → evaluate ONLY the metadata args. This is
        # the whole point of the Annotated special-case: X (the dependency
        # type) is almost always what's unresolvable, but the metadata
        # (InjectMeta(qualifier=...)) nearly always IS resolvable — it lives
        # in providify.type, imported at runtime by definition (the alias
        # sugar itself requires it).
        elts = slice_node.elts if isinstance(slice_node, ast.Tuple) else [slice_node]
        for meta_node in elts[1:]:
            try:
                meta_src = ast.unparse(meta_node)
                meta_val = eval(meta_src, dict(globalns), dict(localns))  # noqa: S307
            except Exception:
                # A metadata arg that itself can't be evaluated tells us
                # nothing either way — try the remaining metadata args
                # before giving up on this Annotated[...] as "not-inject".
                continue
            if isinstance(meta_val, _providify):
                return "inject"
        return "not-inject"

    # Generic `head[slice]` / `head(...)` — Inject[X], Lazy[X], Live[X],
    # Instance[X], InjectInstances[X], or an ordinary generic (Repository[X],
    # list[X], dict[K, V]). Only the HEAD is evaluated — resolving X itself
    # is neither necessary (the marker identity check doesn't need it) nor
    # safe (X is frequently the very name that made the whole annotation
    # unresolvable in the first place).
    return _sniff_head(
        head_src, globalns, localns, allow_textual, raw_text, wrapped=True
    )


def _sniff_head(
    head_src: str,
    globalns: dict[str, Any],
    localns: dict[str, Any],
    allow_textual: bool,
    raw_text: str,
    *,
    wrapped: bool,
) -> Literal["inject", "not-inject", "unknown"]:
    """Resolve and identity-check a marker HEAD name; classify its failure to resolve.

    Args:
        wrapped: True when this head was subscripted/called (`X[...]` /
                 `X(...)`) — i.e. syntactically shaped exactly like every
                 real providify marker usage. False for a bare identifier
                 used directly as an annotation (`dep: LocalOnly`).

    Design decision — why an unresolvable WRAPPED head defaults to "inject"
    while an unresolvable BARE name defaults to "unknown":
    ✅ `X[...]` / `X(...)` is precisely the shape every providify marker is
       used in (`Inject[T]`, `Lazy[T]`, `Annotated[T, ...]` handled above,
       ...) — a renamed import (`from providify import Inject as I`) still
       produces this exact shape even though the head name itself changed,
       so treating "unresolvable + wrapped" as a probable marker catches the
       renamed-alias case the plan's "textual last resort" describes,
       without needing to special-case the string `"Inject"` literally.
    ✅ Matches the invariant in the plan's Risks section: "a false 'injection
       point' costs a loud startup error (recoverable, obvious); a false
       'not an injection point' costs silent non-injection (the original
       bug)" — an ambiguous wrapped shape is exactly the situation where we
       have the least information, so leaning toward raising is correct.
    ❌ A locally-defined, unrelated generic alias used as a bare wrapper
       (`dep: SomeUnimportedGeneric[Foo] = None`) will also raise rather
       than skip. Accepted: the fix (import the type, or don't wrap it) is
       one line, and the alternative (silent non-injection) is the bug this
       whole phase exists to remove.
    A bare, unwrapped identifier carries none of that shape evidence, so it
    falls through to the caller's structural tie-break (`has_default` for
    parameters, unconditional skip for class vars) UNLESS the literal marker
    text still appears somewhere in *raw_text* (`_MARKER_TEXT_RE`) — the
    narrow textual fallback documented on that constant.

    Returns:
        `"inject"` when the head resolves to one of the providify alias
        singletons, or when it fails to resolve but the shape/text heuristic
        above fires. `"not-inject"` when it resolves to anything else.
        `"unknown"` when it fails to resolve and no heuristic fires.
    """
    try:
        head_val = eval(head_src, dict(globalns), dict(localns))  # noqa: S307
    except NameError:
        if allow_textual and (wrapped or _MARKER_TEXT_RE.search(raw_text)):
            return "inject"
        return "unknown"
    except Exception:
        # A non-NameError while resolving just the head is unusual (the head
        # is normally a bare Name/Attribute) — treat as ambiguous rather than
        # letting it escape silently; the caller's tie-break decides.
        return "unknown"
    return "inject" if isinstance(head_val, _MARKER_ALIAS_TYPES) else "not-inject"


def _resolve_one(
    raw: Any,
    name: str,
    owner_name: str,
    globalns: dict[str, Any],
    localns: dict[str, Any],
    *,
    has_default: bool | None,
    is_class_var: bool,
) -> Any:
    """Resolve exactly one parameter's / class attribute's annotation (plan §7.2's `resolve_one`).

    Implements the full ordered decision procedure:
        1. Non-str annotation           → return it (authoritative, no eval needed).
        2. `_eval_annotation` succeeds   → return it (authoritative).
        3. Non-`NameError` failure      → ALWAYS raise (a defect regardless
                                           of injection-point status).
        4. `NameError`                  → bootstrap-classify via
                                           `_sniff_injection_marker`:
             "inject"     → raise, naming *name* and the original NameError.
             "not-inject" → `_SKIP` (silently omitted, no warning — nothing
                             to configure).
             "unknown"    → structural tie-break:
                               class var           → `_SKIP`.
                               param, has_default   → `_SKIP`.
                               param, no default    → raise.

    Args:
        raw:          The raw (possibly string) annotation.
        name:         Parameter or class-attribute name, embedded in any
                      raised error.
        owner_name:   Human-readable owner (class/function) name, embedded
                      in any raised error.
        globalns:     Globals namespace for evaluation.
        localns:      Locals namespace for evaluation (already seeded with
                      any PEP-695 type params).
        has_default:  Whether the parameter has a default value. `None` for
                      class attributes (which have no default concept).
        is_class_var: True when resolving a class-level attribute rather
                      than a callable parameter — selects the "unknown"
                      tie-break (always skip vs. `has_default`-dependent).

    Returns:
        The resolved hint, or `_SKIP` when the annotation was classified as
        NOT an injection point (caller must check `is not _SKIP` before
        using the result — `_SKIP` must never leak into a hints dict).

    Raises:
        AnnotationResolutionError: When the annotation resolves to something
            that IS (or plausibly is) an injection point, or when evaluation
            failed for a reason other than an unresolvable name.
    """
    if not isinstance(raw, str):
        return raw
    try:
        return _eval_annotation(raw, globalns, localns)
    except NameError as exc:
        name_error: Exception = exc
    except Exception as exc:
        # Step 3: any non-NameError failure is a genuine defect — e.g. a
        # metadata argument whose constructor divides by zero — regardless
        # of whether this parameter would even have been an injection
        # point. Never silenced; the original exception is chained.
        raise AnnotationResolutionError(owner_name, exc, param_name=name) from exc

    classification = _sniff_injection_marker(raw, globalns, localns, allow_textual=True)
    if classification == "inject":
        raise AnnotationResolutionError(
            owner_name, name_error, param_name=name
        ) from name_error
    if classification == "not-inject":
        return _SKIP

    # classification == "unknown" — genuinely ambiguous (plan §7.2, step 4c).
    if is_class_var:
        # Class-level annotations have no defaults; a class attribute is an
        # injection point only if it carries providify metadata, so
        # ambiguity resolves to "skip" — the sniff already tried the
        # textual/shape last resort above and came up empty.
        return _SKIP
    if has_default:
        # `_build_localns()` maps every registered binding's name; if the
        # name STILL fails to resolve after it is applied, no binding of
        # that name exists, so `_resolve_hint_sync` would have returned
        # `_UNRESOLVED` for it anyway — skipping here changes nothing that
        # today's behaviour didn't already produce (minus the spurious
        # whole-signature hint loss Phase 7 removes).
        return _SKIP
    raise AnnotationResolutionError(
        owner_name, name_error, param_name=name
    ) from name_error


def resolve_params(
    target: Any,
    owner_name: str,
    globalns: dict[str, Any],
    localns: dict[str, Any],
) -> dict[str, Any]:
    """Resolve every injectable parameter of *target*, one at a time.

    Args:
        target:     The callable whose parameters are resolved — typically
                    `cls.__init__` or a provider function. Unwrapped via
                    `inspect.unwrap` before both the signature and the raw
                    annotations are read, so a `functools.wraps`-decorated
                    provider resolves the REAL signature.
        owner_name: Human-readable name embedded in any raised error.
        globalns:   Globals namespace (see `_annotation_namespaces`).
        localns:    Locals namespace (typically the container's
                    `_build_localns()`, already seeded with type params).

    Returns:
        `dict[param_name -> resolved hint]`. Never contains `"return"` (this
        walks `inspect.signature(...).parameters`, which never includes it).
        Parameters with no annotation, `self`/`cls`, and `*args`/`**kwargs`
        are omitted. Parameters whose annotation was classified as NOT an
        injection point are also omitted — silently, no warning.

    Raises:
        AnnotationResolutionError: A parameter's annotation resolves to
            something that IS (or plausibly is) an injection point but
            cannot be evaluated — see `_resolve_one`.

    Edge cases:
        - `__init__` not inspectable (C-extension type) → `inspect.signature`
          raises `ValueError`/`TypeError` → `{}` (mirrors today's
          `init_params = set()` fallback elsewhere in the container).
        - `*args` / `**kwargs`  → skipped; never injection points.
        - `self` / `cls`        → skipped.
    """
    try:
        fn = inspect.unwrap(target)
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return {}

    raw = _raw_annotations(target)
    resolved: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        if param_name not in raw:
            continue
        has_default = param.default is not inspect.Parameter.empty
        hint = _resolve_one(
            raw[param_name],
            param_name,
            owner_name,
            globalns,
            localns,
            has_default=has_default,
            is_class_var=False,
        )
        if hint is not _SKIP:
            resolved[param_name] = hint
    return resolved


def resolve_class_annotations(cls: type, localns: dict[str, Any]) -> dict[str, Any]:
    """Resolve every class-level attribute annotation across *cls*'s MRO, one at a time.

    Args:
        cls:     The class whose (and whose ancestors') class-level
                 annotations are resolved.
        localns: Locals namespace (typically the container's
                 `_build_localns()`).

    Returns:
        `dict[attr_name -> resolved hint]`, in reverse-MRO declaration order
        (base classes first) with more-derived classes overriding entries a
        base class declared under the same name — mirroring
        `get_type_hints`' own override order. An attribute redeclared by a
        subclass but classified as "skip" there still overrides (removes)
        the base class's resolved entry, matching "the most-derived
        declaration wins", never a stale inherited value.

    Edge cases:
        - `cls` (or a base) has no annotations at all → contributes nothing.
        - An attribute carrying no providify marker → resolved and included
          if it evaluates cleanly (callers filter to providify metadata
          themselves — this function does not gatekeep on that); if
          unresolvable and not classified as an injection point, silently
          omitted.
        - Inherited class var declared in another module → evaluated against
          THAT module's globals, not `cls`'s own module (see the namespace
          table, plan §7.1).
        - PEP-695 generic owner → its own `T` resolves via
          `klass.__type_params__` seeding, per defining class.

    Raises:
        AnnotationResolutionError: A class attribute's annotation resolves
            to something that IS (or plausibly is) an injection point but
            cannot be evaluated — see `_resolve_one`.
    """
    resolved: dict[str, Any] = {}
    # reversed(cls.__mro__): base classes first, so a subclass's own
    # annotation for the same name is processed LAST and wins — matching
    # get_type_hints' documented override order.
    for klass in reversed(cls.__mro__):
        if klass is object:
            continue
        raw = _raw_annotations(klass)
        if not raw:
            continue

        module = sys.modules.get(klass.__module__)
        globalns = vars(module) if module is not None else {}
        seeded_localns = dict(localns)
        for tp in getattr(klass, "__type_params__", ()):
            seeded_localns[tp.__name__] = tp

        for attr_name, raw_ann in raw.items():
            # A more-derived class redeclaring `attr_name` always shadows a
            # base class's entry — even if this declaration resolves to
            # "skip" — never leave a stale inherited value behind.
            resolved.pop(attr_name, None)
            hint = _resolve_one(
                raw_ann,
                attr_name,
                f"{klass.__name__} (class-level annotations)",
                globalns,
                seeded_localns,
                has_default=None,
                is_class_var=True,
            )
            if hint is not _SKIP:
                resolved[attr_name] = hint
    return resolved
