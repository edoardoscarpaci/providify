# Plan 001 — Annotation-resolution failure policy: Tier 1 (class-var injection), Tier 2 (validators), and per-parameter resolution

## Goal
Finish the `get_type_hints()` broad-`except` cleanup started in `_collect_kwargs_sync/_async`.
After Phases 1-6 (**release A**): class-var injection uses the same NameError-only tolerant helper
(never a bare `except Exception`), the two scope-leak validators **raise** instead of silently
reporting a clean container, and the two genuinely-optional sites log a warning instead of
swallowing in silence. A new `AnnotationResolutionError(ValidationError)` names the exact binding
that could not be type-resolved.
After Phase 7 (**release B, follow-up**): annotations are evaluated one parameter / one attribute at
a time, so a failure is attributable to a single injection point — and `NameError` tolerance is
deleted from the codebase entirely.

## Release split & ordering (read this first)
- **Phases 1-6 = release A.** Small; finishes an already-shipped bug fix. Ship independently.
- **Phase 7 = release B (next minor).** Depends on release A: it reuses `AnnotationResolutionError`,
  assumes the validators already raise, and *relaxes* part of A's strictness (unresolvable
  annotations on non-injected parameters stop failing anything). Do **not** start it while Phases
  1-6 are in flight — landing both at once makes regressions unattributable.
- Release A's CHANGELOG therefore states "the next release removes most of these failures"
  (Step 19); release B's CHANGELOG points back at it (Step 39).

## Non-goals
- **No `strict_annotations=` / any strictness flag** — in either release. Permanent design decision.
- Release A does not change `_get_provider_return_type`'s call signature (`get_type_hints(fn)` stays
  without `localns` / `include_extras`) — only its logging. Phase 7 revisits it.
- Do not touch the already-landed uncommitted fix in `_collect_kwargs_sync/_async` or
  `_build_localns` / `ProviderBinding` return-type resolution; this plan builds on top of them.
- Phase 7 does **not** change *which* dependencies get injected for annotations that resolve today.
  It changes only *when a failure is fatal* and *how precisely it is reported*. Any difference in
  resolved hints for a resolvable annotation is a port bug — Step 34's parity table exists to
  prove there is none.

## Design

Three failure policies, one per tier, each with exactly one implementation:

```
                      get_type_hints() fails
                               │
        ┌──────────────────────┼──────────────────────────┐
        │                      │                          │
   TIER 1 injection       TIER 2 validation          TIER 3 enrichment
   (what gets injected)   (is the graph legal?)      (graph / reporting)
        │                      │                          │
 _resolve_hints_or_warn   _resolve_hints_or_raise    inline try/except
  NameError → {} + WARN    ANY exception →            + logger.warning
  other      → propagate   AnnotationResolutionError
        │                      │                          │
 _inject_class_vars_sync  _collect_class_var_hints   _collect_dependencies
 _inject_class_vars_async _check_scope_violation     _get_provider_return_type
 (_collect_kwargs_* —     _check_provider_scope_     _get_dependencies (wraps the
  already done)            violation                  Tier-2 helper back to lenient)
```

Phase 7 collapses the left two columns into a single per-parameter resolver and deletes the
`NameError → {} + WARN` branch outright.

### Two helpers, side by side in `container.py`

`_resolve_hints_or_warn` already exists at `providify/container.py:1684-1726`. Its first parameter
is typed `fn: Callable[..., Any]`, but `get_type_hints` accepts a class equally well — widen it to
`target: Callable[..., Any] | type` so the class-var path can share it. No behavioural change for
the existing two callers (`container.py:1755`, `container.py:1814`, both positional).

Add `_resolve_hints_or_raise(target, owner_name)` immediately after it, so the two policies are
readable as a pair:

```python
def _resolve_hints_or_raise(self, target, owner_name: str) -> dict[str, Any]:
    try:
        return get_type_hints(target, include_extras=True, localns=self._build_localns())
    except Exception as exc:
        raise AnnotationResolutionError(owner_name, exc) from exc
```

`include_extras=True` is mandatory — the validators inspect `Annotated` args for
`InjectMeta` / `LazyMeta` / `LiveMeta` / `InstanceMeta` before stripping the wrapper
(`container.py:3091-3099`, `container.py:3204-3212`).

### Why the validators must also gain `localns`

`_check_scope_violation` currently calls
`get_type_hints(binding.implementation.__init__, include_extras=True)` at `container.py:3072`
— **without `localns`**. Its provider twin already passes `localns=self._build_localns()`
(`container.py:3190`, per CHANGELOG line 64). So today, under `from __future__ import annotations`,
any class whose `__init__` references a locally-defined type raises `NameError` → swallowed →
`return leaks` (empty) → binding validates clean. That is the exact "validator is a liar" failure.
Routing this site through `_resolve_hints_or_raise` fixes both halves at once: the names now
resolve, and anything that still fails is loud.

⚠️ Consequence: real scope leaks that were hidden behind the missing `localns` will start failing.
This is the point of the change, but it means existing tests may go red — see *Risks*.

### `_collect_class_var_hints` has two callers on different tiers

`container.py:2983` is called from `_check_scope_violation` (`:3084`, Tier 2) **and** from
`_get_dependencies` (`:3403`, Tier 3 graph/`describe()`). The helper itself becomes strict
(`_resolve_hints_or_raise`); the *lenient* caller absorbs it:

```python
# _get_dependencies, container.py:3403
try:
    class_var_hints = self._collect_class_var_hints(binding.implementation)
except AnnotationResolutionError as exc:
    logger.warning("Dependency graph for '%s' is incomplete: %s", ..., exc)
    class_var_hints = {}
```

Policy lives at the call site that is allowed to be lenient — not as a `strict=` parameter on the
helper.

### New exception

`providify/exceptions.py`, after `ScopeViolationDetectedError`:

```python
class AnnotationResolutionError(ValidationError):
    def __init__(self, owner_name: str, cause: Exception) -> None: ...
```

Subclassing `ValidationError` matters: `validate_all()` (`container.py:3318-3330`) catches
`Exception` and appends `str(e)`, so an unresolvable annotation surfaces as a *violation message*
and leaves `_validated = False`. "I don't know" is reported as "not clean", never as "you're fine".

Message must name the owner, the original exception type + text, and the fix:

```
Cannot resolve type hints for 'OtelConfiguration.__init__' (NameError: name 'LocalDep' is not
defined). Scope-leak validation cannot run for it, so the container refuses to start rather than
report a clean bill of health it cannot prove.
Fix: import the annotated type at runtime instead of under TYPE_CHECKING, or move locally-defined
types to module level.
```

Phase 7 extends the constructor with an optional `param_name` so the same class can say
`'OtelConfiguration.__init__' parameter 'tracer'`.

### Alternatives considered
- **`strict_annotations=` flag on `DIContainer`** — rejected. ✅ zero breakage for downstream.
  ❌ default-lenient means nobody flips it and the bug ships unchanged; default-strict makes the
  flag a permanent escape hatch for a false positive that should be *removed* instead. These are
  startup-time wiring failures on a developer's machine, not runtime degradation; configurable
  leniency is for retries/circuit breakers, not for wiring correctness.
- **Let the raw `NameError` propagate from the validators (delete the try/except, no wrapper)** —
  rejected. ✅ smallest diff, zero new API. ❌ the message says `name 'X' is not defined` with no
  indication of *which binding* failed to validate, and `validate_all()` would collect that opaque
  string; also indistinguishable from a genuine user-code `NameError`.
- **`_collect_class_var_hints(cls, *, strict: bool)`** — rejected. ✅ one call site to change.
  ❌ re-introduces a leniency knob one layer down; the two callers differ in *policy*, and policy
  belongs at the caller.
- **Make Tier 1 raise on `NameError` too, in release A** — rejected. ✅ maximally fail-fast.
  ❌ `get_type_hints` is all-or-nothing over the whole class: one unresolvable non-injected
  annotation would break construction of a class whose injected vars are all fine. Tier 1 stays
  consistent with `_collect_kwargs_*` until Phase 7 removes the false positive at the root.

---

## Steps

### Phase 1 — exception type

1. [x] `providify/exceptions.py` — add `class AnnotationResolutionError(ValidationError)` after
   `ScopeViolationDetectedError` (`:122-131`). Constructor `(owner_name: str, cause: Exception)`,
   stores both as attributes, builds the message shown in *Design*. Full docstring
   (summary / Attributes / why raising beats returning `[]`).
2. [x] `providify/__init__.py` — export `AnnotationResolutionError`: add to `__all__` next to
   `"ScopeViolationDetectedError"` (`:41-42`) and to the import block (`:114-115`).

### Phase 2 — failing tests (TDD; all in one new file)

3. [ ] `tests/test_annotation_resolution.py` — new file, shaped like
   `tests/test_forward_ref_provider.py`: `from __future__ import annotations`, module docstring
   explaining the three tiers and why each policy differs, module-level domain types, one test
   class per tier. Add the shared selective-boom helper:

   ```python
   def _boom_for(monkeypatch, target, exc):
       """Make get_type_hints raise `exc` for `target` only, real behaviour otherwise."""
       import providify.container as container_module
       real = container_module.get_type_hints
       def fake(t, *args, **kwargs):
           if t is target:
               raise exc
           return real(t, *args, **kwargs)
       monkeypatch.setattr(container_module, "get_type_hints", fake)
   ```
   Selectivity is required: a blanket boom fires in `_collect_kwargs_sync` (which receives
   `cls.__init__`) before the class-var path is ever reached. Targeting the class object itself
   isolates the class-var path.

4. [ ] `tests/test_annotation_resolution.py::TestClassVarInjectionHintFailure` — Tier 1, failing
   until Phase 3:
   - `test_unexpected_hint_error_propagates_sync` — `_boom_for(..., Consumer, AttributeError("'str' object has no attribute '__name__'"))`;
     `container.get(Consumer)` raises `AttributeError`. (Today: swallowed, instance returned with
     the class var unset.)
   - `test_unexpected_hint_error_propagates_async` — same via `await container.aget(Consumer)`.
   - `test_unresolvable_name_is_tolerated_and_logged_sync` — boom with
     `NameError("name 'LocallyDefined' is not defined")`; instance is constructed, a
     `providify.container` WARNING mentions the class name, and `hasattr(instance, "dep") is False`
     (documents the tolerated gap that Phase 7 closes — **this test is rewritten in Step 37**).
   - `test_unresolvable_name_is_tolerated_and_logged_async` — async mirror.
   - `test_class_var_injection_still_works_sync` / `_async` — positive control, no monkeypatch:
     `var: Inject[Dep]` (and a `ClassVar[Inject[Dep]]` variant) is set on the instance.

5. [ ] `tests/test_annotation_resolution.py::TestValidatorNeverSilentlyPasses` — Tier 2:
   - `test_class_init_hint_failure_fails_validation` — boom (`NameError`) for
     `Impl.__init__`; `container.validate_bindings()` raises `AnnotationResolutionError` whose
     message contains `Impl`. (Today: passes clean.)
   - `test_class_var_hint_failure_fails_validation` — boom for the class object itself (hits
     `_collect_class_var_hints`); `validate_bindings()` raises `AnnotationResolutionError`.
   - `test_provider_hint_failure_fails_validation` — singleton `@Provider`, boom for
     `binding.fn`; `validate_bindings()` raises and the message names the provider function.
   - `test_dependent_provider_short_circuits_before_hint_resolution` — a **DEPENDENT**-scoped
     provider with a boom-ing annotation validates clean, because `container.py:3177` returns
     before hint resolution. Locks in the deliberate exemption.
   - `test_validate_all_reports_annotation_failure_as_violation` — `validate_all()` returns a
     non-empty list containing the owner name **and** `container.is_valid is False`.
   - `test_regression_locally_defined_init_annotation_is_now_validated` — the headline case, no
     monkeypatch: a `SINGLETON` class taking `dep: Inject[ReqScoped]` where both classes are
     defined *inside the test function* and both are bound. Must raise
     `LiveInjectionRequiredError`. Today the missing `localns` at `container.py:3072` makes
     `get_type_hints` raise → swallowed → **green light on a real leak**.
     (If `_build_localns` turns out not to register the implementation class name, bind both
     classes explicitly so their names enter the namespace — verify before assuming.)

6. [ ] `tests/test_annotation_resolution.py::TestOptionalEnrichmentWarns` — Tier 3:
   - `test_graph_survives_class_var_hint_failure` — boom for the class object; `container.describe()`
     (or `_get_dependencies(binding)`) returns normally and logs a WARNING naming the class.
   - `test_collect_dependencies_failure_warns` — boom for `fn`; `_collect_dependencies` returns `[]`
     and logs a WARNING.
   - `test_provider_return_type_failure_warns` — boom for a provider fn; `_get_provider_return_type`
     returns `None` and logs a WARNING.

