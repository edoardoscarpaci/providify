# Plan 017 — `@Fallback` marker: yield when any active non-fallback binding exists

Upstream gap: **P27-FALLBACK-BINDING**
(`/home/edoardo/projects/varco/design/upstream-gaps/providify-fallback-binding.md` —
cited below as **[GAP §n]**; it is the contract).
Index: `plans/000-index-upstream-gaps-p24-p27.md` (slice 3 of 4; build order
**014 → 015 → 017 → 016**).

Research basis:
- `/home/edoardo/projects/varco/design/research/017-fallback-binding-evaluation-timing-and-diagnostics.md`
  — cited as **[R017:line]**.
- `/home/edoardo/projects/varco/design/research/010-default-implementation-di-conventions.md`
  — cited as **[R010:line]**.

Coding standard for the implementer:
`/home/edoardo/.claude/skills/coding-practice/SKILL.md` (docstrings with
Args/Returns/Raises, `DESIGN:` tradeoff comments with ✅/❌, thread/async
safety notes where state is involved, edge cases called out).

Target release: **2.1.0** (minor — see §Versioning). All line numbers below
are against `1402f0a` (v2.0.1) and were read for this plan; re-check them
after Plans 014/015 merge, since both edit `container.py` above the regions
cited here.

---

## Goal

A new marker decorator `@Fallback` — usable on classes and on `@Provider`
functions / `@Configuration` methods, exactly like `@Profile` — marks a
binding as a *default* that is a candidate **only when no active non-fallback
binding matches the same request**. `get()`, `aget()`, `get_all()`,
`aget_all()`, `is_resolvable()`, `get_binding()`, `get_all_bindings()`, and
`list[T]` multibinding collection all honour it through one post-filter step
at the end of `_filter()`. `container.validate()` reports each shadowed
fallback as `IssueKind.FALLBACK_SHADOWED` at `Severity.INFO`, with a
`shadowed_by` field naming the winner. `Fallback` and `FallbackMarker` are
exported from `providify`.

Evaluation is **lazy, at resolve time** — the same moment `@Profile` is
checked — so a shadowing binding registered *after* the fallback still wins
on the next lookup **[GAP §4b]**, **[R017:41]**.

## Non-goals

- ❌ No `Singleton(fallback=True)` / `Provider(fallback=True)` kwarg sugar
  (**[GAP §4a]** option 2). Activation markers in this repo are decorators
  (`@Alternative` `decorator/scope.py:669-686`, `@Profile` `:694-798`), not
  kwargs; four signature changes for sugar is not "minimal abstraction".
  May be added later as sugar over the marker without changing semantics.
- ❌ No `@DefaultBean` name — providify exports `Default` as a **qualifier**
  (`decorator/scope.py:646-654`); the collision already misled a reader
  (**[GAP §4a]** option 3).
- ❌ No `@Requires(missing_beans=T)` spelling — couples to Plan 015 and
  repeats the interface the binding already declares (**[GAP §4a]** option 4).
- ❌ No second issue kind for "two fallbacks tie" — `AMBIGUOUS_BINDING`
  (`validation.py:77-79`) already is that kind (**[GAP §4d]** option 4).
- ❌ `_get_best_candidate()` (`container.py:2354-2382`) is **unchanged**; the
  shadowing rule lives entirely inside `_filter()`.
- ❌ `_binding_is_active()` (`container.py:1606-1676`) is **unchanged** —
  Plan 015 edits it; this plan must not.
- ❌ `_interface_matches()` (`utils.py:130`) is **unchanged** — Plan 016 edits
  it; the generic-alias edge rows below rely on today's semantics as a
  baseline that 016 then extends.
- ❌ No change to `describe()` / `BindingDescriptor`: it lists no activation
  flags today (`descriptor.py:49-54` has only `interface`, `implementation`,
  `scope`, `qualifier`, `priority`, `dependencies` — no `profiles`, no
  `alternative`), so there is nothing to mark a fallback *beside*. Adding a
  descriptor field for every activation marker at once is a separate plan
  (see §Follow-ups).
- ❌ No eviction of an already-cached fallback singleton when a shadower
  arrives later — mirrors `activate_profile()`'s documented caveat
  (`container.py:1865-1868`). Documented, not fixed.
- ❌ No per-interface cache of the shadowing decision (**[GAP §4b]** "Cost").
- ❌ No `@Stereotype` composition of `@Fallback` (§Follow-ups).
- ❌ Nothing in varco changes (see §Downstream).

---

## Design

### The rule (one sentence, settles every edge by construction)

> For a request `(T, qualifier, priority)`, a fallback binding is a candidate
> **iff** the request's candidate set contains no active non-fallback binding.

**[GAP §5]** "Chosen rule". Because it is evaluated on the candidate list that
`_filter()` has *already* narrowed by interface, `exact_only`, qualifier,
priority, and activation, every row of the edge table below follows without
special cases.

### Where it lives — a post-filter step at the end of `_filter()`

`container.py:1678-1733`. Today `_filter()` is one comprehension returning
`[b for b in self._bindings if ...]` (`:1714-1733`). It becomes:

```python
candidates = [ ...today's comprehension, unchanged... ]
# DESIGN: fallback rule is request-relative — it needs the sibling list,
# which only exists here (plan 017 §Design). Runs AFTER every other narrowing
# so an inactive/other-qualifier/other-priority sibling never shadows.
#   ✅ zero new container state; _binding_is_active() signature untouched
#   ✅ one place covers get/aget/get_all/aget_all/is_resolvable/get_binding/
#      get_all_bindings/_collect_sync/_collect_async/validate() memo_filter
#   ❌ O(len(candidates)) extra per lookup — already O(len(self._bindings))
if any(not b.fallback for b in candidates):
    candidates = [b for b in candidates if not b.fallback]
return candidates
```

Why this is the *only* change site needed — every lookup path verified to
obtain candidates from `_filter()`:

| path | call | line |
|---|---|---|
| `get()` | `_get_best_candidate` → `_filter` | `:1368` → `:2375` |
| `aget()` | same | `:1564` → `:2375` |
| `get_all()` | `_filter(cls, qualifier=...)` | `:1400` |
| `aget_all()` | `_filter(cls, qualifier=...)` | `:1593` |
| `is_resolvable()` | `bool(self._filter(...))` | `:1780` |
| `list[T]` collection, sync | `_collect_sync` → `_filter(inner, ...)` | `:1491` |
| `list[T]` collection, async | `_collect_async` → `_filter(inner, ...)` | `:1520` |
| `get_binding()` | `_get_best_candidate` | `:6281` |
| `get_all_bindings()` | `_filter` | `:6315` |
| `validate()` pass 2 | `memo_filter` → `_filter` | `:5365-5381` |
| interceptor self-resolution guard | `_filter` | `:1364` |
| literal `list[T]` precedence check | `_filter(cls)` | `:1347`, `:1559` |

`_multibound_inner()` (`:1438-1461`) is a pure type-shape gate and does not
call `_filter()` itself — the collection happens in `_collect_sync`/
`_collect_async` one call down, both of which do (**[GAP §1]** multibinding
paragraph, verified here). The private `_is_resolvable(hint)` (`:2812-2825`)
scans `self._bindings` without `_filter()` — but it is already
activation-blind (no `_binding_is_active`), is only a "is this hint
DI-shaped at all" gate for constructor params (`:3425`, `:3554`), and the
fallback binding *does* exist, so it correctly answers True and the real
choice happens in `_get_best_candidate`. **Unchanged, same as `@Profile`.**

Because `validate()`'s pass-2 `memo_filter` goes through `_filter()`, the
`AMBIGUOUS_BINDING` check at `:5652-5675` automatically runs on the
**post-shadowing** set: `{F1, F2, X}` reports nothing, `{F1, F2}` at equal
priority still reports the tie as `ERROR` (**[GAP §5]** row 4) — for free.

### The marker

`providify/metadata.py`:

- `_FALLBACK_ATTR = "__di_fallback__"` beside `_PROFILE_ATTR` (`:85`).
- `@dataclass(frozen=True) class FallbackMarker:` — zero fields, placed after
  `ProfileMetadata` (`:159-188`). Frozen dataclass rather than
  `AlternativeMarker`'s bare `__slots__` class (`:100-103`) so it gets
  `__eq__`/`__hash__` for free, matches `ProfileMetadata`'s shape, and can
  grow fields later without a shape change. The **type** is the signal
  (`isinstance`), never the attribute name — repo rule at `:167-169`.
- `_is_fallback(obj: Any) -> bool` — reads `obj.__dict__` then, if absent,
  `obj.__func__.__dict__`, exactly the bound-method fallback
  `_get_profile_metadata` does at `:578-590`. **Never walks the MRO** — a
  subclass of a `@Fallback` class does not inherit it, matching `@Profile`
  (`:558-560`) and `_is_alternative` (`:533-535`).
- `_set_fallback_marker(obj: Any) -> None` — sole write path,
  `setattr(obj, _FALLBACK_ATTR, FallbackMarker())`, mirroring
  `_set_profile_marker` (`:619-645`).

### The decorator

`providify/decorator/scope.py`, new section after `@Profile` (`:798`) and
before `@Stereotype` (`:801`):

```python
def Fallback(target: Any) -> Any:
    """Mark a class or @Provider function as a fallback — yields to any active non-fallback binding. ..."""
    _set_fallback_marker(target)
    return target
```

Bare `@Fallback` (no parentheses), the `@Alternative` call shape (`:669`),
because it carries no arguments. Works on classes, plain functions,
`@Configuration` methods, and `@Provider @property` getters when placed
**beneath** `@property` — the same placement rule `@Profile` has
(`tests/test_profiles.py:357-362`). Decorator order relative to
`@Singleton`/`@Component`/`@Provider`/`@Profile`/`@Alternative` is
irrelevant: bindings are constructed at `bind()`/`register()`/`provide()`/
`scan()` time (`container.py:1011`, `:1024`, `:1046`, `:1091`,
`scanner.py:220-233`), long after every decorator has run.

### Binding-level flag — `b.fallback: bool`, cached at construction

`providify/binding.py`: `ClassBinding.__init__` gains
`self.fallback: bool = _is_fallback(implementation)` directly under
`self.profiles` (`:187-189`); `ProviderBinding.__init__` gains
`self.fallback: bool = _is_fallback(fn)` under its `self.profiles`
(`:659-661`). Both existing lines carry the repo rule this follows:
*"resolved once at construction — `_filter()` runs per resolution and must
not re-read markers (plan 005 §Design)"*.

- A `bind(Base, A)` self-binding (`ClassBinding(A, A, exact_only=True)`,
  `:1024`) of a `@Fallback` class is therefore also `fallback=True` — the
  marker is a property of the class wherever it is bound. Harmless: the
  self-binding only ever answers `get(A)` (`exact_only` guard `:1728`), where
  it is normally the sole candidate (see E9/E10).
- For a `@Configuration` method, `fn` is a bound method (`:6199`), hence the
  `__func__` fallback in `_is_fallback`. For `@Provider @property`, the
  wrapper copies the getter's `__dict__` (`:6195`), so the marker travels.
- `AnyBinding` is exactly `ClassBinding | ProviderBinding` (`binding.py:827`,
  verified in plan 012) — no third shape to handle.

### The diagnostic — `IssueKind.FALLBACK_SHADOWED` at `Severity.INFO`

Inside `validate()` (`container.py:5249-5714`), a new **pass 1c** in the
per-binding loop, immediately after pass 1b (`:5487-5490`) and before
`if unresolved: continue` (`:5492-5493`) — same placement reasoning as plan
012: per-binding, independent of parameter-annotation resolution, keeps the
report's documented ordering (`validation.py:198-202`).

Nested closure beside `unreachable_pre_destroy_issue` (`:5399-5424`):

