# Plan 012 — `UNREACHABLE_PRE_DESTROY` validation for `@Provider`-produced types

Upstream gap: **P22-PROVIDER-PREDESTROY**
(`/home/edoardo/projects/varco/design/upstream-gaps/providify-provider-predestroy.md`,
ledger row `/home/edoardo/projects/varco/UPSTREAM-GAPS.md:59`)

Research basis:
`/home/edoardo/projects/providify/design/cdi-predestroy-producers/research/001-producer-predestroy-lifecycle.md`
(cited below as **[R001]**, with line numbers).

Target release: **2.1.0** (minor — see §Versioning).

---

## Goal

`container.validate()` reports a new `IssueKind.UNREACHABLE_PRE_DESTROY`
**warning** whenever a `SINGLETON`-scoped `ProviderBinding` has no `@Disposes`
disposer *and* the type it produces carries a `@PreDestroy` hook — the exact
silent-leak shape varco hit. `@PreDestroy`'s docstring, the README, and
`docs/agents/` are corrected to state that `@PreDestroy` applies to class
bindings and that `@Disposes` is the teardown pathway for provider-produced
instances.

**Runtime teardown behaviour is unchanged.** Nothing that runs today starts or
stops running.

## Non-goals

- ❌ **Do not** make `@PreDestroy` fall back for `ProviderBinding` in
  `_dispose_sync()` / `_adispose()` (gap-report option 1). See
  §"Considered and rejected — option 1" and Appendix A.
- ❌ Do not touch `providify/container.py:4316-4354` (`_dispose_sync`) or
  `:4550-4582` (`_adispose`) at all in the main sequence.
- ❌ Do not widen `@Disposes` wiring (`_register_module_providers`,
  `container.py:6211-6224`) — its `break`-after-first-match and
  qualifier-blind `_interface_matches` narrowness is a *separate* defect; this
  plan surfaces it as a warning rather than fixing it (see §Edge cases E12).
- ❌ Do not add teardown for `REQUEST`/`SESSION`/`DEPENDENT` provider bindings
  (they get none today — see §Edge cases E9 and §Follow-ups).
- ❌ Do not change `Severity` semantics or `validate(raise_on_error=...)`.
- ❌ Do not change anything in varco (see §Downstream).

---

## Design

### Why validation, not a runtime fallback

Jakarta CDI is explicit and has been stable on this from CDI 1.0 through 4.0:
objects returned from producer methods are **not container-managed** and
"receive no lifecycle callbacks" **[R001:13]**; lifecycle callbacks such as
`@PostConstruct`/`@PreDestroy` are "supported for managed beans, but not for
objects returned from producer methods" **[R001:15]**. A disposer method is
"the only teardown mechanism for producer-produced instances" **[R001:17]**, and
Weld — the RI — "enforces this strictly: no `@PreDestroy` invocation on
producer-produced instances, period" **[R001:83]**. The Weld community
explicitly describes disposers as existing "to provide a way for producers to
simulate the `@PreDestroy` callback because that's something you can't do with
producer methods and fields" **[R001:23]**.

Spring is the opposite: `@PreDestroy` *does* run on `@Bean`-produced singleton
and request-scoped instances **[R001:39]**, but not on prototypes **[R001:43]**
— so even Spring does not run it for *all* factory-produced instances.

providify targets **Jakarta CDI parity**: this repo's framing is that
`@Configuration` is grouping for `@Provider` (= `@Produces`), *not* a
Spring-style factory (see `memory/feedback_jakarta_over_spring.md`). So
providify's current runtime behaviour is **correct by the spec it mirrors**.
The real defects are the two the gap report lists as (b) and (c): the silence,
and the docstring that overstates the contract. **[R001:89]** rates the
validation option as "aligns with CDI spec intent"; **[R001:35]** notes CDI/Weld
themselves ship no such diagnostic, so this is a strict improvement over the RI
rather than a divergence from it.

### Where the check goes

Inside `DIContainer.validate()` (`providify/container.py:5292-5713`), in the
**pass-1 per-binding loop** (`:5428`), immediately after the pass-1
`try/except` block and **before** the `if unresolved: continue` guard at
`:5489-5490`.

```
for idx, binding in enumerate(self._bindings):        # 5428
    owner_name = owner_of(binding)
    try: binding.validate(self)                       # pass 1 — scope tier
    except ScopeViolationDetectedError ...            # 5435
    except LiveInjectionRequiredError ...             # 5455
    except AnnotationResolutionError ...              # 5474  -> unresolved=True
    ► NEW: pass 1b — unreachable @PreDestroy          ◄  insert here
    if unresolved: continue                           # 5489
    ...pass 2 — graph tier (injection points, edges)  # 5492
```

Why here, and not pass 2:

