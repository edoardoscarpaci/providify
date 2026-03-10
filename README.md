# providify

A Python dependency injection container inspired by Jakarta CDI and Spring.
Supports sync and async resolution, multiple scopes, lifecycle hooks, and configuration modules.

---

## Installation

```bash
poetry install
```

Requires Python 3.12+.

---

## Quick start

```python
from providify import DIContainer, Component, Singleton

class Notifier:
    def send(self, msg: str) -> None: ...

@Component
class EmailNotifier(Notifier):
    def send(self, msg: str) -> None:
        print(f"email: {msg}")

@Singleton
class AlertService:
    def __init__(self, notifier: Notifier) -> None:
        self._notifier = notifier   # injected automatically

    def alert(self, msg: str) -> None:
        self._notifier.send(msg)

container = DIContainer()
container.bind(Notifier, EmailNotifier)
container.register(AlertService)

svc = container.get(AlertService)
svc.alert("hello")   # -> email: hello
```

---

## Core concepts

The container operates in two phases:

1. **Registration** — declare bindings via `bind()`, `register()`, `provide()`, `scan()`, or `install()`
2. **Resolution** — the first `get()` / `aget()` call validates all bindings, then resolves them

Constructor parameters are injected automatically when a matching binding exists.
No annotation is needed for plain type hints — the container inspects `__init__` at resolution time.

---

## Scope decorators

Mark a class so the container knows how to manage its lifetime.

```python
from providify import Component, Singleton, RequestScoped, SessionScoped

@Component        # new instance on every resolution (default)
class EmailSender: ...

@Singleton        # one instance for the lifetime of the container
class Database: ...

@RequestScoped    # one instance per active request context
class RequestLogger: ...

@SessionScoped    # one instance per active session context
class UserSession: ...
```

All scope decorators accept optional keyword arguments:

```python
@Singleton(qualifier="primary", priority=1, inherited=True)
class PrimaryDB(Database): ...
```

| Argument | Type | Meaning |
|----------|------|---------|
| `qualifier` | `str` | Named qualifier — used to distinguish multiple bindings of the same type |
| `priority` | `int` | Lower number wins when multiple candidates match (default `0`) |
| `inherited` | `bool` | Subclasses inherit this metadata via MRO walk (default `False`) |

---

## @Provider

Register a factory function instead of a class. The return type determines the resolved interface.

```python
from providify import Provider

@Provider
def make_sender() -> EmailSender:
    return EmailSender(host="smtp.example.com")

# singleton=True caches the result — provider called only once
@Provider(singleton=True)
def make_db() -> Database:
    return Database(url=os.environ["DB_URL"])

# async providers are supported — resolve with aget()
@Provider(singleton=True)
async def make_pool() -> ConnectionPool:
    pool = ConnectionPool()
    await pool.connect()
    return pool
```

Providers also accept `qualifier=` and `priority=`.

---

## Container API

```python
from providify import DIContainer

container = DIContainer()

# ── Registration ──────────────────────────────────────────────────
container.bind(Interface, ConcreteClass)   # bind interface -> implementation
container.register(ConcreteClass)          # self-bind: interface == implementation
container.provide(factory_fn)              # register a @Provider function
container.scan("myapp.services")           # auto-discover decorated classes in a module
container.install(MyModule)                # install a @Configuration module (see below)

# ── Sync resolution ───────────────────────────────────────────────
svc  = container.get(Service)
svc  = container.get(Service, qualifier="primary")
svc  = container.get(Service, priority=1)
svcs = container.get_all(Service)          # all matching bindings, sorted by priority

# ── Async resolution ──────────────────────────────────────────────
svc  = await container.aget(Service)
svc  = await container.aget(Service, qualifier="primary")
svcs = await container.aget_all(Service)

# ── Global singleton ──────────────────────────────────────────────
container = DIContainer.current()          # sync — thread-safe
container = await DIContainer.acurrent()   # async — never blocks the event loop
DIContainer.reset()                        # wipe global (useful in tests)

# ── Scoped global — swap in a fresh container for one block ───────
with DIContainer.scoped() as c:
    c.bind(...)
    c.get(Service)
# original global is restored on exit, even if an exception is raised

async with DIContainer.scoped() as c:
    await c.aget(Service)

# ── Instance lifecycle ────────────────────────────────────────────
with container:                   # calls shutdown() on __exit__
    ...

async with container:             # calls ashutdown() on __aexit__
    ...
```

