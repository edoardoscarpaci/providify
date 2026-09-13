# Usage Rules — Read First

These are the rules an agent must follow to use Providify correctly. They are
ordered by how often they are violated. Each rule states **what to do** and **what
breaks if you don't**.

---

## R1 — Annotate classes you own; reach for `@Provider` only for the rest

**Do:** put a scope decorator directly on classes you control.

```python
from providify import Singleton

@Singleton
class UserRepository:
    def __init__(self, db: Database) -> None:   # db is injected by the container
        self._db = db
```

**Don't:** wrap a class you own in a `@Provider` factory just to construct it — that
is the #1 anti-pattern (Spring habit). `@Provider` is Jakarta's `@Produces`: a typed
factory for things you *cannot* decorate (third-party / stdlib types, runtime-chosen
implementations, values needing imperative construction).

See **[choosing-decorators.md](choosing-decorators.md)**.

---

## R2 — `@Configuration` is grouping sugar, not a config-bean dumping ground

A bare `@Provider` function discovered by `scan()` is just as valid as one inside a
`@Configuration` class. Group producers in `@Configuration` only when they share
configuration or belong together. **Jakarta CDI has no `@Configuration`** — do not
treat it as "where all beans are defined."

If one `@Configuration` module needs a type produced by another, see **R16** —
declare it with `depends_on=`, don't rely on scan order.

---

## R3 — Inject producer dependencies as parameters; never call sibling producers

```python
@Configuration
class DbModule:
    @Provider(singleton=True)
    def pool(self, cfg: AppConfig) -> ConnectionPool:
        return ConnectionPool(cfg.db_url)

    @Provider
    def repo(self, pool: ConnectionPool) -> UserRepository:   # ✅ pool injected
        return UserRepository(pool)
```

**Never** call `self.pool()` from `repo()`. That bypasses the container and re-runs
the factory, **defeating `singleton=True` caching**. Declare it as a parameter.

---

## R4 — Use `Annotated[T, InjectMeta(...)]` for any injection that needs options

This is a hard Python constraint, not a style preference:

```python
from typing import Annotated
from providify import Inject, InjectMeta

dep: Inject[MyService]                                    # ✅ no options — clean
dep: Annotated[MyService, InjectMeta(qualifier="x")]      # ✅ with options
dep: Annotated[MyService, InjectMeta(optional=True)]      # ✅ optional → None if unbound

dep: Inject[MyService, qualifier="x"]   # ❌ raises TypeError at import time
dep: Inject(MyService, qualifier="x")   # ❌ runs, but type checkers show "Unknown"
```

`Inject[T]` subscript accepts exactly **one** type argument. The same rule applies to
`Lazy[T]` (use `LazyMeta`) and `Live[T]` (use `LiveMeta`).

See **[injection-cheatsheet.md](injection-cheatsheet.md)**.

---

## R5 — Wrap a narrow-scoped dependency in `Live[T]` before injecting it into a wider scope

Injecting a `@RequestScoped` or `@SessionScoped` dependency directly into a
`@Singleton` raises `LiveInjectionRequiredError` at validation time. The fix:

```python
from providify import Singleton, Live

@Singleton
class RequestProcessor:
    def __init__(self, ctx: Live[RequestContext]) -> None:
        self._ctx = ctx                 # re-resolved per call — always the current request's instance

    def process(self) -> None:
        current = self._ctx.get()       # use .aget() in async code (see R7)
```

`Instance[T]` is the alternative when you want a programmatic handle and choose the
qualifier at call time. Both pass scope validation; plain injection does not.

---

## R6 — Match the call to the context: `get()` in sync, `aget()` in async

- Sync resolution: `container.get(T)`, `container.get_all(T)`.
- Async resolution: `await container.aget(T)`, `await container.aget_all(T)`.
- **Async providers called from a sync `get()` raise `RuntimeError`** — if any
  provider in the graph is `async def`, resolve with `aget()`.

---

## R7 — Proxy `.get()` / `.aget()` must match the surrounding context