```python
def fallback_shadowed_issue(b: AnyBinding) -> ValidationIssue | None:
    # Only an ACTIVE fallback can be shadowed; an inactive one (profile off,
    # @Alternative not enabled) is simply invisible — that is @Profile's /
    # Plan 015's story, not this one's.
    if not b.fallback or not self._binding_is_active(b):
        return None
    # "Would the most natural request for this binding be shadowed?" —
    # (b.interface, b.qualifier, None) [GAP §4c]. b is always in the raw
    # candidate set for that request (_interface_matches(X, X) is True,
    # qualifier equal, no priority narrowing, active) — so its ABSENCE from
    # the post-filtered list means exactly one thing: a non-fallback exists.
    candidates = memo_filter(b.interface, b.qualifier, None)
    if any(c is b for c in candidates):
        return None
    winner = max(candidates, key=lambda c: c.priority or 0)   # what get() picks
    ...build ValidationIssue...
```

| field | value |
|---|---|
| `kind` | `IssueKind.FALLBACK_SHADOWED` |
| `severity` | `Severity.INFO` |
| `owner` | `owner_of(b)` — `"A"` or `"@Provider(make_bus)"` |
| `message` | see below |
| `param_name` | `None` (not anchored to a parameter) |
| `requested` | `_type_name(b.interface)` |
| `qualifier` | `b.qualifier` |
| `candidates` | `()` — stays `AMBIGUOUS_BINDING`-only |
| `shadowed_by` | **new field** — `owner_of(winner)` |

Message (what / why / how, one f-string):

```python
f"@Fallback {owner_of(b)} for {_type_name(b.interface)}"
f"{f' qualifier={b.qualifier!r}' if b.qualifier else ''} is shadowed by "
f"{owner_of(winner)} ({_type_name(winner.interface)}, "
f"qualifier={winner.qualifier!r}, priority={winner.priority}): the fallback "
f"is never resolved while that binding is active. Expected for a default — "
f"no action needed. To use the fallback instead, remove or deactivate "
f"{owner_of(winner)}."
```

`shadowed_by: str | None = None` is a new trailing field on
`ValidationIssue` (`validation.py:146-153`), emitted by `to_dict()`
(`:166-182`). ✅ Structured, so varco's `DI-7` wiring report can render
"default / overridden by X" (Spring's `/actuator/conditions`
`negativeMatches` shape, **[R017:49-51]**, **[R010:62-64]**) without parsing
prose. ❌ One more key in every `to_dict()` — additive; no in-repo test pins
the key set (grepped `tests/`: only `report1.to_dict() == report2.to_dict()`
at `tests/test_validation.py:1020`, which is shape-agnostic).
Rejected alternative: reuse `candidates=(winner,)` — its docstring
(`:132-133`) says "Populated only for `AMBIGUOUS_BINDING`"; overloading it
changes the meaning of an existing filter.