---

## Injection types

### Plain type annotation

The simplest case — annotate the parameter with the type to inject.

```python
@Component
class OrderService:
    def __init__(self, db: Database) -> None:
        self.db = db
```

### Inject[T] — with options

Use `Inject[T]` when you need a qualifier, exact priority, or optional injection.

```python
from providify import Inject

@Component
class ReportService:
    def __init__(
        self,
        db:      Inject[Database],
        cache:   Inject(Cache, qualifier="redis"),
        metrics: Inject(Metrics, optional=True),   # None if nothing is bound
        audit:   Inject(AuditLog, priority=1),
    ) -> None: ...
```

### InjectInstances[T] — all bindings as a list

Inject every registered implementation of an interface, sorted by priority.

```python
from providify import InjectInstances

@Component
class NotificationFanout:
    def __init__(self, senders: InjectInstances[Sender]) -> None:
        self.senders = senders   # list[Sender]

    def notify(self, msg: str) -> None:
        for sender in self.senders:
            sender.send(msg)
```

### Lazy[T] — deferred injection

Wraps the dependency in a `LazyProxy`. The real instance is not resolved until
`.get()` (or `.aget()`) is called for the first time. Useful for two things:

1. **Breaking circular dependencies** — `A` can hold `Lazy[B]` while `B` holds `A` directly
2. **Scope-safe singletons** — a `@Singleton` holding `Lazy[T]` for a `@RequestScoped` dep will
   re-resolve on every `.get()` call instead of caching a stale request instance

```python
from providify import Lazy

@Singleton
class ReportService:
    def __init__(self, repo: Lazy[ReportRepository]) -> None:
        self._repo = repo   # proxy — ReportRepository not resolved yet

    def run(self) -> Report:
        return self._repo.get().fetch_all()   # resolved here on first call

# Async resolution
async def run_async(self) -> Report:
    repo = await self._repo.aget()
    return await repo.fetch_all_async()
```

`Lazy` also accepts `qualifier=` and `priority=`:

```python
Lazy(Cache, qualifier="redis", priority=1)
```

---

## Scope contexts

`@RequestScoped` and `@SessionScoped` bindings require an active scope context.

```python
# Sync request scope
with container.scope_context.request():
    svc = container.get(RequestLogger)   # same instance within this block

# Async request scope
async with container.scope_context.arequest():
    svc = await container.aget(RequestLogger)

# Session scope — provide a stable ID to share state across multiple requests
with container.scope_context.session("user-abc") as sid:
    profile = container.get(UserProfile)

# Resume the same session later
with container.scope_context.session("user-abc"):
    profile = container.get(UserProfile)   # same cached instance

# Destroy a session on logout
container.scope_context.invalidate_session("user-abc")
```

> Resolving a `@RequestScoped` or `@SessionScoped` binding outside an active context
> raises `RuntimeError` immediately.

---

## Lifecycle hooks

### @PostConstruct

Called by the container immediately after the instance is constructed and all
dependencies are injected. Both sync and async forms are supported.

```python
from providify import PostConstruct

@Singleton
class SearchIndex:
    @PostConstruct
    def build(self) -> None:
        self._load_from_disk()

    # Async — must resolve with aget()
    @PostConstruct
    async def async_build(self) -> None:
        await self._fetch_from_s3()
```

### @PreDestroy

Called during `shutdown()` / `ashutdown()` for every **cached singleton** instance.
DEPENDENT instances are not owned by the container and are never destroyed this way.

```python
from providify import PreDestroy

@Singleton
class ConnectionPool:
    @PreDestroy
    def close(self) -> None:
        self._pool.close()

    # Async — use ashutdown() to invoke
    @PreDestroy
    async def async_close(self) -> None:
        await self._pool.aclose()
```