### Phase 3 — Tier 1 implementation

7. [x] `providify/container.py:1684-1688` — rename `_resolve_hints_or_warn`'s first parameter
   `fn` → `target`, widen its type to `Callable[..., Any] | type`, update the docstring
   (Args + a line noting it is shared by the kwargs path and the class-var path).
8. [x] `providify/container.py:2290-2302` (`_inject_class_vars_sync`) — replace the
   `try/except Exception: hints = {}` block with
   `hints = self._resolve_hints_or_warn(cls, f"{cls.__name__} (class-level annotations)")`.
   Keep the `if not hints: return` early exit. Update the docstring `Edge cases` line
   (`:2288` currently reads "get_type_hints raises → swallowed; no class-var injection") to:
   unresolvable name → warning + no class-var injection; any other exception → propagates.
   Add a `Raises:` entry.
9. [x] `providify/container.py:2349-2357` (`_inject_class_vars_async`) — identical change; its
   docstring says "Edge cases: same as `_inject_class_vars_sync`", so only add the `Raises:` line.

### Phase 4 — Tier 2 implementation

10. [x] `providify/container.py` — add `_resolve_hints_or_raise(target, owner_name)` immediately
    after `_resolve_hints_or_warn` (i.e. after `:1726`), body as in *Design*, with a full docstring
    stating the invariant: *a validator that cannot read the annotations must never return an empty
    result set, because empty means "clean".*  Import `AnnotationResolutionError` at the top of
    `container.py` alongside the other exception imports.
11. [x] `providify/container.py:3006-3015` (`_collect_class_var_hints`) — replace the
    `try/except Exception: return {}` with
    `hints = self._resolve_hints_or_raise(cls, f"{cls.__name__} (class-level annotations)")`.
    Update docstring: `Returns` (drop "Empty dict if get_type_hints raises", `:2997`),
    `Edge cases` (`:3001`), and add `Raises: AnnotationResolutionError`.
12. [x] `providify/container.py:3068-3076` (`_check_scope_violation`) — replace with
    `init_hints = self._resolve_hints_or_raise(binding.implementation.__init__, f"{binding.implementation.__name__}.__init__")`.
    **This also adds the missing `localns`** — leave a short WHY comment saying so. Add a `Raises:`
    section to the docstring covering `LiveInjectionRequiredError` (currently undocumented) and
    `AnnotationResolutionError`.
13. [x] `providify/container.py:3183-3197` (`_check_provider_scope_violation`) — replace with
    `fn_hints = self._resolve_hints_or_raise(binding.fn, f"@Provider({binding.fn.__name__})")`.
    Update the `Edge cases` line at `:3171` ("get_type_hints raises → returns [] (defensive)") and
    add `AnnotationResolutionError` to `Raises`.
14. [x] `providify/binding.py:208-225` (`ClassBinding.validate`) and `providify/binding.py:526-553`
    (`ProviderBinding.validate`) — add `AnnotationResolutionError` to each `Raises:` block. No code
    change; the exception propagates through untouched.

### Phase 5 — Tier 3 implementation

15. [x] `providify/container.py:1876-1881` (`_collect_dependencies`) — keep the broad catch, but
    `except Exception as exc:` + `logger.warning("Dependency graph for '%s' is incomplete — cannot resolve type hints (%s: %s).", getattr(fn, "__qualname__", fn), type(exc).__name__, exc)` before
    `hints = {}`. Update `Edge cases` (`:1872`) to "…swallowed **and logged**".
16. [x] `providify/container.py:2549-2553` (`_get_provider_return_type`) — same treatment: warn with
    the function name, then `return None`. Update the docstring's "suppresses all exceptions" wording.
17. [x] `providify/container.py:3403` (`_get_dependencies`) — wrap the `_collect_class_var_hints`
    call in `try/except AnnotationResolutionError` → `logger.warning(...)` → `class_var_hints = {}`,
    with a comment explaining the tier downgrade (graph/`describe()` is reporting, not wiring).
    Note it in the method's `Edge cases`.

### Phase 6 — suite triage & docs (end of release A)

18. [x] Run the full suite. Triage every new failure into exactly one bucket:
    (a) a **real** scope leak previously hidden by the missing `localns` at `container.py:3072`
        → fix the *test fixture* (`Live[T]` / `Instance[T]`), never the validator;
    (b) a test whose annotations genuinely cannot resolve → move the offending type to module level
        (same fix as CHANGELOG line 63);
    (c) an unexpected regression → stop and reconsider the design.
    Record the bucket for each fixed test in the commit message.
19. [x] `CHANGELOG.md` — under `## [Unreleased]`:
    - **Changed / ⚠️ breaking**: scope-leak validation now raises `AnnotationResolutionError`
      (a `ValidationError`) when a binding's annotations cannot be evaluated, instead of reporting
      the binding clean. Include the migration note (import annotated types at runtime rather than
      under `TYPE_CHECKING`) and state that per-parameter resolution in the next release will remove
      most of these failures.
    - **Fixed**: `_check_scope_violation` now passes `localns=self._build_localns()` — class
      scope-leak detection previously produced false negatives for locally-defined `__init__`
      annotation types (the class-side twin of the fix already logged at line 64).
    - **Fixed**: `_inject_class_vars_sync` / `_inject_class_vars_async` no longer swallow every
      exception from `get_type_hints()`; only `NameError` is tolerated (and logged), matching
      `_collect_kwargs_*`. Previously an unrelated error left annotated class attributes unset, and
      the failure surfaced much later as an `AttributeError` in unrelated code.
    - **Added**: `AnnotationResolutionError` exported from `providify`.
20. [x] Docs — `grep -n ScopeViolationDetectedError docs/agents/*.md README.md SKILL.md` and add
    `AnnotationResolutionError` to whichever error tables/lists already enumerate the validation
    exceptions (at minimum `docs/agents/injection-cheatsheet.md`). One line each: what triggers it,
    what to do about it.

---

# Phase 7 — per-parameter annotation resolution (release B)

