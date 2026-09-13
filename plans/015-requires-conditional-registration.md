# Plan 015 — `@Requires(condition=…, env=…, value=…)` conditional registration

Upstream gap: **P25-CONDITIONAL-REGISTRATION**
(`/home/edoardo/projects/varco/design/upstream-gaps/providify-conditional-registration.md`,
cited below as **[GAP:line]**; index row `plans/000-index-upstream-gaps-p24-p27.md:15`).

Research basis:
`/home/edoardo/projects/varco/design/research/014-multi-binding-qualifiers-and-conditional-di.md`
(cited as **[R014:line]**). ⚠️ That brief's claims that providify *already ships*
`@Requires` (`[R014:196-197, 211-213]`) are wrong at 2.0.1 — the brief carries a
correction banner at `[R014:3-5]` and the gap report re-confirms the absence by
source grep **[GAP:47-58]**. Nothing in this plan relies on those lines; only
the Micronaut/Spring *spelling* precedent is cited.

Coding standard for every step: `/home/edoardo/.claude/skills/coding-practice/SKILL.md`
(why-comments, full docstrings with Args/Returns/Raises/Thread safety/Async
safety/Edge cases/Example, minimal abstraction).

Target release: **2.1.0** (minor — see §Versioning). Plan 2 of 4 in the set;
build order **014 → 015 → 017 → 016** (index `:21`). **This plan adds
`Severity.INFO`**; Plan 017 reuses it (index `:26-29`).

---

## Goal

A `@Requires(...)` marker decorator, exported from `providify`, gates a class
binding or a `@Provider` function/`@Configuration` method on a **predicate
evaluated lazily at resolve time** — the third conjunct of
`DIContainer._binding_is_active()` beside `@Profile` and `@Alternative`. Two
spellings, combinable by AND:

- `@Requires(condition=Callable[[], bool])` — the primitive **[GAP:160]**.
- `@Requires(env="NAME")` / `@Requires(env="NAME", value="x")` — sugar over the
  primitive reading `os.environ` at evaluation time **[GAP:161]**.

`container.validate()` gains a new **informational** issue,
`IssueKind.CONDITION_INACTIVE` at the new `Severity.INFO` tier, for every
registered binding whose `@Requires` currently evaluates `False`
**[GAP:163]**. `validate(raise_on_error=True)` never raises on INFO.

## Non-goals

- ❌ **No scan-time / install-time evaluation, no `eager=` option.** Rejected as
  the primary by the gap report **[GAP:162]** and *not* offered even as opt-in
  here — see §Alternatives.
- ❌ No memoisation of predicate results. Conditions are evaluated on every
  `_filter()` call exactly like `@Profile` (`container.py:1665`); the contract
  is "cheap and pure" (docstring + README). See §Alternatives.
- ❌ No eviction of an already-cached singleton whose condition later turns
  false — same documented caveat as `activate_profile()`
  (`container.py:1865-1868`).
- ❌ No change to `describe()`. `BindingDescriptor` (`descriptor.py:49-54`)
  carries no `profiles`/`alternative` field today (grep of `descriptor.py` for
  `profile|alternative`: zero hits), so there is nothing to add the condition
  *beside*. Listed in §Follow-ups.
- ❌ No `@Requires` on a `@Configuration` **class** gating all its providers.
  `ProviderBinding.profiles` is read off the provider **function** only
  (`binding.py:661`); `@Requires` mirrors that exactly. Documented as an edge
  case, not implemented (§Follow-ups).
- ❌ Do not restructure `_filter()` (`container.py:1714-1733`) — Plan 017 owns
  a post-filter step at the end of that comprehension. This plan touches only
  `_binding_is_active()` and `_filter()`'s **docstring** truth table.
- ❌ Do not touch `_interface_matches` (`utils.py`) — Plan 016.
- ❌ Do not add a settings-object/`@ConfigProperties`-aware condition form
  (`condition=` closes over whatever the caller wants — **[GAP:161]** says
  env-only must never be the sole form, and it is not).
- ❌ No `Severity.INFO` semantics beyond "never raises, never affects
  `report.ok`". No per-binding suppression mechanism.

---

## Design

### Naming — `Requires`, `condition=`, `env=`, `value=`

Micronaut spells conditional registration `@Requires(property=..., value=...)`
**[R014:73, R014:99]** and has an `env=` form **[R014:101]**; Spring Boot spells
it `@ConditionalOnProperty(name=..., havingValue=...)` **[R014:87]** with a
family of `@ConditionalOnClass/Bean/MissingBean` siblings **[R014:88-90]**. The
gap report asks for the Micronaut spelling verbatim **[GAP:161]**, and varco's
guard imports `Requires` by name **[GAP:111, GAP:225-231]**.

- ✅ `Requires` is one name, one decorator, keyword-only — greppable, no
  taxonomy of `ConditionalOn*` variants to promise.
- ✅ `condition=` is the generic escape hatch; `env=`/`value=` are the 80% case
  **[GAP:161]** and read as `os.environ` to any Python reader.
- ⚠️ Divergence to document: in Micronaut `@Requires(env=...)` names an
  *environment* (≈ providify `@Profile`), not an OS variable. In providify the
  profile role is already taken by `@Profile`, so `env=` here is unambiguously
  `os.environ[...]`. The docstring says so explicitly.

### Where the pieces live

```
providify/metadata.py      RequiresMarker (frozen dataclass) + _REQUIRES_ATTR
                           + _get_requires_markers() / _set_requires_markers()
providify/decorator/scope.py   def Requires(*, condition=None, env=None, value=None)
providify/binding.py       ClassBinding.conditions / ProviderBinding.conditions
                           (registration-time cache, mirrors .profiles :189/:661)
providify/exceptions.py    ConditionEvaluationError(providifyError)
providify/container.py     _conditions_hold(b)  (module-level, beside :443)
                           _binding_is_active() third conjunct  (:1606-1676)
                           _filter() docstring truth table  (:1691-1698)
                           validate() pass 1c -> CONDITION_INACTIVE  (:5487-5493)
providify/validation.py    Severity.INFO, IssueKind.CONDITION_INACTIVE,
                           ValidationReport.infos, __repr__ third tier
providify/__init__.py      export Requires, RequiresMarker, ConditionEvaluationError
```