### Shutdown

```python
container.shutdown()         # calls @PreDestroy on all cached singletons, clears caches
await container.ashutdown()  # async — awaits async @PreDestroy hooks
```

Calling `shutdown()` when any cached singleton has an `async @PreDestroy` raises `RuntimeError` —
use `ashutdown()` in that case.

---

## @Configuration modules

Group related `@Provider` methods in a single class.
**Spring-style**: the module's own `__init__` parameters are injected by the container at `install()` time,
so providers can share config or other injected collaborators via `self`.

```python
from providify import Configuration
from providify.decorator.scope import Provider, Singleton

@Singleton
class AppConfig:
    db_url  = "postgresql://localhost/mydb"
    pool_size = 10

@Configuration
class DatabaseModule:
    def __init__(self, config: AppConfig) -> None:
        self._config = config   # injected at install() time

    @Provider(singleton=True)
    def connection_pool(self) -> ConnectionPool:
        return ConnectionPool(self._config.db_url, size=self._config.pool_size)

    @Provider
    def user_repo(self) -> UserRepository:
        return UserRepository(self._connection_pool())

container.register(AppConfig)
container.install(DatabaseModule)         # sync
await container.ainstall(DatabaseModule)  # async — use when module deps need aget()
```

All `@Provider` options (`qualifier=`, `priority=`, `singleton=`) work normally inside modules.

---

## Autodiscovery — `scan()`

`scan()` inspects a module (or an entire package tree) and automatically registers every
class and function that carries a scope decorator or `@Provider` — no manual `bind()` /
`register()` / `provide()` call needed.

```python
container = DIContainer()

# Scan a single module by dotted name
container.scan("myapp.services")

# Scan a whole package and every sub-package inside it
container.scan("myapp", recursive=True)

# Pass an already-imported module object instead of a string
import myapp.repositories
container.scan(myapp.repositories)
```

### What gets discovered

| Decorator on the member | What the scanner registers |
|-------------------------|---------------------------|
| `@Component` / `@Singleton` / `@RequestScoped` / `@SessionScoped` | The class, bound to every abstract base class it implements; self-bound if it has none |
| `@Provider` function | The function, equivalent to calling `container.provide(fn)` |
| `@Configuration` class | **Not** picked up by `scan()` — use `container.install()` instead |

### Abstract base class auto-binding

When a scanned class implements one or more abstract base classes (ABCs), the scanner
automatically binds each ABC to the concrete class. You can then resolve by the interface
without writing any `bind()` call yourself.

```python
from abc import ABC, abstractmethod
from providify import Component

class IRepository(ABC):
    @abstractmethod
    def find_all(self) -> list: ...

@Component
class SqlRepository(IRepository):
    def find_all(self) -> list:
        return []

container.scan("myapp.repositories")
# Equivalent to: container.bind(IRepository, SqlRepository)

repo = container.get(IRepository)   # SqlRepository is resolved
```

### What the scanner skips

- **Private members** — anything whose name starts with `_`
- **Re-exports** — symbols imported *into* the scanned module from somewhere else;
  only members *defined* in that module are registered (prevents duplicate bindings)
- **Plain classes** — classes without a scope decorator are silently ignored

### Idempotency

Calling `scan()` multiple times on the same module is safe — the scanner checks for
existing bindings before registering and skips any class or provider that is already
registered.

```python
container.scan("myapp.services")
container.scan("myapp.services")   # no-op — bindings already present
```

### Recursive scanning

Pass `recursive=True` to discover every sub-package automatically. Sub-modules that
fail to import are logged as warnings and skipped rather than halting the entire scan.

```python
# Registers decorated members from myapp, myapp.services,
# myapp.repositories, myapp.utils, and so on
container.scan("myapp", recursive=True)
```

---

## Named qualifiers and priority

### @Named and @Priority decorators

Qualifiers and priorities can be applied inline via the scope decorator or as
separate `@Named` / `@Priority` modifiers on top of any scope decorator.