`Live[T]`, `Lazy[T]`, and `Instance[T]` proxies have both sync and async accessors.
Inside async code call `.aget()`; in sync code call `.get()`. Mixing them fails.

---

## R8 — `validate_bindings()` runs once, on first resolution

Bindings added via `bind()` **after** the first `get()`/`aget()` are **not**
re-validated automatically. If you register late, call `container.validate_bindings()`
yourself, or (better) register everything before the first resolution.

---

## R9 — Prefer `scan()` for discovery; it auto-installs `@Configuration`

```python
container.scan("myapp", recursive=True)   # discovers @Component/@Singleton/etc.
                                          # AND auto-installs every @Configuration found
```

You do **not** need a separate `install()` after scanning (dedup is by class
identity, so scanning twice is safe). Use `install()` / `ainstall()` only for the
explicit, no-scan path.

---

## R10 — Decorate before you `register()` / `provide()`

`register(cls)` on an undecorated class raises `ClassBindingNotDecoratedError`;
`provide(fn)` on an undecorated function raises `ProviderBindingNotDecoratedError`.
`@Named` requires the keyword form `@Named(name="smtp")` — both bare `@Named` and
positional `@Named("smtp")` raise `TypeError`.

If the interface a factory produces is only known at call time — a
parameterised generic alias built inside a loop, for example — pass
`returns=` on `@Provider` or `container.provide()` instead of mutating
`fn.__annotations__["return"]` before registering:

```python
for model in (User, Order):
    def repo_factory(model=model) -> Any:
        return InMemoryRepo(model)
    container.provide(repo_factory, returns=Repository[model])
```

**Prefer a single open-generic registration** (plan 016) over the loop above
whenever the factory can take the closed type as a `type[T]` parameter — one
`provide()` call replaces the whole loop:

```python
T = TypeVar("T")

def repo_factory(entity: type[T]) -> Repository[T]:
    return InMemoryRepo(entity)

container.provide(repo_factory, returns=Repository[T])   # open — no loop
container.get(Repository[User])    # -> InMemoryRepo(User)
container.get(Repository[Order])   # -> InMemoryRepo(Order)
```

Keep the per-model loop above only when the factory CANNOT take a uniform
`type[T]` parameter — e.g. each model needs a genuinely different factory
body, not just a different type argument to the same one.

Mutating another function's `__annotations__` is an anti-pattern (see
`anti-patterns.md`): it patches a function object the caller may not own,
the ordering between decoration and the mutation is load-bearing but
invisible in either signature, and it breaks the moment anything reads
annotations at decoration time instead of registration time. `returns=`
bypasses return-annotation reading entirely, so there is nothing to patch.

---

## R11 — Manage the container's lifecycle

Use the container as a context manager so `shutdown()` (and `@PreDestroy` hooks) run:

```python
with DIContainer() as container:        # async: `async with` → ashutdown()
    container.scan("myapp")
    svc = container.get(Service)
```

`@PreDestroy` fires for cached singletons on shutdown, and for `@RequestScoped` /
`@SessionScoped` instances when their scope block exits. `DEPENDENT` instances are
never tracked, so their `@PreDestroy` never fires. `@PreDestroy` also never fires
for instances returned from a `@Provider` — that's `@Disposes`'s job, not
`@PreDestroy`'s; `container.validate()` reports `UNREACHABLE_PRE_DESTROY` (R12)
when a singleton provider produces a type with a `@PreDestroy` hook and no
`@Disposes`. `@Disposes` only attaches to providers declared in the **same**
`@Configuration`.

**Ordering guarantee**: `shutdown()` / `ashutdown()` tear down cached singletons in
**reverse creation order** — every dependent is destroyed before the dependencies it
may still reference, matching Spring/.NET/Quarkus. Creation order is a valid reverse
topological order of the graph *as actually constructed* (including runtime-only
edges like `Lazy[T]`/`Live[T]`/`Provider[T]`), so no instance is disposed before
something created after it. The same guarantee applies to `@RequestScoped` /
`@SessionScoped` teardown on scope exit. ⚠️ A singleton resolved late via
`Lazy[T]`/`Provider[T]` — after some other singleton that will go on to reference
it — is created (and therefore torn down) out of "true" dependency order; this is
inherent to reverse-creation-order tracking.