## Goal of this phase
Evaluate each parameter's / each class attribute's annotation **independently**, so one unresolvable
annotation can no longer destroy the hints of every other injection point on the same callable.
Then: unresolvable annotation on something that IS an injection point → raise, naming the exact
parameter; unresolvable on something that is not → skip silently, no warning, nothing to configure.
`_resolve_hints_or_warn` and its `NameError` branch are deleted.

## Why this is the root fix
`get_type_hints(fn)` is all-or-nothing over an entire signature. One `TYPE_CHECKING`-only import on
a parameter nobody injects nukes the hints for every other parameter — that is the *only* legitimate
reason the broad `except` ever existed, and the only reason release A still tolerates `NameError` in
the injection path. Remove the all-or-nothing behaviour and the false positive disappears with it,
taking the tolerance with it.

## 7.1 The evaluation primitive — synthetic single-annotation holder

The naive version of this phase hand-rolls `eval()` plus `ForwardRef` / `Annotated` / `ClassVar`
unwrapping. That is a large amount of `typing`-internals surface to re-implement and keep correct
across 3.12 / 3.13 / 3.14. There is a much smaller way to get **per-annotation isolation while
keeping `get_type_hints`' exact semantics**: call `get_type_hints` on a throwaway class carrying
exactly one annotation, with the globalns/localns passed explicitly.

```python
def _eval_annotation(raw: Any, globalns: dict[str, Any], localns: dict[str, Any]) -> Any:
    """Evaluate ONE annotation in isolation, with full typing semantics."""
    if not isinstance(raw, str):
        return raw                      # module without PEP 563 — already an object
    holder = type("_AnnotationHolder", (), {"__annotations__": {"_": raw}})
    return get_type_hints(holder, globalns, localns, include_extras=True)["_"]
```

Why a **class** holder rather than a function holder: `ClassVar[...]` is only legal in a class
namespace, and the class-var path must evaluate `ClassVar[Inject[T]]`. A class holder accepts both
forms; a function holder rejects `ClassVar`.

This gives us for free, per annotation, everything the request listed as ❌ hand-rolling cost:
- **string / `ForwardRef` eval** against the globalns/localns we pass (including nested refs such as
  `Annotated["Foo", InjectMeta()]` and `Repository["User"]`);
- **`include_extras=True`** — `Annotated` markers survive, which the whole container depends on;
- **`ClassVar[...]`** — legal and preserved, then stripped by the existing
  `_unwrap_classvar` (`providify/type.py:1228`);
- **`Optional` / `T | None`** — evaluated identically to today. Note: implicit-Optional-from-`None`-
  default was **removed in Python 3.11** and `requires-python >= 3.12`, so no implicit wrapping
  exists to reproduce. The container's `optional=True` behaviour comes from the *pipe union inside
  the alias* (`Live[T | None]`, per `providify/type.py:290-294`), which is a property of the
  evaluated object and is unaffected;
- **PEP 563** — handled by construction: raw annotations are strings and we eval them one at a time.

Getting the RAW (unevaluated) annotations must not itself trigger evaluation:
use `inspect.get_annotations(obj, eval_str=False)` — the documented, version-safe accessor (it
handles the 3.14 `__annotate__` machinery and, for classes, does **not** inherit from bases).

**Namespaces per target:**
| target | globalns | localns |
|---|---|---|
| function / provider | `getattr(inspect.unwrap(fn), "__globals__", {})` | `container._build_localns()` + `fn.__type_params__` |
| bound/unbound method | `fn.__func__.__globals__` (via `inspect.unwrap`) | same |
| `cls.__init__` slot wrapper (no `__globals__`) | `vars(sys.modules.get(cls.__module__, None)) or {}` | same |
| class attribute on `Base` in the MRO | `vars(sys.modules.get(Base.__module__))` — **the defining class's module, not the subclass's** | same + `Base.__type_params__` |

PEP-695 generics (`class Repository[T]`, already used at
`tests/test_forward_ref_provider.py:62`) lose their type params in the holder trick, because the
holder is not the generic class. Seed `localns` with
`{tp.__name__: tp for tp in getattr(owner, "__type_params__", ())}` for the owning callable/class.

### Alternatives considered (7.1)
- **Hand-rolled `eval(compile(ann, ...))` + manual `ForwardRef`/`Annotated`/`ClassVar` unwrapping** —
  rejected. ✅ no dependency on `get_type_hints` quirks. ❌ re-implements a large slice of `typing`
  internals (nested forward refs inside `Annotated`/generic args are the hard part) and must be
  re-verified on every Python release; guaranteed to drift from `get_type_hints` semantics, which is
  precisely what Step 34's parity table forbids.
- **`typing.ForwardRef(...)._evaluate(...)`** — rejected. ✅ exactly one call, no holder object.
  ❌ private API whose signature changed between 3.12 and 3.13 (`type_params`, keyword-only
  `recursive_guard`); breaks silently on upgrade.
- **Keep `get_type_hints(fn)` and retry with the failing name stubbed out** — rejected.
  ❌ requires parsing the `NameError` message to learn the name, is O(failures) re-evaluations, and
  cannot attribute the failure to a parameter at all.

## 7.2 The bootstrap problem (the crux)

To apply the policy "raise if it is an injection point, skip if it is not", we must classify an
annotation we could not evaluate. Ordered decision procedure, per parameter:

```
resolve_one(owner, param_name, raw, has_default):
  1. raw is not a str (no PEP 563)      → hint = raw            → classify on the OBJECT (authoritative)
  2. _eval_annotation(raw) succeeds     → hint                  → classify on the OBJECT (authoritative)
  3. eval raised something != NameError → RAISE always
        (a TypeError/AttributeError/ZeroDivisionError inside an annotation is a defect
         regardless of whether we would have injected it — never silence it)
  4. eval raised NameError              → BOOTSTRAP CLASSIFY the raw text:
     4a. AST head sniff  → "definitely an injection point"  → RAISE, naming param + missing name
     4b. AST head sniff  → "definitely NOT an injection point" → skip silently
     4c. ambiguous → structural tie-break:
            parameter path : has_default → skip silently ; no default → RAISE
            class-var path : textual last resort (below), else skip silently
```

**4a/4b — AST head sniff.** Parse once with `ast.parse(raw, mode="eval")` and walk the *outer shell*
only:

```
ClassVar[X]                 → recurse into X
Annotated[X, m1, m2, ...]   → evaluate ONLY m1.. (the metadata args, which are almost always
                              resolvable even when X is not) → any isinstance(_, _providify) ⇒ 4a
Inject[X] / Inject(X, ...)  → resolve the HEAD NAME only (a bare Name/Attribute node, cheap);
Lazy / Live / Instance /      identity-check the resolved object against the alias singletons in
InjectInstances               providify.type (_InjectedAlias, _InjectedInstancesAlias, Lazy, Live,
                              Instance) ⇒ 4a
anything else whose head     ⇒ 4b  (bare type, list[X], dict[...], Annotated with no _providify)
resolves
head itself unresolvable     ⇒ 4c
```

The head name is the *marker*, not the dependency type, so it is resolvable in exactly the cases we
care about: a module that writes `dep: Inject[Foo]` must import `Inject` at runtime (under
`TYPE_CHECKING` `Inject` is a `type` alias statement and the code would already be broken for
everyone else).

**4c parameter tie-break — why `has_default` is the right and *sufficient* discriminator.**
`localns` is `_build_localns()`, which maps **every registered binding's `__name__`** (interface,
implementation, generic origins and args — `container.py:1633-1681`). Therefore:

> If a name still fails to resolve *after* `localns` is applied, no registered binding carries that
> name, so `_resolve_hint_sync` would have returned `_UNRESOLVED` for it anyway
> (`container.py:1779`).

Which makes the tie-break provably behaviour-preserving:
- **has default** → today `_collect_kwargs_sync` skips the param (`container.py:1781`) and the
  default applies. Per-param skips it too. Identical outcome, minus today's spurious
  container-wide hint loss.
- **no default** → today the call fails with either `LookupError: Cannot resolve '<p>' in '<owner>'`
  or (when the whole-signature hints were nuked) a bogus
  `TypeError: missing N required positional arguments`. Per-param raises
  `AnnotationResolutionError` naming the parameter and the unresolvable name. Strictly better
  message, same "this cannot work" verdict.

**4c class-var fallback.** Class-level annotations have no defaults, and a class-level annotation is
an injection point *only if* it carries providify metadata (`container.py:2318`). So ambiguity
resolves to *skip*, with one textual last resort for renamed imports
(`from providify import Inject as I`): if the raw text matches
`\b(Inject|InjectInstances|Lazy|Live|Instance|InjectMeta|LazyMeta|LiveMeta|InstanceMeta)\b`
treat it as 4a. Documented explicitly as heuristic-of-last-resort, reached only when the head name
did not resolve at all.

**Rejected bootstrap mechanisms:**
- **"Look the binding up by name"** (check whether any binding's `__name__` equals the unresolved
  name) — rejected as a *mechanism*, kept as the *justification* above. ❌ It is already subsumed:
  `_build_localns()` is in `localns`, so any such name would have resolved in step 2; reaching step
  4 proves no binding matches.
- **"Treat every unresolvable annotation as an injection point"** — rejected. ❌ That is release A's
  behaviour with extra steps; it keeps the exact false positive this phase exists to remove.
- **"Treat none of them as injection points (always skip + warn)"** — rejected. ❌ Reintroduces
  silent non-injection for `store: Inject[Unresolvable] = None`, the Tier-1 bug in a new costume.

## 7.3 Module layout

New module `providify/_annotations.py` holding **pure functions** (no container, no I/O):
`_eval_annotation`, `_raw_annotations`, `_annotation_namespaces`, `_sniff_injection_marker`,
`resolve_params`, `resolve_class_annotations`. The container supplies `localns` and the cache.
✅ unit-testable without constructing a container; ✅ keeps `container.py` (already >3800 lines) from
growing another ~200. ❌ one more module and an import edge (`container` → `_annotations` →
`type`; no cycle, `_annotations` must not import `container`).
Rejected alternative: put it all in `container.py` next to `_build_localns` — ❌ file size, and the
logic is genuinely container-independent.

## 7.4 Caching

```python
# DIContainer.__init__, next to _localns_cache (container.py:376)
self._hints_cache: dict[Any, dict[str, Any]] = {}
```
- **Key**: the callable / class object itself (`fn`, `cls`, `cls.__init__`). *Not* `id(fn)` — ids
  are recycled after GC and would serve another callable's hints. *Not* a `WeakKeyDictionary` —
  `object.__init__` and other slot wrappers are not weakref-able. Strong refs are acceptable
  because the cache is per-container and every key is already reachable from `self._bindings`;
  the cache is cleared on every binding mutation anyway.
- **Value**: the resolved `dict[name → hint]` with `"return"` already excluded (the parameter path
  iterates `inspect.signature(fn).parameters`, which never contains it — so the existing
  `hints.pop("return", None)` lines at `container.py:1757`, `:1816`, `:1883`, `:3078`, `:3199`
  become dead and are deleted). Callers get `dict(cached)` — a shallow copy — so that a caller
  mutating its result cannot corrupt the cache. This is not hypothetical: today's callers *do*
  `hints.pop(...)` on the returned dict.
- **Only successes are cached.** Failures re-raise fresh each time: they are rare, and a cached
  exception object would carry a stale traceback.