```python
from providify import Named, Priority

# Inline form — shorter, good for simple cases
@Singleton(qualifier="primary", priority=1)
class PrimaryDB(Database): ...

# Modifier form — useful when the qualifier or priority is a separate concern
@Singleton
@Named(name="replica")
@Priority(priority=2)
class ReplicaDB(Database): ...
```

`@Named` requires keyword argument `name=` — bare `@Named` raises `TypeError` immediately.

Both modifiers work on `@Provider` functions too:

```python
@Provider(singleton=True)
@Named(name="readonly")
@Priority(priority=5)
def make_replica() -> Database:
    return ReplicaDB(url=os.environ["REPLICA_URL"])
```

---

## Warm-up — eager singleton instantiation

By default singletons are created lazily on the first `get()` call.
Call `warm_up()` to pre-create them at startup so the first real request
doesn't pay the construction cost.

```python
# Sync — raises RuntimeError if any singleton has an async provider
container.warm_up()
container.warm_up(qualifier="db")    # only bindings with qualifier="db"
container.warm_up(priority=0)        # only bindings with priority=0

# Async — handles both sync and async singleton providers
await container.awarm_up()
await container.awarm_up(qualifier="db")
```

`warm_up()` is all-or-nothing: if any matching singleton is backed by an async
provider it raises **before** touching the cache, so the cache is never left
partially warmed.  Use `awarm_up()` when you have async providers.

---

## Named qualifiers and priority (resolution)

```python
@Singleton(qualifier="primary")
class PrimaryDB(Database): ...

@Singleton(qualifier="replica", priority=1)
class ReplicaDB(Database): ...

# Resolve by name
db = container.get(Database, qualifier="primary")

# Resolve all, sorted by priority (lowest number first)
all_dbs = container.get_all(Database)
```

---

## Circular dependency detection

The container detects circular dependencies at resolution time and raises
`CircularDependencyError` with a readable chain:

```
CircularDependencyError: Circular dependency detected: OrderService -> PaymentService -> OrderService
```

To break a cycle intentionally, use `Lazy[T]`:

```python
@Component
class A:
    def __init__(self, b: Lazy[B]) -> None:
        self._b = b   # proxy — B is not resolved during A's construction

@Component
class B:
    def __init__(self, a: A) -> None:
        self.a = a    # A is fully constructed here — no cycle
```

---

## Running tests

```bash
cd tests
poetry install
poetry run pytest
```

Tests are organised by feature — one file per subsystem:

| File | Covers |
|------|--------|
| `test_binding.py` | `ClassBinding`, `ProviderBinding` construction and errors |
| `test_container.py` | `bind`, `register`, `provide`, `get`, `get_all`, `current`, `scoped` |
| `test_scopes.py` | SINGLETON, DEPENDENT, REQUEST, SESSION, scope violation detection |
| `test_inject.py` | `Inject[T]`, `InjectInstances[T]`, `optional=True/False` |
| `test_lazy.py` | `LazyProxy` unit tests, `Lazy[T]` injection, circular-via-lazy |
| `test_lifecycle.py` | `@PostConstruct`, `@PreDestroy`, `shutdown`, `ashutdown` |
| `test_async.py` | `aget`, `aget_all`, async providers, async context manager |
| `test_configuration.py` | `@Configuration`, `install()`, `ainstall()`, Spring-style injection |
| `test_circular.py` | `CircularDependencyError`, diamond dependency, `Lazy` cycle-break |
| `test_warmup.py` | `warm_up()`, `awarm_up()`, all-or-nothing guard, qualifier/priority filter |
| `test_decorators.py` | `@Named`, `@Priority`, `@Inheritable`, stacking, error paths |

---

## Scope reference

| Decorator | Lifetime |
|-----------|----------|
| `@Component` | New instance on every `get()` |
| `@Singleton` | One instance per container — shared for the container's lifetime |
| `@RequestScoped` | One instance per `scope_context.request()` block |
| `@SessionScoped` | One instance per `scope_context.session(id)` — survives across requests |