### The marker — `RequiresMarker`

Frozen dataclass in `providify/metadata.py`, placed after `ProfileMetadata`
(`:159-188`), mirroring its storage guarantees and its "the TYPE is the
signal" rule (`:167-169`):

```python
_REQUIRES_ATTR = "__di_requires__"   # storage slot only — value is tuple[RequiresMarker, ...]

@dataclass(frozen=True)
class RequiresMarker:
    condition: Callable[[], bool] | None = None
    env: str | None = None
    value: str | None = None

    def is_satisfied(self) -> bool:
        """AND of every populated clause; reads os.environ NOW (lazy)."""
        if self.env is not None:
            actual = os.environ.get(self.env)
            if self.value is None:
                if not actual:            # unset OR empty string -> False
                    return False
            elif actual != self.value:    # exact, case-sensitive equality
                return False
        if self.condition is not None and not self.condition():
            return False
        return True

    def describe(self) -> str:
        """Declaration-form rendering for messages: "@Requires(env='X', value='y')"."""
```

Decisions baked into the marker:

- **Stacking → tuple of markers, not a merge.** `_REQUIRES_ATTR` stores
  `tuple[RequiresMarker, ...]`; a second `@Requires` appends. AND across the
  tuple. ✅ each marker keeps its own `(condition, env, value)` so the
  `CONDITION_INACTIVE` message and `ConditionEvaluationError` can name *which*
  clause failed/raised; ✅ a marker stays a plain, picklable value. ❌ Merging
  into one composed closure (the `@Profile` union-merge analogue, `scope.py:786-796`)
  would erase that — rejected.
- **Truthiness, not strict `bool`.** `not self.condition()` coerces. ✅ matches
  the `if condition():` idiom and lets `lambda: os.environ.get("X")` work;
  ❌ a predicate returning the string `"false"` is truthy — documented in the
  docstring as the caller's responsibility. Strict `isinstance(result, bool)`
  rejected: it would turn the most natural lambdas into `TypeError`s.
- **`env=` without `value=` means "set to a non-empty string".** `""` and
  unset are both inactive — the same rule `parse_profiles` applies to
  `PROVIDIFY_PROFILES=""` (`profiles.py:119`).
- **`value=` is exact, case-sensitive equality; env names are not normalised.**
  Unlike `@Profile`'s lower-casing (`scope.py:712-714`), OS environment
  variable names are case-sensitive on POSIX and values are user data
  (`"Redis"` ≠ `"redis"`). Documented.
- **`os.environ` is read inside `is_satisfied()`, never at decoration time** —
  that is what makes the sugar genuinely lazy **[GAP:161]** and lets tests use
  `monkeypatch.setenv` after import.
- `is_satisfied()` performs **no** exception handling. Wrapping happens in the
  container, which knows the binding's name (below).

Accessors, mirroring `_get_profile_metadata` (`metadata.py:548-590`) including
its bound-method `__func__` fallback (`:581-588`) — that fallback is what makes
the marker readable off a `@Configuration` method's bound `ProviderBinding.fn`:

- `_get_requires_markers(obj: Any) -> tuple[RequiresMarker, ...]` — `()` when
  unmarked; own `__dict__` only, never the MRO (subclasses do not inherit,
  matching `@Profile`, `scope.py:754-755`).
- `_set_requires_markers(obj: Any, markers: tuple[RequiresMarker, ...]) -> None`
  — sole write path (mirrors `_set_profile_marker`, `:619-645`).

### The decorator — `Requires`

In `providify/decorator/scope.py`, directly after `Profile` (`:694-798`),
same shape (a factory returning `decorator(target)` that stamps and returns
`target` unchanged). Keyword-only:

```python
def Requires(
    *,
    condition: Callable[[], bool] | None = None,
    env: str | None = None,
    value: str | None = None,
) -> Callable[[Any], Any]:
```

Decoration-time validation (fail fast, mirroring `@Profile`'s `ValueError` at
`scope.py:773-774`):

| input | result |
|---|---|
| neither `condition` nor `env` | `ValueError("@Requires needs at least one of condition= or env=.")` |
| `value=` given, `env` is `None` | `ValueError("@Requires(value=...) is only meaningful together with env=.")` |
| `env=""` / whitespace-only | `ValueError("@Requires(env=...) must name a non-empty environment variable.")` |
| `condition` given but not `callable` | `TypeError` |
| `env` given as non-`str` | `TypeError` |
| `value=""` with `env="X"` | **allowed** — means "X is set and exactly empty" (E9) |

The decorator is applied *after* `@Provider`/`@Singleton` in the stack (i.e.
written above them), exactly like `@Profile`'s examples (`scope.py:757-768`),
and is order-insensitive because every scope decorator returns the same object
it was given (that is why `@Profile` composes today).

### Binding-level cache — `.conditions`

`ClassBinding.__init__` (`binding.py:189`) and `ProviderBinding.__init__`
(`:661`) already cache `.profiles` "once at construction — `_filter()` runs per
resolution and must not re-read markers (plan 005 §Design)". Add, on the very
next line of each:

```python
self.conditions: tuple[RequiresMarker, ...] = _get_requires_markers(implementation)   # / (fn)
```

The hot-path short-circuit is then `not b.conditions` — zero cost for the
overwhelmingly common unconditional binding, same shape as `not b.profiles`
(`container.py:1662-1665`). The synthetic self-binding that `bind()` appends
(`container.py:1023-1024`) reads the same class, so it is gated identically —
`container.get(OnImpl)` and `container.get(Base)` agree.

### Evaluation — third conjunct in `_binding_is_active()`

New module-level private helper in `providify/container.py`, placed directly
**above** `_unreachable_pre_destroy` (`:443`) so the two module-level
binding-predicate helpers sit together:

```python
def _conditions_hold(b: AnyBinding) -> bool:
    """AND of every @Requires marker on *b*; wraps a raising predicate."""
    for marker in b.conditions:
        try:
            satisfied = marker.is_satisfied()
        except Exception as exc:           # a broken predicate is a programming error
            raise ConditionEvaluationError(_owner_label(b), marker, exc) from exc
        if not satisfied:
            return False
    return True
```

`_owner_label(b)` is the same `"ClassName"` / `"@Provider(fn_name)"`
vocabulary as `validate()`'s `owner_of` closure (`container.py:5383-5390`);
implement it as a two-line module-level helper and have `owner_of` keep its
body (do **not** refactor `owner_of` — Plan 017 also edits `validate()`).

`_binding_is_active()` (`:1606-1676`) becomes:

```
profile_ok(b)      : unchanged                                   (:1665-1667)
alternative_ok(b)  : unchanged rule, but computed into a local
                     instead of `return`ing early                (:1669-1676)
condition_ok(b)    : not b.conditions or _conditions_hold(b)     <- NEW, last
return profile_ok and alternative_ok and condition_ok
```

Why the condition is evaluated **last**:

- ✅ User code never runs for a binding that profile/alternative state already
  excludes — cheaper, and a `@Profile("prod")`-gated predicate that only works
  in prod cannot break a dev `get()`.
- ✅ Keeps the `@Profile`-overrides-`@Alternative` rule (`:1672-1675`) textually
  intact — only the two `return` tails are replaced by a local.
- ❌ `validate()` pass 1c (below) evaluates conditions on *every* registered
  binding regardless of profile state — so a raising predicate on a
  profile-inactive binding surfaces in `validate()` but not in `get()`. This
  is the intended asymmetry (E14): `validate()` is the "tell me everything"
  surface.

Update the docstring formula at `:1614-1623` to three conjuncts and add
`condition_ok` to the `Edge cases` block. Update `_filter()`'s truth table
(`:1691-1698`) with three new rows:

```
| <any "yes" row above> + every @Requires satisfied        | yes |
| <any "yes" row above> + any @Requires not satisfied      | no  |
| @Requires predicate raises                                | ConditionEvaluationError propagates |
```

and the inline comment at `:1731` becomes
`# @Profile / @Alternative / @Requires activation — see _binding_is_active().`