- **Invalidation**: resolved hints depend on `_build_localns()`, so the two caches must die
  together. Today `_localns_cache = None` is written at six places
  (`container.py:587`, `:626`, `:642`, `:3805`, `:3881`, and `scanner.py:211` — which reaches into
  the container's private attribute). Introduce `DIContainer._invalidate_type_caches()` that nulls
  `_localns_cache` and clears `_hints_cache`, and replace all six sites (including making
  `scanner.py:211` call the method instead of poking the attribute). One site to add the next cache
  to; no seventh-site-forgotten bug.
- **Thread safety**: no lock. Every value is a pure function of (target, bindings); a race
  recomputes identical data, and dict item assignment is atomic under the GIL. The only mutable
  hazard — a caller editing a returned dict — is removed by the copy-on-read above. Document this
  reasoning in the docstring (the codebase documents thread/async safety per method).
- **Async safety**: no awaits inside the resolver; the sync and async paths share one cache.

## 7.5 Migration — call sites and the removal of `NameError` tolerance

| site | now (after release A) | after Phase 7 |
|---|---|---|
| `container.py:1755` `_collect_kwargs_sync` | `_resolve_hints_or_warn(fn, ...)` | `self._resolve_params(fn, owner_name)` |
| `container.py:1814` `_collect_kwargs_async` | `_resolve_hints_or_warn(fn, ...)` | same |
| `container.py:2290` `_inject_class_vars_sync` | `_resolve_hints_or_warn(cls, ...)` | `self._resolve_class_annotations(cls)` |
| `container.py:2349` `_inject_class_vars_async` | `_resolve_hints_or_warn(cls, ...)` | same |
| `container.py:3006` `_collect_class_var_hints` | `_resolve_hints_or_raise(cls, ...)` | `self._resolve_class_annotations(cls)` (strict by construction) |
| `container.py:3072` `_check_scope_violation` | `_resolve_hints_or_raise(impl.__init__, ...)` | `self._resolve_params(impl.__init__, ...)` |
| `container.py:3190` `_check_provider_scope_violation` | `_resolve_hints_or_raise(fn, ...)` | `self._resolve_params(fn, ...)` |
| `container.py:1877` `_collect_dependencies` | bare except + warn | `_resolve_params` inside `try/except AnnotationResolutionError` → warn; now yields a **partial** graph instead of an empty one |
| `container.py:2550` `_get_provider_return_type` | bare except + warn | `_eval_annotation(sig.return_annotation, ...)` in `try/except` → warn → `None` |

Then: **delete `_resolve_hints_or_warn` entirely** (no callers remain) and fold
`_resolve_hints_or_raise` into `_resolve_params` / `_resolve_class_annotations`, which raise by
construction. `AnnotationResolutionError` stays, gaining an optional `param_name`.
Grep afterwards: `get_type_hints(` must appear in `providify/` only inside
`_annotations._eval_annotation`.

## Phase 7 steps

### 7A — primitive + tests

21. [ ] `tests/_annotations_no_future.py` — new helper module **without**
    `from __future__ import annotations`, exporting a class and a provider whose annotations are
    therefore real objects, plus `class AnnotatedBase` declaring `dep: Inject[Thing]` for the
    cross-module MRO test. Needed by Steps 25 and 33.
22. [ ] `tests/test_per_param_annotations.py` — new file (same shape/docstring discipline as
    `tests/test_forward_ref_provider.py`), class `TestEvalAnnotationPrimitive`, failing first:
    `_eval_annotation` returns the identical object as `get_type_hints(...)[name]` for each of
    `"Inject[Foo]"`, `"Annotated[Foo, InjectMeta(qualifier='x')]"`, `"Foo | None"`,
    `"Optional[Foo]"`, `"Live[Foo | None]"` (asserting `optional is True` on the marker),
    `"list[Foo]"`, `"InjectInstances[Foo]"`, `"ClassVar[Inject[Foo]]"`, `"Repository[User]"`
    (PEP-695 alias), `"'Foo'"` (nested quoted ref), and a non-str annotation object (pass-through).
23. [ ] `providify/_annotations.py` — new module. Implement `_eval_annotation`,
    `_raw_annotations(target)` (`inspect.get_annotations(..., eval_str=False)` + `inspect.unwrap`),
    and `_annotation_namespaces(target, localns)` (the globalns table in 7.1, incl.
    `__type_params__` seeding). Full docstrings incl. `Edge cases`.
24. [ ] `tests/test_per_param_annotations.py::TestNamespaceSelection` —
    `cls.__init__` slot wrapper (class with no explicit `__init__`) → no crash, empty result;
    `functools.wraps`-decorated provider → annotations read from the unwrapped function;
    generic `class Repository[T]` method annotated `T` → resolves via `__type_params__`.

### 7B — the resolvers

25. [ ] `tests/test_per_param_annotations.py::TestPerParamIsolation` — the headline behaviour,
    all no-monkeypatch, all using function-local (hence unresolvable) types:
    - `test_unresolvable_defaulted_param_does_not_break_sibling_sync` — provider with
      `(dep: Inject[Tracer], junk: LocalOnly | None = None)` → `dep` **is** injected.
      (Today: `NameError` → `{}` → nothing injected.)
    - `_async` mirror.
    - `test_unresolvable_param_in_middle_of_signature` — params before *and* after the bad one are
      injected.
    - `test_unresolvable_return_annotation_does_not_block_params` — provider params still injected.
    - `test_class_var_isolation` — class with `good: Inject[Tracer]` and `junk: LocalOnly` →
      `good` is set on the instance, `junk` is not, no exception. Sync + async.
    - `test_no_future_annotations_module_still_works` — same via `tests/_annotations_no_future.py`
      (real annotation objects, step-1 branch).
26. [ ] `tests/test_per_param_annotations.py::TestInjectionPointFailuresRaise`:
    - `test_marked_injection_point_with_default_raises` — `store: Inject[LocalOnly] = None` →
      `AnnotationResolutionError`; message contains `store` **and** `LocalOnly`. (This is the case
      the `has_default` tie-break alone would wrongly skip — it proves the AST sniff is load-bearing.)
    - `test_marked_injection_point_without_default_raises` — same without the default.
    - `test_bare_unresolvable_required_param_raises` — `dep: LocalOnly` (no marker, no default) →
      `AnnotationResolutionError` naming the param, replacing today's misleading
      `TypeError: missing 1 required positional argument`.
    - `test_non_name_error_always_raises` — annotation text `"1/0"` (or a metadata arg whose
      construction raises) on a **defaulted, non-injected** param → still raises (step 3 of the
      procedure), and the original exception is chained (`__cause__`).
    - `test_class_var_injection_point_failure_raises_sync` / `_async` —
      `dep: ClassVar[Inject[LocalOnly]]` → raises at construction.
    - `test_renamed_alias_import_is_still_detected` — `from providify import Inject as I`;
      `dep: I[LocalOnly]` with a default → raises via the textual last resort.
27. [ ] `tests/test_per_param_annotations.py::TestNonInjectionPointsAreSilent`:
    - `test_unresolvable_defaulted_param_emits_no_warning` — `caplog` at WARNING for
      `providify.container` and `providify._annotations` is **empty**. ("Nothing left to configure"
      — no warning, not just no exception.)
    - `test_type_checking_only_import_on_defaulted_param_works` — the canonical downstream shape.
    - `test_plain_unresolvable_class_var_is_skipped_silently`.
28. [ ] `providify/_annotations.py` — implement `_sniff_injection_marker(raw, globalns, localns)`
    returning `Literal["inject", "not-inject", "unknown"]` per 7.2 (AST head walk; for `Annotated`,
    evaluate only the metadata args; identity-check alias singletons from `providify.type`;
    textual last resort behind an explicit `allow_textual: bool` argument so the parameter path can
    disable it). Docstring must state that this runs **only** after a `NameError`.
29. [ ] `providify/_annotations.py` — implement
    `resolve_params(target, owner_name, globalns, localns) -> dict[str, Any]`: iterate
    `inspect.signature(inspect.unwrap(target)).parameters`, skip `self`/`cls`, skip
    `*args`/`**kwargs` (`VAR_POSITIONAL`/`VAR_KEYWORD` — they are never injection points), skip
    unannotated params, run `resolve_one` per parameter, pass `has_default =
    param.default is not inspect.Parameter.empty`. Never contains `"return"`.
30. [ ] `providify/_annotations.py` — implement
    `resolve_class_annotations(cls, localns) -> dict[str, Any]`: walk `reversed(cls.__mro__)`
    skipping `object`, `_raw_annotations(klass)` per class, globalns from **each defining class's**
    module, later (more derived) classes override earlier ones — mirroring `get_type_hints`' order.
    Ambiguity → skip; `allow_textual=True`.
31. [ ] `providify/container.py` — add `_resolve_params(target, owner_name)` and
    `_resolve_class_annotations(cls)` thin methods: consult `self._hints_cache`, delegate to
    `providify._annotations`, store, return `dict(...)` copy. Full docstrings incl. the
    thread-safety rationale from 7.4.

### 7C — cache

32. [ ] `tests/test_per_param_annotations.py::TestHintsCache`:
    - `test_second_resolution_is_cached` — count `_eval_annotation` calls via monkeypatch; second
      `container.get(X)` performs zero further evals.
    - `test_binding_mutation_invalidates_cache` — resolve a class whose param type is not yet bound
      (defaulted, so it skips), then `container.bind(Dep, DepImpl)`, resolve again → the param is
      now injected. Proves the cache dies with `_localns_cache`.
    - `test_cache_is_per_container` — two containers with different bindings resolve the same class
      differently.
    - `test_returned_dict_is_a_copy` — mutate the returned dict, resolve again, result unchanged.
    - `test_concurrent_resolution_is_consistent` — 8 threads × `container.get(X)`; all succeed and
      return equivalent hints (`ThreadPoolExecutor`, assert no exception).
33. [ ] `providify/container.py:376` — add `self._hints_cache: dict[Any, dict[str, Any]] = {}`.
    Add `_invalidate_type_caches()` and replace the six `_localns_cache = None` sites
    (`container.py:587`, `:626`, `:642`, `:3805`, `:3881`, `scanner.py:211`) with a call to it.
    `container.py:3881` is `copy`/`child`-container construction — verify the clone gets its own
    empty `_hints_cache` rather than sharing the parent's dict object.

### 7D — migration & removal

34. [ ] `tests/test_per_param_annotations.py::TestParityWithGetTypeHints` — table-driven parity
    guard: for ~12 fully-resolvable module-level shapes (bare type, `Inject[T]`, `Annotated` with
    qualifier/priority/optional, `T | None`, `list[T]`, `InjectInstances[T]`, `Lazy[T]`, `Live[T]`,
    `Instance[T]`, `Repository[User]`, quoted `"T"`, inherited class var), assert
    `container._resolve_params(fn, "x") == {k: v for k, v in get_type_hints(fn, include_extras=True, localns=container._build_localns()).items() if k != "return"}`.
    This is the "Phase 7 changes no resolvable outcome" contract from *Non-goals*.
35. [ ] `providify/container.py` — switch every call site in the 7.5 table. Delete the now-dead
    `hints.pop("return", None)` lines (`:1757`, `:1816`, `:1883`, `:3078`, `:3199`) and the
    `if not hints: return` guards that assumed whole-signature failure.
36. [ ] `providify/container.py` — **delete `_resolve_hints_or_warn`** and fold
    `_resolve_hints_or_raise` into the two resolvers. Extend `AnnotationResolutionError.__init__`
    with `param_name: str | None = None` and include it in the message when present. Verify with
    `grep -n "get_type_hints(" providify/` → only `providify/_annotations.py`.
37. [ ] `tests/test_annotation_resolution.py` — rewrite the release-A tolerance tests that this
    phase invalidates: `TestClassVarInjectionHintFailure::test_unresolvable_name_is_tolerated_and_logged_sync`
    / `_async` become *raise* tests for a marked class var and *silent-skip* tests for an unmarked
    one. Keep the `AttributeError`-propagates tests as-is (still true).
38. [ ] `tests/test_forward_ref_provider.py:371` `test_unresolvable_name_is_tolerated_but_logged` —
    the blanket `get_type_hints` monkeypatch is meaningless once resolution is per-parameter.
    Replace with the per-param equivalent: a defaulted, genuinely unresolvable parameter resolves
    silently; a marked one raises. Keep the rest of the file untouched — it is the regression suite
    this whole plan descends from.
39. [ ] `CHANGELOG.md` — new release-B section:
    **Changed**: annotations are now resolved per parameter / per class attribute; an unresolvable
    annotation on a parameter that is not an injection point (a `TYPE_CHECKING`-only import, a
    defaulted local type) no longer affects anything — this removes most of the
    `AnnotationResolutionError` failures introduced by the previous release.
    **Changed / ⚠️**: an unresolvable annotation on something that IS an injection point now raises
    `AnnotationResolutionError` naming the exact parameter, replacing the previous warning-and-skip
    (which silently injected nothing) and the misleading
    `TypeError: missing N required positional arguments`.
    **Removed**: internal `NameError` tolerance (`_resolve_hints_or_warn`).
40. [ ] Docs — update `docs/agents/injection-cheatsheet.md` and `docs/agents/anti-patterns.md`:
    the rule is now "annotate injection points with types that exist at runtime; a defaulted,
    non-injected parameter may reference anything". Remove any guidance that recommends working
    around whole-signature hint loss.

---

## Edge cases

**Release A**
- Class with no annotations at all → `hints == {}` → no-op. **Not** an error, in every tier.
- Class-var annotation with an unresolvable name (`NameError`) → WARNING, no class-var injection,
  construction succeeds. Tolerated until Phase 7.
- Class-var hint resolution raising anything other than `NameError` → propagates out of `get()`.
- Any hint failure inside `_check_scope_violation` / `_check_provider_scope_violation` /
  `_collect_class_var_hints` → `AnnotationResolutionError` (including `NameError`).
- `DEPENDENT`-scoped provider with unresolvable annotations → still validates clean:
  `container.py:3177` returns before hint resolution. Deliberate — nothing to validate.
- `validate_all()` with an unresolvable binding → violation string in the returned list,
  `_validated` stays `False`, `is_valid is False`. No exception escapes.
- `describe()` / `_get_dependencies` with an unresolvable class → WARNING, partial graph, no raise.
- `__init__` not inspectable (C-extension type) → `init_params = set()` as today; unchanged.
- A class with no explicit `__init__` → `get_type_hints(object.__init__)` yields no injectable
  params; no new failure mode.

**Phase 7**
- Unannotated parameter → skipped (no annotation, nothing to evaluate), as today.
- `*args` / `**kwargs` → skipped explicitly; never injection points.
- `self` / `cls` → skipped.
- Annotation resolvable but its *marker metadata* fails to construct → non-`NameError` → raise.
- `Annotated[Unresolvable, InjectMeta()]` → metadata args evaluate fine → classified as injection
  point → raise, naming the parameter and `Unresolvable`.
- `dep: LocalOnly = None` → skip silently, **no warning**.
- `dep: LocalOnly` (required, unresolvable) → raise (previously a misleading `TypeError`).
- `dep: Inject[LocalOnly] = None` → raise (the AST sniff overrides the default-based tie-break).
- Renamed alias import (`Inject as I`) with unresolvable head → textual last resort → raise.
- Module without `from __future__ import annotations` → annotations are objects; step 1 returns them
  unchanged; classification is authoritative and no bootstrap is ever reached.
- Inherited class var declared in another module → evaluated against **that** module's globals;
  a subclass re-annotating the same name wins (reverse-MRO order, matching `get_type_hints`).
- PEP-695 generic owner (`class Repository[T]`) → `T` resolves via seeded `__type_params__`.
- Cache: binding added between two resolutions of the same class → second resolution sees the new
  binding (caches invalidated together).
- Cache: two containers, same class → independent entries; no cross-container leakage.

## Verification

Release A:
```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_annotation_resolution.py -q          # new tests (Phase 2 → red, Phase 5 → green)
uv run pytest tests/test_forward_ref_provider.py -q           # the fix this plan completes
uv run pytest -q                                              # full suite; triage per Step 18
uv run ruff check providify tests
uv run ruff format --check providify tests
```
Release B (Phase 7), additionally:
```bash
uv run pytest tests/test_per_param_annotations.py -q
uv run pytest tests/test_annotation_resolution.py tests/test_forward_ref_provider.py -q  # rewritten tests
uv run pytest -q
grep -rn "get_type_hints(" providify/        # must match only providify/_annotations.py
grep -rn "_resolve_hints_or_warn" providify/ # must return nothing
```
(If `uv` is unavailable: `python -m pytest -q`, `python -m ruff check providify tests`.)

Manual acceptance checks:
- A container with a genuine `SINGLETON → REQUEST` leak declared with locally-defined types must
  raise `LiveInjectionRequiredError` from `validate_bindings()`, where before it returned silently.
- Phase 7 performance sanity (scratchpad script, **not** a test — timing assertions are flaky):
  a 20-parameter constructor resolved 10 000 times must not be materially slower than the current
  implementation, given the cache. If it is, the cache key or the copy-on-read is wrong.

## Risks

**Release A**
- **Previously-hidden leaks become startup failures.** Adding `localns` at `container.py:3072` makes
  class scope-leak detection actually run for locally-defined annotation types. Invariant: the
  validator's job is to be correct, not quiet — fix the offending binding, never re-widen the catch.
  Expect red tests in Step 18; each must land in bucket (a) or (b).
- **Breaking for downstream apps** whose `__init__` annotations are `TYPE_CHECKING`-only imports:
  they now fail at container wiring. Mitigation: precise error message naming the class + the
  original exception, CHANGELOG migration note, and Phase 7 removing the false-positive class.
- **Tier-3 downgrade must be exhaustive.** `_collect_class_var_hints` is now strict and has a
  lenient caller. Invariant: every caller is either (i) a validator that lets
  `AnnotationResolutionError` escape, or (ii) a reporting path that catches it and warns.
- **Selective monkeypatching in tests is fragile** — it depends on `_collect_kwargs_*` receiving
  `cls.__init__` while `_inject_class_vars_*` receives `cls`. Mitigation: the positive-control
  tests in Step 4 fail loudly if class-var injection breaks for any reason.
- **`_build_localns` is now on the validation hot path** for class bindings. It is cached
  (`_localns_cache`, `container.py:1681`); confirm it is not rebuilt per binding during
  `validate_bindings()`.

**Phase 7**
- **Semantic drift from `get_type_hints`.** The whole container's behaviour rests on hint shapes.
  Invariant: for any annotation that resolves today, the per-param resolver must return an *equal*
  object — enforced by Step 34's parity table. If a shape cannot be made to match, stop; do not
  "improve" resolution in this phase.
- **The bootstrap classifier is a heuristic** by construction (it runs only when evaluation already
  failed). Its failure modes are asymmetric and must stay that way: a false "injection point" costs
  a loud startup error (recoverable, obvious); a false "not an injection point" costs silent
  non-injection (the original bug). When in doubt the classifier must lean toward raising — the
  `has_default` tie-break is the only place a doubt resolves to skip, and only because
  `_build_localns` proves no binding of that name exists.
- **Required-but-unresolvable parameters change exception type** (`TypeError`/`LookupError` →
  `AnnotationResolutionError`). Downstream code catching `LookupError` around `container.get()`
  breaks. Mitigation: CHANGELOG entry; consider whether `AnnotationResolutionError` should also
  subclass nothing new (it stays a `ValidationError`, deliberately not a `LookupError` — the two
  mean different things).
- **Cache invalidation is the classic hard problem.** Invariant: `_hints_cache` and `_localns_cache`
  are cleared by the *same* method, always together. The `scanner.py:211` external poke is exactly
  the kind of site that gets missed — Step 33 must convert it.
- **Per-annotation evaluation is N `get_type_hints` calls.** Mitigated by the per-callable cache;
  the risk is a cache miss storm on a container that mutates bindings during resolution. Check that
  no code path binds while resolving (it would already be a re-entrancy bug).