- ✅ The check is **per-binding**, not per-injection-point. Pass 2 iterates
  `_iter_injection_points(binding)`; a provider with zero parameters yields
  zero points, so a pass-2 placement would silently never fire for the exact
  shape in the gap report's reproduction (a zero-arg `@Provider`).
- ✅ It needs **no** candidate resolution, no `memo_filter`, no adjacency —
  only `binding.scope`, `binding.disposer`, `binding.interface`, all set at
  registration time.
- ✅ Placing it **before** the `unresolved` guard means a binding whose
  *parameter* annotations cannot be resolved still gets its teardown defect
  reported. `ProviderBinding.interface` is resolved in `__init__`
  (`binding.py:628-657`) and is independent of parameter-annotation
  resolution, so there is no risk of reporting off a half-built binding.
- ✅ Issue ordering stated in `ValidationReport`'s docstring
  (`validation.py:190-194`: "binding registration order, then per-binding
  injection-point order; cycles appended last") is preserved — the new issue
  sits inside its binding's group.

### The predicate

New **module-level private** helper in `providify/container.py`, placed
directly above `validate()`:

```python
def _unreachable_pre_destroy(binding: AnyBinding) -> LifecycleMarker | None:
    """Return the @PreDestroy hook that *binding* will never invoke, if any."""
    if not isinstance(binding, ProviderBinding):
        return None
    if binding.scope is not Scope.SINGLETON:
        return None
    if binding.disposer is not None:
        return None
    produced = get_origin(binding.interface) or binding.interface
    if not isinstance(produced, type):
        return None
    try:
        return _find_pre_destroy(produced)
    except TypeError:
        return None
```

Notes on each guard (all four are load-bearing, see §Edge cases):

- `isinstance(binding, ProviderBinding)` — `AnyBinding` is exactly
  `ClassBinding | ProviderBinding` (`binding.py:827`, **verified**), so this is
  a total dispatch; no third binding kind exists.
- `binding.scope is not Scope.SINGLETON` — deliberate narrowing, see E9.
- `get_origin(...) or ...` — **mandatory, not defensive**. `_find_pre_destroy`
  → `_find_lifecycle_hook` reads `cls.__mro__`
  (`decorator/lifecycle.py:111`). `typing._GenericAlias.__getattr__` refuses
  to forward dunder attributes, so `Repository[User].__mro__` raises
  `AttributeError` and would crash `validate()` outright. (`types.GenericAlias`
  — `list[int]` — *does* forward it, so the failure is shape-dependent and
  easy to miss.) This settles **scout open question 1: no, `_find_pre_destroy`
  does not handle parameterised generics; the caller must normalise.**
- `isinstance(produced, type)` — `binding.interface` can be `NoneType`
  (`def p() -> None`, handled at `binding.py:646-655`) or any object a
  `returns=` override resolved to.
- `except TypeError` — `_find_lifecycle_hook` raises `TypeError` when a class
  declares two `@PreDestroy`s on itself (`lifecycle.py:123-128`).
  `validate()` is a report builder; it must not raise from a defect it was not
  asked about. ❌ Tradeoff: a produced class with a duplicate `@PreDestroy` is
  reported as "no unreachable hook" rather than as its own issue. Accepted —
  that defect already raises at registration for any class binding, and
  inventing a second issue kind for it is out of scope.

The `ValidationIssue` itself is built by a nested closure beside the existing
`owner_of` / `candidate_name` closures (`container.py:5412-5426`), because
`IssueKind` / `Severity` / `ValidationIssue` are function-local imports
(`:5374`). It reuses `owner_of(binding)` verbatim — so the owner label is
`"@Provider(make_conn)"`, identical to every other provider-owned issue.

### The issue

| field | value |
|---|---|
| `kind` | `IssueKind.UNREACHABLE_PRE_DESTROY` |
| `severity` | `Severity.WARNING` |
| `owner` | `owner_of(binding)` → `"@Provider(redis_cache)"` |
| `param_name` | `hook.fn_name` — the unreachable method's name |
| `requested` | `_type_name(produced)` — the produced type |
| `qualifier` | `binding.qualifier` (may be `None`) |
| `candidates` | `()` — untouched; that field stays `AMBIGUOUS_BINDING`-only |

**Exact message** (one f-string; what / why / how, per this repo's
error-message standard, naming `@Disposes`):

```python
f"@PreDestroy '{hook.fn_name}' on {_type_name(produced)} will never run: "
f"{_type_name(produced)} is produced by {owner_of(binding)}, and @PreDestroy "
f"is only invoked for class bindings (@Singleton/@Component), never for "
f"provider-produced instances. Fix: add a "
f"'@Disposes({_type_name(produced)})' method to the @Configuration that "
f"declares this provider, or register {_type_name(produced)} as a class "
f"binding instead of a provider."
```

Both fixes are named because both are real and the right one depends on
context — a provider registered through bare `container.provide()` outside a
`@Configuration` has *no* way to attach a disposer today (the only writer of
`ProviderBinding.disposer` is `container.py:6223`, **verified by grep — single
call site**), so for that caller the second clause is the only option.

### Why `WARNING` and not `ERROR`

- ✅ `validate(raise_on_error=True)` is the documented default
  (`container.py:5333-5336`) and `docs/agents/usage-rules.md:223-227` (R12)
  tells users to call it at startup. A new **ERROR** kind would turn a
  previously-clean production boot into a hard `ContainerValidationError` on
  upgrade — a breaking change requiring **3.0.0**, for an app that has been
  running fine.
- ✅ The graph is not defective: every injection resolves, `get()` succeeds,
  construction is correct. What is wrong is a *latent teardown* declaration.
  That is the same class of thing as `MISSING_BINDING_DEFAULTED`
  (`validation.py:71-73`), which is `WARNING` precisely because "the runtime
  silently falls back" rather than failing.
- ✅ Every existing `ERROR` kind describes something that either raises at
  runtime or silently picks the wrong object. This does neither.
- ❌ Tradeoff: a strict gate that only inspects `report.errors` will not catch
  it. Callers who want it fatal use `report.issues` /
  `report.warnings`, or `report.ok` (which is `False` for any issue). This is
  documented in the README change (Step 11).

### Alternatives considered

- **Runtime `@PreDestroy` fallback for provider bindings (gap-report option 1)**
  — rejected; see the dedicated section below.
- **`ERROR` severity** — rejected: ❌ makes `validate()` newly raise for
  working applications, which is a major-version change; ✅ would have been
  louder. Severity can be raised later in a major; it can never be lowered
  without the same argument in reverse.
- **Emit a `warnings.warn` at `install()` time instead of a validation issue**
  — rejected: ❌ unstructured, unfilterable, fires during import in test
  suites, and cannot be suppressed per-binding; ✅ would reach users who never
  call `validate()`. The gap report itself asks for the validation surface
  ("varco already calls `assert_no_structural_di_issues()`", gap report §4.2),
  and `validate()` is the repo's designated place for "the library knows this
  at install time and says nothing".
- **Reuse `SCOPE_LEAK` or `LIVE_REQUIRED`** — rejected: ❌ both are anchored to
  `binding.validate()`'s structured exceptions and mean something specific
  about scope width; overloading them would break every existing
  `i.kind == IssueKind.SCOPE_LEAK` filter's meaning.
- **Flag every non-`SINGLETON` provider scope too** — rejected for now: ❌ for
  `DEPENDENT`/`REQUEST`/`SESSION` providers, `@Disposes` does not help either
  (nothing disposes them at all — verified, see E9), so the message's fix
  advice would be wrong; ✅ it *is* a real leak. Tracked in §Follow-ups as its
  own gap rather than mis-advised here.
- **Put the check in `describe()` / a new `container.audit()`** — rejected:
  ❌ new public surface, and downstream gates already call `validate()`.

### Considered and rejected — option 1 (runtime `@PreDestroy` fallback)

The gap report's option 1 is: in `_dispose_sync`/`_adispose`, when
`binding.disposer is None`, look up the produced instance's class
`@PreDestroy` the way the `ClassBinding` branch does.

**Rejected as a deliberate divergence from the specification providify
mirrors.** The CDI rule that producer-returned objects "receive no lifecycle
callbacks" is normative and unchanged across CDI 1.0 → 4.0 **[R001:13, R001:31,
R001:60-62]**; Weld enforces it absolutely **[R001:83]**; and the disposer
method exists *specifically* to fill that gap **[R001:23]**. Shipping the
fallback would mean a `@Configuration`/`@Provider` pair behaves like a Spring
`@Configuration`/`@Bean` pair **[R001:39]** rather than like
`@Produces`/`@Disposes`, quietly re-framing the feature this library models.