Because every public lookup — `get()`, `aget()`, `get_all()`, `aget_all()`,
`is_resolvable()` (`:1777-1780`), `InstanceProxy`, and `validate()`'s
`memo_filter` (`:5365-5381`) — goes through `_filter()` ("the same logic is
shared by both the sync and async resolution paths", `:1686-1687`), the
condition takes effect everywhere with this one change.

### `ConditionEvaluationError`

New in `providify/exceptions.py`, subclassing `providifyError` directly
(same rationale `ModuleCycleError` gives at `:329-345` for not reusing a
sibling: a caller with a narrow `except LookupError` around `get()` must not
start catching predicate bugs). Attributes `owner: str`, `marker: Any`
(untyped to keep `exceptions.py` free of a `metadata.py` import),
`__cause__` = the original exception via `raise ... from exc`. Message:

```
f"@Requires condition on {owner} raised {type(exc).__name__}: {exc}. "
f"Conditions must be cheap, pure, and must not raise — fix the predicate "
f"({marker.describe()}) rather than catching this error."
```

- ✅ Original exception type/traceback preserved through `__cause__`.
- ❌ Alternative "re-raise the original unchanged" rejected: the binding name
  is the one thing a user needs and the original has no way to carry it.
- ❌ Alternative "swallow → treat as inactive" rejected: silently hides bugs;
  the brief and **[GAP:160]** both treat a broken condition as a programming
  error.

### `validate()` — pass 1c, `CONDITION_INACTIVE` at `Severity.INFO`

Pass 1 stays **unfiltered** (`:5426`, docstring `:5327-5333`). Insert
**Pass 1c** immediately after pass 1b (`:5487-5490`) and before
`if unresolved: continue` (`:5492`), mirroring exactly how
`unreachable_pre_destroy_issue` is a closure beside `owner_of` (`:5399-5424`)
and called from the loop:

```python
def condition_inactive_issue(b: AnyBinding) -> ValidationIssue | None:
    if not b.conditions:
        return None
    failed = [m for m in b.conditions if not m.is_satisfied()]   # may raise -> propagates
    if not failed:
        return None
    return ValidationIssue(
        kind=IssueKind.CONDITION_INACTIVE,
        severity=Severity.INFO,
        owner=owner_of(b),
        message=...,                      # below
        requested=_type_name(b.interface),
        qualifier=b.qualifier,
    )
```

⚠️ Evaluate via `marker.is_satisfied()` per marker (not `_conditions_hold`)
so the message can list *every* unsatisfied clause; wrap the loop in the same
`try/except Exception → ConditionEvaluationError` as `_conditions_hold` (or
factor a tiny `_evaluate_marker(b, marker)` both call — implementer's choice,
keep it to one helper).

**Exact message** (what / why / how, naming the fix):

```python
clauses = "; ".join(_render_failed(m) for m in failed)
f"{owner_of(b)} is registered but currently inactive: {clauses}. It is "
f"excluded from get()/get_all()/is_resolvable() and from candidate "
f"resolution in this report until the condition holds, so any injection "
f"point that only this binding could satisfy is reported as "
f"MISSING_BINDING. Nothing to fix unless you expected it to be active."
```

where `_render_failed(m)` is `m.describe()` plus, for an `env=` marker, the
current state — `" (X is unset)"`, `" (X is '')"`, or
`" (X is 'actual')"` — because "what is the variable *actually* set to" is the
first thing an operator asks. For a `condition=` marker just
`"@Requires(condition=<qualname>) returned False"`.

| field | value |
|---|---|
| `kind` | `IssueKind.CONDITION_INACTIVE` |
| `severity` | `Severity.INFO` |
| `owner` | `owner_of(b)` |
| `param_name` | `None` (binding-level, not anchored to a parameter) |
| `requested` | `_type_name(b.interface)` |
| `qualifier` | `b.qualifier` |
| `candidates` | `()` |

Ordering guarantee (`validation.py:198-202`) is preserved: the issue sits in
its binding's group, after 1b's warning if both fire.

**Why INFO and not WARNING.** `MISSING_BINDING_DEFAULTED` and
`UNREACHABLE_PRE_DESTROY` are WARNINGs because "the runtime silently falls
back" / "a declared hook never runs" — something is *off*. A false `@Requires`
is the feature **working as declared**; reporting it at all exists so DI-7's
"why is this the live one" question has an answer **[GAP:163]**. A WARNING
would make `report.warnings` noisy for every correctly-configured deployment
and would stop `_validated` being set... (it does anyway, see E17). Hence a
third tier.

### `Severity.INFO` — exhaustive audit

`Severity` is `ERROR`/`WARNING` only (`validation.py:41-55`). Grep
`Severity\.` over `providify/` and `tests/` (`--glob '!plans/*'`) at planning
time gave **exactly** these sites; each gets the listed action:

| site | action |
|---|---|
| `providify/validation.py:54-55` | **add** `INFO = "info"` after `WARNING`; extend the class docstring (`:42-52`): INFO = "the container is doing what it was told; recorded for wiring reports; never raises, never affects `ok`" |
| `providify/validation.py:117` | docstring: `Severity.ERROR`, `Severity.WARNING`, **or `Severity.INFO`** |
| `providify/validation.py:138` | example — no change |
| `providify/validation.py:222-230` | **add** `infos` property after `warnings` (same shape, `is Severity.INFO`) |
| `providify/validation.py:233-239` | `ok` — **no change** (`not self.errors`); add one docstring line: INFO issues, like warnings, never make `ok` False |
| `providify/validation.py:258-277` | `__repr__` — **must change**: `for issue in (*self.errors, *self.warnings, *self.infos)`. Without this an INFO issue is silently dropped from the rendered report. Update the format block in its docstring (`:261-265`) with a `[INFO]` line |
| `providify/validation.py:186-217` | `ValidationReport` docstring `Example` — no change; `Attributes` — no change |
| `providify/container.py:5409, 5440, 5458, 5476, 5506, 5534, 5571, 5615, 5632, 5658, 5692` | constructors of existing issues — **no change, verify only** |
| `providify/container.py:5704-5709` | `_validated = True` only when `not report.issues` — **no change** (E17 documents the consequence) |
| `providify/container.py:5711-5712` | `raise_on_error and report.errors` — **no change**; this is the invariant "INFO never raises" |
| `providify/container.py:5298-5302` | `validate()` Args docstring: "Warnings **and INFO issues** never raise" |
| `providify/exceptions.py:242-250` | `ContainerValidationError` message counts `report.errors` only — **no change, verify only** |
| `tests/test_validation.py:108-115` | extend the partition test to assert `report.infos` is a tuple and the three views are disjoint |
| `tests/test_validation.py:331-633`, `tests/test_unreachable_pre_destroy.py:246-329` | existing `== Severity.ERROR/WARNING` assertions — untouched |
| `README.md:1516-1521` | the inspect-the-report example gains `for issue in report.infos: log.info(...)` |
| `docs/agents/usage-rules.md:243-248` | add the INFO tier sentence (Step 22) |
| `providify/__init__.py:72` | `Severity` already exported — members ride along; verify only |

No `match issue.severity` / exhaustive dict exists in-repo (re-grep before
finishing: `rg -n 'match .*severity' .`).

### `IssueKind.CONDITION_INACTIVE` — exhaustive audit

Same table as plan 012 §"Exhaustiveness audit" (`plans/012:440-452`) applies
verbatim: add the member as the **last** entry of `IssueKind`
(`validation.py:96`, after `UNREACHABLE_PRE_DESTROY`) with a `#:` comment;
`IssueKind` is already in `__all__` (`__init__.py:71`); no exhaustive `match`
on `.kind` exists; `CHANGELOG.md:16-25` is historical — do not edit; the
`validate()` docstring is the canonical per-kind breakdown (`README.md:1532-1533`).
⚠️ Plan 014 adds `DISPOSER_OWERWRITTEN` and Plan 017 adds `FALLBACK_SHADOWED`
to the same enum — when merging, keep all three, order by plan number.

### Alternatives considered

- **Scan-time / install-time evaluation (drop the binding if false)** —
  rejected **[GAP:162]**: ❌ breaks the ordering-independence
  `activate_profile()` preserves (`container.py:1839-1846` — the reachable
  graph may change after registration); ❌ a condition over a DI-provided
  settings object cannot run before that provider exists; ❌ `describe()`
  would lie by omission. ✅ would give zero per-lookup cost. Not offered even
  as `eager=True`: two evaluation points means two sets of semantics to keep
  consistent forever under `CONTRIBUTING.md`'s public-API rule.
- **Memoise `is_satisfied()` per marker (`functools.cache`)** — rejected:
  ❌ a memoised predicate can never flip, which defeats the `flag["on"] = False`
  reproduction **[GAP:132-133]**; ❌ `os.environ` changes (tests,
  `monkeypatch`) would be invisible; ✅ would bound cost. The `@Profile`
  precedent already evaluates `matches()` on every `_filter()`
  (`container.py:1665`) with no cache — same contract, documented as "cheap
  and pure".
- **Settle conditions once in `install()` / on first `get()` with an explicit
  `container.refresh_conditions()`** — rejected: ❌ new container state and a
  new public method for a problem that lazy evaluation does not have;
  ❌ contradicts "zero new container state" **[GAP:160]**.
- **Merge stacked `@Requires` into one composed closure (mirror `@Profile`'s
  union merge)** — rejected, see §The marker.
- **`WARNING` severity for `CONDITION_INACTIVE`** — rejected, see §validate();
  it would also pollute `report.warnings`, which `docs/agents/usage-rules.md:247`
  tells strict gates to inspect.
- **Emit `CONDITION_INACTIVE` only when the binding is *otherwise* active
  (profile matches, alternative enabled)** — rejected: ❌ pass 1 is
  unfiltered by design (`:5327-5333`) and `UNREACHABLE_PRE_DESTROY` already
  fires for profile-inactive bindings (plan 012 E14); ✅ would reduce noise.
  Consistency wins; the message is INFO precisely so noise is cheap.
- **A `CONDITION_FAILED` ERROR issue instead of raising
  `ConditionEvaluationError` from `validate()`** — rejected: ❌ a second new
  kind; ❌ `get()` must raise for the same predicate anyway, so `validate()`
  swallowing it would make the two surfaces disagree.
- **Strict `bool` return check** — rejected, see §The marker.
- **`ConditionEvaluationError(BindingError)`** — rejected: `BindingError`
  subclasses (`exceptions.py:24-45`) all mean "registration was malformed";
  this is a resolution-time failure of user code.

---

## Steps

TDD-ordered. Steps 1-4 are RED and must fail before Steps 5-15 land.

1. [x] `tests/test_requires.py` — **new file**. Module docstring naming
       `P25-CONDITIONAL-REGISTRATION` and this plan. `from __future__ import
       annotations`; module-level sentinel classes (see `tests/test_profiles.py:33-40`
       for why `@Provider` return types must be module-level). Use the
       `container` fixture (`tests/conftest.py:36-37`); `asyncio_mode = "auto"`.
       Class `TestRequiresReproduction`:
       - `test_gap_report_reproduction` — **[GAP:111-133]** ported verbatim
         **except** that `OnImpl`/`OffImpl` each get `@Component`.
         ⚠️ Verified: `bind()` → `ClassBinding.__init__` raises
         `ClassBindingNotDecoratedError` for an undecorated class
         (`binding.py:191-193`); the gap report's bare classes cannot be bound
         at 2.0.1 or after this plan. See §Downstream.
       - `test_condition_toggles_at_resolve_time` — same shape, asserts
         `get(Base)` flips back and forth twice (lazy, not memoised).
       - `test_condition_true_then_false_does_not_evict_cached_singleton` —
         `@Singleton` impl resolved under a true condition; flip to false;
         `get(Base)` now raises `LookupError` (or returns the other impl) while
         the old instance is still in `container._singleton_cache` — locks in
         the documented non-eviction (E12).

2. [x] `tests/test_requires.py` — class `TestRequiresDecorator` (decoration-time
       contract):
       - `test_requires_is_exported` — `from providify import Requires, RequiresMarker, ConditionEvaluationError`.
       - `test_marker_is_frozen_dataclass_with_own_dict_storage` —
         `_get_requires_markers(Cls)` returns a 1-tuple of `RequiresMarker`;
         subclass returns `()`.
       - `test_stacked_requires_append_markers_and_and_together` — two
         decorators, four truth-table combinations via two flags.
       - `test_no_condition_and_no_env_raises_value_error`
       - `test_value_without_env_raises_value_error`
       - `test_empty_env_name_raises_value_error`
       - `test_non_callable_condition_raises_type_error`
       - `test_condition_and_env_combine_by_and` (`monkeypatch.setenv`).

3. [x] `tests/test_requires.py` — class `TestRequiresEnvSugar`:
       - `test_env_unset_is_inactive` (`monkeypatch.delenv`)
       - `test_env_empty_string_is_inactive`
       - `test_env_non_empty_is_active`
       - `test_env_value_exact_match_is_active`
       - `test_env_value_mismatch_is_inactive` — includes a case-difference
         mismatch (`"Redis"` vs `"redis"`) to lock in case-sensitivity.
       - `test_env_value_empty_string_matches_set_but_empty` (E9)
       - `test_env_is_read_lazily_after_decoration` — decorate first, `setenv`
         afterwards, resolve → active.

4. [x] `tests/test_requires.py` — class `TestRequiresResolution` (every
       lookup path + composition):
       - `test_get_all_excludes_inactive`
       - `test_is_resolvable_reflects_condition`
       - `async def test_aget_and_aget_all_respect_condition`
       - `test_and_with_profile_matching_and_not_matching` — 2×2 over
         profile match × condition.
       - `test_and_with_alternative_enabled_and_not_enabled` — 2×2 over
         `enable_alternative` × condition; also `@Alternative + @Profile +
         @Requires` (profile activates the alternative, condition still gates).
       - `test_on_provider_function` — `@Requires` above
         `@Provider(singleton=True)`.
       - `test_on_configuration_provider_method` — `@Requires` on a method
         inside a `@Configuration` class, installed via `container.install()`;
         proves the bound-method `__func__` fallback.
       - `test_self_binding_from_bind_is_gated_identically` —
         `container.get(OnImpl)` and `get(Base)` agree (`container.py:1023-1024`).
       - `test_raising_predicate_propagates_condition_evaluation_error` —
         `pytest.raises(ConditionEvaluationError, match="OnImpl")`;
         `exc.__cause__` is the original `RuntimeError`; the message contains
         "cheap, pure".
       - `test_raising_predicate_is_not_evaluated_when_profile_excludes_binding`
         — `@Profile("prod")` + raising predicate, no active profile → `get()`
         of a *different* candidate succeeds (evaluation order, E14).
       - `test_copy_and_snapshot_keep_conditions` — `container.copy()`
         (`container.py:6680-6684`) still gates; no new state is required.

5. [x] `tests/test_requires.py` — class `TestConditionInactiveValidation` and
       `tests/test_validation.py` additions (Severity.INFO surface):
       - `test_inactive_condition_reports_info_issue` — one
         `CONDITION_INACTIVE`, `severity is Severity.INFO`, `owner`,
         `requested`, message names the env var and its current value.
       - `test_active_condition_reports_nothing`
       - `test_validate_raise_on_error_true_never_raises_on_info` —
         `container.validate()` (default) returns normally; `report.ok is True`;
         `report.errors == ()`, `report.warnings == ()`, `len(report.infos) == 1`.
       - `test_repr_renders_info_tier` — `"[INFO]"` appears in `repr(report)`.
       - `test_to_dict_severity_is_info_string`
       - `test_inactive_condition_plus_dependent_reports_missing_binding_error`
         — the only provider of `Base` is condition-inactive and `Service`
         injects `Base` → one `MISSING_BINDING` ERROR **and** one
         `CONDITION_INACTIVE` INFO in the same report (pass 2 goes through
         `_filter`, `:5287-5296`).
       - `test_profile_inactive_binding_still_reports_condition_inactive` (E13).
       - `test_raising_predicate_propagates_from_validate` (E14).
       - `test_validated_flag_not_set_when_only_info_present` (E17 — locks in
         the deliberate choice; if the team later flips it, this is the test
         to change).
       - In `tests/test_validation.py:108-115`: extend
         `test_errors_and_warnings_properties_partition_issues` to
         `report.infos`; add `test_severity_has_info_member`.

6. [x] `providify/validation.py:41-56` — add `Severity.INFO = "info"` and the
       docstring per the audit table. **Merge note:** if Plan 017 landed first
       and already added it, skip; do not redefine.

7. [x] `providify/validation.py:96` — add
       `CONDITION_INACTIVE = "condition_inactive"` as the last `IssueKind`
       member, `#:` comment: "A binding carries `@Requires` whose condition
       currently evaluates `False` — excluded from resolution, working as
       declared. Informational (`Severity.INFO`); exists so wiring reports can
       explain why a candidate is not the live one."

8. [x] `providify/validation.py:117, 222-239, 258-277` — `ValidationIssue`
       docstring tier list; `ValidationReport.infos`; `ok` docstring line;
       `__repr__` third tier (the load-bearing one).

9. [x] `providify/exceptions.py` — add `ConditionEvaluationError(providifyError)`
       after `ModuleCycleError` (`:329`) with the message in §Design, full
       docstring (why not `BindingError`, why `from exc`), attributes
       `owner`, `marker`.

10. [x] `providify/metadata.py` — after `ProfileMetadata` (`:188`): `import os`
        and `from collections.abc import Callable` at top; `_REQUIRES_ATTR`
        beside `_PROFILE_ATTR` (`:85`); `RequiresMarker` with `is_satisfied()`
        and `describe()`; after the profile helpers (`:645`):
        `_get_requires_markers()` (own-`__dict__` + `__func__` fallback,
        `isinstance` every element) and `_set_requires_markers()`. Full
        docstrings per SKILL.md; state "reads `os.environ` at call time".

11. [x] `providify/decorator/scope.py` — import `RequiresMarker`,
        `_get_requires_markers`, `_set_requires_markers` in the `..metadata`
        block (`:12-28`); add `Requires(...)` after `Profile` (`:798`) with the
        validation table from §Design, and a docstring that: names the
        Micronaut precedent **[R014:99-101]** and the `env=` divergence; states
        lazy evaluation, "cheap and pure, must not raise", AND with
        `@Profile`/`@Alternative`, stacking = AND, no MRO inheritance, no
        singleton eviction, and that it goes on the `@Provider` method not the
        `@Configuration` class (E15). Example mirrors `scope.py:757-772`.

12. [x] `providify/binding.py:28` import `_get_requires_markers`; `:189` and
        `:661` — add `self.conditions` on the next line of each, with the same
        "resolved once at construction" why-comment.

13. [x] `providify/container.py` — import `ConditionEvaluationError`
        (`:55-62`); add module-level `_owner_label(b)` and `_conditions_hold(b)`
        directly above `_unreachable_pre_destroy` (`:443`); full docstrings
        (Raises: `ConditionEvaluationError`; Thread safety: pure w.r.t.
        container state, but the predicate is user code).

14. [x] `providify/container.py:1606-1676` — `_binding_is_active()`: compute
        `alternative_ok` into a local, append
        `condition_ok = not b.conditions or _conditions_hold(b)`, return the
        AND. Docstring: three-conjunct formula, new `Raises:` section, new
        `Edge cases` bullets (evaluation order; env read per call; singleton
        non-eviction). **Do not touch `_filter()`'s comprehension** — only its
        docstring table (`:1691-1698`) and the `:1731` comment.

15. [x] `providify/container.py:5399-5424, 5487-5493` — add the
        `condition_inactive_issue` closure beside `unreachable_pre_destroy_issue`
        and call it as **Pass 1c** after 1b, before `if unresolved: continue`.
        **Merge note:** Plan 017 adds its own closure/pass in the same region;
        keep 1b → 1c (this plan) → 1d (017) ordering.

16. [x] `providify/container.py:5249-5341` — `validate()` docstring: add pass
        1c to the numbered list (`:5262-5281`); `Args` (`:5299-5302`) "Warnings
        and INFO issues never raise"; new `Edge cases` bullet: conditions are
        evaluated for every registered binding regardless of profile state, a
        raising predicate propagates as `ConditionEvaluationError`; the
        `⚠️ Profile-aware` paragraph (`:5287-5296`) becomes "Profile- and
        condition-aware".

17. [x] `providify/container.py:1836-1875` — `activate_profile()` docstring
        `Edge cases`: one sentence that the same non-eviction applies to a
        `@Requires` condition flipping false (cross-reference E12).

18. [x] `providify/__init__.py` — `__all__`: `"Requires"`, `"RequiresMarker"`
        after `"ProfileMetadata"` (`:22`); `"ConditionEvaluationError"` after
        `"ModuleCycleError"` (`:63`); matching imports in the `decorator.scope`
        block (`:149-159`), the `metadata` block (`:188`), and the
        `exceptions` block. **Merge note:** Plans 014/016/017 edit the same
        lists.

19. [x] `README.md:196-246` — new `## @Requires — condition-gated bindings`
        section **after** `@Profile` and before `@Stereotype` (`:248`): the
        `condition=` primitive with the flag reproduction; the `env=`/`value=`
        sugar; AND with `@Profile`/`@Alternative`; stacking; "cheap and pure,
        never raise — a raising predicate is `ConditionEvaluationError`";
        singleton non-eviction; a `>` callout mirroring `:236-239`:
        `validate()` reports `MISSING_BINDING` for dependents of a
        condition-inactive sole provider *and* an INFO `CONDITION_INACTIVE`
        explaining why. Contrast with `@Profile` in one sentence: profile =
        named deployment token from providify's own vocabulary; `@Requires` =
        your predicate over your configuration **[GAP:62-71]**.

20. [x] `README.md:1495-1533` — the `validate()` section: add
        `report.infos` to the inspect example (`:1516-1521`); add "condition-
        inactive bindings (INFO)" to the checks list (`:1524-1529`); add one
        line that INFO never raises and never affects `report.ok`.

21. [x] `docs/agents/usage-rules.md:252-302` (R13) — add a `@Requires`
        paragraph after `:295` and extend the per-profile `validate()` loop
        remark: "the same applies per env-var/condition state you ship".

22. [x] `docs/agents/usage-rules.md:243-248` (R12) — extend with
        `IssueKind.CONDITION_INACTIVE` (`INFO`): never raises, not in
        `report.warnings`, read `report.infos` for the wiring report.

23. [x] `docs/agents/choosing-decorators.md:112-114` — the "Qualifiers &
        stereotypes" bullet: append "`@Profile` / `@Requires(condition=…,
        env=…)` for activation".

24. [x] `CHANGELOG.md:10` — under `## [Unreleased]` (currently empty; Plan 014
        may have created `### Added` already — append, do not duplicate the
        heading): `### Added` — `@Requires(condition=, env=, value=)`,
        `RequiresMarker`, `ConditionEvaluationError`, `Severity.INFO`,
        `IssueKind.CONDITION_INACTIVE`, `ValidationReport.infos`; note that
        `repr(report)` now has a third tier and that `_validated` is not set
        when INFO issues exist. Link this plan. Do **not** bump
        `pyproject.toml` (release cut is separate, index `:57-58`).

25. [x] Verification pass — §Verification in full; confirm RED→GREEN and that
        the pre-existing count (~1043 at 2.0.1, plus whatever 014 added) is
        unchanged except for additions.

---

## Edge cases

| # | input / state | expected |
|---|---|---|
| E1 | unconditional binding | `b.conditions == ()` → `condition_ok` short-circuits `True`; zero user code runs |
| E2 | `condition` returns `True` / `False` | active / inactive on the very next `_filter()` — no memo |
| E3 | `condition` returns a truthy non-bool (`"yes"`, `1`) | active (coerced); documented |
| E4 | `condition` raises | `ConditionEvaluationError` from `get()`/`aget()`/`get_all()`/`is_resolvable()`/`validate()`, `__cause__` set, message names the owner and says "cheap, pure" |
| E5 | `@Requires(env="X")`, X unset | inactive |
| E6 | `@Requires(env="X")`, `X=""` | inactive (same rule as `PROVIDIFY_PROFILES=""`, `profiles.py:119`) |
| E7 | `@Requires(env="X")`, `X="anything"` | active |
| E8 | `@Requires(env="X", value="redis")`, `X="Redis"` | inactive — exact, case-sensitive |
| E9 | `@Requires(env="X", value="")`, `X=""` | active — "set and exactly empty"; unset → inactive |
| E10 | `@Requires()` / `@Requires(value="x")` / `@Requires(env="")` | `ValueError` at decoration time |
| E11 | `@Requires(condition=..., env=..., value=...)` all given | AND of both clauses |
| E12 | `@Singleton` resolved under true condition, then condition → false | binding excluded; cached instance **not evicted** (still disposed at shutdown); `override()`/`reset_binding()` remain the eviction tools (`container.py:1865-1868`) |
| E13 | `@Profile("prod")` inactive **and** `@Requires` false | `get()` excludes (profile first); `validate()` pass 1c still emits `CONDITION_INACTIVE` (pass 1 unfiltered) |
| E14 | `@Profile("prod")` inactive **and** `@Requires` predicate raises | `get()` of other candidates succeeds (condition evaluated last); `validate()` raises `ConditionEvaluationError` — intended asymmetry |
| E15 | `@Requires` on a `@Configuration` **class** | no effect on its providers (mirrors `@Profile`, `binding.py:661`); documented in the decorator docstring; §Follow-ups |
| E16 | `@Alternative` + `@Requires`, not enabled | inactive regardless of condition (AND) |
| E17 | `validate()` with only INFO issues | returns normally; `ok is True`; `errors == warnings == ()`; `_validated` stays `False` (`container.py:5704-5709`, conservative rule kept deliberately — only costs a re-run of `validate_all()` on first `get()`) |
| E18 | `container.copy()` / `ContainerSnapshot` | conditions live on the class/function and in `binding.conditions`; no container state to copy (`:6680-6684`, `:6750`, `:6812-6813` untouched) |
| E19 | stacked `@Requires` ×2, one false | inactive; `CONDITION_INACTIVE` message lists only the failing clause(s) |
| E20 | `bind(Base, OnImpl)` synthetic self-binding (`:1023-1024`) | gated identically — same class, same markers |
| E21 | subclass of a `@Requires`-decorated class | **not** gated (own `__dict__` only), same as `@Profile` (`scope.py:754-755`) |
| E22 | `@Requires` applied to an *undecorated* class then `bind()` | `ClassBindingNotDecoratedError` as today — `@Requires` never stamps `DIMetadata` |
| E23 | empty container | report unchanged: `issues=()`, `infos=()`, `ok is True` |

---

## Verification

```bash
cd /home/edoardo/projects/providify

# RED first: after Steps 1-5, before Steps 6-15
uv run pytest tests/test_requires.py -x -q            # must FAIL (ImportError on Requires)

# GREEN: after Step 15
uv run pytest tests/test_requires.py -q

# no regression on the activation + validation surfaces
uv run pytest tests/test_profiles.py tests/test_validation.py \
              tests/test_unreachable_pre_destroy.py tests/test_alternative*.py -q

# full suite + lint/format (Makefile:6-18; ruff only, no type-check target)
make test
make format-check

# exhaustiveness re-greps before finishing
rg -n 'Severity\.' --glob '!plans/*' .
rg -n 'match .*\.(kind|severity)' .
rg -n 'IssueKind' --glob '!plans/*' .
rg -in 'Requires\b' providify/ | rg -v '^\S+:\d+:\s*#|"""|requires (a|an|at|the)'   # only real definitions/uses remain
```

Manual: paste a scratch script with an `env=` marker unset and read
`repr(container.validate())` — the `[INFO]` line must name the variable and say
"is unset".

---

## Versioning

**Minor — 2.1.0.** Not a patch, not a major.

- New public names (`Requires`, `RequiresMarker`, `ConditionEvaluationError`),
  a new `Severity` member and a new `IssueKind` member — all additive to
  `__all__` (`CONTRIBUTING.md:71-74` defines the public API as `__all__`).
- `validate()` output can newly contain issues (INFO) and `repr(report)` a
  third tier; `report.ok` and `raise_on_error` semantics are unchanged.
- **Not** a major: no existing call raises where it did not before, unless the
  user opts in by decorating with `@Requires` whose predicate raises.
- CHANGELOG under `[Unreleased]`; `pyproject.toml` bump in the release plan.

---

## Downstream — what varco must do

- ⚠️ **The guard as written will not flip to pass by itself.**
  `varco_core/tests/test_providify_upstream_gaps.py::TestP25ConditionalRegistration::test_requires_decorator_is_exported`
  binds bare classes (`class OnImpl(Base): pass`, **[GAP:115-124]**) via
  `container.bind(Base, OnImpl)`. `bind()` constructs a `ClassBinding`, which
  raises `ClassBindingNotDecoratedError` for a class without `@Component`/
  `@Singleton` (`binding.py:161-163, 191-193`) — at 2.0.1 and after this plan.
  Once providify 2.1.0 ships, the guard's `xfail(strict=True, raises=ImportError)`
  will fail with the *wrong* exception type. varco must add `@Component` to
  `OnImpl`/`OffImpl` (and drop the xfail) in the same upgrade commit. This
  plan's Step 1 test is the corrected reproduction.
- The `activate_profile()` translation path varco considered for DI-4
  **[GAP:190-201]** remains valid; `@Requires(condition=lambda: settings.x)`
  is now the direct spelling for the five `enable_*` sites **[GAP:81-93]** and
  for the `bus.py` selector **[GAP:143-154]** (two `@Requires`-gated providers
  instead of an `if` inside one).
- varco's `assert_no_structural_di_issues()` should decide whether to log
  `report.infos` (for the DI-7 wiring report) — they never fail a gate.

---

## Risks

- ⚠️ **ASSUMPTION** — no third-party consumer treats `Severity` or
  `IssueKind` as a closed set. In-repo verified false (no `match` on either).
  Mitigated by minor bump + CHANGELOG.
- ⚠️ **ASSUMPTION** — `@Provider` / `@Singleton` / `@Component` all return the
  decorated object unchanged, so `@Requires` stacks in either order. Verified
  for the `@Profile` examples the repo already ships (`scope.py:757-768`,
  `README.md:206-216`); the `@Provider @property` shape is covered by
  `_get_requires_markers`' `__func__` fallback only if `@Profile` works there
  today (its docstring `scope.py:726-729` says it does). Add a test only if it
  is cheap; otherwise document.
- ⚠️ **ASSUMPTION** — varco's guard file was read only through the gap
  report's quotation **[GAP:106-134]**; the `@Component` finding above is
  derived from providify's own `bind()`/`ClassBinding` source, not from
  running varco's test.
- **Merge risk (sibling plans).** All four plans edit `providify/__init__.py`
  (`__all__` + imports), `providify/validation.py` (`IssueKind`; 015 and 017
  both need `Severity.INFO` — whichever lands second must *not* redefine it),
  `CHANGELOG.md [Unreleased]`, `README.md`, `docs/agents/`. Plans 015 and 017
  both edit `container.py:1606-1733` (`_binding_is_active` here; `_filter`'s
  comprehension tail in 017) and both add a closure + pass inside `validate()`
  (`:5399-5493`). Plan 016 edits `_interface_matches` in `utils.py` and the
  singleton cache key — E12 (non-eviction) must be re-read against 016's
  per-closed-alias cache. Build on separate branches off `2.1.0-plan`, merge
  in index order, re-run `make test` after each (index `:37-40`).
- **User-code-on-the-hot-path risk.** A slow or impure predicate slows every
  lookup of every binding type it is registered under. Invariant: providify
  never caches the result; the docstring/README say "cheap and pure" in those
  words. If this bites, the fix is a *separate* opt-in memo plan, not a
  silent cache.
- **Exception-from-`is_resolvable()` risk.** `is_resolvable()` is documented as
  "side-effect-free" (`container.py:1744`); it can now raise
  `ConditionEvaluationError`. Add a `Raises:` line to its docstring (fold into
  Step 14's edits). Invariant: it raises only for a predicate the user wrote.
- **`__repr__` silent-drop risk.** If Step 8's `__repr__` change is missed, INFO
  issues exist in `report.issues`/`to_dict()` but vanish from the printed
  report. Step 5's `test_repr_renders_info_tier` is the guard.
- **Do not repeat an existing doc inaccuracy.** `CHANGELOG.md:22` (2.0.1) says
  a WARNING can make `report.ok` `False`; per `validation.py:233-239` `ok` is
  `not self.errors`, so warnings do not affect it. Do not copy that phrasing
  for INFO; do not retro-edit the historical entry either.
- **Evaluation-order risk.** If a future edit moves `condition_ok` before
  `profile_ok`, E14's asymmetry inverts and a prod-only predicate can break dev
  `get()`. `test_raising_predicate_is_not_evaluated_when_profile_excludes_binding`
  locks the order.

---

## Follow-ups (not this plan)

1. **`describe()` activation state** — `BindingDescriptor` (`descriptor.py:49-54`)
   has no `profiles`/`alternative`/`conditions` fields; DI-7's wiring report
   would want all three at once. One plan, one descriptor change.
2. **`@Requires`/`@Profile` on a `@Configuration` class** gating every provider
   it declares (E15) — a `validate()` warning for the no-op case would be the
   cheap first step.
3. **Opt-in memoisation** of conditions (`settle_conditions()` or a
   `memoize=True` kwarg) if hot-path cost is ever measured to matter.
