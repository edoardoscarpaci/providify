# Plan 009 — Container observability hooks: creation / disposal / scope events (F6)

Implements backlog item **F6** (🟢 nice, complexity S–M — `BACKLOG.md:41`): *"no existing Python DI
container exposes instrumentation hooks for dependency resolution or scope transitions — a genuine
differentiator, not just parity-catchup."*

External grounding: `design/observability-hooks/research/001-otel-hook-patterns.md`
(hereafter **research 001**). Ecosystem gap confirmed across python-dependency-injector, dishka,
svcs, wireup, that-depends, Lagom and Rodi — research 002 §OpenTelemetry Instrumentation, cited via
`BACKLOG.md:41`.

## Goal

A consumer can observe what the container does, with timing, and bridge it to OpenTelemetry in ~10
lines of their own code — and pays **nothing** when they don't:

```python
from opentelemetry import trace
from providify import InstanceCreated, ScopeEntered

tracer = trace.get_tracer("providify")

def on_created(e: InstanceCreated) -> None:
    span = tracer.start_span("di.create", attributes={
        "di.interface": getattr(e.interface, "__name__", str(e.interface)),
        "di.scope": str(e.scope),
        "di.async": e.is_async,
    })
    span.end(end_time=None)          # duration also available as e.duration_ns

container.add_hook(InstanceCreated, on_created)
container.add_hook(ScopeEntered, lambda e: ...)
```

providify takes **no dependency on opentelemetry** — not even `opentelemetry-api`.

## Non-goals

- **No dependency on `opentelemetry-api` or the SDK, and no bundled OTel bridge.** Research 001
  §Librarian's Note: *"Providify should NOT take a hard dependency on opentelemetry-api; instead,
  provide a consumer guide for wiring hooks into OTel spans using the public OTel API."* A separate
  `opentelemetry-instrumentation-providify` package (research 001 §1) is explicitly deferred until
  there is user demand.
- **No cache-hit events.** Only *creation* is instrumented. The cache fast path
  (`container.py:1914-1916`) is the hottest line in the library and stays untouched.
- **No spans, no tracer, no context propagation.** providify emits plain event objects; span
  creation, sampling and `contextvars` propagation are the consumer's job (research 001 §5 — no
  async-hook consensus exists to copy).
- **No async hook callbacks.** Callbacks are sync-only, called inline. Research 001 §3/§5: OTel,
  SQLAlchemy and Django are all sync. See §Risks — this is the one genuinely open question.
- **No reuse of `DIMetadata.track`.** **Verified**: `track` is load-bearing today — it gates
  DEPENDENT-scope instance tracking for `flush_dependents()` (`container.py:1969-1973`, `:2056-2059`,
  `:1547-1598`) and is a documented public decorator parameter (`decorator/scope.py:151-200`).
  It is **not** repurposed.
- **No collision with the `Event[T]` / `@Observes` application-event system**
  (`container.py:1464-1543`). That dispatches *domain* events to *resolved beans*; this dispatches
  *container telemetry* to *plain callables*. Different names, different state, no shared code.
- **No metrics/counters API** (`container.stats()`), no built-in logging hook.
- **No version bump** — `CHANGELOG.md` entry only.

## Design

### Payload shape: frozen dataclass events, one type per event

