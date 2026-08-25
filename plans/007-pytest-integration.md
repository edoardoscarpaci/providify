# Plan 007 — Pytest integration: fixture-based container overrides (F4)

Implements backlog item **F4** (🟡 should, complexity M — `BACKLOG.md:39`): *"Cuts test boilerplate
vs manual `override()`/`reset_binding()` calls; testing-first DI is expected baseline, not bolted
on."*

External grounding: `design/pytest-integration/research/001-pytest-plugin-patterns.md`
(hereafter **research 001**).

## Goal

A consumer installs providify and immediately has working test fixtures, with no conftest
boilerplate:

```python
def test_checkout(di_container, di_overrides):
    di_container.scan("myapp")
    di_overrides.instance(Clock, FrozenClock("2026-01-01"))   # instance, not class
    di_overrides.bind(Notifier, FakeNotifier)                 # class swap
    di_overrides.remove(PaymentGateway)                       # unregister

    assert di_container.get(Checkout).run() == "ok"
    # every override is undone automatically at teardown — no reset_binding() calls
```

Delivered as a `pytest11` entry-point plugin (research 001 §1) whose fixtures are **function-scoped
and `yield`-based** (research 001 §2, §4). The same machinery works without pytest:

```python
with ContainerOverrides(container) as ov:
    ov.instance(Clock, FrozenClock(...))
```

## Non-goals

- **No autouse fixtures.** The plugin is loaded into *every* pytest session of *every* project that
  installs providify (that is what an entry point means). It must therefore have zero effect until
  a fixture is explicitly requested. See §Risks.
- **No session/module-scoped override fixtures.** Function scope only — research 001 §4 records the
  concrete failure mode (`python-dependency-injector` issue #421: overrides that do not reset when
  the container lives in a session fixture; plus xdist "once per worker" semantics).
- **No mock library integration.** `di_overrides.instance(I, Mock())` already works; providify does
  not depend on or wrap `unittest.mock`/`pytest-mock`.
- **No auto-discovery of "the app container".** The plugin cannot know how a consumer builds theirs;
  `di_container` is a fixture the consumer **overrides in their own conftest** (standard pytest
  fixture overriding). The default is a bare `DIContainer()`.
- **No new resolution semantics.** Overrides are expressed entirely through existing public API
  (`override()`, `provide(..., returns=)`, `reset_binding()`, `activate_profile()`,
  `enable_alternative()`).
- **No snapshot of instances created *during* an override window.** See §Edge cases and §Risks.
- **No runtime dependency on pytest.** `import providify` must not import `pytest`.
- **No version bump** — `CHANGELOG.md` entry only.

## Design

### Shape: pytest11 entry point + yield fixtures

Research 001 §Librarian's Note is unambiguous: *"evidence strongly favours a pytest11 entry-point
plugin that exposes a yield-based fixture (function-scoped by default)"*, matching pytest-asyncio
and pytest-mock (research 001 §1) and FastAPI's `dependency_overrides`-in-a-yield-fixture pattern,
which the brief calls *"the closest prior art"* (research 001 §3). Yield is non-negotiable —
cleanup is guaranteed even when the test raises (research 001 §2); `addfinalizer` is explicitly the
rare-case tool.

Research 001 §3 also records that **no DI library ships a polished public testing fixture**
(python-dependency-injector makes users hand-roll one), so this is greenfield: the API below is
providify's own convention.

```
pyproject.toml
  [project.entry-points.pytest11]
  providify = "providify.pytest_plugin"          ← pytest imports this at session start
         │
         ▼
providify/pytest_plugin.py    fixtures only, no autouse, no side effects
  di_container   → DIContainer(); yield; shutdown()          (function scope)
  di_acontainer  → DIContainer(); yield; await ashutdown()   (function scope, async)
  di_overrides   → ContainerOverrides(di_container) as CM    (function scope)
  di_global      → installs di_container as DIContainer._global for the test
         │  imports
         ▼
providify/testing.py          pytest-free — usable from unittest, scripts, REPL
  ContainerOverrides
```

