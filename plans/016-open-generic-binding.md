# Plan 016 — Open-generic binding: `returns=Repo[T]` resolvable by `get(Repo[User])`

Upstream gap: **P26-OPEN-GENERIC-BINDING**
(`/home/edoardo/projects/varco/design/upstream-gaps/providify-open-generic-binding.md`,
cited below as **[GAP]** with line numbers; its §4 semantics table is the
contract — every one of its 8 rows is settled explicitly in §Design below).

Index: `plans/000-index-upstream-gaps-p24-p27.md` — this is slice 4 of 4,
built **last**, after 014 (`@Disposes` wiring), 015 (`@Requires`, adds
`Severity.INFO`) and 017 (`@Fallback`, adds a post-filter step at the END of
`_filter()`). Those three plan files were not on disk when this plan was
written; their contracts are taken from the index (`000-index:14-33`).

Research basis:
- **[R001]** `/home/edoardo/projects/providify/design/upstream-gaps-2-1-0/research/001-typevar-introspection-py312.md`
  — Python 3.12/3.13 `TypeVar` introspection, PEP 695/696, `type[T]`
  introspection, alias hashing, bound/constraint gotchas, substitution.
- **[R016]** `/home/edoardo/projects/varco/design/research/016-open-generic-binding-in-di-containers.md`
  — .NET / Autofac / Spring / Guice / Python-ecosystem survey; resolve-time
  factory model; the shared-singleton anti-pattern.

Coding standard for the implementer: `/home/edoardo/.claude/skills/coding-practice/SKILL.md`
(why-comments, full Args/Returns/Raises/Edge-cases docstrings, thread/async
safety notes, ✅/❌ DESIGN blocks). Test/lint commands: `Makefile:6-18`.

Target release: **2.1.0** (minor — see §Versioning).

---

## Goal

`container.provide(factory, returns=Repo[T])` (or `@Provider(returns=Repo[T])`,
or a `-> Repo[T]` return annotation) registers an **open** binding. A request
for any closed alias with the same origin — `container.get(Repo[User])`,
`await container.aget(Repo[User])`, `Inject[Repo[User]]`, a bare
`repo: Repo[User]` constructor parameter, `get_all(Repo[User])`,
`InjectInstances[Repo[User]]` — matches it. The factory receives the closed
type argument through a parameter annotated `type[T]`. A `singleton=True`
open binding caches **one instance per closed alias**, each of which is torn
down at shutdown. A closed binding always beats an open one for the same
request. `validate()` neither flags the `type[T]` parameter as missing nor
misses a closed request that an open binding serves.

Both varco guards in
`varco_core/tests/test_providify_upstream_gaps.py::TestP26OpenGenericBinding`
flip from `xfail(strict=True)` to passing.

## Non-goals

- ❌ Partial closure / mixed aliases (`Mapper[S, T]` with only `S` closed,
  `Repo[list[T]]`, `dict[str, T]`) — **rejected at registration** with
  `TypeError` (**[GAP:193]** row 8). Follow-up, not this plan.
- ❌ Origin-subclass wildcard matching (binding `Sub[T]` serving request
  `Repo[User]` where `class Sub(Repo[T])`) — the wildcard branch requires an
  **identical origin**; see §Design "Match rule" for the counter-example that
  makes positional mapping unsound across origins. Follow-up.
- ❌ A request-side `TypeVar` (`get(Repo[T])`) gaining wildcard semantics —
  keeps today's literal rule (**[GAP:186]** row 1, "out of scope").
- ❌ A new `IssueKind` for `bound=` violations — `MISSING_BINDING` already
  reports the consequence; its message is enriched instead (§Design row 7).
