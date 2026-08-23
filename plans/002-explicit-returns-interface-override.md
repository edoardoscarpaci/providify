# Plan 002 — `returns=`: explicit interface override for `@Provider` and `container.provide()`

Implements upstream gap **U-20** (P2, hygiene / API-surface completeness).

## Goal
A factory's binding interface can be stated explicitly, bypassing return-annotation derivation
entirely, through one keyword on two entry points:

```python
container.provide(factory, returns=AsyncRepository[User])       # direct value
@Provider(returns=AsyncRepository[User])                        # direct value
@Provider(returns=lambda: AsyncRepository[User])                # deferred, evaluated at provide()
```

When `returns=` is given, `ProviderBinding` never reads `fn.__annotations__` at all — the factory
may be annotated `-> Any`, annotated with an unresolvable forward ref, or unannotated. This removes
the only reason a caller ever has to mutate `factory.__annotations__["return"]` before registering.

## Non-goals
- **No string forward refs in `returns=`.** `returns="Repository[User]"` is rejected with a
  pointed error. The whole point of the feature is to escape namespace-dependent resolution;
  accepting a string would re-import that ambiguity (which namespace? `fn.__globals__`? the
  caller's frame?). The deferred-callable form covers every case a string would.
- No change to how the return annotation is resolved when `returns=` is **absent** — the existing
  `_resolve_return_annotation()` path is untouched (behaviour and error messages identical).
- No `returns=` on `bind()` / `ClassBinding` / `@Component`. Class bindings already take their
  interface explicitly.
- No deprecation of the annotation-derived path. It stays the idiomatic default (rule R1 in
  `docs/agents/usage-rules.md` is unchanged).
- No version bump in `pyproject.toml` — CHANGELOG entry only; release process owns the bump.
- **Out of this repo's scope (informational):** once merged, the downstream shim
  `varco_core.providify_compat.provide_factory()` and its six `__annotations__["return"] = ...`
  patch sites become obsolete and should be deleted there, replaced with
  `container.provide(fn, returns=Iface)`. Nothing in this plan touches `varco_core`.

## Design

### Data flow

```
@Provider(returns=X)                container.provide(fn, returns=Y)
        │                                     │
        ▼                                     ▼
ProviderMetadata.returns = X          ProviderBinding(fn, returns=Y)
        │                                     │
        └──────────────► meta.returns ────────┤
                                              ▼
                             override = Y if Y is not None else meta.returns
                                              │
                       ┌──────────────────────┴──────────────────────┐
                override is None                              override is not None
                       │                                             │
        _raw_annotations(fn)["return"]                _normalize_explicit_interface(
        → _resolve_return_annotation()                     override, fn)
                       │                                             │
                       └──────────────► self.interface ◄─────────────┘
```

Precedence, highest first (state this verbatim in docstrings):
1. `container.provide(fn, returns=...)` — call-site override
2. `@Provider(returns=...)` — decoration-time override
3. `fn`'s resolved return annotation

**When an override is present the annotation is not read, not evaluated, and not validated.**
That is deliberate: a caller who already told providify what the factory produces must not also
have to repair an unrelated static annotation (`-> Any`, `-> "TYPE_CHECKING.Thing"`) to register.
The "must declare a return type hint" `TypeError` is likewise skipped.

### `returns=` accepted shapes

`_normalize_explicit_interface(returns, fn)` in `binding.py`, applied identically to both entry
points, resolves in this order:

| input | handling |
|---|---|
| a type (`isinstance(v, type)`) | used directly — **not** called, even though classes are callable |
| a parameterised generic alias (`get_origin(v) is not None`), e.g. `Repository[User]`, `list[int]` | used directly |
| `Annotated[X, ...]` | unwrapped to `X`, then re-validated (mirrors `_resolve_return_annotation`) |
| any other callable (lambda, function, `functools.partial`) | called with zero args; the result is re-run through the table above (one level only, no recursion into a callable returning a callable) |
| `str` | `TypeError` naming the provider and pointing at the deferred-callable form |
| anything else (`42`, `None`-as-value is unreachable, an instance) | `TypeError` naming the provider, the given value, and its type |
| deferred callable that raises | `TypeError` naming the provider, chained `from exc` |

The type-before-callable ordering is the disambiguation rule: `returns=SomeClass` means "the
interface is `SomeClass`", never "call `SomeClass()` to obtain the interface". A factory that
*returns a type object* is not a thing providify can bind anyway.

Final validation is the same predicate `_resolve_return_annotation` already enforces at
`binding.py:419` — `isinstance(value, type) or get_origin(value) is not None` — so an explicitly
given interface can never be weaker than a derived one, and every downstream consumer
(`_build_localns`'s `interface.__name__`, `_interface_matches`, `_is_generic_subtype`,
descriptors, cycle detection) sees exactly the same shape it sees today.

### Sentinel choice
`None` means "not given". A provider producing `None` is meaningless, and an author who genuinely
wants `NoneType` can write `returns=type(None)`. This avoids exporting a public `_UNSET` sentinel
from `binding.py` / `container.py` just for a case that cannot occur.

### `@Provider` merge semantics
`ProviderMetadata.merge()` uses `updates.get(key, self.key)`, so a key that is *not passed* is
preserved. Today `@Provider`'s decorator body always passes every kwarg, meaning stacking
`@Provider(qualifier="a")` on top of `@Provider(returns=X)` would clobber `returns` back to `None`.
Fix by building the merge kwargs dict and **omitting `returns` when it is `None`** (Step 5). Only
`returns` gets this treatment — do not "fix" `qualifier`/`priority`/`scope`, whose current
clobbering behaviour is relied on by existing tests.

### Alternatives considered
- **Keep the `__annotations__["return"] = ...` patch-then-register idiom, just document it.**
  ❌ Mutates a function object the caller may not own; ordering between decorate and patch is
  load-bearing but invisible in either signature; breaks the moment `@Provider` starts reading
  annotations at decoration time. ✅ Zero code. Rejected — this is precisely the trap U-20 reports.
- **New method `container.provide_as(interface, fn)`.** ✅ Unambiguous, no sentinel needed.
  ❌ Duplicates `provide()`'s entire docstring/behaviour surface, and does nothing for providers
  discovered by `scan()` or `install()` — those never reach a manual call site, so a generic-alias
  provider inside a scanned module would remain unfixable. Rejected.
- **`returns=` on `provide()` only, not on `@Provider`.** ❌ Same discovery-path hole as above.
  Rejected.
- **`returns=` on `@Provider` only, not on `provide()`.** ✅ One place to look.
  ❌ Leaves the "wrap someone else's plain function" case (the varco `provide_factory` shim's actual
  use) needing a decorator application, i.e. still mutating a foreign function object. Rejected.
- **Positional `provide(fn, interface)`.** ❌ Reads ambiguously at the call site and diverges from
  `@Provider`'s all-keyword style. Rejected.
- **Accept `str` forward refs in `returns=`.** ❌ Re-introduces the namespace question the feature
  exists to eliminate; the deferred callable is strictly more capable. Rejected (see Non-goals).
- **Callable-only (`returns` must always be a lambda).** ✅ One shape, no disambiguation rule.
  ❌ Forces `returns=lambda: Foo` noise on the 90% case where the type is already in hand.
  Rejected in favour of accepting both.

## Steps

TDD order: the failing-test step precedes its implementation step in each pair.

1. [ ] `tests/test_explicit_returns.py` — **new file**. Module docstring citing U-20 (why the
   override exists: a per-domain-model generic alias built inside a loop cannot be named by any
   static annotation). Follow `tests/test_forward_ref_provider.py` layout: `from __future__ import
   annotations`, module-level domain types under a banner comment, then test sections. Write the
   full suite from "Test coverage" below; all of it fails/errors at this point (`TypeError:
   provide() got an unexpected keyword argument 'returns'`).

2. [x] `providify/metadata.py:196` — `ProviderMetadata.__slots__` += `"returns"`.

3. [x] `providify/metadata.py:198-213` — `__init__` gains `returns: Any = None` (last parameter,
   keyword-usable) and `self.returns = returns`. Docstring: document the accepted shapes in one
   sentence and point at `_normalize_explicit_interface` for the full rule; state that the value is
   stored **raw and unvalidated** here — validation happens once, at `ProviderBinding` construction,
   so a deferred callable is never invoked at decoration time.

4. [x] `providify/metadata.py:215-241` — `merge()` passes `returns=updates.get("returns",
   self.returns)`; `__repr__` includes `returns={self.returns!r}`; `__setstate__` seeds defaults for
   slots missing from `state` (so a pickle written by ≤1.1.0, which has no `"returns"` key, unpickles
   into a usable object instead of leaving the slot unset and raising `AttributeError` on first
   read). `default()` needs no change — it relies on the new `None` default.

5. [x] `providify/decorator/scope.py:474-500` — add `returns: Any = None` to the keyword-only
   `@overload` and to the real `Provider(...)` signature. In the decorator body (lines 538-564):
   build `updates: dict[str, Any]` with `singleton`/`qualifier`/`priority`/`scope`/`is_async`, then
   `if returns is not None: updates["returns"] = returns` — omitting the key so `merge()` preserves
   a `returns` set by an earlier stacked `@Provider`. The fresh-metadata branch passes
   `returns=returns` unconditionally. Extend the docstring `Usage:` block with the two override
   examples and the precedence list.

6. [x] `providify/binding.py` (new module-level helper, directly after
   `_resolve_return_annotation`, ~line 426) — `_normalize_explicit_interface(returns: Any, fn:
   Callable[..., Any]) -> Any`. Implements the shape table above. Full docstring with
   `Args`/`Returns`/`Raises`/`Edge cases`, and a `WHY` comment recording the type-before-callable
   disambiguation rule and the string rejection. Error messages must name `fn.__name__`, the offending
   value, and the fix — same tone as lines 401-406 and 420-423.

7. [x] `providify/binding.py:456-513` — `ProviderBinding.__init__(self, fn, *, returns: Any = None)`.
   After the metadata lookup:
   ```
   override = returns if returns is not None else meta.returns
   if override is not None:
       self.interface = _normalize_explicit_interface(override, fn)
   else:
       <existing raw-annotation block, unchanged>
   ```
   Everything after (`self.fn`, `is_async`, scope precedence, qualifier, priority, disposer) is
   untouched. Update the class docstring (lines 434-454) and `__init__` docstring: new `Args` entry
   for `returns`, the three-level precedence list, and the explicit statement that the return
   annotation is **not read** when an override is present (so "Provider with no return annotation →
   `TypeError`" gains the qualifier "…unless `returns=` is given").

   **Deviation from the literal pseudocode above:** `ProviderBindingNotDecoratedError` is now only
   raised when *both* `meta is None` and the call-site `returns` kwarg is `None`. When `meta is None`
   but a call-site `returns` override is given, `meta` falls back to `ProviderMetadata.default()`
   instead of raising. This was required by test coverage item 1 and the motivating-case test (item
   7 / `test_motivating_case_generic_alias_per_loop_iteration`), both of which call
   `container.provide(plain_undecorated_fn, returns=...)` with a factory that was never decorated
   with `@Provider` at all — matching the `container.provide(factory, returns=...)` example in this
   plan's Goal section. Without an override, undecorated is still an error (scope/qualifier/priority
   have nowhere else to come from).

8. [x] `providify/container.py:658-672` — `provide(self, fn: Callable[..., Any], *, returns: Any =
   None) -> None`, forwarding `ProviderBinding(fn, returns=returns)`. Docstring: new `Args` entry,
   the precedence list, a `Raises: TypeError` line, and the loop-built-generic-alias example that
   motivates the feature.

9. [x] `providify/binding.py` — verify (test, no code change) that the `@Configuration` /
   `scan()` discovery paths inherit the override for free: `container.py:3823` copies
   `effective_fn.__dict__` onto the property wrapper, `container.py:3826` passes a bound method whose
   attribute lookup proxies to `__func__`, and `scanner.py:233` passes the raw function — all three
   carry `ProviderMetadata.returns`. Covered by tests 13-14. If any fails, fix the discovery site,
   **not** `ProviderBinding`. Verified: no code change needed, both discovery-path tests pass.

10. [x] `docs/agents/usage-rules.md` — extend **R10** ("Decorate before you `register()` /
    `provide()`") with a short paragraph: when the interface is only known at call time, pass
    `returns=` instead of mutating `fn.__annotations__`; mutating another function's annotations is
    an anti-pattern. Add the mirroring entry to `docs/agents/anti-patterns.md` ("patching
    `__annotations__['return']` before `provide()`" → use `returns=`).

11. [x] `CHANGELOG.md` — `### Added` under Unreleased: `returns=` on `DIContainer.provide()` and
    `@Provider`, accepting a type, a parameterised generic alias, or a zero-arg callable evaluated at
    registration time; note that it fully bypasses return-annotation reading and that no existing
    behaviour changes when it is omitted.

12. [x] `README.md` — if (and only if) the README already documents `@Provider`'s kwargs, add
    `returns=` to that list with the one-line loop example. Do not add a new top-level section.

## Test coverage

All in `tests/test_explicit_returns.py` unless noted. Module-level fixtures: a `Repository`
`Generic[T]` protocol/base, `User`/`Order` models, and an `InMemoryRepo` implementation.

**Happy paths**
1. `container.provide(fn, returns=Foo)` where `fn` has **no** return annotation → `container.get(Foo)`
   returns the produced instance.
2. `@Provider(returns=Repository[User])` on a `-> Any` factory → `container.get(Repository[User])`
   resolves; `container.get(Repository[Order])` raises the normal not-found error.
3. `@Provider(returns=lambda: Repository[User])` — assert a module-level call counter is `0` after
   decoration and `1` after `container.provide(fn)` (proves deferral), then resolution works.
4. `returns=Annotated[Foo, "meta"]` → `binding.interface is Foo`.
5. Async factory + `returns=` → `binding.is_async is True`, `await container.aget(Iface)` works.
6. `returns=` combined with `qualifier=`, `priority=`, `singleton=`/`scope=` → all still honoured
   (assert `binding.scope`, `binding.qualifier`, and singleton identity across two `get()` calls).
7. **Motivating case:** a loop over `[User, Order]` building one factory + one
   `container.provide(f, returns=Repository[model])` per iteration → both resolve to distinct
   instances of the right alias.

**Precedence**
8. `provide(fn, returns=B)` on a factory decorated `@Provider(returns=A)` → interface is `B`.
9. `@Provider(returns=A)` on a factory annotated `-> C` → interface is `A`.
10. Factory annotated `-> "NotImportableAtRuntime"` (a `TYPE_CHECKING`-only / function-local name)
    **plus** `returns=Foo` → registers cleanly, no `TypeError`, no `AnnotationResolutionError`.
    This is the "don't force callers to repair an unrelated annotation" guarantee.
11. No `returns=` anywhere → existing behaviour byte-for-byte: same interface, and the unresolvable
    annotation still raises `TypeError` with the existing message (regression guard for Step 7's
    branch).

**Errors** — each asserts the provider's name appears in `str(excinfo.value)`
12. `returns="Repository[User]"` → `TypeError` mentioning the deferred-callable alternative.
13. `returns=42` and `returns=SomeInstance()` → `TypeError`.
14. `returns=lambda: 42` → `TypeError` naming the produced value.
15. `returns=lambda: (_ for _ in ()).throw(RuntimeError("boom"))` → `TypeError` with
    `excinfo.value.__cause__` being the `RuntimeError`.

**Discovery paths**
16. `tests/test_configuration.py` (append) — a `@Configuration` class with a
    `@Provider(returns=Repository[User])` method, and a second with `@Provider @property`
    + `returns=` → `container.install(Mod)` registers under the explicit interface.
17. `tests/test_scanner.py` (append) — a module-level `@Provider(returns=...)` picked up by `scan()`
    registers under the explicit interface.

**Metadata**
18. `tests/test_decorators.py` (append) — `ProviderMetadata(returns=X).merge(qualifier="q").returns
    is X`; stacked `@Provider(qualifier="q")` over `@Provider(returns=X)` (both orders) preserves
    `returns`; `repr()` includes `returns=`; `pickle.loads(pickle.dumps(meta))` round-trips a
    `returns` set to a module-level type; and `__setstate__({"qualifier": None, "priority": 0,
    "singleton": False, "is_async": False, "scope": None})` (a pre-1.2 state dict, no `"returns"`)
    yields `meta.returns is None` rather than `AttributeError`.

## Edge cases
- `returns=None` → treated as "not given"; falls back to `@Provider(returns=...)`, then to the
  annotation. `returns=type(None)` is the explicit way to bind `NoneType`.
- `returns=SomeClass` where `SomeClass` is callable → bound as the interface, **never invoked**.
- `returns=Repository` (bare generic class, unparameterised) → legal, `isinstance(..., type)` is
  True; binds under the unparameterised interface exactly as `-> Repository` would.
- Deferred callable returning `Annotated[X, ...]` → unwrapped to `X` (the table is applied to the
  callable's result).
- Deferred callable returning another callable → `TypeError` (no recursion; one level only).
- `functools.partial` as `returns` → not a type, `get_origin` is `None`, callable → invoked. Works.
- Factory with an unresolvable **parameter** annotation + `returns=` → unchanged from today:
  registration succeeds, the parameter surfaces at resolution time (per Plan 001, Phase 7).
- `@Provider(returns=X)` applied twice with different values → last decorator applied (innermost-
  outward) wins, consistent with `qualifier`.
- Pickled `ProviderMetadata` from ≤1.1.0 → `returns` defaults to `None`.

## Verification
```bash
cd /home/edoardo/projects/providify
uv run pytest -q                                    # full suite must stay green (552+ tests)
uv run pytest tests/test_explicit_returns.py -v     # new suite
uv run pytest tests/test_forward_ref_provider.py tests/test_annotation_resolution.py \
              tests/test_per_param_annotations.py tests/test_configuration.py \
              tests/test_scanner.py tests/test_decorators.py -q   # blast radius
uv run ruff check .
uv run ruff format --check .
```
Green means: zero pre-existing tests changed (only appended to), and the no-`returns=` path produces
identical interfaces and identical error strings.

## Risks
- ⚠️ **Design call without a stronger signal than "seems reasonable": accepting both a direct value
  and a zero-arg callable in the same parameter.** The disambiguation rule (type/generic-alias wins
  over callability) is unambiguous for every realistic input, but it is a rule a reader must learn.
  A reviewer should sanity-check the alternative of two kwargs (`returns=` / `returns_factory=`) —
  rejected here as more API surface for a P2 hygiene feature, but it is a defensible call the other
  way. If reversed, only Step 6's helper and the two signatures change; tests 3 and 14-15 move.
- ⚠️ **Design call: `None` as the "absent" sentinel** rather than a private `_UNSET`. Safe only
  while "a provider producing `None`" stays meaningless. If providify ever supports null-object
  bindings, this must become a real sentinel.
- **Invariant that must hold:** `ProviderBinding.interface` is always a type or a parameterised
  generic alias — never a `str`, never an arbitrary object. Every container path
  (`_build_localns`'s `interface.__name__`, `_interface_matches`, `_is_generic_subtype`, cycle
  detection, `describe()`) depends on it; violating it reproduces the whole-container poisoning
  documented in `tests/test_forward_ref_provider.py`. Step 6 must apply the *same* final predicate
  as `binding.py:419`.
- **Regression surface:** `ProviderMetadata.__slots__` is read by `__getstate__`; adding a slot
  changes pickle payloads. Step 4's defensive `__setstate__` is what keeps old payloads loadable —
  do not skip it.
- **Silent-skip risk:** because an override suppresses annotation reading entirely, a typo'd
  `returns=` value binds under the wrong interface with no warning, surfacing later as a
  not-found error at `get()`. Accepted: identical to the failure mode of a wrong return annotation
  today, and the alternative (cross-checking `returns=` against the annotation) would defeat the
  feature's purpose for `-> Any` factories.