Two fallbacks that tie with **no** non-fallback: neither is dropped by the
post-filter, so pass 1c reports nothing for either; pass 2's existing
`AMBIGUOUS_BINDING` (`ERROR`, `:5657-5658`) reports the tie for any
single-valued injection point that requests them — unchanged (**[GAP §5]**
row 4; Quarkus's model, **[R017:86-87]**).

### `Severity.INFO` — reuse from Plan 015; add here only if building out of order

**Reuse `Severity.INFO` (Plan 015).** If this plan is built before 015, add
it here with the **same semantics**, and 015 must then not redefine it:

- `validation.py:41-55` — `INFO = "info"` after `WARNING`; docstring: an
  *expected* condition worth surfacing (a shadowed default), never a defect;
  contrast `WARNING` = "legal-but-notable" (`:46-48`).
- `validate(raise_on_error=True)` **never raises on INFO** — it already only
  raises on `report.errors` (`container.py:5711-5712`); no code change,
  document it in `validate()`'s Args (`:5299-5302`).
- `ValidationReport.ok` stays `not self.errors` (`validation.py:233-239`) —
  INFO does not flip `ok`.
- `ValidationReport.infos` property beside `errors`/`warnings`
  (`:222-230`), same shape.
- `ValidationReport.__repr__` (`:258-277`) iterates
  `(*self.errors, *self.warnings)` at `:275` — **must** become
  `(*self.errors, *self.warnings, *self.infos)` or INFO issues vanish from
  the printed report. ⚠️ This is the one line 015's INFO addition could
  plausibly miss; verify it on merge regardless of build order.
- `ValidationIssue.severity` docstring (`:117`) says "ERROR or WARNING" —
  extend.
- `self._validated = True` only when `not report.issues`
  (`container.py:5708-5709`) — **unchanged** in this plan. Consequence: a
  container with a shadowed fallback is never pre-marked validated by
  `validate()`; the first `get()` runs `validate_bindings()` once
  (`:1374-1376`), as it would have anyway. Cheap; not worth a rule change
  here. If Plan 015 relaxes the rule for INFO, this plan inherits it.

### Alternatives considered

- **Extend `_binding_is_active(b)` → `_binding_is_active(b, siblings)`**
  (**[GAP §4c]** alternative): ✅ keeps the "single activation predicate"
  docstring promise (`container.py:1609-1612`); ❌ needs the sibling list
  computed *before* activation filtering, so inactive siblings would have
  to be re-filtered inside the predicate — duplicating `_filter()`'s own
  work and turning edge row E1 (inactive shadower) into a special case
  instead of something free; ❌ collides with Plan 015, which edits that
  function. Rejected.
- **Registration-time check** (ASP.NET `TryAdd*`, **[R017:36]**;
  **[R010:36]**): ✅ zero per-lookup cost; ❌ `scan("varco_core")` before
  `install(AppModule)` would keep the fallback *and* then admit the app's
  binding, reintroducing the floor-priority race this feature removes
  (**[GAP §4b]**); ❌ contradicts `is_resolvable()`'s promise that a new
  binding "will be reflected immediately" (`container.py:1770-1771`) and
  the mutable-after-registration model `enable_alternative()`/
  `activate_profile()` already rely on (`:1793-1794`, `:1874-1875`).
  **[R017:197]**: "the evidence favors resolution-time evaluation."
  Rejected.
- **Live marker read in `_filter()` instead of `b.fallback`** (the
  `_is_alternative(source)` precedent at `:1669-1670`): ✅ no `binding.py`
  edit; ❌ contradicts the explicit repo rule written beside `profiles`
  (`binding.py:187-189`, `:659-661`) that `_filter()` must not re-read
  markers; ❌ two `getattr`/`isinstance` per candidate per lookup versus one
  attribute read. Rejected — the cached bool is two lines and follows the
  newer precedent.
- **`FALLBACK_SHADOWED` at `WARNING`** (**[GAP §4d]** option 2): ✅ no
  `Severity` change; ❌ a varco app with ~20 shadowed defaults would emit
  ~20 warnings on every `validate()`, so any "zero warnings" CI gate rejects
  *correct* wiring; ❌ `WARNING`'s own definition is "legal-but-notable"
  (`validation.py:46-48`), and a shadowed default is neither notable nor
  unexpected. Accepted only if `INFO` is declined at review.
- **No issue kind, `describe()` only** (**[GAP §4d]** option 3): ❌
  Micronaut (debug log only, **[R017:59-62]**) and ASP.NET (nothing,
  **[R017:65]**) are the cautionary examples; the whole ask is operator
  visibility. Rejected.
- **`ERROR` when a fallback is shadowed**: ❌ nonsensical — shadowing is the
  feature working. Not considered further.

---

## Edge cases

Every row is a test in `tests/test_fallback.py` (Step 2/3). `F` = fallback,
`X`/`B` = non-fallback, `T`/`Base` = interface, `FLOOR = -sys.maxsize - 1`.

| # | input / state | expected | grounding |
|---|---|---|---|
| E1 | `F` plain; `X` gated by `@Profile("x")`; `DIContainer(profiles=())` | `get(T)` → `F`; `get_all(T)` → `[F]`. After `activate_profile("x")`: `get(T)` → `X`, `get_all(T)` → `[X]` | `X` never enters `candidates` (`:1732`) until active — Quarkus/Micronaut "count only if active" **[R017:108-115]**, not Spring `#2897` **[R017:102-105]**. **[GAP §5]** row 1 |
| E2 | `F` plain; `X` is `@Alternative`, not enabled | `get(T)` → `F`; after `enable_alternative(X)` → `X`; after `disable_alternative(X)` → `F` again | same predicate, `:1676` |
| E3 | `X` has `qualifier="x"`, `F` unqualified; `get(T)` | both are candidates (unqualified request matches any qualifier, `:1729`) → `F` dropped → `X` | **[GAP §5]** row 2; deliberate deviation from CDI identity, **[R017:125-126]** |
| E4 | `F` has `qualifier="in_memory"`, `X` unqualified; `get(T, qualifier="in_memory")` | only `F` matches the qualifier → all-fallback → `F` returned. `get(T)` → `X` | the varco `"in_memory"` escape hatch keeps working **[GAP §2]** bucket 2 |
| E5 | `F` and `X` as E3; `get(T, qualifier="y")` | `LookupError`, unchanged | nothing matches before the post-step runs |
| E6 | `F` bound to `Repo[User]`, `X` bound to `Repo[Post]`; `get(Repo[User])` | `[F]` — `_interface_matches(Repo[Post], Repo[User])` is False (`utils.py:142`) → `F` | **[GAP §5]** row 3; **[R017:135]**. Baseline for Plan 016 |
| E7 | `F` and `X` both bound to `Repo[User]` | `get(Repo[User])` → `X` | same alias → same candidate set |
| E8 | E6 state, bare-origin sweep `get_all(Repo)` / `get(Repo)` | `[X]` / `X` — both bindings match the bare origin (`utils.py:144`), so the sweep is one request and `F` yields | consequence of request-relativity; **documented**, not special-cased |
| E9 | `@Fallback @Singleton class A(Base)`; `bind(Base, A)`; `bind(Base, B)`; `get(A)` | `A` — the `exact_only` self-binding is the sole candidate for request `A` (`:1728`) | `exact_only` unchanged **[GAP §5]** row 6 |
| E10 | E9 plus `bind(A, SubA)` (non-fallback) ; `get(A)` | `SubA` — `A`'s self-binding carries the marker (class-level property) and yields even on its own concrete key | documented corner; consistent with the rule |
| E11 | `F1`, `F2` fallbacks, no `X`; `F2.priority > F1.priority` | `get(T)` → `F2`; `get_all(T)` → `[F1, F2]` (ascending priority, `:1417`) | all-fallback set unchanged → today's `max()` rule (`:2382`) |
| E12 | `F1`, `F2` at equal priority, no `X`; a class with `Inject[T]` registered | `get(T)` → first registered (today's rule); `validate(raise_on_error=False)` reports **one** `AMBIGUOUS_BINDING` `ERROR` naming both; **zero** `FALLBACK_SHADOWED` | **[GAP §5]** row 4 — existing kind, `:5655-5675` |
| E13 | `F1`, `F2`, `X` (any priorities); same injector class | `get(T)` → `X`; `validate()` → **zero** `AMBIGUOUS_BINDING`, **two** `FALLBACK_SHADOWED` INFO (one per fallback, each `shadowed_by == owner_of(X)`) | pass 2 sees the post-shadowing set; no false ambiguity |
| E14 | `F` at `FLOOR`, `X` at `0`; `get(T, priority=FLOOR)` | `F` — priority narrowing (`:1730`) leaves an all-fallback set; explicit "give me the default". `is_resolvable(T, priority=FLOOR)` → True | **[GAP §5]** row 7 — named so the maintainer decides; this plan keeps it |
| E15 | `F`, `X`; `get_all(T)`, `await aget_all(T)` | `[X]` both | `:1400`, `:1593` |
| E16 | `container.multibind(T)`; `F`, `X`; `get(list[T])` and `await aget(list[T])`; a class injecting `list[T]` | `[X]` in all three | `_collect_sync` `:1491`, `_collect_async` `:1520`; **[GAP §5]** row 8; Quarkus `@All` **[R017:35]** |
| E17 | `multibind(T)`; only `F` | `get(list[T])` → `[F]` | lone fallback is a real binding |
| E18 | `@Configuration` with `@Fallback @Provider(singleton=True) def make(self) -> T`; `install()`; plus `bind(T, X)` | `get(T)` → `X`; without `X` → provider result. `binding.fallback is True` on the `ProviderBinding` (bound-method `__func__` read) | `:6199`; mirrors `tests/test_profiles.py:336-352` |
| E19 | `@property` `@Fallback` `@Provider(singleton=True)` getter in a `@Configuration` | `binding.fallback is True` | `__dict__` copy at `:6195`; mirrors `tests/test_profiles.py:354-369` |
| E20 | `@Fallback @Provider` **async** provider `F`, class `X`; `await aget(T)` | `X`; lone `F` → `await aget(T)` returns the awaited value | `:1564` |
| E21 | `@Fallback @Profile("dev") @Singleton F`; `X` plain | profiles `()` → `X`; profiles `("dev",)` → still `X` (fallback yields); `("dev",)` and no `X` → `F`; `()` and no `X` → `LookupError` | AND composition **[GAP §4a]** |
| E22 | `@Singleton` above `@Fallback` vs `@Fallback` above `@Singleton` | identical behaviour; `_is_fallback(cls)` True both ways | bindings built after decoration |
| E23 | `class Sub(F)` with its own `@Singleton`, no `@Fallback`; `bind(T, Sub)`; `bind(T, X)` | `get_all(T)` → `[Sub, X]` order by priority — `Sub` is **not** a fallback | `__dict__`-only lookup, no MRO — matches `@Profile` `:754-755` |
| E24 | `F` resolved and cached (`get(T)` → `F` instance); then `bind(T, X)`; `get(T)` | `X` — cache is keyed per binding source (`_get_cache_key`, `:2432-2447`), so the old `F` instance is not served; it is **not evicted** and is still torn down at `shutdown()`. A dependent constructed earlier keeps its `F` reference | mirrors `activate_profile()` caveat `:1865-1868`; **[GAP §4b]** "Caveat" |
| E25 | `get_binding(T)` / `get_all_bindings(T)` with `F`, `X` | `X` / `[X]`; `is_resolvable(T)` True in every configuration that has any binding | `:6281`, `:6315` |
| E26 | `validate()` with `F` active and shadowed | exactly one `FALLBACK_SHADOWED`, `severity is Severity.INFO`, `shadowed_by == "X"`, `"X" in issue.message`, `report.ok is True`, `report.warnings == ()`, `validate(raise_on_error=True)` does **not** raise, issue appears in `repr(report)` | **[GAP §3]** guard + strengthening |
| E27 | `validate()` with `F` gated by an inactive `@Profile` and `X` present | **no** `FALLBACK_SHADOWED` — `F` is inactive, not shadowed | pass 1c's `_binding_is_active` gate |
| E28 | `validate()` with qualified `F("in_memory")` and unqualified `X` | **no** issue — natural request `(T, "in_memory", None)` is not shadowed | **[GAP §4c]** "natural request" |
| E29 | `validate()` with a lone `F` | no issue | |
| E30 | `validate()` on an empty container | `issues == ()`, `ok is True`, `checked_bindings == 0` — unchanged | regression on `tests/test_validation.py` `TestReportShape` |
| E31 | `@Fallback` applied to a class **before** `@Singleton` and then `register()` | works — `register()` only requires DI metadata (`:1042-1043`), the marker is orthogonal | |

Circularity (**[GAP §5]** row 5, Micronaut `#9315` **[R017:24]**) is
impossible by construction — `b.fallback` is a stored bool, never a
resolution — and needs no test beyond E11-E13.

---

## Steps

TDD-ordered. Steps 1-3 are RED (must fail with `ImportError` on
`from providify import Fallback`, then `AttributeError` on
`IssueKind.FALLBACK_SHADOWED`, before Steps 4-11 land).

1. [x] `tests/test_fallback.py` — **new file**. Module docstring naming
       `P27-FALLBACK-BINDING` and this plan. `from __future__ import
       annotations`; `import sys`; `FLOOR = -sys.maxsize - 1`; `container:
       DIContainer` fixture from `tests/conftest.py:36-37`; `asyncio_mode =
       "auto"` (`pyproject.toml:64`) so `async def test_*` needs no marker.
       Class `TestGuardPort` — the two **[GAP §3]** bodies pasted verbatim
       (imports at top of file, not inside the function; drop the
       `# raises ... today` comments):
       - `test_fallback_decorator_is_exported`
       - `test_validate_reports_shadowed_fallback` — keep the two asserts
         from the report **and add** the E26 strengthening asserts
         (`severity is Severity.INFO`, `shadowed_by == "B"`, `"B" in
         issue.message` — note `"B" in str(issue)` alone would pass
         trivially because `IssueKind.FALLBACK_SHADOWED`'s repr contains a
         capital B; the message assert is the real one).
       Class `TestControlsStillPass` — the three **[GAP §3]** controls, each
       written out in full (no `...`):
       - `test_equal_priority_tie_breaks_to_first_registered_today`
       - `test_higher_priority_already_wins_regardless_of_order_today`
       - `test_inactive_sibling_is_not_a_candidate_today`

2. [x] `tests/test_fallback.py` — class `TestFallbackResolution`, one test
       per edge row E1-E25 (names: `test_e01_inactive_profile_shadower_...`
       through `test_e25_get_binding_and_get_all_bindings_...`; keep the
       `eNN` prefix so the table and the file line up). E16/E20 are `async
       def`. E24 asserts both `isinstance(container.get(T), X)` and that the
       earlier `F` instance is still the same object held by a dependent
       resolved before `bind(T, X)`.

3. [x] `tests/test_fallback.py` — class `TestFallbackValidation`, one test
       per E12, E13, E26-E30, plus:
       - `test_fallback_marker_is_exported_and_is_the_stamp` —
         `from providify import FallbackMarker`; `isinstance(A.__dict__["__di_fallback__"], FallbackMarker)`
         is **not** the assertion (attribute name is storage, not API) —
         assert via `providify.metadata._is_fallback(A)` instead.
       - `test_info_never_raises_with_raise_on_error_true` (E26's
         `raise_on_error=True` half, separately named so a regression is
         loud).

4. [x] `providify/validation.py:41-55` — **if and only if** `Severity.INFO`
       is absent (Plan 015 not yet merged): add `INFO = "info"` with the
       semantics in §Design "Severity.INFO", plus `ValidationReport.infos`
       (`:222-230`), the `__repr__` fix at `:275`, and the `severity`
       docstring at `:117`. If 015 is merged, **verify** each of those four
       points instead and touch nothing.

5. [x] `providify/validation.py:58-96` — add
       `FALLBACK_SHADOWED = "fallback_shadowed"` as the **last** `IssueKind`
       member (after `UNREACHABLE_PRE_DESTROY`, `:96`; after Plan 014's
       `DISPOSER_OVERWRITTEN` and Plan 015's `CONDITION_INACTIVE` if they are
       already there), with the `#:` comment style: an active `@Fallback`
       binding that `get(interface, qualifier=...)` would not return because
       an active non-fallback binding matches the same request; `INFO` —
       expected, not a defect.

6. [x] `providify/validation.py:104-182` — `ValidationIssue`: add trailing
       field `shadowed_by: str | None = None`; document it in `Attributes`
       ("Populated only for `FALLBACK_SHADOWED` — the `owner` label of the
       binding that wins the fallback's natural request"); emit it in
       `to_dict()` (`:166-182`) as `"shadowed_by": self.shadowed_by`.
       Extend the `requested` / `qualifier` docs with the
       `FALLBACK_SHADOWED` meaning (the fallback's own interface/qualifier).

7. [x] `providify/metadata.py` — `_FALLBACK_ATTR` (beside `:85`),
       `FallbackMarker` (after `:188`), `_is_fallback(obj)` and
       `_set_fallback_marker(obj)` (new "Fallback marker helpers" section
       after `:645`). Full docstrings per SKILL.md: Args / Returns /
       Thread safety (pure read; setter is import-time last-write-wins like
       `:638-641`) / Edge cases (bound method → `__func__`; object without
       `__dict__` → False; subclass does not inherit).

8. [x] `providify/binding.py` — import `_is_fallback` beside
       `_get_profile_expressions` (`:28`); `ClassBinding.__init__`: add
       `self.fallback: bool = _is_fallback(implementation)` after `:189`;
       `ProviderBinding.__init__`: add `self.fallback: bool = _is_fallback(fn)`
       after `:661`. One shared `DESIGN:` comment referencing the
       `profiles` rule above it. Add `fallback` to both classes' attribute
       docs. `__repr__` (`:208-213`, `:694`) unchanged.

9. [x] `providify/decorator/scope.py` — import `_set_fallback_marker`
       beside `_get_profile_expressions` (`:18`); add the `Fallback`
       decorator in a new section between `:798` and `:801`. Docstring
       must state: the rule in one sentence; lazy evaluation at resolve
       time; works on classes and `@Provider` functions/`@Configuration`
       methods (beneath `@property`); composes with `@Profile`/
       `@Alternative` by AND; not inherited by subclasses; contrast with
       `@Default` (a qualifier, `:646-654`) in one line; the not-evicted
       caveat (E24) in Edge cases; `validate()` reports
       `FALLBACK_SHADOWED` at `INFO`; Jakarta/Quarkus note — CDI has no
       fallback primitive, this mirrors Quarkus `@DefaultBean`
       **[R010:19-22]** adapted to runtime evaluation **[R017:41]**.

10. [x] `providify/container.py:1678-1733` — `_filter()`: bind the
        comprehension to `candidates`, append the post-filter step from
        §Design, `return candidates`. Extend the docstring: a new
        "Fallback rule (plan 017)" paragraph after the activation table
        (`:1689-1698`) stating the rule and that it runs **after** every
        other narrowing; add `Edge cases` bullets for E3/E4 (qualifier
        request-relativity), E8 (bare-origin sweep), E14 (priority
        narrowing returns the fallback).

11. [x] `providify/container.py` — `validate()`: add the
        `fallback_shadowed_issue` closure after `unreachable_pre_destroy_issue`
        (`:5399-5424`) exactly as in §Design; call it as **pass 1c**
        between `:5490` and `:5492`:
        ```python
        # ── Pass 1c: fallback tier — an active @Fallback that is shadowed ──
        shadowed_issue = fallback_shadowed_issue(binding)
        if shadowed_issue is not None:
            issues.append(shadowed_issue)
        ```
        Docstring (`:5250-5341`): add `1c.` to the pass list after `1b.`
        (`:5268-5276`); add an `Edge cases` bullet: inactive fallbacks are
        not reported (E27); qualified fallbacks are evaluated for their own
        qualifier (E28); two tied fallbacks with no non-fallback are
        reported by the existing `AMBIGUOUS_BINDING`, never by this kind
        (E12); `raise_on_error` never raises on `INFO` (`:5299-5302`).

12. [x] `providify/container.py` — one-line cross-references, no logic:
        `get_all()` docstring (`:1384-1398`) and `aget_all()` (`:1575-1591`)
        gain "Shadowed `@Fallback` bindings are excluded (plan 017)";
        `activate_profile()`'s not-evicted bullet (`:1865-1868`) gains "the
        same holds for a `@Fallback` singleton cached before its shadowing
        binding was registered".

13. [x] `providify/__init__.py` — add `"Fallback"` after `"Profile"` and
        `"FallbackMarker"` after `"ProfileMetadata"` in `__all__`
        (`:21-22`); import `Fallback` in the `.decorator.scope` block
        (`:148-167`) and `FallbackMarker` in the `.metadata` block
        (`:186-191`). Keep alphabetical order within each block (ruff
        isort).

14. [x] `README.md` — new section `## @Fallback — yield to any real binding`
        between `## @Profile` (`:196-246`) and `## @Stereotype` (`:248`).
        Content: the one-sentence rule; a `bind(Base, A)`+`bind(Base, B)`
        example mirroring the guard test; the qualifier example (E3/E4)
        with the `"in_memory"` escape hatch; "lazy — later registrations
        win, no registration-order coupling"; a `>` callout **"Not
        `@Default`"** — `@Default` (`:143-157`) is a *qualifier* meaning
        "no qualifier", `@Fallback` is an *activation rule*; a `>` callout
        for the not-evicted caveat (E24) pointing at `override()`/
        `reset_binding()`; a line that `validate()` reports
        `FALLBACK_SHADOWED` at `INFO`. Also: the `validate()` section
        (`:1495-1538`) — add "shadowed `@Fallback` defaults (`INFO`)" to the
        check list at `:1500-1502` and `:1526-1528`, and extend the
        `report.ok`/severity paragraph (`:1535-1538`) with the `INFO` tier
        (never raises, never flips `ok`, visible via `report.infos`). Test
        table (`:2146-2152`): add a `test_fallback.py` row.

15. [x] `docs/agents/usage-rules.md` — new rule after the last one (`R18`
        at `:480`; use the next free number after Plan 015's rule if it
        landed first): "`@Fallback` for framework defaults; never
        `priority=-sys.maxsize - 1`" — the rule, the qualifier
        request-relativity, the `@Default` contrast, and the E24 caveat.
        `docs/agents/choosing-decorators.md:112-114` — extend the
        "Qualifiers & stereotypes" bullet: "`@Alternative` / `@Default` /
        `@Fallback` for selection" with a half-line each. Add a row to the
        CDI/Spring mapping table there: `@Fallback` ↔ Quarkus `@DefaultBean`
        / Spring `@ConditionalOnMissingBean` (evaluated at resolve time,
        not registration time).

16. [x] `CHANGELOG.md:10` — under `## [Unreleased]` → `### Added` (create
        the heading if Plans 014/015 have not; otherwise append): one entry
        for `@Fallback` / `FallbackMarker` (rule, lazy evaluation,
        qualifier request-relativity, collections exclusion), one for
        `IssueKind.FALLBACK_SHADOWED` at `Severity.INFO` with the new
        `ValidationIssue.shadowed_by` field and the `to_dict()` key, and —
        only if Step 4 added it here — one for `Severity.INFO` /
        `ValidationReport.infos`. Link this plan. Do **not** bump
        `pyproject.toml` (release cut is its own plan, per the index).

17. [x] Verification pass — §Verification in full; confirm RED→GREEN, the
        pre-existing suite count is unchanged except for the additions, and
        the exhaustiveness grep below is clean.

### Exhaustiveness audit (do all of these)

| site | action | why |
|---|---|---|
| `providify/validation.py` `IssueKind` | add member (Step 5) | the enum |
| `providify/validation.py` `Severity` | reuse or add (Step 4) | see §Design |
| `providify/validation.py` `__repr__` `:275` | must include infos | otherwise INFO issues are invisible in `print(report)` |
| `providify/__init__.py` `__all__` | `Fallback`, `FallbackMarker` (Step 13) | public API is `__all__` (`CONTRIBUTING.md:71-74`, per plan 012) |
| any `match issue.kind` / exhaustive `Severity` handling | **verified absent** in-repo (`rg 'match .*\.kind'`, `rg 'list\(Severity\)'` → none) | re-grep before finishing |
| `descriptor.py` | no change | no activation flags exist there (`:49-54`) |
| `scanner.py:220-233` | no change | bindings built via constructors; flag computed there |
| `container.py` copy/override paths (`:6481-6573`) | no change | bindings are never cloned (`rg 'copy\.copy\(' providify/` → none); constructors recompute the flag |

---

## Verification

```bash
cd /home/edoardo/projects/providify

# RED first: after Steps 1-3, before Steps 4-11
uv run pytest tests/test_fallback.py -x -q          # must FAIL (ImportError: Fallback)

# GREEN: after Step 11
uv run pytest tests/test_fallback.py -q

# no regression on every surface _filter() feeds
uv run pytest tests/test_validation.py tests/test_profiles.py \
              tests/test_multibinding.py tests/test_unreachable_pre_destroy.py \
              tests/test_disposes.py tests/test_generics.py -q
# (if a listed file does not exist under that name, `ls tests/` and pick the
#  multibinding / generics file — do not skip the surface)

# full suite (~1043 tests at 2.0.1 + Plans 014/015 additions + this file)
make test

# lint + format (ruff only — the repo has no type-check target, Makefile:9-18)
make format-check

# exhaustiveness re-grep
rg -n 'IssueKind' --glob '!plans/*' .
rg -n 'match .*\.kind' .
rg -n 'errors, \*self\.warnings' providify/validation.py   # must also list infos
```

Manual check: `print(container.validate(raise_on_error=False))` for the
guard-test container must show an `[INFO] A: @Fallback A for Base is shadowed
by B (...)` line.

---

## Versioning

**Minor — 2.1.0.** Not a patch.

- New public names `Fallback`, `FallbackMarker` in `__all__`; new
  `IssueKind` member; new `ValidationIssue.shadowed_by` field and
  `to_dict()` key; (with Plan 015) new `Severity` member and
  `ValidationReport.infos`. Each is observable to consumers
  (`CONTRIBUTING.md:71-83` via plan 012 §Versioning).
- Not a major: **no existing behaviour changes for any binding without the
  marker** — the post-filter is a no-op when no candidate has
  `fallback=True`; `validate(raise_on_error=True)` cannot newly raise
  (INFO never raises; the only ERROR path touched, `AMBIGUOUS_BINDING`, can
  only report *fewer* ties, never more). `report.ok` is unaffected by INFO.
- CHANGELOG under `[Unreleased]`; `pyproject.toml` bump happens in the
  2.1.0 release plan (index: "cut by a separate release plan (mirror
  `plans/013`)").

---

## Downstream — what varco gets

- `varco_core/tests/test_providify_upstream_gaps.py::TestP27FallbackBinding::test_fallback_decorator_is_exported`
  and `::test_validate_reports_shadowed_fallback` flip from
  `xfail(strict=True)` to **passing** — Steps 1's port is byte-for-byte the
  same bodies, so if `tests/test_fallback.py::TestGuardPort` is green here,
  varco's guards are green against this build. `strict=True` makes the
  flip loud (**[GAP §8]**); varco must then remove the two `xfail` markers.
- The three varco controls keep passing (Step 1's `TestControlsStillPass`
  proves it here first).
- varco's 72 `priority=-sys.maxsize - 1` sites (**[GAP §2]**) can migrate
  to `@Fallback` incrementally; the `"in_memory"`-qualified sites (bucket 2)
  keep both roles — E3/E4 are the contract. The `-sys.maxsize` second tier
  (bucket 3) is **not** expressible as `@Fallback` alone (a fallback never
  shadows a fallback); those sites keep a priority *among fallbacks* (E11).
- `DI-7`'s `varco diagnose` reads `issue.shadowed_by` (structured) rather
  than parsing `issue.message`.
- ⚠️ varco's `assert_no_structural_di_issues()` must **not** treat INFO as
  a failure — if it gates on `report.issues` / `not report.ok`... `ok`
  stays True for INFO, but a gate on `len(report.issues) == 0` would go red
  on every correctly-shadowed default. varco should gate on
  `report.errors` (+ `report.warnings` if desired), never on all issues.

---

## Risks

- ⚠️ **ASSUMPTION** — Plan 015 adds `Severity.INFO` with the semantics in
  §Design *including* the `__repr__` line at `validation.py:275` and an
  `infos` property. Not verifiable (015 is not on disk at the time of
  writing). Step 4 is written as "verify or add"; the invariant that must
  hold whichever plan lands first: `validate(raise_on_error=True)` never
  raises on INFO, `report.ok` ignores INFO, INFO issues appear in
  `repr(report)`.
- ⚠️ **ASSUMPTION** — Plan 015's `@Requires` gating is folded into
  `_binding_is_active()` (index row says so). If instead 015 filters
  elsewhere in `_filter()`'s comprehension, this plan's post-step must
  still run **after** it; the invariant is "post-step last in
  `_filter()`". Re-read `_filter()` after 015 merges.
- ⚠️ **ASSUMPTION** — `_interface_matches(X, X)` is True for every shape a
  binding interface can take, so a fallback is always in its own natural
  request's raw candidate set (pass 1c relies on "absent ⇒ shadowed").
  Verified for concrete (`issubclass`, `utils.py:138`) and generic
  (`origin issubclass + args ==`, `:142`). Unverified for exotic
  `returns=` results; those fall back to "not shadowed" (no false INFO),
  which is the safe direction. Plan 016's rewrite of the both-generic
  branch must preserve reflexivity — call it out in 016's review.
- **Merge risk — shared files.** `container.py` (`_filter` here; Plan 015
  `_binding_is_active`; Plan 014 `_register_module_providers`; Plan 016
  `_filter`'s tie-break), `validation.py` (`IssueKind` by all four,
  `Severity` by 015/017), `__init__.py` (exports by all), `decorator/scope.py`
  (015 `@Requires`, 017 `@Fallback`), `binding.py` (017; possibly 015 if it
  caches a flag the same way), `CHANGELOG.md`, `README.md`, `docs/agents/`.
  Build on its own branch off `2.1.0-plan`; after each merge re-run
  `make test` (index "Build order"). Add new `IssueKind` members strictly
  in merge order at the end of the enum to keep diffs orthogonal.
- **Behaviour-change risk for existing users** — none for unmarked code
  (post-step is a no-op when no candidate is a fallback). Invariant: with
  zero `@Fallback` usages, `_filter()` returns exactly today's list, same
  order.
- **Hot-path cost** — one `any()` scan + at most one list rebuild per
  `_filter()` call, on a list `_filter()` already built. Invariant: no
  per-candidate marker read, no `isinstance` in the post-step (`b.fallback`
  is a plain attribute, Step 8).
- **`_validated` staleness** — `validate()` no longer pre-marks a container
  with a shadowed fallback as validated (`:5708-5709`). Only cost is one
  `validate_bindings()` on the first `get()` (`:1374-1376`), which would
  have happened for any un-validated container anyway.
- **Guard-test weakness inherited from varco** — `"B" in str(issue)` is
  satisfied by the `IssueKind` repr alone. Mitigated by the stronger asserts
  in Step 1; varco should tighten its guard once it flips (note for
  the varco side, not this repo).
- **SETTLED** — decorator order does not matter (bindings are built at
  registration, `container.py:1011/1024/1046/1091`, `scanner.py:220-233`).
- **SETTLED** — `_multibound_inner` does not bypass `_filter()`; the
  collection happens in `_collect_sync`/`_collect_async` (`:1491`, `:1520`).

---

## Follow-ups (not this plan)

1. **`describe()` activation flags** — `BindingDescriptor` (`descriptor.py:49-54`)
   carries no `profiles`/`alternative`/`fallback`; a single plan should add
   all three at once so `DI-7`'s report can render "default / profile-gated /
   alternative" from one snapshot.
2. **`Singleton(fallback=True)` / `Provider(fallback=True)` sugar** over the
   marker (**[GAP §4a]** option 2) — only if varco asks after migrating.
3. **`@Stereotype` composition** — bundling `@Fallback` into a stereotype
   alongside scope/qualifier/priority (`metadata.py:112-125`).
4. **`_validated` and INFO** — if INFO-only reports become common, relax
   `container.py:5708-5709` to "no errors and no warnings" (coordinate with
   Plan 015).