- ❌ `warm_up()`/`awarm_up()` pre-creating closed instances of an open binding
  — there is no registry of "every X that will be requested" (**[GAP:190]**
  row 5's ❌); open bindings are skipped by warm-up, documented.
- ❌ Adding `typing_extensions` (or any runtime dependency —
  `pyproject.toml:41-54` has none). PEP 696 defaults are irrelevant to
  matching (**[R001:66]**: "the container must still match based on the
  closed type argument passed at `container.get()`"), so nothing here reads
  `__default__`/`has_default()`; no 3.12-vs-3.13 branch is needed.
- ❌ Changing `ClassBinding`, `bind()`, `register()`, `override()`.
- ❌ Any varco change (see §Downstream).

---

## Design

### Vocabulary

| term | meaning |
|---|---|
| **open alias** | a parameterised generic alias whose args are **all** plain `TypeVar`s: `Repo[T]`, `Pair[A, B]`, `Pair[T, T]` |
| **closed alias** | a parameterised generic alias with **no** free type parameters: `Repo[User]`, `Pair[int, str]` (`alias.__parameters__ == ()`, **[R001:109]**) |
| **open binding** | a `ProviderBinding` whose `interface` is an open alias; `binding.type_params` is non-empty |
| **closed binding** | any binding that is not open (concrete class, closed alias, or `ClassBinding`) |
| **closing args** | the `{TypeVar → concrete}` mapping that turns an open alias into the requested closed alias |

### Model: Autofac's resolve-time factory, not .NET's type substitution

.NET registers `typeof(IRepository<>) → typeof(Repository<>)` and does **not**
support factory delegates for open generics because the type argument is
unknown at registration (**[R016:16]**). Python has no runtime "instantiate
the closed generic class with substituted parameters" (**[R016:84-85]**), and
varco's factories need the closed type *as a value* (`get_repository(entity_cls)`,
**[GAP:152-155]**). Autofac's delegate is "generated at resolution time, not
registration time — the closed type argument is available when the delegate
is invoked" (**[R016:25]**) — that is exactly the shape a Python factory needs,
so it is the model adopted. No surveyed Python DI library has this
(**[R016:65]**); providify adapts Autofac rather than copying a Python
precedent because none exists (**[GAP:160-163]**).

### The eight semantics rows, settled

| # | semantic | **decision** | where |
|---|---|---|---|
| 1 | match rule | ✅ Binding-side `TypeVar` is a wildcard; request-side `TypeVar` stays literal. ✅ A **bare** `Repo` request never matches an open `Repo[T]` binding — guarded in the *lookup* layer (`_filter`/`_is_resolvable`), not in `_interface_matches`, so `@Disposes(Repo)` and `reset_binding(Repo)` keep working (see "Where the bare guard lives"). | `utils._interface_matches` both-generic branch; new `utils._closing_args`; `container._binding_serves` |
| 2 | specificity | ✅ Closed beats open regardless of priority/registration order, as a new step in `_get_best_candidate()` **after** `_filter()` (and therefore after 017's fallback-drop — see "Order relative to @Fallback"). Applies to single-pick sites only; `get_all` keeps every serving binding. | new `_prefer_closed()` used by `_get_best_candidate`, the two `DelegateMeta` branches, and `validate()`'s ambiguity/edge block |
| 3 | type-arg delivery | ✅ A factory parameter annotated exactly `type[X]` where `X` is a `TypeVar` whose **name** equals a name in `binding.type_params` is filled from the closing args; positional by `TypeVar` (**[GAP:177]**). Name-based, not identity-based, so PEP 695 `def factory[T](entity: type[T])` works with a module-level `returns=Repo[T]` (different `T` objects, same name). Positional fallback (decided during build, kept): if the binding has exactly **one** `TypeVar`, no parameter is name-matched, and a factory parameter carries **no annotation at all** (e.g. `lambda entity: Repo(entity)`), that parameter is filled positionally with the sole closing type; never fires with 2+ `TypeVar`s or any name match. ❌ Accepted: a factory that omits the parameter (or annotates it with something other than `type[X]`/nothing) silently gets no type info (**[GAP:188]**). | `_collect_kwargs_sync/_async` gain `type_args=`; `ProviderBinding.create/acreate` gain `requested=` |
| 4 | singleton cache | ✅ Key `(binding.fn, closed_alias)` for open bindings; never one instance across all `X` — **[R016:18]** names the naive "cache by binding" default a "dangerous anti-pattern"; **[R016:124]** (Evidence Gap 5) says no surveyed framework documents its choice, so providify documents this one explicitly. Applies uniformly to SINGLETON/REQUEST/SESSION because `_get_cache()` routes by scope and the key is scope-independent (`container.py:2386-2430`). Aliases are safe dict keys (**[R001:103]**: `Repo[User] == Repo[User]`, hash from `(__origin__, __args__)`). | `_get_cache_key(binding, requested)` |
| 5 | enumeration | ✅ `get_all(Repo[User])` / `InjectInstances[Repo[User]]` include the open binding **once**; ✅ `get_all(Repo)` (bare) excludes it (unbounded expansion has no registry to draw from, **[GAP:190]**). | falls out of row 1's lookup guard — `get_all` uses `_filter` (`container.py:1400`) |
| 6 | `validate()` | ✅ `type[T]` parameters of an open binding are skipped in `_iter_injection_points` (they are container-supplied, not injection points); ✅ closed requests are checked against open bindings automatically because pass 2 uses `memo_filter → _filter` (`container.py:5593`); ✅ ambiguity/edge selection applies `_prefer_closed` so a closed+open pair is not a false `AMBIGUOUS_BINDING`. No new `IssueKind`. | `_iter_injection_points` ProviderBinding branch; validate pass 2 |
| 7 | `bound=`/constraints | ✅ Violation = **non-match** (a second binding may serve; `LookupError` only if nothing does). ✅ Uncheckable bounds (Protocol without `@runtime_checkable`, lazily-evaluated PEP 695 bound raising `NameError`) are **permissive** (treated as satisfied) — a check that cannot run must not block resolution; static checkers own that case. Subscripted-generic bounds are checked against `get_origin(bound)` (**[R001:87-88]**). Constraints use identity membership (**[R001:92]**). ❌ No INFO-level `IssueKind`: `validate()`'s `MISSING_BINDING` message is enriched with the near-miss ("an open binding `Repo[T]` exists but `Order` does not satisfy `bound=Entity`") instead of adding enum surface. | `utils._closing_args`; validate `MISSING_BINDING` message |
| 8 | multiple TypeVars / partial closure | ✅ All-`TypeVar` (open) or no-free-`TypeVar` (closed) aliases only; anything with free type parameters that is not all plain `TypeVar`s → `TypeError` at registration. `ParamSpec`/`TypeVarTuple` args → `TypeError` (**[R001:33]**). Multiple TypeVars **are** supported when all args are TypeVars (`Pair[A, B]`); a repeated TypeVar (`Pair[T, T]`) must close consistently (`Pair[int, int]` matches, `Pair[int, str]` does not). Enforced in `ProviderBinding.__init__`, the single funnel for all three interface sources (`binding.py:628-655`), so `@Provider(returns=)`, `provide(returns=)` and the return annotation cannot disagree. | `binding.ProviderBinding.__init__` via `utils._open_type_params` |

### Match rule (row 1) — `_interface_matches()` both-generic branch

Today (`utils.py:174-180`):

```python
if req_origin is not None and bind_origin is not None:
    if not issubclass(bind_origin, req_origin):
        return False
    return get_args(binding_interface) == get_args(requested)
```

After:

```python
if req_origin is not None and bind_origin is not None:
    if not issubclass(bind_origin, req_origin):
        return False
    if get_args(binding_interface) == get_args(requested):
        return True                       # literal — closed==closed, open==open (unchanged)
    return _closing_args(binding_interface, requested) is not None   # NEW wildcard
```

New pure helpers in `providify/utils.py`:

```python
def _open_type_params(interface: Any) -> tuple[TypeVar, ...]:
    """() for a concrete type or closed alias; the TypeVars for an open alias.
    Raises TypeError for a partially-open / nested-TypeVar / ParamSpec /
    TypeVarTuple alias (row 8)."""

def _closing_args(binding_interface: Any, requested: Any) -> dict[TypeVar, Any] | None:
    """The {TypeVar → concrete} mapping that closes *binding_interface* into
    *requested*, or None when *requested* does not close it."""
```

`_closing_args` returns `None` when any of these hold:
- `binding_interface` is not open (`_open_type_params(...) == ()`).
- `get_origin(requested) is not get_origin(binding_interface)` — **identical
  origin required**. Why not `issubclass` like the literal branch: with
  `class Flip(Pair[B, A], Generic[A, B])`, an open binding `Flip[A, B]` served
  positionally against a request `Pair[int, str]` would deliver `A=int, B=str`
  while the class semantics say `A=str, B=int`. Positional mapping is only
  sound when both sides share the parameter list. ✅ sound; ❌ `Sub[T]` cannot
  serve `Repo[User]` — a documented first-cut limit (§Non-goals).
- arg counts differ.
- any requested arg has free type parameters (`getattr(arg, "__parameters__", ())`
  non-empty, or `type(arg) is TypeVar`) — the request is not closed.
- a repeated `TypeVar` would map to two different args.
- a `bound=`/constraint violation (row 7 procedure below).

Bound/constraint check per `(tp, arg)`:

```python
check_target = get_origin(arg) or arg          # Repo[list[int]] → list is checked against the bound
if not isinstance(check_target, type):
    continue                                    # Literal[...], strings, etc. — cannot check, permissive
if tp.__constraints__:
    if arg not in tp.__constraints__:           # identity/equality membership [R001:92]
        return None
    continue
bound = tp.__bound__                            # PEP 695 bounds evaluate lazily HERE [R001:42-43]
if bound is None:
    continue
try:
    if not issubclass(check_target, get_origin(bound) or bound):   # [R001:88]
        return None
except (TypeError, NameError):                  # non-runtime_checkable Protocol / unresolvable forward ref
    continue                                    # permissive — see row 7 [R001:143]
```

`_open_type_params(interface)`:

```python
if isinstance(interface, type) or get_origin(interface) is None:
    return ()
args = get_args(interface)
free = getattr(interface, "__parameters__", ())          # [R001:109]
if not free:
    return ()                                            # closed alias
if all(type(a) is TypeVar for a in args):                # strict `type(x) is TypeVar` [R001:33, R001:133]
    return tuple(args)
raise TypeError(...)  # mixed / nested / ParamSpec / TypeVarTuple — names the alias and says "all-TypeVar or all-concrete"
```

The module-level DESIGN comment at `utils.py:24-25` ("❌ TypeVar-parameterised
aliases … won't match concrete requests") and the docstring row at
`utils.py:157-158` (⚠️) are rewritten to state the new rule and the remaining
request-side limitation.

### Where the bare guard lives (row 1's ❌)

`_interface_matches(Repo[T], Repo)` returns `True` today through the fourth
branch (`utils.py:187-190`, documented at `:164`) and **must keep doing so**:
the `@Disposes` wiring loop (`container.py:6206-6214`, rewritten by plan 014
but still `_interface_matches`-based per `000-index:14`) and
`reset_binding()` (`container.py:6553-6558`) use it structurally, and
`@Disposes(Repo)` is the natural disposer spelling for an open binding. So the
"bare request must not match an open binding" rule is a **lookup** rule, not a
structural one:

```python
def _binding_serves(self, b: AnyBinding, requested: Any) -> bool:
    """Lookup-layer predicate: can *b* produce an instance for *requested*?"""
    type_params = getattr(b, "type_params", ())
    if type_params:
        # An open binding needs closing args to call its factory — a bare
        # `Repo` or a TypeVar-carrying `Repo[T]` request has none to give.
        return _closing_args(b.interface, requested) is not None
    return _interface_matches(b.interface, requested)
```

Used at exactly two sites: `_filter()` (`container.py:1720`, replacing the
direct `_interface_matches` call) and `_is_resolvable()` (`container.py:2824`,
which bypasses `_filter` today and would otherwise say "resolvable" for a bare
`Repo` and then let `get()` raise `LookupError` through the plain-annotation
branch at `:3425-3429`). `is_resolvable()` (public, `:1780`) already delegates
to `_filter`.

### Order relative to `@Fallback` (plan 017)

017 drops fallback candidates at the **end of `_filter()`** whenever a
non-fallback candidate exists (`000-index:16`). Specificity lives in
`_get_best_candidate()` on `_filter()`'s output. Therefore the order is
**fallback-drop first, then closed-beats-open**, enforced by structure rather
than by care. Justification by the two mixed cases:

| bindings | fallback-first (chosen) | closed-first (rejected) |
|---|---|---|
| open `@Fallback Repo[T]` + closed `Repo[User]` | fallback dropped → closed wins | closed wins | 
| closed `@Fallback Repo[User]` + open `Repo[T]` | fallback dropped → **open wins** | open dropped → the fallback wins, violating the user's explicit "only if nothing else" |

`@Fallback` is an explicit user declaration; specificity is a structural
inference. Explicit intent must not be overridden by an inference, so
fallback-drop runs first. Both rules agree in the first case; only the chosen
order honours intent in the second.

`_prefer_closed` (module-level, `container.py`, placed above
`_get_best_candidate`):

```python
def _prefer_closed(candidates: list[AnyBinding]) -> list[AnyBinding]:
    """Row 2: if any closed candidate exists, open ones are dropped."""
    closed = [c for c in candidates if not getattr(c, "type_params", ())]
    return closed or candidates
```

Applied in `_get_best_candidate` (`:2375`, before the `max`), both
`DelegateMeta` branches (`:3389-3396`, `:3529-3536` — they call `max` on a raw
`_filter` result and bypass `_get_best_candidate`), and `validate()`'s
ambiguity block (`:5648-5675`) and edge pick (`:5677-5683`) so the report
mirrors the runtime pick exactly (the invariant `validate()` already promises
at `:5678-5679`).

### Threading the closed alias to the factory (row 3) — the minimal seam

```
get(cls)                                  container.py:1368,1377
  best = _get_best_candidate(cls)         unchanged signature
  _instantiate_sync(best, requested=cls)  NEW kwarg, default None
    key = _get_cache_key(binding, requested)          (fn, cls) when open
    binding.create(self, requested=requested)          ProviderBinding only reads it
      type_args = self.type_args_for(requested)        {name: concrete}, {} when closed
      container._call_provider(fn, type_args=..., cycle_key=requested or None)
        _collect_kwargs_sync(fn, name, type_args=...)  fills type[X] params first
```

- `_instantiate_sync` / `_instantiate_async` gain `requested: Any = None`.
  Every caller that has the request in hand passes it: `get` (`:1377`),
  `get_all` (`:1416`), `_collect_sync` (`:1504`), `_collect_async` (`:1525`),
  `aget` (`:1568`), `aget_all` (`:1600`), the two delegate branches (`:3397`,
  `:3537`). `warm_up`/`awarm_up` (`:1271`, `:1310-1312`) do not — see
  "Warm-up". If an open binding reaches `_instantiate_*` with `requested=None`
  or with a request it does not close, raise `TypeError` naming the binding
  and the request — a programmer error, not a lookup miss.
- `Binding.create(container)` / `acreate` (`binding.py:62-85`) gain
  `requested: Any = None` on the ABC so the container never has to
  `isinstance`-dispatch; `ClassBinding` ignores it (documented as "reserved for
  open bindings").
- `ProviderBinding.type_args_for(requested) -> dict[str, Any]`: `{}` when
  closed; else `{tp.__name__: arg for tp, arg in _closing_args(...).items()}`.
  Keyed by **name** (row 3).
- `_collect_kwargs_sync/_async` (`:3054-3170`): before the per-param
  `_resolve_hint_*` call, `tp = _type_arg_param(hint)` (new `utils` helper:
  the `TypeVar` inside a hint of the exact shape `type[X]` / `typing.Type[X]`
  with `get_origin(hint) is type` and one `TypeVar` arg, **[R001:71-73]**;
  `None` for anything else, including `type[X] | None`). If `tp` is not `None`
  and `tp.__name__ in type_args`, set `resolved[param_name] = type_args[name]`
  and `continue` — no `InjectionPoint` context, no container lookup. When
  `type_args` is empty (every closed binding, every constructor) the branch
  costs one `get_origin` call and changes nothing: a `type[T]` parameter on a
  closed provider still resolves — or fails — exactly as today.
- Cycle key: `_call_provider` (`:3743-3774`) uses the *return annotation* as
  the cycle key (`:3760-3765`). For an open binding that would push `Repo[T]`
  for every closed request, so a `Repo[User]` factory that legitimately
  resolves `Repo[Order]` through the same binding would trip a false
  `CircularDependencyError` (`_check_cycle`, `:3831-3833`). `cycle_key=`
  overrides the key with the closed alias; `_format_cycle` renders it via
  `_type_name` as `Repo[User]`, which is also the more useful message.

### Cache key, disposal, eviction (row 4)

- `_get_cache_key(binding, requested=None)` (`:2432-2447`): returns
  `(binding.fn, requested)` when `binding.type_params` is non-empty, else the
  current key. The tuple is hashable (**[R001:99-103]**) and works unchanged as
  the per-key lock key (`:2620-2623`, `:2747-2751`), the re-entrancy key
  (`:2636`, `:2758`) and the `_singleton_order` entry (`:2646`, `:2766`).
- Teardown needs **no change**: `_teardown_plan` (`:4195-4266`) walks
  `_singleton_order` by key, and `_dispose_sync`/`_adispose` read
  `self._singleton_cache[key]` (`:4323`, `:4542`) — each closed instance has
  its own key, so every one is disposed, in reverse creation order. The
  defensive fallback tail (`:4256`) maps `_get_cache_key(b) → b` and would not
  recognise tuple keys; it is unreachable today (`:4251-4253`) and stays
  defensive — noted in its docstring, not fixed.
- `reset_binding()` (`:6563-6579`) evicts `b.fn`; for an open binding it must
  also evict every key `k` with `isinstance(k, tuple) and k[0] is b.fn` from
  `_singleton_cache`, `_singleton_locks`, `_async_singleton_locks` and
  `_singleton_order`. `reset_binding(Repo[User])` removes the open `Repo[T]`
  binding too — consistent with today's rule that `reset_binding(Base)`
  removes every binding whose interface satisfies `Base` (it is
  `_interface_matches`-based, `:6556`); documented as an edge case.
- `InstanceCreated.interface` (`:2658`, `:2688`, `:2775`, `:2800`) reports the
  **closed** alias for an open binding (`requested`), since that is what was
  created. `InstanceDisposed.interface` at the four singleton-shutdown sites
  (`:4447`, `:4459`, `:4602`, `:4614`) reports `key[1]` when the key is a
  tuple — one tiny helper `_event_interface(binding, key)` keeps the two
  event kinds consistent.

### Warm-up

`_filter_singleton()` (`:2324-2352`) feeds only `warm_up`/`awarm_up`
(`:1267`, `:1307`; verified by grep — three call sites total including its
own docstring). It gains `and not getattr(b, "type_params", ())`: an open
singleton cannot be pre-created because there is no closed alias to hand the
factory. Documented in both `warm_up` docstrings' Edge cases.

### `describe()` and `__repr__`

`ProviderBinding.describe()` (`binding.py:774-814`) already renders
`_type_name(self.interface)` → `"module.Repo[~T]"` — the `~T` is Python's own
`TypeVar` repr, so open bindings are visibly open with no descriptor field
change. `_collect_dependencies` on a `type[T]` param returns `None` at
`container.py:3250` (`_has_providify_metadata(type[T])` is `False`), so
`describe()` cannot crash on it. `__repr__` (`binding.py:685-699`) gains an
`, open` suffix beside `, async` so `repr(container._bindings)` in a debugger
shows it.

### Registration (row 8)

`ProviderBinding.__init__` (`binding.py:572-683`), immediately after
`self.interface = interface` (`:657`):

```python
# Row 8 — computed ONCE at registration, never in _filter() (same rule as
# profiles, :659-661). Raises for partially-open aliases.
self.type_params: tuple[TypeVar, ...] = _open_type_params(interface)
```

This is the only place the `TypeError` is raised, and it fires identically for
`container.provide(fn, returns=Repo[str, T])`, `@Provider(returns=...)`, and
`-> dict[str, T]`. `_normalize_explicit_interface` (`:432-540`) is **not**
changed — its job is shape normalisation, and its generic-alias branch
(`:460-461`, `:510-511`) already passes both open and closed aliases through
verbatim (**[GAP:15-24]**). The `@Provider` decorator (`decorator/scope.py:493-567`)
stores `returns` raw and is not changed either.

### Alternatives considered

- **Wildcard inside `_filter()` only, leaving `_interface_matches` literal** —
  rejected: ❌ `@Disposes(Repo[User])` could then never wire to an open
  binding, and `reset_binding(Repo[User])` would leave it in place; ✅ smaller
  diff. The structural helper must know about wildcards; the lookup helper
  adds the bare-request guard on top.
- **Bare guard inside `_interface_matches` (make `(Repo[T], Repo)` return
  `False`)** — rejected: ❌ silently breaks `@Disposes(Repo)` for open
  bindings (the natural disposer spelling) and `reset_binding(Repo)`; ❌
  changes a documented `True` row (`utils.py:164`). ✅ one site instead of two.
- **Explicit marker (`TypeArg[T]` / `Inject[TypeArgs]`) instead of positional
  `type[T]`** (the report's own fallback, **[GAP:178-180]**) — rejected for the
  first cut: ❌ new public symbol to export/document/maintain forever
  (`CONTRIBUTING.md`'s public-API rule); ❌ `type[T]` is already the idiomatic
  static annotation for "a class object of T" (**[R001:76-77]**), so a marker
  would be noise for type checkers; ✅ would make a forgotten parameter an
  error instead of silence. Revisit if row 3's silent-omission ❌ bites in
  practice.
- **Identity-based `TypeVar` mapping** — rejected: ❌ PEP 695 function-scoped
  `T` (`def factory[T](...)`) is a different object from a module-level `T`
  used in `returns=Repo[T]` (**[R001:38-39]** — PEP 695 params live in
  `__type_params__`), so identity would fail the most modern spelling. Names
  are what a reader sees; duplicate-name collisions across *different* scopes
  in one signature are pathological enough to ignore.
- **Specificity in `_filter()` (affecting `get_all`)** — rejected: ❌ makes
  "closed override" also hide the open contribution from enumeration, which
  is not what `get_all`/priority do today (priority is a sort key for
  `get_all`, `:1417`, never a filter); ✅ one site. Specificity is a
  *selection* rule like priority, so it lives beside priority in
  `_get_best_candidate`.
- **Origin-subclass wildcard (`issubclass(bind_origin, req_origin)` in the
  wildcard branch)** — rejected for the first cut (see the `Flip` example);
  ✅ would allow `Sub[T]` to serve `Repo[User]`; ❌ unsound positional mapping
  without walking `__orig_bases__` to re-map parameters. Follow-up.
- **`TypeError` for mixed aliases vs. "register as literal, warn in
  validate()"** — the report's default (`TypeError`) is kept: ✅ a mixed alias
  has never been resolvable for a closed request (**[GAP:40-48]**), so nothing
  working is broken; ✅ a loud registration failure beats a binding that
  matches nothing; ❌ a provider annotated `-> dict[str, T]` that was only
  ever requested as bare `dict` now fails to register — judged pathological,
  called out in CHANGELOG (§Risks).
- **Adding `typing_extensions` for `has_default()`** (recommended by
  **[R001:137]**) — rejected: nothing in this design reads defaults
  (**[R001:66]**); `pyproject.toml:41-54` has zero runtime deps and this plan
  keeps it that way.

---

## Steps

TDD-ordered. Steps 1-4 are RED; Steps 5-15 turn them GREEN; 16-20 are docs.

1. [x] `tests/test_open_generic_binding.py` — **new file**. Module docstring
       names `P26-OPEN-GENERIC-BINDING` and this plan; `from __future__ import annotations`;
       module-level `T = TypeVar("T")`, `S`, `Bounded = TypeVar("Bounded", bound=Entity)`,
       `Constrained = TypeVar("Constrained", str, bytes)`; `class Repo(Generic[T])`
       exactly as **[GAP:98-107]**; `class User`, `class Order`, `class Entity`,
       `class Product(Entity)`. Uses the `container` fixture (`tests/conftest.py:36`).
       Class `TestGapReproduction` — port **[GAP:110-131]** verbatim (both
       former-xfail bodies as plain passing tests) plus the control
       `test_closed_alias_still_matches_exactly` (**[GAP:139-142]**).
       **Acceptance (RED)**: the two ports fail with `LookupError` at
       `_get_best_candidate` (`container.py:2377`); the control passes.

2. [x] `tests/test_open_generic_binding.py` — class `TestMatchAndSpecificity`
       (rows 1, 2, 5, 8):
       - `test_bare_request_does_not_match_open_binding` — `get(Repo)` → `LookupError`;
         `is_resolvable(Repo)` is `False`.
       - `test_request_with_typevar_does_not_match_open_binding` — `get(Repo[T])` → `LookupError`.
       - `test_closed_binding_beats_open_regardless_of_priority` — open at
         `priority=10`, closed at `priority=0`, closed registered first and
         then again in the reverse order (two sub-cases) → `get(Repo[User])`
         returns the closed one; `get(Repo[Order])` returns the open one.
       - `test_get_all_closed_request_includes_open_binding_once` — one closed
         + one open → `get_all(Repo[User])` has length 2, exactly one from
         the open factory.
       - `test_get_all_bare_request_excludes_open_binding` — only an open
         binding → `get_all(Repo)` raises `LookupError`; with a closed
         `Repo[User]` also registered → length 1.
       - `test_inject_instances_closed_request_collects_open_binding` —
         `InjectInstances[Repo[User]]` on a `@Component`.
       - `test_two_typevars_all_open_supported` — `Pair[A, B]`, factory
         `(a: type[A], b: type[B])`, `get(Pair[int, str])` delivers `(int, str)`.
       - `test_repeated_typevar_must_close_consistently` — `Pair[T, T]`:
         `get(Pair[int, int])` resolves; `get(Pair[int, str])` → `LookupError`.
       - `test_mixed_alias_rejected_at_registration` — `returns=Pair[str, T]`,
         `returns=Repo[list[T]]`, and a `@Provider`-decorated `-> dict[str, T]`
         each raise `TypeError` from `provide()`; message contains the alias
         and the phrase "all-TypeVar or all-concrete".
       - `test_paramspec_and_typevartuple_rejected` — `TypeError`.
       - `test_pep695_class_syntax` — `class Box[X]: ...`, `returns=Box[T]`
         with module-level `T`, `get(Box[User])` resolves and delivers `User`.
       - `test_pep695_generic_function_type_param_matched_by_name` —
         `def factory[T](entity: type[T]) -> Box[T]` decorated `@Provider`,
         `provide(factory)` with no `returns=`; `get(Box[User])` delivers `User`.
       - `test_open_binding_via_return_annotation_without_returns` —
         `@Provider def f(entity: type[T]) -> Repo[T]`.
       - `test_reset_binding_closed_request_removes_open_binding_and_evicts_all_closed_instances`.
       - `test_describe_renders_open_binding_without_crashing` — descriptor
         `interface` contains `"~T"`; `repr(binding)` contains `", open"`.
       **Acceptance (RED)**: every test fails with `LookupError`/`AssertionError`
       except the two `TypeError` tests, which fail because nothing raises.

3. [x] `tests/test_open_generic_binding.py` — class `TestCachingAndLifecycle`
       (row 4):
       - `test_singleton_caches_per_closed_alias` — `singleton=True`;
         `get(Repo[User]) is get(Repo[User])`; `get(Repo[User]) is not get(Repo[Order])`;
         factory call count is 2.
       - `test_dependent_open_binding_creates_new_instance_each_time`.
       - `test_request_scoped_open_binding_caches_per_closed_alias_per_request`
         — `@Provider(scope=Scope.REQUEST)`; inside one `with container.request():`
         the same alias is cached, different aliases differ; a second request
         block yields new instances.
       - `test_every_closed_singleton_instance_is_disposed_at_shutdown` —
         `@Configuration` with the open `@Provider(singleton=True)` and a
         `@Disposes(Repo)` (bare — proves the structural fourth branch still
         wires); resolve `Repo[User]` and `Repo[Order]`; `shutdown()` calls the
         disposer exactly twice, once per instance, in reverse creation order.
       - `test_disposes_closed_alias_wires_to_open_binding` — `@Disposes(Repo[User])`
         also wires (wildcard in `_interface_matches`); **note**: plan 014's
         first-match loop semantics apply — assert only that the disposer runs.
       - `test_async_disposer_on_open_binding_runs_per_instance` — `ashutdown()`.
       - `test_warm_up_skips_open_bindings` — `warm_up()` does not call the
         factory; a closed singleton registered alongside **is** warmed.
       - `test_instance_created_event_reports_closed_alias` — hook records
         `InstanceCreated.interface is Repo[User]`.
       - `test_open_binding_requested_without_closing_alias_raises_typeerror`
         — call `container._instantiate_sync(binding)` directly on an open
         binding → `TypeError` (programmer-error guard).
       - `test_nested_closed_requests_through_same_open_binding_do_not_false_cycle`
         — factory for `Repo[X]` resolves `Repo[Order]` when `X is User`
         (guarded), otherwise returns plain; `get(Repo[User])` succeeds.
       - `test_open_binding_self_request_is_a_real_cycle` — factory for
         `Repo[X]` resolves `Repo[X]` → `CircularDependencyError` naming
         `Repo[User]`.

4. [x] `tests/test_open_generic_binding.py` — classes `TestBoundsAndConstraints`
       (row 7), `TestValidate` (row 6), `TestAsyncAndInjectionPaths`, and
       `TestFallbackInterplay`:
       - `test_bound_violation_is_non_match_not_error` — open `Repo[Bounded]`
         only → `get(Repo[User])` (`User` is not an `Entity`) → `LookupError`;
         `get(Repo[Product])` resolves.
       - `test_bound_violation_lets_second_binding_serve` — open `Repo[Bounded]`
         + open `Repo[T]` → `get(Repo[User])` comes from the unbounded one,
         `get(Repo[Product])` from… **both match** → priority decides; assert
         with an explicit `priority` so the test is deterministic.
       - `test_subscripted_generic_bound_is_checked_against_origin` —
         `TypeVar("B", bound=Repo[Entity])`; requesting `Outer[Repo[Product]]`
         resolves and `Outer[User]` does not (**[R001:87-88]**).
       - `test_non_runtime_checkable_protocol_bound_is_permissive`.
       - `test_constraints_use_identity_membership` — `Constrained` (str, bytes):
         `get(Repo[str])` resolves; `get(Repo[int])` → `LookupError`; a `str`
         subclass → `LookupError` (**[R001:92]**: no subclassing).
       - `test_validate_does_not_flag_type_param_as_missing` — open singleton
         with `entity: type[T]` and no other params → `validate()` returns
         `ok is True`, zero issues.
       - `test_validate_checks_closed_request_against_open_binding` — a
         `@Component` with `Inject[Repo[User]]` and only the open binding →
         no `MISSING_BINDING`.
       - `test_validate_reports_missing_with_bound_near_miss_message` —
         `Inject[Repo[User]]` with only `Repo[Bounded]` → one
         `MISSING_BINDING` whose message mentions `Repo[~Bounded]`… use
         `"bound"` and the open alias's `_type_name` as the assertion anchors.
       - `test_validate_closed_plus_open_is_not_ambiguous` — same priority,
         `Inject[Repo[User]]` → zero `AMBIGUOUS_BINDING`; graph edge points at
         the closed one (assert via `describe()` or via no `CIRCULAR_DEPENDENCY`
         false positive — keep it simple: zero issues).
       - `test_validate_type_param_on_closed_provider_is_still_missing` —
         `returns=Repo[User]` with `entity: type[T]` and no default → one
         `MISSING_BINDING` (the skip is open-bindings-only).
       - `test_aget_open_binding_async_factory` — `async def factory(entity: type[T]) -> Repo[T]`;
         `await container.aget(Repo[User])`; `get()` raises the existing
         async-provider `RuntimeError` (`container.py:1370-1373`).
       - `test_aget_all_closed_request_includes_open_binding`.
       - `test_constructor_inject_closed_alias_resolves_open_binding` —
         `@Component class Svc: def __init__(self, repo: Inject[Repo[User]])`.
       - `test_constructor_bare_annotation_closed_alias_resolves_open_binding`
         — `repo: Repo[User]` (no `Inject`) → the `_is_resolvable` path
         (`container.py:3425-3429`).
       - `test_constructor_bare_generic_class_annotation_does_not_match_open_binding`
         — `repo: Repo = None`-style default → stays `None` (no `LookupError`,
         `_is_resolvable(Repo)` is `False`).
       - `test_lazy_and_live_wrappers_resolve_closed_alias_through_open_binding`.
       - `test_open_fallback_loses_to_closed_non_fallback` and
         `test_closed_fallback_loses_to_open_non_fallback` — **require plan
         017's `@Fallback`**; mark with `pytest.importorskip`-style guard on
         `from providify import Fallback` so the file stays green if 017 is
         reordered. Assert the order table in §Design.
       **Acceptance (RED)**: fails as in Step 2.

5. [x] `providify/utils.py` — add `_open_type_params()`, `_closing_args()`,
       `_type_arg_param()` exactly per §Design (each with full docstring: Args /
       Returns / Raises / Edge cases incl. `Pair[T, T]`, subscripted bound,
       Protocol bound, lazy PEP 695 bound `NameError`, `ParamSpec`; Thread/
       Async safety ✅ pure). Import `TypeVar` from `typing`. Extend
       `_interface_matches()`'s both-generic branch (`:174-180`) per §Design;
       rewrite its docstring table (`:133-145`) to add the row
       `generic-open (Repo[T]) | generic-closed (Repo[User]) | identical origin + closing args (wildcard)`
       and replace the ⚠️ row at `:157-158` with: binding-side `TypeVar` args
       are wildcards (this plan); request-side `TypeVar` args are still
       compared literally ⚠️. Rewrite the module DESIGN comment `:22-25`.
       **Tests**: add to `tests/test_generics.py` (existing `_interface_matches`
       unit tests live there — see `:1155`) four cases: `(Repo[T], Repo[User]) → True`,
       `(Repo[User], Repo[T]) → False`, `(Repo[T], Repo) → True` (unchanged
       fourth branch — **regression guard for the bare-guard placement
       decision**), `(Sub[T], Repo[User]) → False` (identical-origin rule).
       **Acceptance**: `uv run pytest tests/test_generics.py -q` green.

6. [x] `providify/binding.py` — `Binding.create/acreate` (`:62-85`) gain
       `requested: Any = None` (docstring: "the closed alias being resolved;
       consulted only by open `ProviderBinding`s, reserved otherwise").
       `ClassBinding.create/acreate` (`:240`, `:262`) accept and ignore it.
       `ProviderBinding.__init__` (`:657`) adds `self.type_params` per §Design
       (import `_open_type_params` from `.utils`); class docstring gains an
       "open generic" paragraph and an Edge-cases bullet for the `TypeError`.
       Add `type_args_for(requested) -> dict[str, Any]` (full docstring).
       `create/acreate` (`:734-772`) forward
       `type_args=self.type_args_for(requested), cycle_key=requested if self.type_params else None`.
       `__repr__` (`:685-699`) gains `, open`.
       **Acceptance**: the `TypeError` tests in Step 2 go GREEN; `ruff` clean.

7. [x] `providify/container.py` — add `_binding_serves()` (method, beside
       `_binding_is_active`, `:1606`) and module-level `_prefer_closed()` per
       §Design. Use `_binding_serves` in `_filter()` (`:1720`) and
       `_is_resolvable()` (`:2824`). Use `_prefer_closed` in
       `_get_best_candidate()` (`:2375`, before `max`) and the two
       `DelegateMeta` branches (`:3396`, `:3536`). Update `_filter()`'s
       docstring table with two rows (open binding + closed request →
       included; open binding + bare/TypeVar request → excluded) and
       `_get_best_candidate`'s docstring with the specificity step and its
       ordering relative to the fallback drop (cite plan 017).
       **Acceptance**: `test_bare_request_does_not_match_open_binding`,
       `test_request_with_typevar_…`, `test_get_all_*`, specificity tests GREEN.

8. [x] `providify/container.py` — `_get_cache_key(binding, requested=None)`
       (`:2432-2447`) returns `(binding.fn, requested)` for open bindings.
       `_instantiate_sync` / `_instantiate_async` (`:2559`, `:2697`) gain
       `requested: Any = None`; compute `key = self._get_cache_key(binding, requested)`;
       raise `TypeError` when `getattr(binding, "type_params", ())` is truthy
       and (`requested is None` or `_closing_args(binding.interface, requested) is None`);
       call `binding.create(self, requested=requested)` /
       `await binding.acreate(self, requested=requested)`; `InstanceCreated`
       (`:2656-2665`, `:2684-2694`, `:2772-2782`, `:2797-2807`) uses
       `interface=self._event_interface(binding, key)`. Add
       `_event_interface(binding, key)` (static; returns `key[1]` for tuple
       keys else `binding.interface`) and use it at the four `InstanceDisposed`
       singleton sites (`:4447`, `:4459`, `:4602`, `:4614`). Docstrings:
       Edge-cases bullets for "open binding → key is `(fn, closed alias)`,
       one instance per closed alias (**[R016:18]** anti-pattern avoided)".
       **Acceptance**: `TestCachingAndLifecycle` caching + event tests GREEN.

9. [x] `providify/container.py` — thread `requested=` through every
       instantiation call that has the request: `get` (`:1377`), `get_all`
       (`:1416`), `_collect_sync` (`:1504`), `_collect_async` (`:1525`), `aget`
       (`:1568`), `aget_all` (`:1600`), delegate branches (`:3397`, `:3537`).
       `_filter_singleton()` (`:2346-2352`) excludes open bindings; `warm_up`/
       `awarm_up` docstrings (`:1253-1258`, `:1294-1299`) gain the Edge case.
       **Acceptance**: gap-report ports (Step 1) GREEN; `test_warm_up_skips_open_bindings` GREEN.

10. [x] `providify/container.py` — `_call_provider` / `_call_provider_async`
        (`:3743-3808`) gain `*, type_args: Mapping[str, Any] | None = None, cycle_key: Any = None`;
        `return_type = cycle_key if cycle_key is not None else self._get_provider_return_type(fn)`;
        forward `type_args` to `_collect_kwargs_*`. `_collect_kwargs_sync/_async`
        (`:3054-3170`) gain `type_args` and the `type[X]` pre-fill branch per
        §Design (before the `InjectionPoint` context is built — a
        container-supplied value is not an injection point). Docstrings:
        Args + Edge cases ("`type[X] | None` is not recognised — must be
        exactly `type[X]`"; "closed provider with a `type[T]` param is
        unchanged").
        **Acceptance**: `test_open_alias_factory_receives_closed_type_argument`,
        two-TypeVar, PEP 695, cycle tests, async tests GREEN.

11. [x] `providify/container.py` — `reset_binding()` (`:6563-6579`): for a
        `ProviderBinding` with `type_params`, extend `to_evict` with every
        tuple key whose `[0] is b.fn` found in `_singleton_cache`,
        `_singleton_locks`, `_async_singleton_locks` (union of the three key
        sets); the `_singleton_order` prune (`:6585`) then covers them via
        `evicted`. Docstring Edge case: closed request removes the open
        binding that serves it. `_teardown_plan` docstring (`:4223-4230`):
        note the fallback tail does not recognise open-binding tuple keys and
        why that is acceptable (unreachable today).
        **Acceptance**: `test_reset_binding_…` GREEN; `tests/test_shutdown_order.py`,
        `tests/test_disposes.py` unchanged.

12. [x] `providify/container.py` — `_iter_injection_points()` ProviderBinding
        branch (`:5974-5986`): `continue` when `binding.type_params` is truthy
        and `_type_arg_param(hint)` names one of them — with a why-comment
        ("container-supplied from the closing args, never a graph edge").
        Docstring Edge case added.
        **Acceptance**: `test_validate_does_not_flag_type_param_as_missing`
        GREEN; `test_validate_type_param_on_closed_provider_is_still_missing`
        GREEN.

13. [x] `providify/container.py` — `validate()` pass 2: apply
        `_prefer_closed(candidates)` immediately after the `excludes_self`
        filter (`:5606`) and before the `if not candidates` check (`:5608`);
        the ambiguity ties (`:5652-5653`) and the edge pick (`:5680`) then
        operate on the specificity-reduced list, mirroring
        `_get_best_candidate` exactly. In the `MISSING_BINDING` branch
        (`:5629-5645`), when `memo_filter` found nothing, scan
        `self._bindings` for open bindings with the same origin as
        `spec.base_type` whose `_closing_args` is `None` and append to the
        message: `" An open binding {_type_name(b.interface)} ({owner_of(b)}) exists but {arg} does not satisfy its TypeVar bound/constraints."`
        (row 7 near-miss; no new `IssueKind`). Update `validate()`'s
        docstring: specificity in the ambiguity check; the near-miss sentence;
        `type[T]` skip.
        **Acceptance**: `TestValidate` GREEN; `tests/test_validation.py`
        unchanged.

14. [x] `providify/container.py` — `provide()` docstring (`:1048-1088`): add
        the open-generic example (the varco spelling, **[GAP:167-174]**),
        state the eight rules in one short list (wildcard, closed-beats-open,
        `type[T]` delivery by name, per-closed-alias cache, enumeration,
        validate, bound = non-match, all-or-nothing aliases), and add
        `TypeError` for partially-open aliases to Raises. Mirror the essentials
        in `@Provider`'s docstring (`decorator/scope.py:512-567`, one example).
        **Acceptance**: `make format-check` clean.

15. [x] Full RED→GREEN pass: `uv run pytest tests/test_open_generic_binding.py -q`
        all green; `make test` count = previous count + new tests; `make format-check` clean.
        Re-grep for the merge points listed in §Risks before committing.
        NOTE: `make format-check` fails on ~55 pre-existing files unrelated
        to this plan (verified via `git stash` — same failures exist on the
        base commit before this plan's changes); every file this plan
        touched is individually `ruff format --check` clean.

16. [x] `README.md` — new subsection `### Open-generic providers — returns=Repo[T]`
        directly after `## Generic types` (`:1439-1468`): the varco-shaped
        example, the `type[T]` parameter, "one singleton per closed alias",
        "closed beats open", `get_all` closed-vs-bare, `warm_up` skip,
        `bound=` non-match, mixed alias `TypeError`. One sentence in
        `## @Provider` (`:277`) linking to it. One line in
        `## Startup validation` (`:1495ff`): `type[T]` params are not
        injection points; closed requests are validated against open bindings.

17. [x] `docs/agents/usage-rules.md` — R10 (`:151-168`): after the
        per-model loop example, show the single open registration as the
        preferred form when the factory can take `entity: type[T]`, and keep
        the loop as the form for when it cannot. `docs/agents/injection-cheatsheet.md`:
        one row `entity: type[T]` (in an open `@Provider`) → "filled with the
        closed type argument, not resolved from the container".
        `docs/agents/anti-patterns.md` (`:162-172`): one sentence noting the
        open form removes the per-type loop entirely.

18. [x] `CHANGELOG.md` `## [Unreleased]` (`:10`) — `### Added`: open-generic
        provider bindings (one paragraph naming all eight rules briefly, the
        Autofac model **[R016:25]**, per-closed-alias caching **[R016:18]**,
        link to this plan). `### Changed`: `_interface_matches` wildcard on the
        binding side. ⚠️ Two narrow behaviour changes called out: (a) a
        provider whose interface is an open alias is no longer served for a
        **bare** origin request (`get(Repo)`) — it was served with an
        uninformed factory call before; (b) a partially-open alias
        (`dict[str, T]`, `Repo[list[T]]`) now raises `TypeError` at
        registration instead of registering an unmatchable binding. Do **not**
        bump `pyproject.toml:3`.

19. [x] `docs/agents/README.md` / `choosing-decorators.md` — only if either
        has a `returns=` or generics row; add a one-line pointer to the README
        subsection. (Grep first; skip if absent.)

20. [x] Final verification (§Verification) and the exhaustiveness re-grep in
        §Risks.

---

## Edge cases

| # | input / state | expected |
|---|---|---|
| E1 | open `Repo[T]`, `get(Repo[User])` | factory called; `entity=User` if declared `type[T]`; instance returned (**[GAP:110-131]**) |
| E2 | open `Repo[T]`, `get(Repo)` (bare) | `LookupError`; `is_resolvable(Repo)` `False`; a coexisting closed `Repo[User]` **is** returned for `get(Repo)` (fourth branch, unchanged) |
| E3 | open `Repo[T]`, `get(Repo[T])` | `LookupError` (request-side TypeVar is literal; `_binding_serves` needs closing args) |
| E4 | open `Repo[T]` prio 10 + closed `Repo[User]` prio 0 | `get(Repo[User])` → closed; `get(Repo[Order])` → open |
| E5 | open + closed, `get_all(Repo[User])` | both, priority-ascending; open contributes once |
| E6 | `Pair[T, T]`, `get(Pair[int, str])` | `LookupError` (inconsistent closure); `Pair[int, int]` resolves with `T=int` |
| E7 | `returns=Pair[str, T]` / `-> dict[str, T]` / `Repo[list[T]]` / `Repo[P]` (`ParamSpec`) | `TypeError` at `provide()`/`install()`/`scan()` (wherever `ProviderBinding` is built) |
| E8 | `class Sub(Repo[T])`, open `Sub[T]`, `get(Repo[User])` | `LookupError` — identical origin required (first cut) |
| E9 | `singleton=True`, `get(Repo[User])` ×2, `get(Repo[Order])` | 2 instances, 2 factory calls, both disposed at `shutdown()` in reverse creation order |
| E10 | `scope=Scope.REQUEST` open binding | per closed alias **per request frame**; outside a request → the existing `RuntimeError` (`container.py:2409`) |
| E11 | `warm_up()` with an open singleton | skipped silently; closed singletons still warmed; no `RuntimeError` |
| E12 | `TypeVar("B", bound=Entity)`, `get(Repo[User])` with `User` not an `Entity` | `LookupError` unless another binding serves; `Repo[Product]` resolves |
| E13 | bound is `Repo[Entity]` (subscripted) | checked against `get_origin(bound)` — never raises `TypeError` from `issubclass` (**[R001:87]**) |
| E14 | bound is a non-`@runtime_checkable` `Protocol`, or a lazily-evaluated PEP 695 bound whose forward ref is unresolvable | permissive: treated as satisfied (`TypeError`/`NameError` swallowed, **[R001:143]**) |
| E15 | constraints `(str, bytes)`, `get(Repo[MyStr])` with `class MyStr(str)` | `LookupError` — identity membership, no subclassing (**[R001:92]**) |
| E16 | factory param `entity: type[T] | None = None` | **not** filled (shape must be exactly `type[X]`); resolves to `None` via the union branch as today |
| E17 | factory param `entity: type[T]` on a **closed** binding `returns=Repo[User]` | unchanged: `LookupError` at resolve, `MISSING_BINDING` in `validate()` |
| E18 | factory for `Repo[X]` resolves `Repo[Order]` through the same binding | no false cycle — cycle key is the closed alias |
| E19 | factory for `Repo[X]` resolves `Repo[X]` | `CircularDependencyError` naming `Repo[User]` |
| E20 | `validate()` with open singleton whose only param is `type[T]` | zero issues, `ok is True` |
| E21 | `validate()` — `Inject[Repo[User]]`, only `Repo[Bounded]` registered, `User` violates | one `MISSING_BINDING` whose message names the open binding as a near-miss |
| E22 | `validate()` — closed + open tied at priority | no `AMBIGUOUS_BINDING`; edge → closed |
| E23 | `@Disposes(Repo)` (bare) on the `@Configuration` that declares the open provider | wires (fourth branch unchanged); runs once per closed instance |
| E24 | `@Disposes(Repo[User])` | also wires (wildcard); subject to plan 014's wiring rules |
| E25 | `reset_binding(Repo[User])` | removes the open binding; evicts every `(fn, X)` key |
| E26 | `InstanceCreated`/`InstanceDisposed` for an open binding | `.interface` is the **closed** alias |
| E27 | PEP 695 `class Box[X]` + module `T`, `returns=Box[T]` | works — `get_args` identical for both class styles (**[R001:49]**) |
| E28 | PEP 695 `def factory[T](entity: type[T]) -> Box[T]` with `@Provider`, no `returns=` | works — function `__type_params__` are seeded into the annotation namespace (`_annotations.py:235-239`), mapping is by name |
| E29 | `InjectInstances[Repo]` (bare) on a component, only an open binding | `[]` — never expands (row 5) |
| E30 | `describe()` on a container with an open binding | renders `…Repo[~T]`; no crash; `type[T]` param is not a dependency node |
| E31 | open `@Fallback Repo[T]` + closed `Repo[User]` | closed wins (both rules agree) |
| E32 | closed `@Fallback Repo[User]` + open `Repo[T]` | **open** wins (fallback dropped first) |
| E33 | `_instantiate_sync(open_binding)` with no `requested` | `TypeError` (programmer error) |
| E34 | empty container | unchanged |

---

## Verification

```bash
cd /home/edoardo/projects/providify

# RED first: after Steps 1-4, before Step 5
uv run pytest tests/test_open_generic_binding.py -x -q          # must FAIL (LookupError / no TypeError)

# GREEN: after Step 13
uv run pytest tests/test_open_generic_binding.py -q

# no regression in the surfaces this plan touches
uv run pytest tests/test_generics.py tests/test_validation.py tests/test_disposes.py \
              tests/test_shutdown_order.py tests/test_scoped_providers.py \
              tests/test_multibinding.py tests/test_inject.py tests/test_profiles.py -q

# full suite (≈1043 at 2.0.1 + 014/015/017 additions + this plan's)
make test

# lint + format (ruff only — no type-check target, Makefile:9-18)
make format-check
```

Manual: run the varco guard against the new build —
`cd /home/edoardo/projects/varco && make test PKG=varco_core -- -k TestP26OpenGenericBinding`
(exact invocation per varco's Makefile); both `strict=True` xfails must turn
into **XPASS-as-failure** until varco removes the markers (§Downstream).

Merge-point re-grep before finishing:

```bash
rg -n '_interface_matches\(|_get_cache_key\(|_instantiate_sync\(|_instantiate_async\(|binding\.create\(|binding\.acreate\(' providify/
rg -n 'IssueKind|Severity\.INFO' providify/ tests/ --glob '!plans/*'
```

---

## Versioning

**Minor — 2.1.0.** Not a patch, not a major.

- Additive public behaviour: `provide(returns=Repo[T])` now resolves closed
  requests; `ProviderBinding.type_params` / `type_args_for()` are new
  attributes on an exported class; `Binding.create/acreate` gain an optional
  kwarg (source-compatible for subclasses that do not accept it only if the
  container never passes it — it **does**, so third-party `Binding` subclasses
  must accept `requested=`; `AnyBinding` is a closed union
  (`binding.py:827`), so no such subclass can be registered today).
- Two narrow behaviour changes (CHANGELOG Step 18, Risks below), both on
  registrations that were never resolvable for a closed request.
- `validate(raise_on_error=True)` cannot newly raise for a previously-clean
  graph: the only new `ERROR` path is E17/E21, both pre-existing `MISSING_BINDING`s.
- `pyproject.toml:3` bump and CHANGELOG heading move happen in the release
  cut (mirror `plans/013`), not here.

---

## Downstream — what varco must do

- `varco_core/tests/test_providify_upstream_gaps.py::TestP26OpenGenericBinding::test_open_alias_registration_resolves_closed_request`
  and `::test_open_alias_factory_receives_closed_type_argument` are
  `xfail(strict=True, raises=LookupError)` (**[GAP:213-216]**). Against 2.1.0
  they XPASS, which `strict=True` turns into a **failure** — the loud flip the
  index asks for (`000-index:43-46`). varco removes the two markers; the
  control `test_closed_alias_still_matches_exactly` is untouched.
- The three per-type loops (`varco_sa/varco_sa/di.py:292-393`,
  `varco_beanie/varco_beanie/di.py:103-202`, `varco_fastapi/varco_fastapi/di.py:108-196`,
  **[GAP:63-82]**) can collapse to one open registration each **once varco
  pins providify `>=2.1.0`**. Two things varco must check when it does:
  (a) its factories become `def repo_factory(provider: RepositoryProvider, entity: type[T]) -> AsyncRepository[T]`
  — the parameter must be annotated exactly `type[T]` (E16);
  (b) `singleton=True` semantics are per closed alias (E9), which is what
  `bind_clients` wants and what `bind_repositories` (`singleton=False`) does
  not care about.
- Nothing in this plan requires a varco-side shim (**[GAP:205-209]**).

---

## Risks

- ⚠️ **ASSUMPTION** — plans 014/015/017 were not on disk when this was
  written; their shapes are taken from `000-index:14-33`. Verify before Step 7
  that 017's fallback-drop is **inside `_filter()`** (not in
  `_get_best_candidate`); if it moved, `_prefer_closed` must still run
  *after* it — adjust placement, keep the order, keep E31/E32.
- ⚠️ **ASSUMPTION** — plan 014's rewritten `@Disposes` loop still matches on
  `_interface_matches(binding.interface, disposed_type)`. If it switched to a
  stricter identity/qualifier rule, E23/E24 may need the disposer named as
  `Repo[T]` (literal branch) instead of bare `Repo`; the tests in Step 3 will
  say so.
- ⚠️ **ASSUMPTION** — plan 015's `Severity.INFO` is **not** needed here (no
  new `IssueKind`). If a reviewer asks for the optional INFO-level bound
  near-miss issue from **[GAP:192]**, add `IssueKind.OPEN_BINDING_NEAR_MISS`
  reusing 015's `Severity.INFO` (never redefine it) — and then also add it to
  the exhaustiveness audit table in `plans/012` §Steps.
- **Merge points** (all four slices touch these — rebase carefully, re-run
  `make test` after each merge): `providify/utils.py` (this plan only, but
  017 reads `_interface_matches` semantics); `providify/container.py`
  `_filter()` (017 + this), `_get_best_candidate()` (this), singleton cache/
  `_get_cache_key`/`_instantiate_*` (this), `validate()` pass 2 (015 + 017 +
  this), `@Disposes` wiring loop (014 — untouched here, but E23/E24 depend on
  it); `providify/binding.py` `ProviderBinding.__init__` (015 may add
  `requires` metadata reads nearby); `providify/__init__.py` (**no new export
  from this plan** — verify nothing needs adding); `CHANGELOG.md`
  `[Unreleased]` (all four); README/`docs/agents/` (all four).
- **Behaviour change (a)** — a provider registered with an open alias and
  requested by **bare origin** (`get(Repo)`) was served before (factory called
  with no type info); now `LookupError`. No in-repo test relied on it
  (grepped `tests/test_generics.py` — no TypeVar-alias binding tests), and
  **[GAP:40-48]** documents the whole TypeVar-alias path as a non-feature.
  Invariant: bare requests against **closed** aliases are untouched (E2).
- **Behaviour change (b)** — partially-open aliases now raise at registration.
  Same justification; called out in CHANGELOG.
- **Third-party `Binding` subclasses** — `create/acreate` are now called with
  `requested=`. `AnyBinding` is the closed union `ClassBinding | ProviderBinding`
  (`binding.py:827`) and every registration path constructs one of those two,
  so no external subclass can reach `_instantiate_*` today. Invariant: keep
  `requested` keyword-only with a default so a future subclass that ignores it
  still works.
- **`_hints_cache` keyed by callable** (`container.py:3007-3013`) — the
  `type[T]` pre-fill reads the cached hints; nothing about the cache changes,
  but the pre-fill must run **before** `_resolve_hint_*` or a `type[T]` hint
  would hit `_is_resolvable(type[T])` and fall through to `_UNRESOLVED` →
  `LookupError` at `:3108-3112`. Invariant: pre-fill first, always.
- **Permissive bound check (E14)** — a Protocol-bounded open binding will
  serve any request; two open bindings on the same origin differing only by
  Protocol bounds cannot be distinguished. Documented; static checkers cover
  it. Invariant: a check that cannot run never *blocks* resolution.
- **Cache-key shape leaks into `_singleton_order`/`ShutdownFailure`** — labels
  come from `_owner_label(binding)` (`:4337-4351`), not from the key, so
  messages are unaffected. `_teardown_plan`'s defensive fallback tail (`:4256`)
  cannot map tuple keys; unreachable today (`:4251-4253`), documented.
- **Alias identity across `typing.List[int]` vs `list[int]`** — irrelevant
  for user generics (`Repo[User]` is always a `typing._GenericAlias`, cached
  and identity-stable, **[R001:102]**); a `list[T]` open binding keyed by
  `typing.List[int]` vs `list[int]` would produce two cache entries. Not
  guarded — exotic, and both spellings are `==`-distinct today anyway.
- **PEP 695 lazy bound `NameError` (E14)** — **[R001:157]** flags the exact
  timing as an evidence gap; the `try/except NameError` is the mitigation.
  Add one test with a forward-referenced bound to lock the permissive path.

---

## Follow-ups (not this plan)

1. **Origin-subclass wildcard** (`Sub[T]` serving `Repo[User]`) — requires
   re-mapping parameters through `__orig_bases__` (the `_is_generic_subtype`
   walk, `utils.py:115-127`) so positional delivery is sound across origins.
2. **Partial closure** (`Mapper[S, T]` with `S` fixed, `Repo[list[T]]`) —
   lift the row-8 `TypeError` once a `__parameters__`-driven substitution
   (`alias[*args]`, **[R001:113-117]**) replaces the all-or-nothing rule.
3. **Explicit `TypeArg[T]` marker** — if row 3's silent omission proves to be
   a support burden (**[GAP:178-180]**).
4. **Warm-up of open bindings** — only possible with a user-supplied list of
   closed aliases (`warm_up(closed=[Repo[User], Repo[Order]])`); new public
   surface, separate decision.