`providify/testing.py` must **not** import `pytest`. That split is what keeps `import providify`
clean (same discipline as plan 006's "no pydantic/yaml at import time" guard).

### `ContainerOverrides` — record nothing, restore everything

Undo is **snapshot/restore**, not an undo-log. Every override method is a thin call to existing
public API; the snapshot is taken once, lazily, on the first mutation, and `reset()` puts the
container's mutable state back verbatim.

```
ov.instance(I, obj) ─┐
ov.bind(I, Impl)     ├─► first call: snap = container.snapshot()
ov.remove(I)         │   then: plain public-API mutation
ov.profiles("test")  ─┘
        …
ov.reset()  /  __exit__  /  fixture teardown  ─► container.restore(snap)
```

Why snapshot/restore rather than an inverse-operation log: `override()` (`container.py:5112-5185`)
already performs four coupled mutations (rebuild `_bindings`, evict `_singleton_cache`, prune
`_singleton_locks`/`_async_singleton_locks`/`_singleton_order`, reset `_validated` via `bind()`),
and `reset_binding()` (`:5189-5265`) does the same. Inverting each of those correctly, in reverse,
for arbitrary interleavings is far more code than restoring six attributes.

### `DIContainer.snapshot()` / `.restore()` — the private-state knowledge stays in `container.py`

`ContainerOverrides` must not poke `_bindings` directly. Two new methods live next to `copy()`
(`container.py:5269-5362`) and inherit its *"must be kept in sync when `__init__` adds
attributes"* caveat (`:5316-5318`):

```python
@dataclass(frozen=True, slots=True)
class ContainerSnapshot:
    """Opaque, restorable capture of a container's mutable state."""
    bindings: tuple[AnyBinding, ...]
    singleton_cache: dict[Any, object]
    singleton_order: tuple[tuple[Any, AnyBinding], ...]
    enabled_alternatives: frozenset[type]
    active_profiles: frozenset[str]
    interceptor_classes: tuple[type, ...]
```

`restore()` writes those back and then calls `_invalidate_type_caches()` + `self._validated = False`
so `_localns_cache`/`_hints_cache` are rebuilt from the restored binding list. `_singleton_locks` /
`_async_singleton_locks` are **not** snapshotted — they are pure derived state, and stale entries
are harmless (they key on cache keys and are lazily recreated); `restore()` only drops lock entries
whose key is absent from the restored cache, to bound growth.

`ContainerSnapshot` is a value object in the codebase's established frozen-dataclass style
(`ValidationIssue`, `ShutdownFailure`, `ConfigIssue`).

### The override API

| method | delegates to | resulting scope |
|---|---|---|
| `bind(iface, impl)` | `container.override(iface, impl)` (`:5112`) | impl's own `@Component`/`@Singleton` scope |
| `instance(iface, obj)` | `reset_binding(iface)` + `provide(lambda: obj, returns=iface)` (`:729`) | DEPENDENT — the *same* object is returned every time |
| `factory(iface, fn)` | `reset_binding(iface)` + `provide(fn, returns=iface)` | DEPENDENT — `fn()` per resolution |
| `remove(iface, *, qualifier=None)` | `container.reset_binding(iface, qualifier=...)` (`:5189`) | — |
| `profiles(*names)` | `container.activate_profile(name)` (`:1328`) | — |
| `alternative(cls)` | `container.enable_alternative(cls)` (`:1276`) | — |
| `reset()` | `container.restore(snapshot)` | — |

`instance()` is the ergonomic centrepiece and it needs **no new container API**:
`provide(fn, returns=iface)` accepts an *undecorated* callable precisely when `returns=` is given
(`binding.py:612-623` — "the whole point of `returns=` on provide() is registering a plain factory
someone else wrote"), falling back to `ProviderMetadata.default()`, which is
`singleton=False` → `Scope.DEPENDENT` (`metadata.py:321-324`). A DEPENDENT binding over a closure
returning a captured object yields that object on every resolution with no singleton-cache or
teardown entanglement — exactly the semantics a test double wants.

`reset_binding()` before `provide()` is required because `provide()` only *appends*; without the
removal the original binding would still be a candidate (and could win on `@Priority`).

### `di_global` — opt-in, not autouse

Tests for code that calls `DIContainer.current()` need the fixture container installed as the
global. `scoped()` (`container.py:589-600`) always creates a *fresh* container and cannot adopt an
existing one, so this plan adds an optional parameter:

```python
DIContainer.scoped(container: DIContainer | None = None) -> _ScopedContainer
```

`_ScopedContainer._install()` (`container.py:188-193`) uses `self._container or DIContainer()`.
Backward compatible; `di_global` becomes a four-line yield fixture. It is **not** autouse —
see §Non-goals.

### Alternatives considered

- **Fixture-only, no entry point** (users add `pytest_plugins = ["providify.testing"]` or copy a
  conftest snippet). ✅ Zero packaging change; ✅ no plugin loaded into unrelated projects.
  ❌ Research 001 §Librarian's Note: *"Users will expect a simple, discoverable fixture (not manual
  conftest imports), and the entry-point approach is the modern pytest standard"*; ❌ the whole
  backlog rationale is "cuts boilerplate" — a required conftest snippet is boilerplate.
  **Rejected**, but the fallback is free: `providify/testing.py` is importable and
  `ContainerOverrides` works standalone, so a consumer who dislikes the plugin loses nothing.
- **Session-scoped `di_container` for speed.** ✅ Avoids re-scanning a large app per test.
  ❌ Research 001 §4 documents the exact failure this causes (dependency-injector #421; the mutable
  session-fixture trap; xdist per-worker semantics). **Rejected** — a consumer who wants it can
  build a session-scoped *base* container in their own conftest and have `di_container` return
  `base.copy()` (`container.py:5269`), which is O(len(bindings)) and already isolation-safe.
- **`copy()`-only isolation** (no `ContainerOverrides` at all — every test works on a throwaway
  copy). ✅ Zero new machinery; already shipped. ❌ Useless when the system under test resolves via
  `DIContainer.current()` or holds a reference to the original container, which is precisely the
  case tests find hardest today. **Rejected as the sole mechanism**, kept as the documented
  fast-path recipe for `di_container`.
- **An undo-log inside `ContainerOverrides`** (record each mutation, invert on reset). ✅ Only
  touches what changed; ✅ no snapshot memory. ❌ Must invert `override()`/`reset_binding()`'s four
  coupled mutations for arbitrary interleavings (see §Design). **Rejected.**
- **`addfinalizer` instead of `yield`.** ❌ Research 001 §2 — yield is the standard, cleanup is
  guaranteed on exception, and Ruff PT021 flags the finaliser callback style. **Rejected.**
- **New `container.override_instance()` / `override_factory()` public methods.** ✅ Usable outside
  tests. ❌ Grows the container's already-large public surface for something
  `provide(fn, returns=)` expresses exactly, and encourages instance-binding in production code.
  **Rejected** — it lives on `ContainerOverrides`, where the intent is unambiguous.

## Steps

1. [x] `tests/test_container_snapshot.py` — new file, failing tests for `snapshot()`/`restore()`:
   - snapshot → `bind(I, A)` → restore → `is_resolvable(I)` is `False` again.
   - resolve a singleton → snapshot → `override(I, Fake)` → resolve → restore →
     `get(I)` returns the **original** cached instance (identity check), and the fake's instance is
     gone.
   - `reset_binding()` then restore → binding count back to the snapshot value.
   - `activate_profile("x")` / `enable_alternative(C)` / `add_interceptor(C)` then restore → all
     three back to snapshot values.
   - restore twice from the same snapshot → idempotent, no error.
   - a snapshot taken from container A restored into container B is **not** supported → document
     only (no guard; it is a private-ish escape hatch).
   - `_singleton_locks` holds no entry for a key absent from the restored cache.
2. [x] `providify/container.py` — add module-level
   `@dataclass(frozen=True, slots=True) class ContainerSnapshot` beside `_ScopedContainer`
   (`container.py:174`), with the six fields in §Design and a full docstring stating it is an
   opaque capture whose only supported use is `restore()`.
3. [x] `providify/container.py` — add `snapshot(self) -> ContainerSnapshot` and
   `restore(self, snapshot: ContainerSnapshot) -> None` immediately **after** `copy()`
   (`container.py:5362`). Both docstrings must (a) cross-reference `copy()`'s
   *"fragile if `__init__` adds new attributes — must be kept in sync"* comment (`:5316-5318`),
   (b) state that instances created after the snapshot are **dropped without teardown**, and
   (c) mark thread safety as ⚠️ not safe under concurrent resolution.
4. [x] `providify/container.py:589-600` — `scoped()` gains
   `container: DIContainer | None = None`; `_ScopedContainer.__init__` stores it and `_install()`
   (`:188-193`) uses `self._container_arg or DIContainer()`. Docstring: adopting an existing
   container does **not** shut it down on exit — only the global reference is restored.
5. [x] `tests/test_container_snapshot.py` — failing test: `DIContainer.scoped(existing)` installs
   `existing` as `DIContainer.current()` inside the block and restores the previous global after,
   including when the block raises; the adopted container is not shut down.
6. [x] `tests/test_testing_overrides.py` — new file, failing tests for `ContainerOverrides` used
   **without pytest**, as a context manager:
   - `instance()` — `get(I)` returns the exact object, twice, with `is` identity.
   - `instance()` where `I` had an existing `@Singleton` binding that was already resolved → the
     double wins; after exit the original binding *and* its cached instance are back.
   - `bind()` — class swap; `factory()` — a counting factory called once per `get()`.
   - `remove()` — `is_resolvable(I)` is `False` inside, `True` after exit.
   - `profiles("test")` / `alternative(C)` — active inside, restored after.
   - the block raises → overrides still undone (`__exit__` runs).
   - nested `ContainerOverrides` on the same container → inner exit restores the outer's state.
   - `reset()` called explicitly, then `__exit__` → no double-restore error.
   - no mutation at all → `__exit__` is a no-op (snapshot never taken).
7. [x] `providify/testing.py` — new module implementing `ContainerOverrides` exactly as §Design's
   table. Rules: imports `providify.container` only (**never `pytest`**); snapshot taken lazily in
   a `_ensure_snapshot()` guard; `__enter__`/`__exit__` + explicit `reset()`; every method returns
   `self` for chaining. Full docstrings per CLAUDE.md (Args/Returns/Raises/Thread safety/Async
   safety/Edge cases/Example), and a module-level DESIGN comment recording the
   snapshot-over-undo-log decision and citing research 001 §3 (FastAPI `dependency_overrides` as
   the closest prior art).
8. [x] `tests/test_pytest_plugin.py` — new file using pytest's own `pytester` fixture
   (`pytest_plugins = ["pytester"]` at module top). Failing tests that run a generated test file
   in a subprocess-ish inline runner and assert outcomes:
   - `di_container` yields an empty `DIContainer` and calls `shutdown()` after the test (assert via
     a `@PreDestroy` hook writing to a file in `tmp_path`).
   - `di_overrides` undoes an override between two tests in the same file (test A overrides, test B
     asserts the original) — the cross-test-pollution regression research 001 §4 warns about.
   - a consumer conftest that **redefines** `di_container` wins over the plugin's default.
   - `di_global` makes `DIContainer.current()` return the fixture container inside the test and
     restores the previous global after.
   - requesting no providify fixture → plugin has zero effect (a test asserting
     `DIContainer._global is None` passes with the plugin loaded).
9. [x] `providify/pytest_plugin.py` — new module with the four fixtures, all
   `@pytest.fixture` (function scope, the default — stated explicitly in each docstring, citing
   research 001 §4), all `yield`-based (research 001 §2). Module-level DESIGN comment must state:
   **no autouse fixtures, no `pytest_configure` side effects** — this module is imported into every
   pytest session of every project that depends on providify. `di_acontainer` is an `async def`
   yield fixture whose docstring states it requires pytest-asyncio (`asyncio_mode=auto`) or anyio.
10. [x] `pyproject.toml` — add
    ```toml
    [project.entry-points.pytest11]
    providify = "providify.pytest_plugin"
    ```
    after `[project.optional-dependencies]` (`pyproject.toml:37-38`). Research 001
    §Version/Compatibility confirms hatchling ≥1.20 handles this table. Do **not** add `pytest` to
    `[project.dependencies]` — the project still declares no runtime dependencies.
11. [x] `tests/test_pytest_plugin.py` — failing guard test, run in a **subprocess** (mirroring plan
    006's core-import guard, `plans/006-configuration-binding.md:470-480`):
    `python -c "import providify, sys; assert 'pytest' not in sys.modules"`.
12. [x] `tests/conftest.py:26-37` — dogfood: redefine the existing `container` fixture as a thin
    alias of the plugin's `di_container` (`def container(di_container): return di_container`) so
    the 37 existing test modules exercise the shipped fixture. Keep `reset_global_container`
    autouse **local to providify's own suite** — it must not move into the plugin (§Non-goals).
13. [x] `providify/__init__.py` — export `ContainerOverrides` and `ContainerSnapshot`: a new
    `# Testing helpers` group in `__all__` after `# Configuration binding` (`__init__.py:35-43`),
    plus `from .testing import ContainerOverrides` and `ContainerSnapshot` added to the existing
    `from .container import ...` line (`:104`). **Do not** export anything from
    `providify.pytest_plugin`.
14. [x] `docs/agents/usage-rules.md` — a testing section: *"use the `di_container` / `di_overrides`
    fixtures; do not hand-roll `override()` + `reset_binding()` pairs"*, *"override `di_container`
    in your own conftest to return `app_container.copy()`"*, and the explicit warning that
    instances created during an override window are dropped without teardown.
15. [x] `README.md` — a "Testing" section with the §Goal snippet and the conftest-override recipe.
16. [x] `CHANGELOG.md` — `### Added` under `[Unreleased]`: pytest plugin (entry point
    `pytest11`), `ContainerOverrides`, `container.snapshot()`/`restore()`,
    `DIContainer.scoped(container)`.

## Edge cases

- No override performed → no snapshot taken → teardown is a no-op.
- `instance()` on an interface with **multiple** bindings (qualifiers) → `reset_binding(iface)` with
  `qualifier=None` removes *all* of them (documented at `container.py:5204-5206`); the double
  becomes the only candidate. Use `remove(iface, qualifier=q)` + `instance()` for finer control.
- `instance()` on an interface with **no** existing binding → `reset_binding` returns 0, no error;
  the double is simply registered.
- Overriding an already-resolved singleton → the cached original is evicted by `reset_binding()`
  and **restored** by `restore()` (the snapshot holds it), so post-test code sees the same object.
- A singleton resolved *during* the override window is dropped at restore **without** `@PreDestroy`
  running. Mitigated by `di_container` being fresh-per-test with `shutdown()` in teardown; called
  out in every relevant docstring. See §Risks.
- Test raises → `yield` fixture teardown still runs (research 001 §2); `__exit__` still restores.
- Two `ContainerOverrides` nested on one container → each holds its own snapshot; LIFO exit gives
  correct restoration. Concurrent (non-nested) use on one container is unsupported.
- `di_container` teardown when a binding has an **async** `@PreDestroy` → `shutdown()` raises the
  existing `RuntimeError` telling the user to use `ashutdown()` (`container.py:3476-3480`) — the
  fixture docstring points at `di_acontainer`.
- `di_container` teardown when a `@PreDestroy` raises → `ShutdownError` surfaces as a fixture
  teardown error, which is correct: a leaking teardown is a real defect.
- A consumer project has pytest but not pytest-asyncio and never requests `di_acontainer` → nothing
  happens; the async fixture is defined but never evaluated.
- xdist: all fixtures are function-scoped, so each worker is independent by construction
  (research 001 §4).
- `di_global` nested inside another `DIContainer.scoped()` block → `_ScopedContainer` restores
  `_previous`, so nesting is correct.

## Verification

```bash
cd /home/edoardo/projects/providify
uv sync
uv run pytest tests/test_container_snapshot.py tests/test_testing_overrides.py \
              tests/test_pytest_plugin.py -q
uv run pytest -q                      # full suite — no regressions
uv run ruff check providify tests
uv run ruff format --check providify tests
```

Plugin is actually discovered (must list `providify` under "registered third-party plugins"):

```bash
cd /home/edoardo/projects/providify && uv run pytest --trace-config --collect-only -q 2>&1 | grep -i providify
```

Core-import guard — pytest must not be pulled in by `import providify`:

```bash
cd /home/edoardo/projects/providify && uv run python -c "
import sys, providify
assert 'pytest' not in sys.modules, 'providify must not import pytest'
print('core import clean')
"
```

## Risks

- ⚠️ **ASSUMPTION — shipping a `pytest11` entry point is acceptable.** It means providify's plugin
  module is imported by pytest in **every** project that has providify installed, including
  projects that never use the fixtures. Research 001 §1/§Librarian's Note recommends it and
  pytest-asyncio/pytest-mock set the precedent, but it is a real, permanent side effect on
  consumers. Invariant that bounds it: *`providify/pytest_plugin.py` defines fixtures and nothing
  else — no autouse, no hooks, no `pytest_configure`, no import-time work beyond `import
  providify`.* Step 9 must enforce this; step 8's "zero effect" test locks it in. If the team is
  uncomfortable, the fallback is deleting step 10 and documenting
  `pytest_plugins = ["providify.pytest_plugin"]` — everything else in the plan is unchanged.
- ⚠️ **ASSUMPTION — the fixture names `di_container` / `di_overrides` / `di_global` /
  `di_acontainer` are right.** Fixture names from an entry-point plugin occupy a **global**
  namespace in every consumer's test suite; a collision with an existing consumer fixture silently
  changes which one wins (nearest conftest wins — usually the consumer's, which is the safe
  direction, but is a surprise either way). The `di_` prefix is the mitigation. Renaming after
  release is breaking. Decide before step 9.
- ⚠️ **ASSUMPTION — async fixtures in a pytest11 plugin degrade gracefully.** Research 001
  §Evidence Gaps explicitly flags *"async override fixtures … have less published guidance"*.
  `di_acontainer` is an `async def` yield fixture; in a project with neither pytest-asyncio nor
  anyio installed, requesting it yields an un-awaited async generator rather than a clear error.
  Verify the actual failure message during step 8 and, if it is cryptic, add a `pytest.skip` guard
  keyed on `"pytest_asyncio" in sys.modules or "anyio" in sys.modules`.
- **Instances created during an override window are dropped without teardown.** `restore()` writes
  the snapshotted `_singleton_cache` back wholesale; anything created after the snapshot vanishes
  with no `@PreDestroy`/`@Disposes`. Invariant: *`ContainerOverrides` restores **configuration**,
  not **lifecycle** — for lifecycle correctness use a per-test container that is `shutdown()`.*
  The default `di_container` fixture does exactly that, so the default path is safe; only a
  consumer who points `di_container` at a long-lived app container is exposed. Must be loud in
  steps 7, 14.
- **`snapshot()`/`restore()` inherit `copy()`'s sync-drift fragility** (`container.py:5316-5318`):
  a future `__init__` attribute is silently not restored. Mitigation is the same one `copy()`
  relies on — test coverage (step 1) — plus a cross-reference comment in all three methods so the
  next person changing `__init__` sees three call sites, not one.
- **`instance()` binds at DEPENDENT scope**, so a component that injects `I` gets the double, but
  the double is never torn down by `shutdown()` (DEPENDENT instances are only tracked with
  `@Component(track=True)`, `container.py:1969-1973`). Correct for test doubles; documented so
  nobody expects `@PreDestroy` on a mock.
- **Behaviour change for providify's own suite** (step 12): `container` becomes an alias of
  `di_container`, which now calls `shutdown()` in teardown. Any existing test that leaves a failing
  `@PreDestroy` behind will start erroring at teardown. Expected to be zero cases; if the full
  suite disagrees, fix the tests, not the fixture.
- **Entry-point registration is a packaging change.** `[project.entry-points.pytest11]` did not
  exist before; verify `uv build` still produces a valid wheel and that the entry point appears in
  the built `*.dist-info/entry_points.txt`.
