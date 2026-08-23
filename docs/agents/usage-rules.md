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
never tracked, so their `@PreDestroy` never fires.
