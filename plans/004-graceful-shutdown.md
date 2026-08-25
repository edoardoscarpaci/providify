# Plan 004 — Graceful shutdown: reverse-dependency-order teardown (F9)

## Goal
`container.shutdown()` / `await container.ashutdown()` tear down cached singletons in
**reverse dependency order** (every dependent is destroyed before any of its
dependencies), run **every** teardown hook even when one raises, always clear caches,
and report all failures in one aggregated `ShutdownError`. Request/session scope exit
gains the same reverse-order guarantee at no extra state cost.

## Non-goals
- **Cross-scope ordering** (singleton vs REQUEST/SESSION teardown relative to each
  other). Scope frames are nested context managers; F9 does not re-order across them.
  A singleton that captured a request-scoped instance is already an error caught by
  the `Live[T]` scope-leak validator (`validate_bindings()`, container.py:3543-3610).
- **Shutdown timeouts / signal handling** (SIGTERM wiring, per-phase timeouts as in
  Spring `spring.lifecycle.timeout-per-shutdown-phase` — research 003 §3). Separate
  backlog item; F9 delivers ordering + failure aggregation only.
- **Readiness/liveness probe integration** (research 003 §1) — not in F9.
- **Module-level ordering** (F5, `@Configuration` module DAG) — separate item.
- No new `@PreDestroy`-adjacent decorators; no `@PreConstruct`.
- Teardown of DEPENDENT-scope instances that the container never cached (unchanged:
  only `@Track`ed dependents flushed by `flush_dependents()`).

## Design

### The externally-grounded semantics
Research brief `design/di-features-taxonomy/research/003-production-readiness-conventions.md`
§3 establishes the convention this plan implements:

> "**Reverse-dependency-order teardown**: Services are destroyed in reverse order of
> their initialization; dependers shut down before their dependencies." … "Ensures no
> service attempts to use a dependency after it has been disposed; prevents data loss
> and connection errors during shutdown" — brief 003 §3, *Fundamental Principle*.