Every `@PreDestroy` hook / `@Disposes` disposer runs even if an earlier one raises,
and caches are **always** cleared, even on failure. Failures are aggregated into one
`ShutdownError` (raised after every hook has run) instead of stopping at the first
failure:

```python
from providify import ShutdownError

try:
    container.shutdown()
except ShutdownError as exc:
    for failure in exc.failures:          # list[ShutdownFailure]: .owner, .exception
        log.error("teardown failed: %s", failure.owner, exc_info=failure.exception)
```

`exc.__cause__` is chained to the exception of the earliest-*created* (most
foundational) failing component, not simply the first hook encountered during the
reversed teardown walk — a foundational dependency (e.g. a DB pool) failing to close
is typically the root cause of failures in components created after it. An async
`@PreDestroy` hook reached from sync `shutdown()` still raises `RuntimeError`
immediately (use `ashutdown()` instead) and is **not** aggregated into
`ShutdownError` — it signals the wrong shutdown method was called, not a teardown
failure.

---

## R12 — Call `container.validate()` once, after registration, before serving traffic

```python
container.scan("myapp")
container.validate()   # raises ContainerValidationError on any missing/ambiguous
                        # binding, cycle, scope leak, or unresolvable annotation
```

This is a superset of `validate_bindings()` (R8): it walks the *entire* declared
graph without instantiating anything and additionally catches missing bindings,
ambiguous bindings (two candidates tied at max priority), and static circular
dependencies — defects that only surface at first-`get()` time otherwise.
`validate_bindings()` / `validate_all()` are unchanged and still run
automatically on first `get()`/`aget()`; `validate()` is the explicit,
opt-in, whole-graph startup gate on top of them.

`validate()` also reports `IssueKind.UNREACHABLE_PRE_DESTROY` — a `WARNING`,
not an `ERROR` — for a singleton provider whose produced type carries a
`@PreDestroy` that will never run (no `@Disposes`), alongside
`IssueKind.DISPOSER_OVERWRITTEN` and `IssueKind.UNMATCHED_DISPOSER` (also
`WARNING`) for `@Disposes` wiring defects. Because these are warnings,
`validate()`'s default `raise_on_error=True` does **not** raise for them: a
strict gate must inspect `report.issues` / `report.warnings` / `report.ok`,
not just `report.errors`, to catch them.

`IssueKind.CONDITION_INACTIVE` (`Severity.INFO`) reports a `@Requires`-gated
binding whose condition currently evaluates `False` — this is the feature
working as declared, not a defect, and it is a third, even quieter tier than
`WARNING`: it never raises, is never in `report.warnings`, and never affects
`report.ok`. Read `report.infos` if you want the wiring report to explain
*why* a candidate is not the live one.

---

## R13 — `@Profile` gates activation; `validate()` sees the graph *per profile*, not the whole thing

`@Profile("prod")` / `@Profile("dev", "test")` / `@Profile("!prod")` restricts a class
or `@Provider` function to specific deployments. The active set comes from
`DIContainer(profiles=(...))`, the `PROVIDIFY_PROFILES` env var (comma-separated), or
`container.activate_profile(name)` / `deactivate_profile(name)` — an explicit
`profiles=` argument (even `()`) always beats the env var:

```python
from providify import Profile, Singleton, DIContainer

@Profile("prod")
@Singleton
class RealMailer(Mailer): ...

@Profile("dev", "test")            # OR — active in either
@Singleton
class ConsoleMailer(Mailer): ...

container = DIContainer(profiles=("dev",))
container.get(Mailer)              # -> ConsoleMailer; RealMailer is invisible
```

A binding whose profile does not match the active set is **invisible**, not merely
deprioritized — to `get()`, `get_all()`, `is_resolvable()`, **and** `validate()`.