Research 001 §4 compares positional args (OTel HTTP instrumentation's `request_hook(span, request)`)
against frozen dataclass event objects, and §Librarian's Note lands on the hybrid:
*"expose synchronous hooks as frozen dataclass event objects (single parameter, extensible,
async-safe by context, aligns with providify's existing ValidationIssue/ShutdownFailure pattern)."*
`@dataclass(frozen=True, slots=True, kw_only=True)` is named as the emerging best practice
(research 001 §4).

`providify/observability.py` — pure module, stdlib + `providify.metadata` (for `Scope`) only,
**must not import `container.py`** (the `validation.py`/`config.py`/`modules.py` isolation pattern):

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceCreated:
    interface: Any                 # the bound interface (type or generic alias)
    implementation: type | Callable[..., Any] | None
    scope: Scope
    qualifier: str | type | None
    duration_ns: int               # binding.create()/acreate() + hooks + interceptors
    is_async: bool                 # resolved via aget() rather than get()

@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceDisposed:
    interface: Any
    implementation: type | Callable[..., Any] | None
    scope: Scope
    owner: str                     # same label as ShutdownFailure.owner (container.py:3483-3497)
    duration_ns: int
    error: BaseException | None    # the teardown hook's exception, if it raised

@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeEntered:
    kind: Literal["request", "session"]
    scope_id: str

@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeExited:
    kind: Literal["request", "session"]
    scope_id: str
    duration_ns: int

ContainerEvent: TypeAlias = InstanceCreated | InstanceDisposed | ScopeEntered | ScopeExited
```

Types are carried as **objects**, not names: a consumer filtering on `e.interface is DatabasePool`
is the common case, and `getattr(x, "__name__", str(x))` covers the label case. Holding a strong
reference to a class is free (classes are module-lifetime).

### Registration: one callback per event type

```python
container.add_hook(InstanceCreated, cb) -> Callable[[], None]   # returns an unsubscribe closure
container.remove_hook(InstanceCreated, cb) -> bool
```

State: `self._hooks: dict[type, list[Callable[[Any], None]]] = {}`.

A `ContainerHook` Protocol with four optional methods was rejected (§Alternatives) — optional
Protocol members are awkward to type and force a no-op base class. Keying on the event class gives
exact-type dispatch (`self._hooks.get(type(event))`), needs no base class, and makes the
zero-hook fast path a single truthiness test.

### The zero-cost rule

Every instrumentation point is guarded by `if self._hooks:` **before** any work — including before
`perf_counter_ns()`:

```python
started = perf_counter_ns() if self._hooks else 0
instance = binding.create(self)
...
cache[key] = instance
self._record_singleton_creation(key, binding)
# ← per-key lock released here
if self._hooks:
    self._emit(InstanceCreated(..., duration_ns=perf_counter_ns() - started, ...))
return instance
```

Two properties this shape buys:

1. **No timer when nobody listens.** `perf_counter_ns` is imported by name
   (`from time import perf_counter_ns`) so a test can `mock.patch("providify.container.perf_counter_ns")`
   and assert it was never called — a deterministic zero-overhead test, not a flaky benchmark.
2. **Emission happens outside the per-key singleton lock** (`container.py:1938-1959`). A hook is
   arbitrary user code; running it while holding the creation lock invites deadlock if it resolves
   another singleton. Documented invariant: *hooks never run inside a container lock.*

`_emit` swallows and logs hook exceptions:

```python
def _emit(self, event: object) -> None:
    for cb in self._hooks.get(type(event), ()):
        try:
            cb(event)
        except Exception:
            logger.warning("[DIContainer] telemetry hook %r raised for %r", cb, event, exc_info=True)
```

**Decision**: a broken telemetry hook must never break resolution or shutdown. SQLAlchemy and
Django both propagate listener exceptions (research 001 §3), but they dispatch domain events, not
telemetry; providify's own precedent for "diagnostic path must not break the app" is the scanner's
import-failure warning (`scanner.py:174-175`) and the scope-exit async-hook warning
(`container.py:3683-3752`). Logged at WARNING through the existing module logger
(`container.py:108`).

### Instrumentation points (all verified)

| event | site | note |
|---|---|---|
| `InstanceCreated` (singleton) | `container.py:1938-1960` — after the `with per_key_lock:` block | emitted once per key; cache hits emit nothing |
| `InstanceCreated` (request/session/dependent) | `container.py:1962-1974` | `is_async=False` |
| `InstanceCreated` (async, both branches) | `_instantiate_async`, `container.py:1976-2060` | `is_async=True` |
| `InstanceDisposed` | `shutdown()` loop `container.py:3540-3551`, `ashutdown()` loop `:3626-3632` | wraps `_dispose_sync`/`_adispose`; `error` set from the aggregated exception |
| `InstanceDisposed` (scoped) | `_run_pre_destroy_for_scope` `:3683`, `_arun_pre_destroy_for_scope` `:3754` | `scope` = REQUEST/SESSION |
| `ScopeEntered` / `ScopeExited` | the container façades `request()` `:3186`, `arequest()` `:3202`, `session()` `:3216`, `asession()` `:3236` | each currently just returns `self.scope_context.<x>()`; when `self._hooks` is empty they keep returning it **unchanged** |

Scope events are emitted from the **container façade**, not from `scope.py`. `ScopeContext`'s
stated design goal is to stay generic — *"Container-side logic stays in the container; ScopeContext
stays generic"* (`scope.py:43-46`) — and it already carries four lifecycle callbacks; adding two
more would grow that constructor to six. Documented consequence: calling
`container.scope_context.request()` directly is **uninstrumented**.

### Alternatives considered

- **Hard (or optional) dependency on `opentelemetry-api`, emitting spans directly.** ✅ Research
  001 §1: the API alone is *"abstractions and non-operational implementations"* with zero cost when
  no SDK is installed; ✅ consumers get spans for free. ❌ providify declares **zero** runtime
  dependencies today (`pyproject.toml:1-47` — no `dependencies` key, only an optional `yaml`
  extra), and research 001 §2 records that **no OTel semantic conventions exist for DI or
  lifecycle spans** ([semantic-conventions#2133] is still open) — so providify would be inventing
  span names it would later have to change. ❌ It also forces one tracing vendor's model on
  consumers who use structlog/statsd/Prometheus. **Rejected**; the hook API is strictly more
  general and the OTel bridge is a documented 10-line recipe (step 14).
- **Positional-arg callbacks** (`on_created(interface, implementation, duration_ns)`), the OTel HTTP
  instrumentation shape (research 001 §1, §4). ✅ Simplest; lowest cognitive load. ❌ Research 001
  §4: *"more brittle with additions"* — every new field is a breaking signature change, and this
  codebase already standardised on frozen dataclasses for structured payloads (`ValidationIssue`,
  `ShutdownFailure`, `ConfigIssue`). **Rejected.**
- **A `ContainerHook` Protocol / base class with four optional methods** (SQLAlchemy's
  `listens_for` target model). ✅ One registration call covers all events. ❌ Optional Protocol
  members type poorly and push users toward a no-op base class — an abstraction CLAUDE.md's
  minimal-abstraction rule does not pay for. **Rejected.**
- **Reusing the existing `Event[T]` / `@Observes` dispatcher** (`container.py:1464-1543`). ✅ Zero
  new state; ✅ users already know it. ❌ It resolves *observer beans* through the container — so a
  creation event would re-enter resolution from inside `_instantiate_sync`, which is a deadlock and
  infinite-recursion generator; ❌ it conflates application events with container telemetry.
  **Rejected outright.**
- **Emitting the event inside the per-key lock**, immediately after `cache[key] = instance`.
  ✅ Trivially correct ordering. ❌ Runs arbitrary user code under a container lock. **Rejected.**
- **Instrumenting cache hits too** (a `InstanceResolved` event on the fast path). ✅ Complete
  picture, enables hit-rate metrics. ❌ `container.py:1914-1916` is the hottest path in the
  library; even a guarded branch there is a cost paid by every resolution forever. **Rejected** —
  revisit only with a measured need.
- **Adding two more callbacks to `ScopeContext`** instead of wrapping at the façade. ❌ Six
  constructor callbacks, and it contradicts scope.py's own stated design (`scope.py:43-46`).
  **Rejected.**
- **Propagating hook exceptions** (Django/SQLAlchemy behaviour, research 001 §3). ❌ A telemetry
  bug would abort resolution or, worse, abort `shutdown()` mid-teardown. **Rejected** —
  swallow + WARNING.

## Steps

1. [x] `tests/test_observability.py` — new file, failing tests for the event dataclasses in
   isolation (no container): each of the four is frozen (assignment raises `FrozenInstanceError`),
   `kw_only` (positional construction raises `TypeError`), and hashable/equatable by value where
   all fields are hashable. `ContainerEvent` covers exactly the four types.
2. [x] `providify/observability.py` — new pure module with the four
   `@dataclass(frozen=True, slots=True, kw_only=True)` events and the `ContainerEvent` alias, per
   §Design. Imports stdlib + `providify.metadata` (for `Scope`) only; module-level DESIGN comment
   must (a) state the no-`container.py` isolation rule, (b) cite research 001 §4/§Librarian's Note
   for the frozen-dataclass choice, (c) state loudly that these are **container telemetry**, not
   `Event[T]`/`@Observes` application events. Full docstrings per CLAUDE.md.
3. [x] `tests/test_observability.py` — failing tests for registration:
   `add_hook(InstanceCreated, cb)` returns a callable that unsubscribes; `remove_hook` returns
   `True`/`False`; registering the same callback twice fires it twice; hooks for an event type
   nobody emits are never called; a hook that **raises** does not break `get()` and is logged
   (assert with `caplog` at WARNING); a hook registered for a *supertype* of the event class is
   **not** called (exact-type dispatch — documented).
4. [x] `providify/container.py` — add `self._hooks: dict[type, list[Callable[[Any], None]]] = {}`
   in `__init__` after `_tracked_dependents` (`:513-514`) with a DESIGN comment covering the
   zero-cost rule and the "not the `@Observes` system" distinction; add
   `from time import perf_counter_ns` to the imports (`container.py:1-30`) — **imported by name**
   so step 6's zero-overhead test can patch it.
5. [x] `providify/container.py` — add `add_hook()`, `remove_hook()` and private `_emit()` in a new
   `# ── Observability hooks (F6) ──` section placed after `get_all_bindings()` (`:5108`) and
   before `# ── Override ──` (`:5110`). Docstrings must document: sync-only callbacks, exceptions
   swallowed and logged, hooks never run under a container lock, hooks must not call back into
   `container.get()` (documented as unsupported — re-entrancy is the caller's problem).
6. [x] `tests/test_observability.py` — failing **zero-overhead** test:
   `mock.patch("providify.container.perf_counter_ns")` with no hooks registered → resolve 50
   singletons and assert the patched timer was **never** called; then register one hook and assert
   it *is* called. This is the executable form of the zero-cost rule.
7. [x] `tests/test_observability.py` — failing tests for `InstanceCreated`:
   - one event per singleton creation; a second `get()` (cache hit) emits **nothing**.
   - REQUEST-scoped: one event per scope frame, not per `get()` within it.
   - DEPENDENT: one event per `get()`.
   - `aget()` → `is_async is True`; `get()` → `False`.
   - `duration_ns > 0` and a provider that `sleep(0.01)`s reports ≥ 10 ms.
   - `implementation` is the class for a `ClassBinding` and the factory callable for a
     `ProviderBinding`; `qualifier` and `scope` match the binding.
   - a hook that resolves the *same* singleton re-entrantly returns the cached instance and does
     not deadlock (emission is outside the lock).
8. [x] `providify/container.py:1938-1974` (`_instantiate_sync`) — add the guarded timer and the two
   emission sites per §Design, the singleton one placed **after** the `with per_key_lock:` block.
9. [x] `providify/container.py:1976-2060` (`_instantiate_async`) — the exact async mirror,
   `is_async=True`, emission after the per-key `asyncio.Lock` block.
10. [x] `tests/test_observability.py` — failing tests for `InstanceDisposed`:
    - `shutdown()` emits one event per torn-down singleton, in teardown order (plan 004's
      reverse-creation order), each with `error is None`.
    - a `@PreDestroy` that raises → its event carries `error` and the aggregated `ShutdownError`
      is still raised.
    - `owner` matches `ShutdownFailure.owner`'s format (`container.py:3483-3497`).
    - `await ashutdown()` mirror.
    - request-scope exit emits `InstanceDisposed` with `scope == Scope.REQUEST`.
    - a binding with **no** teardown hook emits **no** event (nothing was disposed).
11. [x] `providify/container.py:3540-3551` (`shutdown`) and `:3626-3632` (`ashutdown`) — wrap each
    `_dispose_sync`/`_adispose` call with the guarded timer and emit `InstanceDisposed` in both the
    success and the `except Exception` branch (setting `error`). The `_AsyncHookInSyncShutdown`
    bail-out path emits nothing (nothing was disposed). Reuse `_owner_label()` (`:3483`) for
    `owner` — do not duplicate the formatting.
12. [x] `providify/container.py:3683` (`_run_pre_destroy_for_scope`) and `:3754`
    (`_arun_pre_destroy_for_scope`) — same guarded emission for scoped instances; `scope` taken
    from the binding found by `_index_class_bindings_by_implementation()` (`:3653`). Keep the
    existing async-hook `warnings.warn`-and-skip behaviour untouched.
13. [x] `tests/test_observability.py` — failing tests for scope events:
    `with container.request():` emits `ScopeEntered(kind="request")` then
    `ScopeExited(kind="request")` with a matching `scope_id` and `duration_ns > 0`; nested request
    frames produce distinct ids in LIFO order; the block raising still emits `ScopeExited`;
    `session("abc")` reports `scope_id == "abc"`; `async with container.arequest():` mirror; with
    **no** hooks registered `container.request()` returns the raw `ScopeContext` context manager
    (identity/`type` assertion — proving the wrapper is not built).
14. [x] `providify/container.py:3186-3253` — the four scope façades: when `self._hooks` is falsy,
    `return self.scope_context.<x>(...)` unchanged; otherwise return a wrapping
    `@contextmanager`/`@asynccontextmanager` that emits `ScopeEntered` after entering, and
    `ScopeExited` in a `finally`. Docstrings must state that
    `container.scope_context.request()` called directly is **not** instrumented.
15. [x] `providify/container.py:5319-5362` (`copy()`) — add
    `new._hooks = {k: list(v) for k, v in self._hooks.items()}` beside `new._tracked_dependents`
    (`:5361`), with a comment: telemetry is *configuration*, not instance state, so a copy keeps
    observing. `_clear_caches()` must **not** touch `_hooks`.
16. [x] `providify/__init__.py` — export `InstanceCreated`, `InstanceDisposed`, `ScopeEntered`,
    `ScopeExited`, `ContainerEvent`: a new `# Observability (container telemetry)` group in
    `__all__` after the validation group (`:64-68`), plus
    `from .observability import ...` in the import block.
17. [ ] `README.md` — an "Observability" section with the §Goal OTel snippet, an explicit note that
    providify has **no** OTel dependency, and the caveat that no OTel semantic conventions exist
    for DI spans yet (research 001 §2), so the attribute names in the recipe are providify's own.
18. [ ] `docs/agents/usage-rules.md` — rules: *"`add_hook` is container telemetry; `@Observes` /
    `Event[T]` is application events — never mix them"*; *"hooks are sync, must be fast, must not
    resolve from the container, and their exceptions are swallowed"*.
19. [ ] `CHANGELOG.md` — `### Added` under `[Unreleased]`: `container.add_hook()`/`remove_hook()`,
    the four event types, and the explicit "no new dependencies" note.

## Edge cases

- No hooks registered → no timer call, no event allocation, scope façades return the underlying
  context manager object unchanged.
- Hook raises → logged at WARNING, resolution/shutdown continues, other hooks for the same event
  still run.
- Hook registered for a supertype of an event class → not called (exact-type dispatch, documented).
- Same callback added twice → called twice; `remove_hook` removes one occurrence and returns `True`.
- `remove_hook` for an unregistered pair → returns `False`, no error.
- Cache hit → no `InstanceCreated` (by design).
- Two interfaces sharing one implementation/cache key → one creation event (the second resolution
  is a cache hit, `container.py:1914-1916`).
- Creation raises → **no** `InstanceCreated` (there is no instance); the exception propagates
  unchanged.
- Binding with no `@PreDestroy`/`@Disposes` → no `InstanceDisposed`.
- `shutdown()` twice → second call has an empty teardown plan → no events.
- Async `@PreDestroy` reached from sync `shutdown()` → `RuntimeError` bail-out, no event for that
  binding.
- Nested `request()` frames → two `ScopeEntered`/`ScopeExited` pairs with distinct ids, LIFO.
- `session("abc")` re-entered → a `ScopeEntered`/`ScopeExited` pair per block (matching
  `ScopeContext.session`'s documented semantics, `scope.py:240-262`); the cache is not necessarily
  discarded on exit.
- `container.scope_context.request()` used directly → no scope events (documented).
- `copy()` → hooks are inherited; both containers emit to the same callbacks.
- Multi-threaded resolution → two threads creating different singletons emit concurrently; hooks
  must be thread-safe. Documented in `add_hook()`'s Thread-safety section.
- `add_hook` called *during* an emission (a hook registering another hook) → the emitting loop
  iterates the list being mutated. Mitigation: `_emit` iterates over a tuple snapshot
  (`tuple(self._hooks.get(type(event), ()))`).

## Verification

```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_observability.py -q
uv run pytest tests/test_shutdown_order.py tests/test_scopes.py tests/test_lifecycle.py -q
uv run pytest -q                      # full suite — no regressions
uv run ruff check providify tests
uv run ruff format --check providify tests
```

No new dependency was introduced:

```bash
cd /home/edoardo/projects/providify && uv run python -c "
import sys, providify
assert 'opentelemetry' not in sys.modules
print(open('pyproject.toml').read().count('dependencies ='), 'dependency keys')  # optional-deps only
"
```

Hot-path sanity (informational, not a gate — record the numbers in the PR):

```bash
cd /home/edoardo/projects/providify && uv run python -m timeit -s "
from providify import DIContainer, Singleton
@Singleton
class A: ...
c = DIContainer(); c.register(A); c.get(A)
" "c.get(A)"
```

## Risks

- ⚠️ **ASSUMPTION — sync-only callbacks are sufficient, even though providify is async-first.**
  Research 001 §5 and §Evidence Gaps are explicit that *"no clear best practice emerged; OTel,
  SQLAlchemy and Django all use sync hooks"* and that async instrumentation patterns are
  *"underspecified"*. providify is the async-first outlier: a consumer wanting to ship an event to
  an async exporter must schedule it themselves (`loop.call_soon`, a queue). Supporting
  `async def` callbacks would mean `_emit` becoming awaitable, which is impossible on the sync
  path (`_instantiate_sync`) — so a mixed API would emit some events only on the async path, which
  is worse than none. **Decision: sync-only, revisit if a consumer hits a real wall.** Invariant:
  *hooks are called inline, in the caller's thread/task, and must not block.*
- ⚠️ **ASSUMPTION — swallowing hook exceptions is right.** It diverges from SQLAlchemy/Django
  (research 001 §3), which propagate. Chosen because a raising telemetry hook inside `shutdown()`
  would abort teardown — exactly the failure class plan 004 exists to close. Consequence: a broken
  hook is silent apart from a WARNING. If consumers ask, add `add_hook(..., strict=True)` later —
  additive, non-breaking.
- ⚠️ **ASSUMPTION — event field sets are right first time.** These four dataclasses are public API
  from day one. `frozen=True, slots=True, kw_only=True` (research 001 §4) means adding a field with
  a default stays backward compatible, but adding a **required** field or renaming one does not.
  Resist adding fields speculatively; `interface`/`implementation` carry the objects, so most
  future needs are derivable consumer-side.
- **`DIMetadata.track` is NOT reusable for F6 — verified, not assumed.** It gates
  `flush_dependents()` (`container.py:1969-1973`, `:2056-2059`) and is documented public decorator
  surface (`decorator/scope.py:177`, `:244`, `:373`). F6 introduces its own `_hooks` state and
  touches no metadata.
- **Hot-path edits to `_instantiate_sync`/`_instantiate_async`.** These are the most-executed
  methods in the library and are covered by intricate double-check-locking comments
  (`container.py:1918-1959`). The guarded-branch design keeps the zero-hook cost to one attribute
  load and one jump, and step 6 tests it deterministically — but any edit here risks the locking
  invariants. Invariant that must hold: *no emission occurs while a per-key lock is held, and no
  emission occurs before `cache[key] = instance`.*
- **Scope-façade wrapping changes return types.** `container.request()` currently returns
  `ScopeContext.request()`'s generator-based context manager; with hooks registered it returns a
  *different* context manager object. Anything that type-checks or duck-types the return value
  breaks. Mitigation: identical `with`/`async with` protocol, same yielded value (the id string),
  and step 13 asserts the unwrapped identity when no hooks exist.
- **Naming collision risk with the existing event system.** `Event[T]`, `@Observes`, `EventProxy`,
  `EventMeta` are all exported already (`__init__.py:75`, `:80`, `:91`). The new names
  (`InstanceCreated`, `ScopeEntered`, `add_hook`) deliberately avoid the word "event" in the API
  surface, but `ContainerEvent` does use it — accept it (it is a type alias, not a decorator) and
  make steps 2/18 state the distinction loudly.
- **A hook that calls `container.get()` can deadlock** across threads (hook on thread A resolves a
  key thread B is creating). Emission outside locks removes the self-deadlock; the cross-thread
  case is documented as unsupported in `add_hook()`'s docstring.