- ✅ Would make `@PreDestroy`'s docstring true unconditionally (the gap
  report's strongest argument).
- ✅ Purely additive at the call site; no existing `@Disposes` changes.
- ❌ Contradicts the spec **[R001:88]** and diverges from the RI's enforced
  behaviour **[R001:83]**.
- ❌ Silently changes teardown for anyone who upgrades — a `@PreDestroy` that
  has never run starts running, possibly twice for an instance also reachable
  as a class binding, and possibly *after* a `@Disposes` on a different
  interface already closed the resource.
- ❌ Even Spring, the precedent it appeals to, excludes prototype scope
  **[R001:43, R001:92]**, so "always run it" matches neither model.
- ❌ Would need its own reverse-order/failure-aggregation reasoning inside
  `ashutdown()`'s `ShutdownFailure` handling (`container.py:4584-4613`), plus
  the `_AsyncHookInSyncShutdown` path (`:4349`) — meaningfully more surface
  than a validation kind.

**What would have to be true to reconsider it.** Any one of:
(a) providify's stated target moves from CDI parity toward Spring-style
factories (contradicts `memory/feedback_jakarta_over_spring.md`); (b) a future
CDI revision changes the producer-lifecycle rule — **[R001:31]** finds no such
change through 4.0, so this would need re-research; or (c) it ships strictly
**behind an explicit opt-in flag**, off by default, so nobody's teardown
changes silently. Only (c) is actionable today, and it is written up as
**Appendix A** — a separate, separately-approvable step that is **not** part of
the sequence below and must not be implemented without explicit sign-off.