⚠️ **The gotcha**: `container.validate()` reports `MISSING_BINDING` for an interface
whose *only* provider is gated by a profile that is not currently active — even though
the binding is registered. This is intentional (`validate()` reports the graph as it
will actually be wired under the container's *current* `active_profiles`), but it means
a single `validate()` call does not prove every deployment configuration is wired
correctly — validate once per profile combination you actually ship:

```python
for active in (("dev",), ("prod",)):
    c = DIContainer(profiles=active)
    c.scan("myapp")
    c.validate()   # each profile's graph must be independently complete
```

The same applies per env-var/condition state you ship — see `@Requires` below.

`@Profile` also composes with `@Alternative`: an `@Alternative` bean that also carries
`@Profile` no longer needs an imperative `enable_alternative()` call — the profile
itself is the activator, and it is a hard AND-gate (`enable_alternative()` cannot
override a non-matching profile).

⚠️ **Behaviour change**: `@Alternative` on a `@Provider` **function** (not a class) is
now genuinely disabled by default, matching the decorator's documented promise. Code
that previously relied on `@Alternative` doing nothing on a provider function will now
see `LookupError` until the provider is enabled (or given a matching `@Profile`).

`@Requires(condition=..., env=..., value=...)` is the third activation gate, AND'd with
`@Profile`/`@Alternative` and evaluated last, lazily, on every `get()` — never cached,
never evaluated at decoration time. Prefer it over a hand-rolled `if` inside a
`@Provider` body for anything that should show up as `IssueKind.CONDITION_INACTIVE` in
`container.validate()`'s report — note `describe()` itself carries no
profiles/alternative/conditions field, so it does not reflect activation state:

```python
from providify import Requires, Singleton

@Requires(env="FEATURE_REDIS_CACHE")
@Singleton
class RedisCache(Cache): ...
```

A raising `@Requires` predicate propagates as `ConditionEvaluationError` from every
public lookup **and** from `validate()` — it is a programming error, not a legitimate
"off" state, so it is never silently swallowed.

---

## R14 — Declare settings with `@ConfigProperties`; never hand-write a `@Provider` that reads `os.environ`

```python
from dataclasses import dataclass
from providify import ConfigProperties, EnvSource, YamlSource

@ConfigProperties(prefix="db", sources=(EnvSource(), YamlSource("config.yaml", required=False)))
@dataclass(frozen=True)
class DbSettings:
    url: str
    pool_size: int = 5

container.bind_config(DbSettings)   # …or container.scan("myapp") discovers it
container.get(DbSettings)
```

**Don't** do this instead:

```python
@Provider(singleton=True)
def make_db_settings() -> DbSettings:                       # ❌ anti-pattern
    return DbSettings(
        url=os.environ["DB_URL"],
        pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
    )
```

`@ConfigProperties` gives you multi-source merging (env + YAML/JSON/TOML,
later source wins), type coercion (or a full pydantic `model_validate`
hand-off if the target exposes it), and **aggregated** error reporting — a
config file with four typos raises one `ConfigBindingError` naming all four,
instead of a `KeyError`/`ValueError` on the first bad field. A hand-rolled
`@Provider` gets none of this for free.

⚠️ **`@ConfigProperties` is NOT `@Configuration`** (R2 above). They are
unrelated decorators with unrelated jobs: `@Configuration` is a *grouping
namespace* for `@Provider` methods (Jakarta CDI has no such concept at all);
`@ConfigProperties` is a *typed settings binder* — it marks a plain data
class (dataclass, annotated `__init__`, or pydantic `BaseModel`) as bound
from env/YAML/JSON/TOML. Putting `@Provider` methods inside a
`@ConfigProperties` class, or expecting `@Configuration` to read `os.environ`
for you, are both category errors. If a class carries both markers, the
scanner treats it as `@ConfigProperties` — this combination is
unsupported/ambiguous, not a supported dual-role class.

⚠️ **Config errors surface at the first `get()`, not at `bind_config()`.**
Binding is lazy and singleton: sources are read and fields are
coerced/validated the first time `container.get(cls)` (or `aget()`) resolves
it — a missing required env var or a malformed YAML value will not be caught
by `container.scan()` or `container.bind_config()` alone. To fail at startup
instead of on first use, call `container.warm_up()` (or `awarm_up()`), which
fully instantiates every singleton, including config bindings.
`container.validate()` (R12) does **not** catch a bad config *value* — by
design it never instantiates anything, so it can confirm a `DbSettings`
binding exists but cannot prove `DB__POOL_SIZE="not-a-number"` will fail
until something actually calls `get(DbSettings)` or `warm_up()` runs.

## R15 — Use the `di_container` / `di_overrides` fixtures; do not hand-roll `override()` + `reset_binding()` pairs

Installing providify registers a `pytest11` plugin that exposes
`di_container` / `di_overrides` / `di_global` / `di_acontainer` fixtures —
no conftest boilerplate required:

```python
def test_checkout(di_container, di_overrides):
    di_container.scan("myapp")
    di_overrides.instance(Clock, FrozenClock("2026-01-01"))   # instance, not class
    di_overrides.bind(Notifier, FakeNotifier)                 # class swap
    di_overrides.remove(PaymentGateway)                       # unregister

    assert di_container.get(Checkout).run() == "ok"
    # every override is undone automatically at teardown — no reset_binding() calls
```

**Don't** do this instead:

```python
def test_checkout(container):
    container.override(Notifier, FakeNotifier)                 # ❌ anti-pattern
    ...
    container.reset_binding(Notifier)                           # easy to forget,
    container.bind(Notifier, RealNotifier)                      # easy to get wrong
                                                                 # under a failing assert
```

A manual `override()`/`reset_binding()` pair never runs if the test fails
before reaching it — `di_overrides` is a `yield`-based fixture, so undo
always runs, exception or not.

⚠️ **Override your own `di_container` in your own conftest if you have an
app container** — the plugin cannot know how you build yours; the shipped
default is a bare `DIContainer()`:

```python
@pytest.fixture
def di_container(app_container):
    return app_container.copy()   # isolated per test, no re-scan
```

⚠️ **Instances created *during* an override window are dropped without
teardown.** `ContainerOverrides`/`di_overrides` restore **configuration**
(bindings, profiles, alternatives), not **lifecycle** — anything
instantiated after the snapshot vanishes with no `@PreDestroy`/`@Disposes`
running. The default `di_container` fixture sidesteps this by being
fresh-per-test with `shutdown()` in its own teardown; only a consumer who
points `di_container` at a long-lived app container is exposed.

`ContainerOverrides` also works without pytest at all (unittest, scripts,
a REPL):

```python
from providify import ContainerOverrides

with ContainerOverrides(container) as ov:
    ov.instance(Clock, FrozenClock(...))
```

---

## R16 — Declare cross-module ordering with `depends_on=`; never rely on scan order

If module `B`'s `__init__` or an `@Provider` method needs a type produced by module
`A`, write `@Configuration(depends_on=[A])` on `B` — **do not** rely on `scan()`'s
alphabetical `inspect.getmembers()` walk to happen to install `A` first:

```python
@Configuration
class InfraModule:
    @Provider(singleton=True)
    def pool(self) -> DatabasePool: ...

@Configuration(depends_on=[InfraModule])   # ✅ explicit — installs InfraModule first
class RepoModule:
    def __init__(self, pool: DatabasePool) -> None: ...   # resolvable
```

`container.install(RepoModule)` (or `scan()` discovering it) then transitively
installs `InfraModule` first, regardless of declaration or scan order. A cycle in
`depends_on` raises `ModuleCycleError` naming every class in the cycle — detected
**before** any module is instantiated, so a cycle leaves the container untouched
(no partial installation).

Two lifecycle guarantees that ship with `depends_on=`:
- A module's `@PostConstruct` runs once, at install time, after `__init__` and
  before its `@Provider` methods are registered.
- A module's `@PreDestroy` runs at `shutdown()`/`ashutdown()`, in **exact reverse
  install order**, strictly **after** every singleton has already been torn down —
  a module's `@PreDestroy` typically releases a resource (a pool, a client) that
  every singleton consumer of it is already gone by the time it runs.

`install()`/`ainstall()` are idempotent per container: installing the same module
twice (explicitly, or via `install()` followed by `scan()` covering it) registers
its providers exactly once — the container is the dedup authority, keyed on the
module class.

## R17 — Declare the collection point before injecting `list[T]`

A bare `list[T]` annotation only collects every registered implementation of
`T` if `T` was explicitly declared a collection point first — via
`container.multibind(T)` or `@Multibound` on `T` itself:

```python
@Multibound                             # ✅ declare first
class Handler(ABC): ...

@Component
class Dispatcher:
    def __init__(self, handlers: list[Handler]) -> None: ...   # now resolves
```

Without the declaration, `list[Handler]` is `_UNRESOLVED` like any other
unbound type — it does **not** silently collect everything. If you cannot
decorate `T` and did not call `multibind()`, use `InjectInstances[T]`
instead, which needs no declaration but keeps `LookupError` on zero matches
(a declared collection point injects `[]` instead).

## R18 — Field interceptors: only `Advised` fields are join points, and never during construction

`@AroundGet`/`@AroundSet` advice only fires for fields declared
`balance: T = Advised(...)` in the class body — there is no
`__getattribute__`/`__setattr__` override, so every other attribute is
invisible to the interceptor chain. Three more rules that trip people up:

- **No advice during construction.** Writes performed by `__init__`,
  class-var injection, and `@PostConstruct` are never advised — the chain is
  attached only after the bean is fully built. If an interceptor needs to see
  the initial value, read it explicitly after `container.get()` returns.
- **`Advised` fields are unsupported on dataclasses, frozen dataclasses,
  pydantic `BaseModel`s, and `attrs` classes** — rejected with `TypeError` at
  weave time, not silently broken. A frozen dataclass's generated `__init__`
  writes via `object.__setattr__`, which a data descriptor cannot intercept,
  so reads would silently return stale/default data instead of what was
  written — this is rejected outright rather than shipped as a footgun.
- **This is not a CDI feature.** Jakarta Interceptors 2.1 has no field-level
  interception in either profile — do not describe `Advised` as "closing a
  CDI gap" in code, comments, or commit messages. It is modelled on AspectJ's
  `get`/`set` pointcuts, implemented via Python's descriptor protocol.

## R19 — `@Fallback` for framework defaults; never `priority=-sys.maxsize - 1`

If you find yourself giving a binding an artificially low `priority=` just so
any "real" binding automatically outranks it, that is `@Fallback`, spelled
correctly:

```python
# ❌ floor-priority trick — fragile, and a second real binding at an even
# lower priority (or a tie) reintroduces the exact bug this was meant to avoid
@Singleton(priority=-sys.maxsize - 1)
class InMemoryCache(Cache): ...

# ✅ @Fallback — a candidate only when no active non-fallback binding matches
@Fallback
@Singleton
class InMemoryCache(Cache): ...
```

A request is `(interface, qualifier, priority)` — an unqualified `@Fallback`
yields to a *qualified* non-fallback sibling for an unqualified request, but a
fallback that carries its own qualifier still wins when a caller asks for
exactly that qualifier (the `qualifier="in_memory"` escape hatch keeps
working; see the README's `@Fallback` section).

Do not confuse it with `@Default`: `@Default` is a **qualifier** ("no named
qualifier"); `@Fallback` is an **activation rule** ("yield when a real
binding exists"). The names sound similar; the problems are unrelated.

A `@Fallback` singleton resolved and cached *before* its shadowing binding is
registered is **not evicted** — any dependent already holding a reference to
it keeps that reference, the same non-eviction caveat `activate_profile()`
already documents. `override()`/`reset_binding()` remain the eviction tools.
