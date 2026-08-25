# Plan 008 — Multi-module startup/shutdown ordering: the `@Configuration` DAG (F5)

Implements backlog item **F5** (🟡 should, complexity M — `BACKLOG.md:40`): *"Needed once internal
services split into >1 module with cross-module lifecycle dependencies."* Evidence baseline: Spring
Modules, Guice Modules, dishka Components — research 001 §Differentiators #9, cited via
`BACKLOG.md:40`.

> **No external research brief covers this item.** The `depends_on=` vs inferred-ordering choice
> below is an **internal API design decision** (§Design/"Design decision"), not a research-backed
> one. It is called out as such deliberately.

## Goal

`@Configuration` modules declare their ordering explicitly, install in dependency order (deps
first), get their `@PostConstruct` run at install, and get their `@PreDestroy` run at shutdown in
**exact reverse** install order — after every singleton has been torn down.

```python
@Configuration
class InfraModule:
    @Provider(singleton=True)
    def pool(self) -> DatabasePool: ...
    @PreDestroy
    def close(self) -> None: ...          # ← runs today: never. After F5: last.

@Configuration(depends_on=[InfraModule])
class RepoModule:
    def __init__(self, pool: DatabasePool): ...   # resolvable because Infra installed first
```

```
install order  :  InfraModule → RepoModule → ServiceModule
shutdown order :  singletons (reverse creation, plan 004)
                  then ServiceModule → RepoModule → InfraModule
```

`container.install(ServiceModule)` transitively installs its dependencies first. A cycle in
`depends_on` raises `ModuleCycleError` naming the cycle.

## Non-goals

- **No change to singleton teardown ordering.** Plan 004's reverse-creation-order
  (`_teardown_plan`, `container.py:3363-3441`) is untouched and still runs **first**; module
  teardown is a second, separate phase. Verified present in code: `_teardown_plan` / `shutdown` /
  `ashutdown` at `container.py:3363`, `:3499`, `:3606` — F9 is shipped, not planned.
- **No inferred module ordering** from `@Provider` parameter types. See §Design decision.
- **No module scoping / isolation** (dishka Components, Guice `PrivateModule`): a module's
  providers still land in the one flat `_bindings` list, visible to everybody. F5 orders
  *installation*, it does not partition the graph.
- **No module-level `validate()` integration.** `container.validate()` (plan 003,
  `container.py:4183`) gains no `MODULE_CYCLE` issue kind — cycles raise at install time, which is
  strictly earlier than `validate()` could report them.
- **No `@Configuration` becoming a bean.** The module class is still not registered as a binding
  (`decorator/module.py:63-65`); F5 only gives the container an owned reference to the instance so
  it can run its lifecycle hooks.
- **No async `depends_on` resolution differences** — `ainstall()` mirrors `install()` exactly.
- **No shutdown timeouts / signal handling** (already a non-goal of plan 004).
- **No version bump** — `CHANGELOG.md` entry only.

## Design

### What is actually broken today

1. **Install order is arbitrary.** `scan()` → `_scan_module` (`scanner.py:129-149`) walks
   `inspect.getmembers()` — alphabetical — and calls `_autoregister_configurator` →
   `container.install(cls)` (`scanner.py:263-269`). `install()` resolves the module's constructor
   **eagerly** (`container.py:4926`), so a module whose `__init__` needs a type produced by another
   module's `@Provider` raises `LookupError` purely because `A` sorts after `B`.
2. **Module `@PostConstruct` never runs.** `install()` calls `_resolve_constructor`
   (`container.py:2927-2960`), which injects constructor params and class vars and **returns** —
   verified: no `_run_post_construct_sync` call anywhere in it.
3. **Module `@PreDestroy` never runs.** The container keeps no reference to the module instance
   after `_register_module_providers` (`container.py:4953-5007`); it survives only as `self` inside
   the bound provider methods. `shutdown()` therefore cannot see it.
4. **`scan()` + explicit `install()` double-registers.** Dedup lives on the *scanner*
   (`_installed_configurations`, `scanner.py:84`), and `container.install()` bypasses it entirely.
   Installing a module explicitly and then scanning its package registers every `@Provider` twice.