---

## Steps

TDD-ordered. Steps 1-2 are RED (must fail before Step 3-5 land).

1. [x] `tests/test_unreachable_pre_destroy.py` — **new file**. Module docstring
       naming the gap ID `P22-PROVIDER-PREDESTROY` and this plan.
       `from __future__ import annotations`. Class `TestUnreachablePreDestroy`,
       `container: DIContainer` fixture (from `tests/conftest.py`), pytest
       `asyncio_mode = "auto"` so `async def test_*` needs no marker.
       Cases that **must produce exactly one** `UNREACHABLE_PRE_DESTROY`
       warning:
       - `test_singleton_provider_with_sync_pre_destroy_is_flagged` — the
         gap-report reproduction with a sync `@PreDestroy`.
       - `test_singleton_provider_with_async_pre_destroy_is_flagged` —
         `async def stop(self)` decorated `@PreDestroy`; asserts the async-ness
         does not change kind or severity.
       - `test_inherited_pre_destroy_is_flagged` — hook declared on a base
         class, provider returns the subclass (exercises the MRO walk at
         `decorator/lifecycle.py:111`).
       - `test_generic_produced_type_is_flagged_without_crashing` — provider
         returns `Repo[User]` where `class Repo(Generic[T])` carries the hook.
         **This test must fail with `AttributeError`, not an assertion, before
         Step 3** — that is the proof the `get_origin` guard is required.
       - `test_qualified_provider_is_flagged_and_records_qualifier` — provider
         with `@Provider(qualifier=...)`; asserts `issue.qualifier` is set.
       - `test_bare_provide_outside_configuration_is_flagged` —
         `container.provide(fn)` with no `@Configuration`.

2. [x] `tests/test_unreachable_pre_destroy.py` — negative / control cases in
       class `TestUnreachablePreDestroyNegatives`, each asserting
       **zero** issues of the new kind:
       - `test_provider_with_disposes_produces_no_issue` — the `InfraModule`
         shape from `tests/test_disposes.py:23-31`.
       - `test_produced_class_without_pre_destroy_produces_no_issue`.
       - `test_dependent_provider_produces_no_issue` —
         `@Provider(singleton=False)`.
       - `test_request_scoped_provider_produces_no_issue` —
         `@Provider(scope=Scope.REQUEST)`.
       - `test_session_scoped_provider_produces_no_issue`.
       - `test_class_binding_with_pre_destroy_produces_no_issue` — the
         `ClassBinding` **control**: `container.register(Singleton(Resource))`,
         same `Resource` class; mirrors the control in varco's guard test.
       - `test_provider_returning_none_produces_no_issue` — `def p() -> None`.
       - `test_provider_returning_non_class_produces_no_issue` — `returns=`
         override resolving to something that is not a `type`.
       - `test_empty_container_report_is_unchanged` — regression on
         `TestReportShape`'s empty-report contract
         (`tests/test_validation.py:57`).

3. [x] `providify/validation.py` — add
       `UNREACHABLE_PRE_DESTROY = "unreachable_pre_destroy"` as the **last**
       member of `IssueKind` (after `UNRESOLVED_ANNOTATION`, `:91`), with the
       `#:` comment style the other eight use: state that it means a
       `SINGLETON` `ProviderBinding` has no `@Disposes` and the produced type
       carries a `@PreDestroy` that therefore never runs, and that this mirrors
       Jakarta CDI (producer-returned objects receive no lifecycle callbacks).