Spring ("DI container manages singleton disposal order; developers do not manually
order teardown"), .NET Generic Host ("Singleton services disposed when
`IServiceProvider` is disposed", `IDisposable`/`IAsyncDisposable` contract) and
Quarkus/CDI all provide this automatically; the brief records it as **absent** from
`python-dependency-injector` and `dishka` ("no automatic reverse-order teardown" —
brief 003 §3, *Python Ecosystem*). Brief 003 §Options Compared marks Graceful Shutdown
as ⛔ for the Python DI ecosystem and ✅ for all three JVM/.NET baselines.

### Chosen mechanism: reverse **creation** order (LIFO)
providify already guarantees the invariant that makes creation order a valid
topological order of the singleton dependency DAG:

`_instantiate_sync` (container.py:1603-1621) resolves the binding's dependencies
*inside* `binding.create(self)` (line 1611) and only then writes
`cache[key] = instance` (line 1615). Every dependency is therefore already in
`_singleton_cache` before its dependent lands there. `_instantiate_async` mirrors this
exactly (lines 1698-1702). `warm_up()` (line 782-783) drives the same path, so eager
startup produces the same ordering.

```
creation order :  Config → Db → Repo → Service        (deps first)
teardown order :  Service → Repo → Db → Config        (reversed = dependents first)
```

So: record each singleton cache key at the moment it is cached, tear down by walking
that record backwards. No graph traversal, no cycle handling, no re-derivation.

```
DIContainer
  _singleton_order: list[tuple[key, AnyBinding]]   # append-on-cache, LIFO on teardown
  _singleton_cache: dict[key, object]              # unchanged
```

### Teardown pipeline

```
shutdown()                      ashutdown()
    │                                │
    └──────┬─────────────────────────┘
           ▼
    _teardown_plan()  ──►  [(key, binding), …]  reverse-creation order,
           │                deduped, evicted keys dropped,
           │                + fallback tail for cached-but-unrecorded keys
           ▼
    for each: run disposer (ProviderBinding) or @PreDestroy (ClassBinding)
           │   exception → captured as ShutdownFailure, loop CONTINUES
           ▼
    finally: _clear_caches() + _singleton_order.clear()
           ▼
    failures? → raise ShutdownError(failures)
```

### Failure aggregation (behaviour change, deliberate)
Today `shutdown()` (container.py:3031-3057) raises on the first failing hook and
**never reaches `_clear_caches()`** (line 3057, no `try/finally`) — one bad hook leaks
every remaining teardown and every cached instance. That is the same risk class F9
exists to close, so it is fixed here: all hooks run, caches always clear, and failures
aggregate into `ShutdownError` following the established `ContainerValidationError`
`.report` aggregation pattern (exceptions.py:204-249).

`ShutdownError.__cause__` is chained to the first captured exception
(`raise ShutdownError(failures) from failures[0].exception`) so existing tracebacks
still surface the root error.

### Alternatives considered
- **Static topological sort over `_get_dependencies()`** (container.py:4076-4165),
  teardown = reverse topo order.
  ✅ Deterministic even for never-instantiated bindings; ✅ no runtime bookkeeping.
  ❌ `_get_dependencies()` is *deliberately lossy* — it swallows unresolvable
  dependencies and downgrades `AnnotationResolutionError` to a logged warning
  (container.py:4133-4141) because its real caller is `describe()`, a reporting tier.
  A teardown order built on a knowingly-partial graph can silently invert an edge.
  ❌ Needs its own cycle policy (plan 003's DFS is **not implemented in code**, only
  in `plans/003-startup-graph-validation.md:114-128` — F9 must not depend on it).
  ❌ Blind to edges that only exist at runtime (`Lazy[T]`, `Live[T]`, `Provider[T]`).
  ❌ Bindings with no instance have nothing to tear down, so its one advantage is moot.
  **Rejected.**
- **`ExceptionGroup` for aggregated failures** (available on the ≥3.12 floor).
  ✅ Native `except*` ergonomics. ❌ Diverges from the codebase's own aggregation
  precedent (`ContainerValidationError.report`); ❌ multiple-inheritance with
  `providifyError` requires `BaseExceptionGroup.__new__` gymnastics and breaks a plain
  `except providifyError`. **Rejected** — `ShutdownError.failures` instead.
- **Raise-on-first, preserved as-is.** ✅ Zero behaviour change. ❌ Leaves the cache
  leak and skips remaining teardown — the exact production failure mode F9 targets.
  **Rejected.**
- **Sorting the singleton cache dict directly** (it is insertion-ordered) instead of a
  separate `_singleton_order` list. ✅ No new state. ❌ `_singleton_cache` holds only
  `key → instance`; teardown needs the *binding* (`pre_destroy`, `disposer`), and
  reverse-scanning `_bindings` per key is O(n²) and registration-ordered, not
  creation-ordered. **Rejected for singletons**, but **adopted for REQUEST/SESSION**
  (step 11) where the per-scope caches are short-lived and a single key→binding index
  makes the lookup O(1).

## Steps

1. [ ] `tests/test_shutdown_order.py` — new file, failing tests for sync ordering:
   - `Config`, `Db(Config)`, `Repo(Db)`, `Service(Repo)` all `@Singleton` with
     `@PreDestroy` appending their name to a shared list; `container.get(Service)`
     then `container.shutdown()` → order `["Service", "Repo", "Db", "Config"]`.
   - Same graph resolved via `container.warm_up()` → same teardown order.
   - Diamond: `A(B, C)`, `B(D)`, `C(D)` → `A` first, `D` last (assert relative
     positions, not a single exact permutation, since B/C order is creation-dependent).
   - Registration order deliberately *reversed* vs dependency order → teardown order
     still follows dependencies, proving `_bindings` order is no longer used.
   - A `@Singleton` that was never resolved contributes no `@PreDestroy` call.
   - `@Provider`-produced singleton with a `@Disposes` disposer participates in the
     same ordering (mixed `ClassBinding`/`ProviderBinding` graph).
2. [ ] `tests/test_shutdown_order.py` — failing tests for async ordering: the same
   4-node chain with `async def` `@PreDestroy` hooks resolved via `aget()` and torn
   down by `await container.ashutdown()`; a mixed sync+async chain; an async
   `@Disposes` disposer (must be awaited — mirrors container.py:3074-3077).
3. [ ] `tests/test_shutdown_order.py` — failing tests for failure aggregation:
   - Two `@PreDestroy` hooks raise → `ShutdownError` raised, `exc.failures` has 2
     entries with `.owner` / `.exception`, and the *non-failing* hooks still ran.
   - `_singleton_cache` is empty after a `ShutdownError` (cache cleared in `finally`).
   - `exc.__cause__ is` the first raised exception.
   - Async variant via `ashutdown()`.
   - `with DIContainer() as c:` where a hook raises → `ShutdownError` propagates out
     of `__exit__` and caches are still cleared.
4. [ ] `tests/test_shutdown_order.py` — failing tests for idempotency/eviction:
   - `shutdown()` twice → second call is a no-op, no hook runs twice.
   - `container.override(I, Other)` after resolving `I` → the evicted instance is not
     torn down by a later `shutdown()` (it was already dropped from the cache), and no
     `KeyError`/stale-binding error is raised.
   - `container.reset_binding(I)` → same.
   - `container.copy()` → the copy has an empty teardown order; shutting down the copy
     runs no hooks and does not touch the original's instances.
5. [x] `providify/exceptions.py` — add, after `ContainerValidationError` (line 249):
   - `@dataclass(frozen=True) class ShutdownFailure` with fields
     `owner: str` (e.g. `"Db.close"` or `"@Disposes(close_pool)"`),
     `exception: BaseException`.
   - `class ShutdownError(providifyError)` with `__init__(self, failures: list[ShutdownFailure])`
     storing `self.failures` and building a message of one line per failure
     (`f"  - {f.owner}: {type(f.exception).__name__}: {f.exception}"`), header
     `f"Shutdown completed with {len(failures)} teardown failure(s); all caches were cleared:"`.
     Full docstring in project style (purpose, Attributes, Example) mirroring
     `ContainerValidationError`'s.
6. [x] `providify/__init__.py` — export `ShutdownError` and `ShutdownFailure`: add to
   `__all__` (near line 44) and to the `from .exceptions import (...)` block (line 120-128).
7. [x] `providify/container.py:380` (`__init__`) — add
   `self._singleton_order: list[tuple[Any, AnyBinding]] = []` immediately after
   `self._singleton_cache`, with a DESIGN comment stating the invariant:
   *"append-only creation log for singletons; reversed at shutdown to obtain
   reverse-dependency order — valid because `_instantiate_*` caches a dependency
   before its dependent (see line 1611-1615)."*
8. [x] `providify/container.py:1615` and `:1702` — record the creation. In both
   singleton branches, immediately after `cache[key] = instance`, call
   `self._record_singleton_creation(key, binding)`. Add that private helper next to
   `_get_cache_key` (line 1466): appends `(key, binding)` under
   `self._singleton_lock_guard` (the existing cheap, no-I/O guard used at lines
   1598/1687) so the log is consistent under multi-threaded creation.
   Docstring must state: called exactly once per key because both call sites are
   inside the per-key double-check lock.
9. [x] `providify/container.py` — add `_teardown_plan(self) -> list[tuple[Any, AnyBinding]]`
   in the "Shutdown" section (before `shutdown()`, line 3022):
   - walk `reversed(self._singleton_order)`; skip keys already emitted (dedupe) and
     keys no longer in `_singleton_cache` (evicted by `override()`/`reset_binding()`);
   - then append a **fallback tail**: any key present in `_singleton_cache` but absent
     from the order log, paired with its binding via a `{_get_cache_key(b): b}` index
     over `self._bindings`, in reverse `_bindings` order. Defensive: today nothing
     seeds `_singleton_cache` outside `_instantiate_*` (`set_scoped`, container.py:2957-3018,
     only writes request/session caches), but a future seeding API must not silently
     skip teardown.
   - Docstring: Args/Returns/Edge cases/Thread safety, per project style.
10. [x] `providify/container.py:3022-3057` — rewrite `shutdown()`:
    - iterate `self._teardown_plan()`;
    - per entry, dispatch to a new shared helper `_dispose_sync(key, binding) -> None`
      that raises `RuntimeError` for an async `@PreDestroy` (preserve the exact message
      at lines 3050-3054) and calls the disposer / hook otherwise;
    - wrap each entry in `try/except BaseException` → append `ShutdownFailure`;
      **the async-`@PreDestroy` `RuntimeError` must NOT be swallowed into `failures`** —
      it is a programmer error, not a teardown failure: re-raise it immediately
      (after the `finally` clears caches) so `async with` guidance stays loud.
      Implementation: catch `Exception` for aggregation, let `RuntimeError` from the
      async-hook guard escape via a dedicated sentinel check before the generic handler.
    - `finally:` `self._clear_caches()`;
    - after the loop, `if failures: raise ShutdownError(failures) from failures[0].exception`.
    - Update the docstring: Raises `ShutdownError`, `RuntimeError`; add an
      "Ordering" paragraph citing reverse-dependency-order semantics and the research
      brief path.
11. [x] `providify/container.py:3059-3095` — rewrite `ashutdown()` as the async mirror:
    same plan, `_adispose(key, binding)` awaits async disposers
    (`inspect.iscoroutinefunction`, preserving line 3074-3077 behaviour) and async
    `@PreDestroy` hooks (`binding.pre_destroy.is_async`), same aggregation and
    `finally: self._clear_caches()`.
12. [x] `providify/container.py:3097-3100` (`_clear_caches`) — also
    `self._singleton_order.clear()` so a second `shutdown()` is a clean no-op.
13. [x] `providify/container.py:4565-4569` (`override`) and `:4637-4641`
    (`reset_binding`) — after `self._singleton_cache.pop(key, None)`, drop the same
    keys from `_singleton_order`
    (`self._singleton_order = [e for e in self._singleton_order if e[0] not in evicted]`
    where `evicted = set(to_evict)`), so an evicted instance is never torn down.
14. [x] `providify/container.py:4705` (`copy()`) — add `new._singleton_order = []`
    beside `new._singleton_cache = {}`, and extend the existing "must be kept in sync"
    DESIGN comment (lines 4698-4700).
15. [ ] `tests/test_lifecycle.py` — update the two existing assertions that depend on
    old behaviour: (a) the raise-on-first shutdown test — now expects `ShutdownError`
    with cleared caches; (b) any test asserting registration-order teardown. Do NOT
    weaken tests that assert *which* hooks ran.
16. [ ] `tests/test_scope.py` (or wherever scope-exit `@PreDestroy` is covered) —
    failing test: two request-scoped components where one depends on the other; on
    `with container.request():` exit the dependent's `@PreDestroy` runs first. Async
    variant via `async with container.arequest():`.
17. [x] `providify/container.py:3139-3161` (`_run_pre_destroy_for_scope`) and
    `:3190-3203` (`_arun_pre_destroy_for_scope`) — invert the loop: build a
    `{binding.implementation: binding}` index once (first binding wins; aliases share
    the same `pre_destroy`), then iterate `reversed(list(cache.items()))`. Scope caches
    are insertion-ordered dicts populated by the same deps-before-dependent rule
    (container.py:1628-1629), so reversing them yields reverse-dependency order with
    **zero new state**. Keep the async-hook `warnings.warn`-and-skip behaviour
    (lines 3148-3160) exactly as is.
18. [ ] `docs/agents/usage-rules.md:171-179` — document the ordering guarantee
    ("singletons are torn down in reverse creation order = dependents before
    dependencies") and `ShutdownError` (all hooks run; caches always cleared).
19. [ ] `README.md` — one bullet in the lifecycle/shutdown section: reverse-dependency-order
    teardown, matching Spring/.NET/Quarkus behaviour.
20. [ ] `CHANGELOG.md` — new entry under the unreleased heading: feature (ordered
    teardown) **and** the breaking-ish behaviour change (aggregated `ShutdownError`
    replaces first-exception propagation; caches now always cleared).

## Edge cases
- Empty container / nothing ever resolved → `shutdown()` is a no-op, no `ShutdownError`.
- `shutdown()` called twice → second call sees an empty plan; no hook runs twice.
- Singleton registered but never resolved → absent from `_singleton_order`, absent
  from `_singleton_cache` → no teardown, as today.
- Two bindings sharing one cache key (same impl bound to two interfaces) → recorded
  once (the second `_instantiate_*` short-circuits on the cache fast path, line 1580) →
  torn down once.
- `@PreDestroy` hook raises → captured, remaining hooks still run, caches cleared,
  `ShutdownError` raised at the end.
- Async `@PreDestroy` reached from sync `shutdown()` → `RuntimeError` with the existing
  "use `await container.ashutdown()`" message, **not** aggregated; caches still cleared.
- Async `@Disposes` disposer under `ashutdown()` → awaited.
- Sync disposer under `ashutdown()` → called inline (no await).
- Instance evicted by `override()` / `reset_binding()` before shutdown → not torn down
  (the container no longer owns it).
- `copy()`-ed container → independent, empty teardown order.
- Cached singleton whose binding was removed from `_bindings` without cache eviction →
  falls into `_teardown_plan`'s recorded entry (the binding object is held by the log),
  so its hook still runs.
- Cycle at singleton scope (A↔B via `Lazy`/`Live`) → no infinite loop: the plan is a
  flat deduped list; teardown follows actual creation order.
- Request/session cache empty on scope exit → no-op (unchanged).

## Verification
```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_shutdown_order.py -v
uv run pytest tests/test_lifecycle.py tests/test_scope.py -v
uv run pytest                      # full suite — no regressions
uv run ruff check providify tests
uv run ruff format --check providify tests
```
Manual acceptance: the chain test in step 1 must print exactly
`["Service", "Repo", "Db", "Config"]`.

## Risks
- ⚠️ **ASSUMPTION — teardown scope is "instantiated singletons only".** The plan tears
  down what is actually in `_singleton_cache` (which is what today's `shutdown()`
  effectively does — lines 3035/3045 skip anything not cached). Never-instantiated
  bindings are deliberately skipped. Invariant: *only instances the container created
  and still owns are destroyed.*
- ⚠️ **ASSUMPTION — cross-scope ordering is out of scope for F9.** Declared a non-goal
  above; singleton↔request ordering is left to the `Live[T]` scope-leak validator.
  If a consumer hits a real cross-scope teardown bug, it becomes a follow-up item.
- ⚠️ **ASSUMPTION — aggregating failures is the right call** (the scout listed this as
  open). Chosen over raise-on-first because today's raise-on-first also skips
  `_clear_caches()`. **Breaking change**: callers doing
  `try: container.shutdown() except MyDbError:` now catch `ShutdownError` (with
  `__cause__` chained). Must be called out in `CHANGELOG.md` (step 20).
- ⚠️ **ASSUMPTION — `_singleton_cache` is only ever written by `_instantiate_sync`/
  `_instantiate_async`.** Verified by grep (writes at container.py:1615, 1702; `copy()`
  resets at 4705; `set_scoped` at 2957-3018 touches only request/session caches). The
  fallback tail in `_teardown_plan` (step 9) exists precisely so this assumption's
  future violation degrades to "torn down last" rather than "never torn down".
- **Late `Lazy[T]`/`Provider[T]` resolution can invert an edge.** If `B` is created
  first and only later pulls `A` through a `Lazy[A]`, creation order is `B, A` and
  teardown becomes `A, B` — `B`'s hook could touch an already-disposed `A`. This is
  inherent to reverse-creation-order (the same limitation .NET has) and must be
  documented in the `shutdown()` docstring (step 10) and usage-rules (step 18).
  Invariant that still holds: *no instance is disposed before something created after it.*
- **`__exit__`/`__aexit__` need no change** (container.py:562-580, 590-603) — they
  delegate to `shutdown()`/`ashutdown()` and inherit the new ordering automatically.
  Risk: they now propagate `ShutdownError` out of the `with` block; step 3's context-manager
  test locks that in.
- **Thread safety.** `_singleton_order.append` runs under `_singleton_lock_guard`
  (step 8) — an uncontended lock already taken on the same code path (lines 1598, 1687),
  so the added cost is negligible, and the log stays consistent without relying on
  GIL atomicity (the container documents GIL reliance at line 278, but this is cheap
  enough to not need it).
- **`_teardown_plan` cost** is O(n + m) with one dict index build; `n` = created
  singletons, `m` = bindings. Runs once per shutdown — not on any hot path.