F5 fixes all four with one piece of state.

### Design decision (not research-backed): explicit `depends_on=`

```python
@Configuration(depends_on=[InfraModule])
class RepoModule: ...
```

Rationale, weighed against inferring order from what each module's providers consume:

| | explicit `depends_on=` | inferred from provider/ctor param types |
|---|---|---|
| Correctness | ✅ Exactly what the author meant | ❌ A provider method's params are resolved *lazily*, at first `get()`, so most real edges are invisible at install time; only `__init__` params are eager |
| `Lazy[T]`/`Live[T]`/`Provider[T]` edges | ✅ Author declares them | ❌ Invisible by construction |
| Failure mode | ✅ Cycle → named error at install | ❌ Missing edge → silent wrong order → `LookupError` far from the cause |
| Cost | ❌ Author must write it | ✅ Zero authoring cost |
| Reuse of existing machinery | ✅ New 40-line pure module | ❌ Would need `_get_dependencies()`, which is *deliberately lossy* — it swallows unresolvable deps and downgrades `AnnotationResolutionError` to a warning (`container.py:4679-4768`) because its real caller is `describe()`. Plan 004 rejected it for teardown ordering for exactly this reason (`plans/004-graceful-shutdown.md:100-111`) |

Chosen: **explicit**. Same call as Guice/Spring (`@Import`), and the one thing inference cannot do
is express an ordering constraint that has no type edge at all (e.g. "run migrations module before
anything else touches the pool"). Cost is bounded: `depends_on` is optional, and modules with no
cross-module needs write `@Configuration` exactly as they do today.

### State: one dict on the container

```python
@dataclass(frozen=True, slots=True)
class _ModuleRecord:
    instance: object
    owned: bool      # False on copy() — the copy did not create it, must not dispose it
    disposed: bool   # True after its @PreDestroy ran — makes shutdown() idempotent

self._installed_modules: dict[type, _ModuleRecord] = {}   # insertion order == install order
```

- **Dedup** = key presence. Moves the authority from the scanner to the container, fixing bug 4.
- **Teardown order** = `reversed(self._installed_modules.items())`, skipping
  `not owned or disposed`. Mirrors plan 004's `_singleton_order` ownership reasoning
  (`container.py:5324-5329`, `copy()` must not replay the source's history).
- **Idempotency**: shutdown replaces each record with `replace(rec, disposed=True)`, so
  `_clear_caches()` does **not** clear this dict — clearing it would let a post-shutdown
  `install()` re-register every provider on top of the still-present `_bindings`.

### Install pipeline

```
install(M)                         ainstall(M)
   │                                   │
   └────────┬──────────────────────────┘
            ▼
   resolve_install_order([M])   ── providify/modules.py, pure, cycle-checked
            │                      DFS post-order over depends_on, declaration order
            ▼
   [Infra, Repo, M]              deps first, M last
            │
            ▼  for each cls not already in _installed_modules:
   _resolve_constructor(cls)          / _resolve_constructor_async
   _run_post_construct_sync(...)      / _run_post_construct_async     ← NEW (bug 2)
   _register_module_providers(cls, instance)                          (unchanged)
   _installed_modules[cls] = _ModuleRecord(instance, owned=True, disposed=False)
```

`resolve_install_order` lives in a new **pure** `providify/modules.py` that imports stdlib +
`providify.metadata` + `providify.exceptions` only, and **must not import `container.py`** — the
isolation pattern already used by `validation.py`, `profiles.py` and `config.py`
(`plans/006-configuration-binding.md:227-236`). That makes cycle/order logic unit-testable with
zero container setup.

```python
def module_dependencies(cls: type) -> tuple[type, ...]        # reads ConfigurationMetadata
def resolve_install_order(roots: Sequence[type]) -> list[type]  # raises ModuleCycleError, TypeError
```

DFS post-order with a `path: list[type]` for cycle detection and a `seen: set[type]` for dedup;
`depends_on` is visited in declaration order, so the result is deterministic.

### Shutdown pipeline

```
shutdown()                                          ashutdown()
  1. for key, binding in _teardown_plan():  …       (plan 004, UNCHANGED)
  2. for cls, rec in reversed(_installed_modules):  ← NEW
        skip unless rec.owned and not rec.disposed
        hook = _find_pre_destroy(cls)               (decorator/lifecycle.py)
        run it; failures append ShutdownFailure(owner=f"{cls.__name__}.{hook}")
        mark rec disposed
  3. finally: _clear_caches()                       (does NOT touch _installed_modules)
  4. failures? → ShutdownError(failures)            (plan 004's aggregation, unchanged)
```

**Why modules last**: a module's `@PreDestroy` typically releases a resource that its own
`@Provider` methods handed out (a pool, a client, an executor). Every consumer of that resource is
a singleton, and every singleton is already gone by the time phase 2 runs. Invariant:
*nothing the container owns is alive when a module's teardown runs.*

Sync/async parity follows plan 004 exactly: an `async def` module `@PreDestroy` reached from sync
`shutdown()` raises the same un-aggregated `RuntimeError` as `_dispose_sync`'s guard
(`container.py:3472-3480`), pointing at `ashutdown()`.

### Alternatives considered

- **Inferred ordering from provider/constructor parameter types.** See the table above.
  **Rejected** — lossy, blind to lazy edges, and its failure mode is a silent mis-order.
- **`depends_on` as strings** (`depends_on=["myapp.infra.InfraModule"]`) to avoid import cycles
  between module files. ✅ No import needed. ❌ Needs a resolver, an import-error path, and defers
  typos to runtime; ❌ `@Configuration` classes importing each other is not a real cycle risk since
  they contain no logic. **Rejected** — class objects only. (A string form can be added later
  without breaking the class form.)
- **A separate `@ModuleOrder(after=[...])` decorator** rather than a `@Configuration` parameter.
  ✅ Keeps `@Configuration` a bare marker. ❌ Two decorators to express one idea; ❌ ordering is
  intrinsically module metadata. **Rejected.**
- **Sorting all discovered modules once, in `scan()`**, instead of transitive install from
  `install()`. ✅ One sort per scan. ❌ Leaves explicit `container.install(M)` unordered, which is
  the path apps actually use in production wiring; ❌ two code paths to keep consistent.
  **Rejected** — `install()` is the single ordering choke point, and `scan()` gets ordering for
  free by going through it.
- **Tearing modules down interleaved with singletons** (one flat order, modules recorded into
  `_singleton_order` at install time). ✅ One list, one loop. ❌ Modules are installed *before* the
  singletons they produce are created, so reverse-creation order would dispose a module **first**
  — the precise inversion F5 exists to prevent. **Rejected.**
- **Clearing `_installed_modules` in `_clear_caches()`** (symmetry with `_singleton_order`,
  `container.py:3642-3649`). ❌ `shutdown()` does not clear `_bindings`, so a post-shutdown
  re-`install()` would double-register every provider — bug 4 again. **Rejected** in favour of the
  `disposed` flag.

## Steps

1. [x] `tests/test_module_order.py` — new file, failing tests for the **pure** ordering function
   (no container): linear chain `C → B → A` returns `[A, B, C]`; diamond `D→(B,C)→A` returns `A`
   first, `D` last, `B` before `C` (declaration order); a module with no `depends_on` returns
   `[itself]`; two roots sharing a dependency install it once; self-dependency raises
   `ModuleCycleError`; a 3-cycle raises `ModuleCycleError` whose `.cycle` names all three in order;
   `depends_on` containing a class **not** decorated with `@Configuration` raises `TypeError`
   naming it.
2. [x] `providify/exceptions.py` — add after `ConfigBindingError` (`exceptions.py:353`):
   `class ModuleCycleError(providifyError)` with `__init__(self, cycle: Sequence[type])` storing
   `self.cycle: tuple[type, ...]` and a message rendering `A → B → C → A`. Docstring in project
   style (purpose, Attributes, Example), mirroring `CircularDependencyError` (`:71`).
3. [x] `providify/metadata.py:327-333` — `ConfigurationMetadata` gains
   `__slots__ = ("depends_on",)` and `__init__(self, depends_on: tuple[type, ...] = ())`. The
   default keeps every existing `ConfigurationMetadata()` construction site valid
   (`decorator/module.py:87`). Update the class docstring.
4. [x] `providify/modules.py` — new pure module: `module_dependencies(cls)` and
   `resolve_install_order(roots)` per §Design. Imports stdlib + `providify.metadata` +
   `providify.exceptions` **only**; a module-level DESIGN comment must state the no-`container.py`
   isolation rule and cite the `validation.py`/`config.py` precedent. Full docstrings per CLAUDE.md.
5. [x] `tests/test_module_order.py` — failing tests for the decorator's dual form:
   `@Configuration` (bare) still returns the class with `depends_on == ()`;
   `@Configuration()` (empty parens) works identically; `@Configuration(depends_on=[A])` stores
   `(A,)`; a `depends_on` given as a tuple, list or single class all normalise to a tuple; the
   marker is **not** inherited by a subclass (`__dict__` lookup, `metadata.py:350-352`).
6. [x] `providify/decorator/module.py:44-88` — make `Configuration` dual-form:
   `def Configuration(cls=None, *, depends_on=())` returning either the stamped class (bare usage)
   or a decorator closure (call usage). Normalise `depends_on` to a tuple, accepting a single class.
   Extend the module-level DESIGN comment (`:5-41`) with the ordering contract and the
   "explicit over inferred" rationale from §Design decision, marked as a design decision.
7. [x] `tests/test_module_install_order.py` — new file, failing tests for container installation:
   - `install(RepoModule)` where `RepoModule` depends on `InfraModule` → `InfraModule`'s providers
     are registered first and `RepoModule.__init__(pool: DatabasePool)` resolves.
   - `install(RepoModule)` **twice** → providers registered once (assert binding count).
   - `install(InfraModule)` then `scan()` over the package containing both → still one binding per
     provider (**the double-registration bug**, item 4 in §Design).
   - `scan()` alone, where the alphabetically-first module depends on the alphabetically-last →
     succeeds (today: `LookupError`).
   - a module `@PostConstruct` runs exactly once, after `__init__`, before its providers are
     registered.
   - `install()` of a class without `@Configuration` still raises `TypeError`
     (`container.py:4924-4925`, unchanged).
   - a `depends_on` cycle → `ModuleCycleError`, and **no** partial installation
     (binding count unchanged).
   - `await ainstall(RepoModule)` — same ordering, async `__init__` deps, async `@PostConstruct`.
8. [x] `providify/container.py` — add module-level
   `@dataclass(frozen=True, slots=True) class _ModuleRecord(instance, owned, disposed)` beside
   `_ScopedContainer` (`:174`), and `self._installed_modules: dict[type, _ModuleRecord] = {}` in
   `__init__` after `_tracked_dependents` (`:513-514`), with a DESIGN comment stating the three
   roles (dedup / install order / teardown order) and why `owned` and `disposed` exist.
9. [x] `providify/container.py:4901-4951` — rewrite `install()` / `ainstall()`:
   both keep the `TypeError` guard, then call `resolve_install_order([module_cls])` and loop,
   skipping classes already in `_installed_modules`, delegating each to a new
   `_install_one(cls)` / `_ainstall_one(cls)` that does
   `_resolve_constructor` → `_run_post_construct_sync(instance, _find_post_construct(cls))` →
   `_register_module_providers` → record. Cycle detection happens **before** any instantiation, so
   a cycle leaves the container untouched. Docstrings must document: transitive installation,
   idempotency, `@PostConstruct`/`@PreDestroy` participation, and `Raises: ModuleCycleError`.
10. [x] `tests/test_module_shutdown_order.py` — new file, failing tests:
    - `Infra → Repo → Service` modules, each with a `@PreDestroy` appending its name;
      `container.shutdown()` → `["Service", "Repo", "Infra"]`.
    - A `@Singleton` component and a module `@PreDestroy` both recording → **every** singleton hook
      runs before **any** module hook.
    - A module whose `@PreDestroy` raises → aggregated into `ShutdownError.failures` with
      `owner == "InfraModule.close"`, and the remaining module hooks still run.
    - `shutdown()` twice → module hooks run once (`disposed` flag).
    - A module with no `@PreDestroy` → skipped, no error.
    - Async: `await ashutdown()` with `async def` module `@PreDestroy` hooks → same reverse order.
    - Sync `shutdown()` reaching an `async def` module `@PreDestroy` → `RuntimeError` pointing at
      `ashutdown()`, **not** aggregated (mirrors `container.py:3543-3547`).
    - `container.copy()` → the copy's `shutdown()` runs **no** module hooks
      (`owned=False`), and the original's module instances are untouched.
11. [x] `providify/container.py:3537-3570` (`shutdown`) and `:3624-3640` (`ashutdown`) — add
    phase 2 after the `_teardown_plan()` loop and **inside** the same `try:` (so `finally:
    _clear_caches()` still runs): iterate `reversed(list(self._installed_modules.items()))`,
    skipping `not owned or disposed`, run the `@PreDestroy` hook found via
    `_find_pre_destroy(cls)`, aggregate failures with the existing `ShutdownFailure`, and mark the
    record `disposed=True`. Reuse the `_AsyncHookInSyncShutdown` sentinel for the async-hook guard
    in the sync path. Extend both docstrings with an "Ordering" paragraph: *singletons first
    (reverse creation), then modules (reverse install)*.
12. [x] `providify/container.py:5319-5362` (`copy()`) — add
    `new._installed_modules = {k: replace(r, owned=False) for k, r in self._installed_modules.items()}`
    beside `new._tracked_dependents = []` (`:5361`), with a comment mirroring `_singleton_order`'s
    (`:5324-5329`): dedup history is inherited so the copy does not re-register providers whose
    bindings it already holds, but ownership is not — the copy never disposes instances it did not
    create.
13. [x] `providify/scanner.py:263-269` — `_autoregister_configurator` keeps its
    `_installed_configurations` fast-path set but its comment (`scanner.py:72-84`) gains a note
    that the **container** is now the authority (`_installed_modules`) and the scanner set is only
    a same-session shortcut.
14. [x] `providify/__init__.py` — export `ModuleCycleError` (add to the exception group in
    `__all__`, `:46-59`, and to the `from .exceptions import (...)` block, `:137-151`).
    `resolve_install_order` / `_ModuleRecord` stay unexported.
15. [x] `docs/agents/usage-rules.md` — a module-ordering rule: *"if module B's `__init__` or a
    provider needs a type produced by module A, write `@Configuration(depends_on=[A])` — do not
    rely on scan order"*, plus the two lifecycle guarantees (module `@PostConstruct` at install,
    `@PreDestroy` after all singletons).
16. [x] `README.md` — a short "Modules" subsection with the §Goal snippet and the ordering diagram.
17. [x] `CHANGELOG.md` — `### Added`: `@Configuration(depends_on=...)`, transitive/ordered install,
    module `@PostConstruct`/`@PreDestroy`, `ModuleCycleError`. `### Fixed`: `scan()` + `install()`
    no longer double-registers a module's providers. `### Changed`: module `@PreDestroy` hooks now
    run at shutdown (previously silently ignored) — behaviourally visible.

## Edge cases

- `@Configuration` used bare (no parens) → `depends_on == ()`; every existing module keeps working.
- `depends_on=[]` / `depends_on=()` → identical to omitting it.
- `depends_on=SomeModule` (single class, not a sequence) → normalised to `(SomeModule,)`.
- `depends_on` naming a class without `@Configuration` → `TypeError` at install, naming both classes.
- Self-dependency (`depends_on=[Self]`) → `ModuleCycleError` with a length-2 cycle.
- Cycle detected → **nothing** is installed (order resolution precedes instantiation).
- Diamond (`D` depends on `B` and `C`, both on `A`) → `A` installed once; teardown order has `A`
  last.
- The same module reached via two roots in one `scan()` → installed once (dict key presence).
- Explicit `install(M)` followed by `scan()` covering `M` → installed once.
- Module with no `@Provider` methods → still instantiated, `@PostConstruct` runs, recorded, and its
  `@PreDestroy` runs at shutdown (this is now a legitimate "lifecycle-only module").
- Module `__init__` needs an async-only dependency → `install()` raises the existing `RuntimeError`
  (`container.py:4917-4918`); `ainstall()` works. Unchanged.
- Module `@PreDestroy` raises → aggregated; later (earlier-installed) modules still torn down.
- `shutdown()` then `install(M)` again → `M` is still in `_installed_modules` with `disposed=True`
  → **not** re-installed, no double registration. Documented: re-installing after shutdown requires
  a fresh container (or `copy()`).
- A module that is also `@ConfigProperties`-decorated → the scanner's branch order treats it as
  config (`scanner.py:142-149`, plan 006) — unchanged, still documented as ambiguous.
- Two containers installing the same module class → independent `_installed_modules` dicts and
  independent instances; the class object is shared but carries no state.

## Verification

```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_module_order.py tests/test_module_install_order.py \
              tests/test_module_shutdown_order.py -q
uv run pytest tests/test_lifecycle.py tests/test_shutdown_order.py -q   # plan 004 regressions
uv run pytest -q                      # full suite
uv run ruff check providify tests
uv run ruff format --check providify tests
```

Manual acceptance — must print `['Service', 'Repo', 'Infra']`:

```bash
cd /home/edoardo/projects/providify && uv run python -c "
from providify import DIContainer, Configuration, Provider, PreDestroy
order = []
@Configuration
class Infra:
    @PreDestroy
    def close(self): order.append('Infra')
@Configuration(depends_on=[Infra])
class Repo:
    @PreDestroy
    def close(self): order.append('Repo')
@Configuration(depends_on=[Repo])
class Service:
    @PreDestroy
    def close(self): order.append('Service')
c = DIContainer(); c.install(Service); c.shutdown(); print(order)
"
```

## Risks

- ⚠️ **ASSUMPTION — explicit `depends_on=` is the right call.** This is a design decision with no
  external research behind it (§Design decision). If the team would rather have zero-authoring
  inference, the correct change is to build the module graph from each module's `__init__`
  parameters only (the eagerly-resolved ones) and keep `depends_on` as an escape hatch for
  non-type edges — a strictly larger feature that still cannot see provider-method or `Lazy[T]`
  edges. **Decide before step 6**; after release, `@Configuration(depends_on=)` is public API.
- ⚠️ **ASSUMPTION — module teardown belongs *after* singleton teardown, not interleaved.** The
  invariant claimed is *nothing the container owns is alive when a module's teardown runs*. It
  breaks for an instance the app created itself and never handed to the container, or for a
  DEPENDENT-scoped instance that was never tracked (`container.py:1969-1973`). Both are already
  outside the container's ownership by plan 004's own scope statement
  (`plans/004-graceful-shutdown.md:292-296`).
- ⚠️ **ASSUMPTION — moving dedup authority from scanner to container is a safe behaviour change.**
  Today `install(M)` twice registers `M`'s providers twice; after F5 the second call is a no-op.
  That is a bug fix, but any existing test or app that relies on repeated `install()` to *replace*
  bindings will silently change behaviour. Grep `tests/` for repeated `install(` on one class
  before step 9; if a legitimate case exists, add `install(M, force=True)` rather than weakening
  the dedup.
- **Module `@PreDestroy` starts firing where it never fired before.** Any module that already
  carries a `@PreDestroy` (written in the expectation it would run, or copy-pasted) will now
  execute it at shutdown. This is the intent, but it is a runtime behaviour change on existing
  code — step 17's CHANGELOG `### Changed` entry is mandatory.
- **Module `@PostConstruct` likewise.** Same class of change; same mitigation.
- **`_installed_modules` survives `shutdown()`.** Deliberate (see §Alternatives), but it means a
  container is not fully reusable after shutdown for module re-installation. Documented in
  `install()`'s docstring and §Edge cases; the supported reuse path is `copy()` or a new container.
- **`ConfigurationMetadata` gaining a slot is a metadata-format change.** It is constructed in
  exactly one place (`decorator/module.py:87`) and read through `_get_configuration_module`
  (`metadata.py:350-352`), both updated here — but any consumer that stamped the marker by hand
  (unsupported) breaks. Acceptable.
- **Cycle detection cost** is O(V+E) over the module graph, run once per `install()` call with the
  root's transitive closure. Module counts are tiny (single digits); not a hot path.
