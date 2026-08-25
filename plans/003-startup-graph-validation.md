# Plan 003 — Startup-time full graph validation (`container.validate()`)

Implements backlog item **F1** (🔴 must — the only must-have in `BACKLOG.md`).

Industry grounding: startup-time wiring validation is table-stakes across Spring, .NET
`Microsoft.Extensions.DependencyInjection` and StructureMap — see
`design/di-features-taxonomy/research/001-table-stakes-vs-differentiators.md` §Table-stakes #8
("Missing dependency detection at startup or deployment"; "modern frameworks validate wiring on
container initialization rather than lazy-on-first-use"). The same brief's closing paragraph names
*clear diagnostics on ambiguous/missing bindings* as providify's nearest production-readiness gap.

## Goal

`container.validate()` walks the **entire declared dependency graph** once, without instantiating
anything, and reports every wiring defect in one shot: missing bindings, ambiguous bindings,
dependency cycles, scope leaks, `Live[T]` violations, and unresolvable annotations. It returns a
structured `ValidationReport` and — by default — raises a single aggregate
`ContainerValidationError` when the report contains errors, so a misconfigured app fails at boot
instead of on the first production request.

```python
container.scan("myapp")
container.validate()                                # raises on any error
report = container.validate(raise_on_error=False)   # or inspect it
for issue in report.errors:
    log.error("%s", issue.message)
```

## Non-goals

- **No change to the lazy path.** `get()` keeps calling `validate_bindings()` (scope-only) on first
  resolution. The full graph walk is **never** run implicitly — it is opt-in at startup. Making
  first-`get()` pay for a whole-graph walk would be both a perf and a behaviour regression.
- **No deprecation or behaviour change to `validate_bindings()` / `validate_all()`.** They keep
  their exact current semantics (scope-leak tier only). `validate()` is additive.
- **No `avalidate()`.** Validation is pure introspection with zero await points; the sync method
  validates async graphs perfectly well. An async mirror would be dead weight.
- **No refactor of `_resolve_hint_sync` / `_resolve_hint_async`.** Plan 003 adds a *parallel*
  pure classifier and pins it to the runtime with a parity test (see Risks).
- **No instantiation.** `validate()` never calls `create()`, never touches a cache, never fires
  `@PostConstruct`. `warm_up()` remains the "actually build it" tool and is unchanged.
- **No validation of ad-hoc `container.get(X)` call sites.** Only the *declared* graph (injection
  points of registered bindings) is validated. Stated as a documented limitation.
- **No binding index / registry data-structure change** for lookup speed (see Risks for the
  per-call memo that keeps this acceptable).
- **No version bump** in `pyproject.toml` — CHANGELOG entry only.

## Design

### Why one flat pass is enough

Every node of the reachable dependency graph *is itself a registered binding* — a dependency edge
can only point at something `_filter()` returned. Therefore:

```
for binding in self._bindings:            # every node, exactly once — O(V)
    for ip in self._iter_injection_points(binding):   # every out-edge  — O(E)
        candidates = memo_filter(ip.base_type, ip.qualifier, ip.priority)
        classify(candidates) -> issue?    # missing / ambiguous
        record edge(s) into adjacency     # for the cycle DFS
run scope-tier checks (binding.validate)  # reuse existing validators
DFS over adjacency                        # cycles, colour-marked
```

No recursive descent is needed for missing/ambiguous detection. Recursion is only used for cycle
detection, and it runs over a pre-built adjacency map keyed by **index into `self._bindings`**
(indices, not binding objects — robust regardless of whether a binding type ever gains
`__eq__`/`__hash__`).

### The three new checks (none exist today)

`validate_bindings()`/`validate_all()` today only call `Binding.validate()`, i.e. scope leaks +
`Live[T]` + annotation resolvability (`container.py:3543`, `container.py:3566`,
`binding.py:211`, `binding.py:694`). `validate()` reuses that tier verbatim and adds:

**1. Missing bindings.** An injection point is *missing* when the runtime rule in
`_collect_kwargs_sync` (`container.py:2023-2029`) would fire: `_resolve_hint_sync` yields
`_UNRESOLVED` **and** the parameter has no default. Mirrored statically:

| hint form | classified as | missing → |
|---|---|---|
| `Inject[T]` | eager, single | ERROR (no default) / WARNING `MISSING_BINDING_DEFAULTED` (has default) |
| `Inject[T \| None]`, `InjectMeta(optional=True)` | optional | not reported (documented `None` injection) |
| bare `T` / `Repository[User]` (no marker) | eager, single | same as `Inject[T]` — runtime *does* raise `LookupError` for an unbound, undefaulted plain annotation |
| `T1 \| T2` / `Optional[T]` | eager, union | ERROR only when **no** member resolves; not reported if `NoneType` in the union |
| `Lazy[T]`, `Live[T]` (not optional) | deferred, single | ERROR — the failure is real, merely postponed to first access |
| `Instance[T]`, `Event[T]` | caller-parameterised | WARNING `MISSING_BINDING_DEFERRED` — `InstanceProxy` exposes `.resolvable()` and takes its qualifier at call time, so "no binding today" is a legal design |
| `InjectInstances[T]` / `InjectMeta(all=True)` | multi | never reported — `get_all` legitimately returns `[]` |
| `Annotated[T, NamedMeta(n)]` | eager, single, qualifier=`n` | same as `Inject[T]` |
| `Annotated[T, DelegateMeta]` | eager, single, **excluding self** | ERROR if no candidate other than the owning implementation |
| `InjectionPoint` | context value | never reported |
| `int`, `str`, unannotated, `return` | not an injection point | skipped |

Qualifier/priority come from the **hint's marker** (`inject_meta.qualifier`, `named_meta.name`, …),
matching `_resolve_hint_sync` — *not* from the owning binding, which is what `_get_dependencies`
and `_check_scope_violation` pass. This distinction is the main reason `_get_dependencies` cannot
be reused here (the other reason: it silently drops unresolvable deps —
`_resolve_dependency` returns `None` on `LookupError`, `container.py:2179`).

**2. Ambiguous bindings.** Runtime picks `max(candidates, key=priority)`
(`_get_best_candidate`, `container.py:1418`). That is deterministic *unless two or more candidates
tie at the maximum priority*, in which case `max` returns the first in registration order —
arbitrary and silent. So:

- **ERROR** when a *single-valued* injection point has ≥2 candidates tied at max priority.
- **Not reported** when several candidates exist at *different* priorities — that is the intended
  `@Priority` override mechanism.
- **Not reported** for multi (`get_all`) or caller-parameterised (`Instance[T]`) injection points.
- **No registry-wide sweep.** Deliberately rejected: `bind(Base, A); bind(Base, B)` for later
  `get_all(Base)` is a legal Jakarta pattern and a registry sweep would flag it as a false positive.
  Ambiguity is only reported where the runtime actually has to choose one.

**3. Cycles, statically.** Today cycles are only caught at instantiation via the
`_resolution_stack` ContextVar (`resolution.py:19`). `validate()` runs a colour-marked DFS over the
adjacency map. Edge rules, mirroring what actually happens during construction:

- **Excluded** (never close a construction cycle): `Lazy[T]` (the documented cycle-breaker — it is
  literally fix #2 in `CircularDependencyError`'s message, `exceptions.py:79`), `Live[T]`,
  `Instance[T]`, `Event[T]`.
- **Included**: eager single (edge → the max-priority candidate only, i.e. exactly the one `get()`
  will pick), multi/`all=True` (edge → *every* candidate; `get_all` resolves them all eagerly),
  union (edge → the **first** member that has a candidate, mirroring the declaration-order rule at
  `container.py:2342`) — so unions never produce a false-positive cycle.
- `DelegateMeta` edges skip candidates whose `implementation is` the owning binding's
  implementation, mirroring the self-exclusion at `container.py:2316-2322`; without this every
  `@Decorator` reports a bogus self-cycle.
- A cycle is reported once, canonicalised by rotating the path to its lowest node index, with the
  same `A → B → C → A` rendering `CircularDependencyError.cycle` uses.

### API shape

```python
def validate(self, *, raise_on_error: bool = True) -> ValidationReport
```

One method, one knob. Always builds and returns the full report; raises
`ContainerValidationError(report)` afterwards iff `raise_on_error` and `report.errors`. Warnings
never raise. Naming: `validate()` matches the backlog wording and Spring/.NET vocabulary; there is
no real collision with `Binding.validate(container)` (different class, different signature,
different direction) — and `validate()` reading as "validate this container" is exactly right.

New module `providify/validation.py` (pure data + rendering, mirroring `descriptor.py`'s role):

```python
class Severity(StrEnum):        ERROR, WARNING

class IssueKind(StrEnum):
    MISSING_BINDING, MISSING_BINDING_DEFAULTED, MISSING_BINDING_DEFERRED,
    AMBIGUOUS_BINDING, CIRCULAR_DEPENDENCY,
    SCOPE_LEAK, LIVE_REQUIRED, UNRESOLVED_ANNOTATION

@dataclass(frozen=True)
class ValidationIssue:
    kind: IssueKind
    severity: Severity
    owner: str                     # "OrderService.__init__" | "@Provider(make_db)"
    message: str                   # complete, actionable, one line + fix hint
    param_name: str | None = None
    requested: str | None = None   # _type_name() of the requested type
    qualifier: str | type | None = None
    candidates: tuple[str, ...] = ()   # populated for AMBIGUOUS_BINDING

@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    checked_bindings: int
    @property errors -> tuple[ValidationIssue, ...]
    @property warnings -> tuple[ValidationIssue, ...]
    @property ok -> bool           # no ERROR-severity issues
    def to_dict(self) -> dict      # JSON/YAML-friendly, like BindingDescriptor.to_dict
    def __repr__(self) -> str      # grouped human-readable block
```

`ValidationReport` deliberately does **not** define `__bool__`: `if report:` would read as
"there are issues" to one person and "the container is fine" to another. Callers write
`if not report.ok:`.

Internal (module-private in `validation.py`, not exported):

```python
@dataclass(frozen=True)
class _HintSpec:
    base_type: Any
    qualifier: str | type | None
    priority: int | None
    optional: bool                 # absent binding → None injected, never fails
    deferred: bool                 # Lazy/Live/Instance/Event — excluded from cycle edges
    multi: bool                    # all=True — absent binding is legal (empty list)
    caller_parameterised: bool     # Instance[T]/Event[T] — missing is a WARNING
    excludes_self: bool            # DelegateMeta

def _classify_hint(hint: Any) -> _HintSpec | None:   # None → not an injection point
```

New exception in `exceptions.py`, under the existing `ValidationError` family (so the existing
`validate_all()` catch-all keeps working and users' `except ValidationError` still covers it):

```python
class ContainerValidationError(ValidationError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        super().__init__(<grouped multi-line message>)
```

Deliberately **not** a `LookupError` subclass: startup validation is an explicit new call, and
aliasing it to the lazy-resolution error type would blur the two failure modes.

### How it composes with what exists

| method | status after this plan |
|---|---|
| `Binding.validate(container)` | unchanged — reused by `validate()` per binding |
| `validate_bindings()` | unchanged (raising, scope-only, auto-called on first `get()`) |
| `validate_all()` | unchanged behaviour; docstring gains a "see also `validate()`" pointer |
| `warm_up()` / `awarm_up()` | unchanged — note added that it only covers SINGLETONs |
| `describe()` / `_get_dependencies()` | unchanged — reporting tier, tolerant by design |
| `validate()` | **new** — the startup gate |

`validate()` sets `self._validated = True` only when the report is completely empty (errors *and*
warnings), matching `validate_all()`'s conservative rule at `container.py:3578`.

### Alternatives considered

- **Extend `validate_all()` in place instead of adding `validate()`** — ❌ silently changes the
  meaning of an existing public-ish return value (callers gating startup on "scope leaks only"
  would suddenly see missing/ambiguous/cycle strings), and `list[str]` cannot carry severity,
  kind, or the candidate list needed for a good ambiguity message. ✅ would have avoided a new
  method. Rejected: semantics change outweighs surface saving.
- **Make first `get()` run the full walk** — ✅ zero-effort adoption. ❌ turns a bounded first
  resolution into an O(V+E) walk, changes exception types thrown from `get()`, and would break the
  legitimate "register more bindings lazily after first use" flow. Rejected.
- **Reuse `_get_dependencies()` for graph traversal** — ✅ zero new traversal code. ❌ it *swallows*
  exactly the failures F1 must report (`_resolve_dependency` returns `None` on `LookupError`,
  `_collect_dependencies` logs-and-continues on `AnnotationResolutionError`), and it filters by the
  *owning binding's* qualifier rather than the hint's. It is a reporting-tier helper; a validator
  built on it would report a clean bill of health it cannot prove. Rejected.
- **Validate by instantiating (`warm_up()`-style dry run)** — ✅ perfect fidelity, no classifier
  duplication. ❌ runs user constructors and `@PostConstruct` side effects at validation time,
  cannot cover REQUEST/SESSION scopes without a fake context, and cannot report more than the first
  failure. Rejected.
- **A separate `GraphValidator` class in `validation.py` owning the walk** — ✅ keeps
  `container.py` (~3.9 kLOC) from growing. ❌ needs `_filter`, `_resolve_params`,
  `_collect_class_var_hints`, `_hints_cache` — i.e. it would reach into container internals
  through a back door, adding an abstraction that owns no state. Precedent in this codebase is
  `_check_scope_violation` living on the container. Rejected; only the *data types* and the pure
  classifier move to `validation.py`.
- **`validate(strict=...)` / `include_warnings=...` knobs** — ❌ speculative configurability; the
  report already lets a caller filter. One knob only.

## Steps

1. [x] `tests/test_validation.py` — new file. Failing tests for the report *shape* first:
   `validate()` on an empty container returns `ValidationReport(issues=(), checked_bindings=0)`
   with `report.ok is True`; a fully-wired container returns `ok is True`; `report.to_dict()`
   round-trips to JSON-safe primitives; `ValidationReport` has no `__bool__`.
2. [x] `providify/validation.py` — new module. `Severity`, `IssueKind`, `ValidationIssue`,
   `ValidationReport` (with `errors`/`warnings`/`ok`/`to_dict`/`__repr__`). Complete docstrings
   per CLAUDE.md (Args/Returns/Raises/Thread safety/Async safety/Edge cases/Example).
3. [x] `providify/exceptions.py` — add `ContainerValidationError(ValidationError)` holding
   `.report`, message = grouped one-line-per-issue block. Place after `AnnotationResolutionError`.
4. [x] `tests/test_validation.py` — failing tests for `_classify_hint` over the full hint table in
   §Design: `Inject[T]`, `Inject[T | None]`, `InjectMeta(optional=True)`, bare `T`, generic alias
   `Repository[User]`, `T1 | T2`, `Optional[T]`, `Lazy[T]`, `Live[T]`, `Instance[T]`, `Event[T]`,
   `InjectInstances[T]`, `InjectMeta(all=True)`, `NamedMeta("x")`, `DelegateMeta`, `InjectionPoint`,
   `ClassVar[Inject[T]]`, plain `int`, `str`, unannotated.
5. [x] `providify/validation.py` — add `_HintSpec` + `_classify_hint()`; mirror
   `_resolve_hint_sync` (`container.py:2200-2367`) branch for branch, including the
   `_unwrap_union`-inside-`Annotated` optionality merge at `container.py:2220-2233`.
6. [x] `tests/test_validation.py` — failing tests for missing-binding detection: unbound
   `Inject[T]` without default → 1 ERROR `MISSING_BINDING` naming the param; with default →
   WARNING `MISSING_BINDING_DEFAULTED`; `Inject[T | None]` → no issue; `InjectInstances[T]` with
   zero candidates → no issue; unbound class-var `Inject[T]` → ERROR (class vars have no default —
   `_inject_class_vars_sync` raises `LookupError`, `container.py:2539`); unbound `Lazy[T]` → ERROR;
   unbound `Instance[T]` → WARNING; `*args`/`**kwargs` annotated but unbound → **no** issue.
7. [x] `providify/container.py` — add `_iter_injection_points(binding)` next to
   `_get_dependencies` (~line 3646). Yields `(owner_name, param_name, hint_spec, has_default)` for
   `ClassBinding.__init__` params (via `_resolve_params(..., owner=impl)`) + class-var hints (via
   `_collect_class_var_hints`), and for `ProviderBinding.fn` params. Skips `VAR_POSITIONAL` /
   `VAR_KEYWORD` parameters (never required, however annotated) — a deliberate, documented
   divergence from `_collect_kwargs_sync`. Propagates `AnnotationResolutionError` to the caller.
8. [x] `providify/container.py` — add `validate(*, raise_on_error=True)` after `validate_all()`
   (~line 3610). Pass 1: per binding, `try: binding.validate(self)` → map
   `ScopeViolationDetectedError`→`SCOPE_LEAK`, `LiveInjectionRequiredError`→`LIVE_REQUIRED`,
   `AnnotationResolutionError`→`UNRESOLVED_ANNOTATION` (one issue per structured violation, not one
   per exception). Pass 2: `_iter_injection_points` → missing/ambiguous issues + adjacency map.
   A binding whose annotations fail to resolve contributes its `UNRESOLVED_ANNOTATION` issue and
   is **skipped** for edges (partial graph — must never silently read as "no dependencies").
   Candidate lookup goes through a call-local memo `dict[(base_type, qualifier, priority), list]`
   wrapping `_filter`.
9. [x] `tests/test_validation.py` — failing tests for ambiguity: two bindings for the same
   interface at equal priority injected via `Inject[T]` → ERROR `AMBIGUOUS_BINDING` listing both
   candidate names; same two at *different* priorities → no issue; same two injected via
   `InjectInstances[T]` → no issue; two bindings with *different* qualifiers → no issue;
   `@Alternative` candidate not enabled → not counted (goes through `_filter`).
10. [x] `providify/container.py` — implement the ambiguity branch inside `validate()` using the
    memoised candidate list: `ties = [c for c in candidates if c.priority == max_priority]`,
    report when `len(ties) > 1` and the injection point is single-valued and not
    caller-parameterised.
11. [x] `tests/test_validation.py` — failing tests for static cycles: `A→B→A` via `Inject` → one
    ERROR `CIRCULAR_DEPENDENCY` with path `A → B → A`; the same pair with one side `Lazy[T]` → no
    issue; `Live[T]`/`Instance[T]` cycle → no issue; self-cycle `A→A` → one issue; a 3-node cycle
    reported exactly **once** regardless of entry node; two disjoint cycles → two issues; a
    `@Decorator` wrapping the type it is bound to → **no** issue (delegate self-exclusion);
    provider→class→provider cycle → one issue; a diamond (non-cyclic, shared dep) → no issue.
12. [x] `providify/container.py` — implement `_find_cycles(adjacency)` as a private helper: iterative
    or recursive colour-marked DFS (white/grey/black) over binding indices, canonical rotation for
    dedupe, path rendered with `_type_name(binding.interface)`.
13. [x] `tests/test_validation.py` — failing tests for the aggregate raise: `validate()` on a broken
    container raises `ContainerValidationError`; `exc.report` carries every issue;
    `validate(raise_on_error=False)` returns the same report without raising; a warnings-only
    container does **not** raise; `str(exc)` contains every error message.
14. [x] `providify/container.py` — wire the raise + `self._validated = True` only when
    `not report.issues` (errors *and* warnings empty).
15. [x] `tests/test_validation.py` — regression guards on composition: `validate()` does not
    instantiate anything (`container._singleton_cache` stays empty; a `@PostConstruct` spy never
    fires); a REQUEST-scoped binding is validated with **no** active request context;
    `validate_all()` and `validate_bindings()` still return/raise exactly as before on a container
    that `validate()` flags for missing bindings only.
16. [x] `tests/test_validation.py` — **parity test** (the anti-drift guard): a table of hint forms
    where each row asserts that `_classify_hint`'s missing/optional verdict agrees with what
    `container.get()`/construction actually does on a fixture container (raises `LookupError` vs
    injects `None` vs uses the default). This is what keeps `_classify_hint` and
    `_resolve_hint_sync` from diverging.
17. [x] `providify/__init__.py` — export `ValidationReport`, `ValidationIssue`, `IssueKind`,
    `Severity`, `ContainerValidationError`; add them to `__all__` in the existing grouped order
    (exceptions with exceptions, report types next to `BindingDescriptor`).
18. [x] `providify/container.py` — docstring cross-links: `validate_all()` and `validate_bindings()`
    gain "see also `validate()` for full-graph checks"; `warm_up()` gains "only instantiates
    SINGLETONs — use `validate()` for whole-graph checks".
19. [x] `README.md` + `docs/agents/usage-rules.md` — one section / one rule: call
    `container.validate()` once after all registration and before serving traffic.
20. [x] `CHANGELOG.md` — `### Added` entry under `[Unreleased]` describing `validate()`, the new
    report/issue types, `ContainerValidationError`, and the explicit statement that
    `validate_bindings()`/`validate_all()` are unchanged.

## Edge cases

- Empty container → `ValidationReport(issues=(), checked_bindings=0)`, `ok is True`, no raise.
- Parameter with a default and no binding → WARNING, never ERROR (runtime uses the default).
- `Inject[T | None]` / `InjectMeta(optional=True)` with no binding → no issue at all.
- `InjectInstances[T]` / `all=True` with zero candidates → no issue (`get_all` returns `[]`).
- `Instance[T]` / `Event[T]` with no binding → WARNING only (`.resolvable()` exists for this).
- `Lazy[T]` / `Live[T]` with no binding, not optional → ERROR (deferred but certain failure).
- `Lazy[T]` participating in a cycle → **no** cycle issue (documented cycle-breaker).
- `@Decorator` bound to the type it delegates → no self-cycle (delegate excludes self).
- Union `T1 | T2` where only `T2` is bound → no missing issue; cycle edge points at `T2` only.
- Unresolvable annotation on an injection point → one `UNRESOLVED_ANNOTATION` ERROR; that binding's
  edges are omitted and no "looks fine" verdict is emitted for it.
- Unresolvable annotation on a *non*-injection-point parameter → nothing (Phase 7 semantics).
- REQUEST/SESSION-scoped bindings validated outside any scope context → validated normally; no
  `RuntimeError` (nothing is instantiated, `_get_cache` is never reached).
- Async `@Provider` in the graph → validated identically; no `RuntimeError` (unlike `warm_up()`).
- `*args: T` / `**kwargs: T` → skipped, never reported missing.
- Two bindings tied at max priority but only reachable via `get_all` → no ambiguity issue.
- Self-cycle `A` depending on `Inject[A]` → one `CIRCULAR_DEPENDENCY` issue with path `A → A`.
- Calling `validate()` twice → identical report both times, no accumulated state.

## Verification

```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_validation.py -q          # new suite
uv run pytest -q                                    # full suite — no regressions (552+ tests)
uv run ruff check providify tests
uv run ruff format --check providify tests
```

Manual smoke check (should print one ERROR and exit non-zero):

```bash
uv run python -c "
from providify import DIContainer, Singleton, Inject, ContainerValidationError
class Missing: ...
@Singleton
class Svc:
    def __init__(self, m: Inject[Missing]): ...
c = DIContainer(); c.register(Svc)
try:
    c.validate()
except ContainerValidationError as e:
    print(e); raise SystemExit(1)
"
```

## Risks

- **Classifier drift.** `_classify_hint` duplicates the branch logic of `_resolve_hint_sync`
  (`container.py:2200-2367`). If one changes and the other does not, `validate()` reports a clean
  graph that then fails at runtime — the exact false negative `AnnotationResolutionError`'s
  docstring warns about. Invariant that must hold: *for every hint form, `_classify_hint`'s
  missing/optional verdict equals the runtime outcome*. Mitigated by the parity test (step 16),
  which is the single most important test in this plan.
- **False-positive errors block startup.** A wrong ERROR verdict is worse than no validation,
  because users will call `validate()` in production boot paths. Every ambiguous case in the table
  above is therefore graded WARNING or skipped, never ERROR. Invariant: *ERROR is reserved for
  cases where the runtime is guaranteed to raise.*
- **Performance.** Per injection point `_filter()` is O(N) over `_bindings`, so the walk is
  O(E·N) — for ~1000 bindings × ~3 deps that is ~3M `_interface_matches` calls. The call-local
  candidate memo collapses repeated `(type, qualifier, priority)` lookups and `_resolve_params` is
  already cached via `_hints_cache`, which should keep this well under a second.
  ⚠️ ASSUMPTION — no benchmark exists in this repo and the scout report gives no timing data. If a
  large-graph benchmark later shows this is unacceptable, the fix is a registry index, which is an
  explicit non-goal here. Record the measured time for the largest existing test container in the
  PR description.
- **Cycle-edge policy is a judgement call.** Excluding `Live[T]`/`Instance[T]`/`Event[T]` edges
  assumes those proxies never resolve during construction of their owner.
  ⚠️ ASSUMPTION for `Event[T]`: `EventProxy` is constructed eagerly and its target resolution is
  deferred (`container.py:2328`), but the scout report does not cover `EventProxy.fire()`
  semantics — verify by reading `providify/type.py`'s `EventProxy` before implementing step 12; if
  it resolves observers eagerly, include the edge.
- **`get_all`/multi cycle edges are conservative.** Edges to *every* candidate can, in principle,
  report a cycle through a candidate that a real run would not construct first. This is intentional
  (`get_all` does construct them all), but a surprising report is possible for exotic graphs.
- **Class-var defaults.** Step 6 assumes class-level injection points never fall back to a default,
  grounded in `_inject_class_vars_sync`'s documented `Raises: LookupError` at `container.py:2539`.
  ⚠️ ASSUMPTION — the body below line 2565 was not read; confirm the `_UNRESOLVED` branch there
  actually raises rather than skipping before grading class-var misses as ERROR.
- **New public surface is permanent.** `ValidationReport`/`ValidationIssue`/`IssueKind` become part
  of the API the moment they land in `__all__`. Keeping them frozen dataclasses + `StrEnum` with a
  `to_dict()` escape hatch keeps future additions (new `IssueKind` members) backward compatible;
  adding a *required* field to `ValidationIssue` later would not be.