4. [x] `providify/validation.py:110-135` — extend `ValidationIssue`'s
       `Attributes` docstring for `param_name` ("...or, for
       `UNREACHABLE_PRE_DESTROY`, the unreachable hook's method name") and
       `requested` ("...or, for `UNREACHABLE_PRE_DESTROY`, the type the
       provider produces"). No field changes — docstring only.

5. [x] `providify/container.py` — add module-level private
       `_unreachable_pre_destroy(binding: AnyBinding) -> LifecycleMarker | None`
       directly above `validate()` (i.e. before `:5292`), exactly as in
       §Design. Full docstring: Args / Returns / Edge cases (generic alias,
       `NoneType`, duplicate-hook `TypeError`) / Thread safety / Async safety /
       Example. Pure function, no container state.

6. [x] `providify/container.py:5412-5426` — add a nested closure
       `unreachable_pre_destroy_issue(b: AnyBinding) -> ValidationIssue | None`
       beside `owner_of` / `candidate_name`, building the issue per the table
       and message in §Design.

7. [x] `providify/container.py` — call it in the pass-1 loop, between the
       `except AnnotationResolutionError` block (ends `:5487`) and
       `if unresolved: continue` (`:5489`):
       ```python
       # ── Pass 1b: teardown tier — an unreachable @PreDestroy ───────
       pre_destroy_issue = unreachable_pre_destroy_issue(binding)
       if pre_destroy_issue is not None:
           issues.append(pre_destroy_issue)
       ```

8. [x] `providify/container.py:5292-5368` — update `validate()`'s docstring:
       add pass 1b to the numbered pass list (`:5303-5315`) and one
       `Edge cases` bullet stating that the check runs on registered bindings
       regardless of `@Profile` activation and requires `install()` to have
       run first (disposers are wired during `install()`, at `:6223`).

9. [x] `providify/decorator/lifecycle.py:161-170` — correct `@PreDestroy`'s
       docstring. It must say: called on shutdown or scope teardown **for
       class bindings** (`@Singleton` / `@RequestScoped` / `@SessionScoped`,
       and `@Component` with `track=True`); **not** called for instances
       returned from a `@Provider` — use `@Disposes` there, matching Jakarta
       CDI, where producer-returned objects receive no lifecycle callbacks
       **[R001:13, R001:15]**. Point at `@Disposes` by name. Keep the existing
       "Equivalent to Jakarta's @PreDestroy" line.

10. [x] `providify/decorator/lifecycle.py:235-258` — add one sentence to
        `@Disposes`'s docstring stating the converse: this is the teardown
        mechanism for provider-produced instances, because `@PreDestroy` is not
        invoked on them.

11. [x] `README.md:936-984` (`### @PreDestroy`) — add a `>` callout after the
        "`DEPENDENT` instances are not owned..." line (`:943`): `@PreDestroy` is
        **not** called for `@Provider`-produced instances; use `@Disposes`
        (link to `### @Disposes` at `:986`); `container.validate()` reports
        `UNREACHABLE_PRE_DESTROY` (WARNING) when a singleton provider produces a
        type with a `@PreDestroy` and has no `@Disposes`.

12. [x] `README.md:986-1013` (`### @Disposes`) — add the reciprocal sentence
        after `:1013` and mention that `validate()` flags the missing case.

13. [x] `README.md:1516-1524` — the `validate()` prose lists the checks as
        "missing bindings, ambiguous bindings, and static cycle detection". Add
        "and unreachable `@PreDestroy` hooks on provider-produced types". Also
        note there that `report.ok` is `False` for warnings while
        `validate(raise_on_error=True)` only raises on errors — so a gate that
        must catch this must inspect `report.issues`, not `report.errors`.

14. [x] `docs/agents/usage-rules.md` — R11 area (`:182-184`) gains the
        provider caveat; R12 (`:223-236`) gains one line naming
        `UNREACHABLE_PRE_DESTROY` and the `report.errors` vs `report.issues`
        distinction.

15. [x] `docs/agents/injection-cheatsheet.md:188-191` — the `@PreDestroy`
        bullet list already says it "never fires on `DEPENDENT` instances";
        add "and never fires for `@Provider`-produced instances — use
        `@Disposes`".

16. [x] `docs/agents/choosing-decorators.md:93` — the row mapping
        `@Disposes` → `@Bean(destroyMethod=...)` is Spring-framed; add the CDI
        column note that in CDI this is `@Disposes` and it is the *only*
        teardown path for `@Produces` results **[R001:17]**.

17. [x] `CHANGELOG.md` — under `## [Unreleased]`, replace `_Nothing yet._`
        with:
        `### Added` — `IssueKind.UNREACHABLE_PRE_DESTROY`, the new
        `validate()` check (WARNING), with the CDI rationale and a link to this
        plan; note it can newly make a previously-clean `report.ok` `False`.
        `### Changed` — `@PreDestroy` / `@Disposes` docstring and README
        corrections.
        Do **not** bump `pyproject.toml:3` in this plan — release cutting is
        its own step (see §Versioning).

18. [x] Verification pass — run §Verification in full; confirm the new tests go
        RED→GREEN and that the pre-existing suite is unchanged in count except
        for the additions.

### Exhaustiveness audit for the new `IssueKind` (do all of these)

| site | action | why |
|---|---|---|
| `providify/validation.py:58-91` | **add member** | the enum itself |
| `providify/__init__.py:71-72, 222` | **no change — verify only** | `IssueKind` is already in `__all__` and already imported; enum *members* ride on the exported type. Confirm by grep that no `__all__` entry names individual kinds. |
| `providify/container.py:5412-5426` | **no change — verify only** | `owner_of` / `candidate_name` dispatch on **binding type** (`ClassBinding` / `ProviderBinding`), not on `IssueKind`. The new issue *reuses* `owner_of`. |
| any `match issue.kind` / exhaustive dict | **verified absent** | grepped: `IssueKind` appears only in `validation.py`, `container.py`, `__init__.py`, `README.md`, `CHANGELOG.md`, `tests/test_validation.py`, `tests/test_profiles.py`, `plans/003`. No `match` on `.kind`, no all-kinds mapping. Re-grep before finishing in case of drift. |
| `README.md:1522` | prose mention (Step 13) | the README defers the per-kind breakdown to the `validate()` docstring |
| `CHANGELOG.md:92-93` | **historical entry — do not edit** | it enumerates the 2.0.0 kinds; the new kind belongs under `[Unreleased]`, not retro-added |
| `providify/container.py` `validate()` docstring | Step 8 | it is the canonical per-kind breakdown per `README.md:1523-1524` |
| test file | Steps 1-2 | |

---

## Edge cases

| # | input / state | expected |
|---|---|---|
| E1 | `SINGLETON` provider, no disposer, produced class has sync `@PreDestroy` | one `UNREACHABLE_PRE_DESTROY` WARNING |
| E2 | same, async `@PreDestroy` | identical issue — sync/async is irrelevant to reachability |
| E3 | `@PreDestroy` inherited from a base class | flagged (`_find_lifecycle_hook` walks the MRO, `lifecycle.py:111`) |
| E4 | produced type is `Repo[User]` (a `typing._GenericAlias`) | flagged via `get_origin`; **must not raise `AttributeError`** |
| E5 | produced type is `list[Thing]` (a `types.GenericAlias`) | `get_origin` → `list`, which has no `@PreDestroy` → no issue |
| E6 | `SINGLETON` provider **with** a `@Disposes` | **no issue** — the designed path is present |
| E7 | produced class has no `@PreDestroy` | no issue |
| E8 | `ClassBinding` with `@PreDestroy` (any scope) | no issue, ever — the hook runs today |
| E9 | `DEPENDENT` / `REQUEST` / `SESSION` provider producing a `@PreDestroy` class | **no issue** (deliberate). Verified: scope-exit teardown indexes **only** `ClassBinding`s (`container.py:4703-4731`, `_index_class_bindings_by_implementation`), and `@Disposes` fires only for cached singletons (`README.md:1013`) — so `@Disposes` would not fix it either and the message's advice would be wrong. See §Follow-ups. |
| E10 | provider declared `-> None` | `NoneType` is a `type` with no hook → no issue |
| E11 | `returns=` override resolving to a non-`type` | `isinstance(produced, type)` guard → no issue, no crash |
| E12 | two providers in one `@Configuration` produce the same interface, one `@Disposes` | the disposer wiring `break`s after the **first** `_interface_matches` hit (`container.py:6219-6224`), so the second provider is flagged. **True positive** — surface it; the narrow wiring is out of scope (see §Non-goals). Add this as a test only if it does not require inventing an unnatural fixture; otherwise document it in the `validate()` docstring Edge cases. |
| E13 | `validate()` called **before** `install()` | disposers are unwired, so every provider that would be fine gets flagged. Documented in Step 8 — `validate()` after registration is already the R12 rule (`docs/agents/usage-rules.md:223`). |
| E14 | provider gated by an inactive `@Profile` | still flagged — pass 1 iterates `self._bindings` unfiltered today (`container.py:5428`); consistent with `binding.validate()` running for inactive bindings too. Document; do not special-case. |
| E15 | declared return type is an ABC/`Protocol` with no hook, but the concrete returned object has one | **not** flagged. `validate()` never instantiates (`container.py:5299-5301`); it can only see the declared type. Documented limitation. |
| E16 | produced class declares **two** `@PreDestroy`s on itself | `TypeError` swallowed → no issue. Tradeoff documented in §Design. |
| E17 | empty container | report unchanged: `issues=()`, `ok is True` |

---

## Verification

```bash
cd /home/edoardo/projects/providify

# RED first: after Steps 1-2, before Steps 3-7
uv run pytest tests/test_unreachable_pre_destroy.py -x -q     # must FAIL

# GREEN: after Step 7
uv run pytest tests/test_unreachable_pre_destroy.py -q

# no regression in the validation surface or the lifecycle surface
uv run pytest tests/test_validation.py tests/test_lifecycle.py \
              tests/test_disposes.py tests/test_shutdown_order.py \
              tests/test_scoped_providers.py tests/test_profiles.py -q

# full suite
make test

# lint + format (the repo has no type-check target — ruff only)
make format-check
```

Manual check that the message reads correctly (paste into a scratch script):
the printed warning must name the hook, the produced type, the provider, and
`@Disposes(<Type>)`.

Also re-run the exhaustiveness grep before finishing:

```bash
rg -n 'IssueKind' --glob '!plans/*' .
rg -n 'match .*\.kind' .
```

---

## Versioning

**Minor — 2.1.0.** Not a patch.

- `CONTRIBUTING.md:71-74`: the public API is everything in
  `providify/__init__.py`'s `__all__`, and `IssueKind` is in it
  (`__init__.py:71`). Adding a member changes the set of values that public
  type can take — observable to every consumer.
- Behaviour visible to users changes: a container that reported
  `report.ok is True` can now report `False`. `ValidationReport.ok` is `False`
  for **any** issue, warnings included (`validation.py:176-200`).
- Concretely: varco's `assert_no_structural_di_issues()` is a strict gate. If
  it inspects `report.issues` (or `report.ok`) rather than `report.errors`, a
  previously-green downstream gate goes red on upgrade. A patch release must
  not be able to do that.
- It is **not** a major, because `validate(raise_on_error=True)` — the
  documented default and the R12-recommended call — still does not raise:
  the new kind is `WARNING`, and warnings never raise
  (`container.py:5333-5336`).
- Sequencing: land this plan's steps with the CHANGELOG under
  `[Unreleased]`; the `pyproject.toml` bump to `2.1.0` and the CHANGELOG
  heading move happen in the release cut, not here.

---

## Downstream — what varco must do

⚠️ **This plan does not turn varco's guard test green, and is not meant to.**

- `varco_core/tests/test_providify_provider_predestroy.py` is a `strict=True`
  xfail asserting **runtime** behaviour (`resource.closed` after `ashutdown()`).
  The recommended path leaves runtime teardown unchanged, so that test stays
  xfailing — correctly, because the runtime behaviour it asserts is the one
  CDI specifies **[R001:13, R001:83]** and providify should not change.
  varco should **rewrite or retire** that guard: keep the `ClassBinding`
  control, and replace the provider assertion with either (a) a
  `container.validate()` assertion that the new
  `UNREACHABLE_PRE_DESTROY` warning is raised, or (b) deletion once the
  `@Disposes` fixes land.
- `varco_redis/tests/test_redis_cache_lifespan_shutdown_integration.py`
  (strict xfail, real Redis) goes green only after varco adds its `@Disposes`.
- **The actual varco fix is the one its own gap report already prescribes in
  §5**: add `@Disposes(CacheBackend)` to `RedisCacheConfiguration` and the
  Memcached equivalent. That is available today and needs nothing from
  providify. The gap report's §5 table is explicit that this is a varco-owned
  defect.
- **What this plan buys varco**: the new `UNREACHABLE_PRE_DESTROY` warning is
  what would have caught both leaks at the moment the wiring was written — but
  only if `assert_no_structural_di_issues()` inspects `report.issues` /
  `report.ok`, not just `report.errors`. varco should confirm that.

---

## Risks

- ⚠️ **ASSUMPTION** — varco's `assert_no_structural_di_issues()` inspects
  warnings, not only `report.errors`. Not verified (varco source not read for
  this plan). If it only checks errors, the new kind will not fire varco's gate
  and varco must widen it. Invariant that must hold either way: providify does
  not raise on warnings.
- ⚠️ **ASSUMPTION** — no third-party consumer treats `IssueKind` as a closed
  set (e.g. `match` with no `case _`). Unverifiable outside this repo;
  in-repo it is **verified false** (no exhaustive match exists). Mitigated by
  shipping as a minor with an explicit CHANGELOG line.
- ⚠️ **ASSUMPTION** — the scout's open question 3 ("does varco's xfail guard
  cover both sync and async `@PreDestroy`?") is **unresolved**; varco's test
  file was not read. It does not gate this plan (both are covered by Steps 1's
  tests here), but varco must confirm when it rewrites the guard.
- ⚠️ **ASSUMPTION** — `_type_name(produced)` renders acceptably for every
  produced shape in the message. Verified for concrete types and generic
  aliases (`utils.py:29-51`); unverified for exotic `returns=` results, which
  are guarded out by `isinstance(produced, type)` anyway.
- **SETTLED (scout open question 1)** — `_find_pre_destroy` does **not** handle
  parameterised generics: `_find_lifecycle_hook` reads `cls.__mro__`
  (`lifecycle.py:111`) and `typing._GenericAlias` does not forward dunders.
  Handled by the mandatory `get_origin` normalisation; E4 is the regression
  test. **Do not remove that guard.**
- **SETTLED (scout open question 2)** — there are exactly two binding kinds:
  `AnyBinding = ClassBinding | ProviderBinding` (`binding.py:827`). No other
  kind carries lifecycle hooks.
- **New-noise risk** — an existing user with a `@Provider` producing a
  `@PreDestroy`-bearing class and a deliberate manual teardown will now see a
  warning they consider a false positive. There is no per-binding suppression
  today. Mitigation: `WARNING` severity, message names both fixes, and
  CHANGELOG calls it out. Invariant: `validate(raise_on_error=True)` must
  still not raise for them.
- **Placement risk** — inserting pass 1b *before* `if unresolved: continue`
  means the new issue can appear for a binding that also reports
  `UNRESOLVED_ANNOTATION`. That is intended (the two are independent), but any
  test asserting exact issue *counts* per binding in `tests/test_validation.py`
  could shift. Invariant: `ProviderBinding.interface` is fully resolved in
  `__init__` (`binding.py:628-657`) and never depends on parameter-annotation
  resolution.
- **Ordering risk** — `disposer` is assigned during `install()`
  (`container.py:6223`). Any code path that calls `validate()` between
  `provide()` and `install()` gets false positives (E13). No mitigation beyond
  documentation; the R12 rule already says validate after registration.

---

## Follow-ups (not this plan)

1. **`REQUEST`/`SESSION`/`DEPENDENT` provider bindings receive no teardown at
   all** — neither `@PreDestroy` (scope exit indexes only `ClassBinding`s,
   `container.py:4703-4731`) nor `@Disposes` (singleton cache only,
   `README.md:1013`). This is a distinct gap from P22 and deserves its own
   report; only once it has a teardown mechanism can the validation in this
   plan be widened past `SINGLETON`.
2. **`@Disposes` wiring is first-match and qualifier-blind**
   (`container.py:6219-6224`): `break` after the first `_interface_matches`
   hit, ignoring `binding.qualifier`. Two same-interface providers cannot both
   get disposers. Separate defect; this plan surfaces it as a warning (E12).

---

## Appendix A — OPTIONAL, separately approvable: opt-in runtime fallback

**Not part of the sequence above. Do not implement without explicit sign-off.**
Everything here is additive to, and independent of, Steps 1-18.

If a runtime fallback is wanted despite §"Considered and rejected", the only
form that does not diverge silently is an explicit, default-off opt-in:

- A1. [ ] `providify/container.py` — `DIContainer.__init__` gains
      `provider_pre_destroy_fallback: bool = False` (keyword-only), stored as
      `self._provider_pre_destroy_fallback`. Docstring must state plainly that
      enabling it makes providify deviate from Jakarta CDI, where
      producer-returned objects receive no lifecycle callbacks **[R001:13]**,
      and behave like Spring `@Bean` singletons **[R001:39]**.
- A2. [ ] `providify/container.py:4550-4582` (`_adispose`) — when the flag is
      on **and** `binding.disposer is None`, resolve
      `_find_pre_destroy(get_origin(binding.interface) or binding.interface)`
      on the cached instance and await/call it, mirroring the `ClassBinding`
      branch.
- A3. [ ] `providify/container.py:4316-4354` (`_dispose_sync`) — sync mirror,
      including raising `_AsyncHookInSyncShutdown` (`:4349`) for an async hook.
- A4. [ ] `providify/container.py:4296-4314` (`_has_teardown_hook`) — must
      return `True` for that case too, or the binding never enters the
      teardown plan (`_teardown_plan`, `:4214-4285`) and A2/A3 never run.
      **This is the step most likely to be missed.**
- A5. [ ] `providify/container.py:6660-6690` — `DIContainer` copy/snapshot
      construction must propagate the flag (same block that re-wires
      `on_scope_exit`).
- A6. [ ] When the flag is on, `_unreachable_pre_destroy` must return `None`
      (the hook is now reachable) — otherwise `validate()` reports a defect
      that no longer exists.
- A7. [ ] Tests: flag off → hook does not run (locks in CDI parity as the
      default); flag on → sync and async hooks run, in reverse-dependency
      order, with `ShutdownError` aggregation intact; flag on **plus**
      `@Disposes` → **only** the disposer runs, never both.
- A8. [ ] README + `docs/agents/choosing-decorators.md`: document it as a
      Spring-compatibility escape hatch, explicitly labelled a deviation.

Tradeoffs: ✅ nobody's teardown changes without asking; ✅ gives Spring
migrants a one-line switch. ❌ a second teardown code path to keep correct
forever; ❌ two containers in the same process can now behave differently at
shutdown, which is a hard thing to debug; ❌ the flag is public API and
therefore permanent under `CONTRIBUTING.md:71-83`.
