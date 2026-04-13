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

Dependencies can be declared in two places:

- **Constructor parameters** — `def __init__(self, svc: Service) -> None` — resolved automatically
- **Class-level annotations** — `svc: Inject[Service]` on the class body — resolved after the constructor runs

Both forms support `Inject[T]`, `Live[T]`, and `Lazy[T]`.

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
Pylance / mypy see the real type directly; no special import needed.

```python
@Component
class OrderService:
    def __init__(self, db: Database) -> None:
        self.db = db
```

### Optional[T] and T | None — nullable injection

Use Python's standard `Optional[T]` or the pipe-union syntax `T | None` when
a dependency may not be registered. If no binding is found, the parameter
receives `None` instead of raising `LookupError`.

```python
from typing import Optional

@Component
class Notifier:
    def __init__(
        self,
        sms:   Optional[SmsService],    # None if SmsService is not registered
        push:  PushService | None,      # equivalent pipe-syntax form
    ) -> None:
        self._sms  = sms   # may be None at runtime
        self._push = push
```

No import from `providify` is needed — plain `Optional[T]` / `T | None` is
enough. The container detects the union at resolution time and handles the
missing-binding case automatically.

> **`Optional[T]` vs `Annotated[T, InjectMeta(optional=True)]`**: both inject
> `None` when the binding is absent, but the `Optional[T]` form is more
> idiomatic Python and works without importing `InjectMeta`.

### Union[T1, T2] — first-match injection

A `Union` with multiple non-`None` types is resolved by trying each candidate
in declaration order. The first type that has a registered binding is used;
`LookupError` is raised only if **no** candidate resolves.

```python
from typing import Union

@Component
class StorageService:
    def __init__(
        self,
        # Prefers S3Storage if registered; falls back to LocalStorage otherwise
        backend: Union[S3Storage, LocalStorage],
    ) -> None:
        self.backend = backend
```

Combining with `None` makes the whole injection optional:

```python
@Component
class AnalyticsCollector:
    def __init__(
        self,
        # Uses SegmentAnalytics if available, Mixpanel as fallback, skipped if neither
        tracker: Union[SegmentAnalytics, MixpanelAnalytics, None] = None,
    ) -> None:
        self.tracker = tracker
```

**Resolution rules:**

| Annotation | First candidate bound? | Second candidate bound? | Result |
|---|---|---|---|
| `Optional[T]` / `T \| None` | yes | — | T instance |
| `Optional[T]` / `T \| None` | no | — | `None` |
| `Union[T1, T2]` | yes | — | T1 instance |
| `Union[T1, T2]` | no | yes | T2 instance |
| `Union[T1, T2]` | no | no | raises `LookupError` |
| `Union[T1, T2, None]` | no | no | `None` |

### Inject[T] — subscript form (recommended)

Use `Inject[T]` when you want to be explicit that this parameter is managed
by the DI container. Linters and type checkers resolve `Inject[Database]`
directly to `Database`, so hover, completion, and type errors work normally.

```python
from providify import Inject

@Component
class OrderService:
    def __init__(self, db: Inject[Database]) -> None:
        self.db = db   # linter sees: db: Database ✅
```

### Annotated[T, InjectMeta(...)] — for qualifier / priority / optional (recommended)

When you need injection **options** (qualifier, priority, optional), use
`Annotated` with `InjectMeta` directly. This is the underlying form that
`Inject[T]` expands to at runtime, and it is fully valid Python — no
`# type: ignore` comment needed. Pylance hover shows the bare type `T`.

```python
from typing import Annotated
from providify import Inject, InjectMeta

@Component
class ReportService:
    def __init__(
        self,
        db:      Inject[Database],                                    # simple — no options needed
        cache:   Annotated[Cache,   InjectMeta(qualifier="redis")],   # named qualifier ✅
        metrics: Annotated[Metrics, InjectMeta(optional=True)],       # None if not bound ✅
        audit:   Annotated[AuditLog, InjectMeta(priority=1)],         # exact priority ✅
    ) -> None: ...
```

> **Why not `Inject(T, qualifier=...)`?**
> The call form `Inject(Cache, qualifier="redis")` works at runtime but is **not
> recommended** — type checkers (Pylance, mypy, pyright) flag it as invalid in
> annotation position and cannot infer the return type, so hover and completion
> show `Unknown` instead of `Cache`. Use `Annotated[T, InjectMeta(...)]` instead.
> It resolves identically and keeps the full type-checker experience intact.

### InjectInstances[T] — all bindings as a list

Inject every registered implementation of an interface, sorted by priority.
Pylance resolves `InjectInstances[Sender]` to `list[Sender]`.

```python
from providify import InjectInstances

@Component
class NotificationFanout:
    def __init__(self, senders: InjectInstances[Sender]) -> None:
        self.senders = senders   # linter sees: senders: list[Sender] ✅

    def notify(self, msg: str) -> None:
        for sender in self.senders:
            sender.send(msg)
```

For qualifier filtering on `InjectInstances`, use `Annotated` with `InjectMeta(all=True)`:

```python
from typing import Annotated
from providify import InjectMeta

@Component
class CloudFanout:
    def __init__(
        self,
        senders: Annotated[list[Sender], InjectMeta(all=True, qualifier="cloud")],
    ) -> None:
        self.senders = senders
```

### Class-level attributes

Injection annotations can be placed directly on class-level attributes instead of (or alongside) constructor parameters. They are resolved and set on the instance **after** the constructor runs, and **before** `@PostConstruct` fires — so lifecycle hooks can access them.

```python
from providify import Inject, Live, Lazy

@Singleton
class ReportService:
    # Class-level — resolved after __init__ returns
    storage: Inject[StorageBackend]
    logger:  Live[RequestLogger]    # re-resolves per request (see Live[T] below)

    # Constructor parameters still work normally alongside class-level annotations
    def __init__(self, db: Database) -> None:
        self.db = db
```

All injection forms (`Inject[T]`, `Live[T]`, `Lazy[T]`, `Instance[T]`) work as class-level annotations.
For options (`qualifier=`, `priority=`, `optional=`), use `Annotated` + the corresponding meta type:

```python
from typing import Annotated
from providify import InjectMeta, LiveMeta, LazyMeta

@Singleton
class ReportService:
    storage:  Annotated[StorageBackend, InjectMeta(qualifier="primary")]
    logger:   Annotated[RequestLogger,  LiveMeta(qualifier="request")]
    slow_svc: Annotated[HeavyService,   LazyMeta(qualifier="heavy")]
```

#### `ClassVar[...]` form

All four injection types also accept the `ClassVar[...]` wrapper, which is useful when
a type checker or style guide requires class-level attributes to be explicitly typed as
class variables:

```python
from typing import ClassVar
from providify import Instance, Live, Lazy, Inject

@Singleton
class AlertService:
    # ClassVar form — treated identically to the plain form by the container
    emailer:  ClassVar[Instance[Emailer]]
    logger:   ClassVar[Live[RequestLogger]]
    config:   ClassVar[Lazy[AppConfig]]
    storage:  ClassVar[Inject[StorageBackend]]
```

The container unwraps `ClassVar[X]` to `X` before dispatching, so resolution,
scope-violation detection, and dependency-graph construction all work identically
to the plain annotation form.

> **Constructor takes priority** — if the same name appears both as a class-level annotation and as an `__init__` parameter, the constructor value is used and the class-level annotation is skipped.

### Inheritance and MRO

The two injection paths intentionally have **different MRO behaviour**:

| Injection path | MRO walk? | Why |
|---|---|---|
| `__init__` parameters | ❌ No | The declared signature is an explicit contract. If a child overrides `__init__`, it is asserting its own construction semantics. |
| Class-level annotations | ✅ Yes | `get_type_hints(cls)` walks the full MRO — annotations declared on a parent class are inherited and injected automatically. |

**When `__init__` is *not* overridden** the parent's `__init__` is already picked up via Python's own MRO — no special handling is needed. The asymmetry only matters when the child *does* override `__init__`.

**Recommended patterns for inheritance:**

```python
# Option A — re-declare the parent dep in the child signature (explicit, zero magic)
class Base:
    def __init__(self, svc_a: Inject[ServiceA]) -> None:
        self.svc_a = svc_a

class Child(Base):
    def __init__(self, svc_a: Inject[ServiceA], svc_b: Inject[ServiceB]) -> None:
        super().__init__(svc_a)   # explicit hand-off — no surprise injections
        self.svc_b = svc_b

# Option B — use class-level annotations for inherited deps (MRO is walked)
class Base:
    svc_a: Inject[ServiceA]   # injected after construction; inherited by all subclasses

class Child(Base):
    def __init__(self, svc_b: Inject[ServiceB]) -> None:
        self.svc_b = svc_b
    # svc_a is still set on self via the class-var injection path ✓
```

> ⚠️ **Avoid injecting parent `__init__` params via MRO manually.** If the container were to merge parent and child `__init__` signatures automatically, it would conflict with any `super().__init__(arg)` call inside the child — the same instance could be resolved twice, producing two separate objects for what should be a single dep.

### Lazy[T] — deferred injection

Wraps the dependency in a `LazyProxy`. The real instance is **not resolved until `.get()` (or `.aget()`) is called for the first time**, after which the result is cached.

The primary use case is **breaking circular dependencies** — `A` can hold `Lazy[B]` while `B` holds `A` directly:

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

`Lazy` also accepts `qualifier=` and `priority=` via `Annotated` + `LazyMeta`:

```python
from typing import Annotated
from providify import LazyMeta

repo: Annotated[Cache, LazyMeta(qualifier="redis", priority=1)]
```

> ⚠️ **`Lazy[T]` is not scope-safe for `@RequestScoped` or `@SessionScoped` deps.**
> After the first `.get()` call the proxy caches the resolved instance — subsequent calls return the same (stale) object regardless of which request is active.
> Use **`Live[T]`** instead when a longer-lived component needs a scoped dep.

### Live[T] — always-fresh injection

Returns a `LiveProxy` that calls `container.get(T)` on **every `.get()` or `.aget()` invocation** — it never caches. The correct choice when a longer-lived component (`@Singleton`, `@SessionScoped`) holds a `@RequestScoped` or `@SessionScoped` dependency.

```python
from providify import Live

@Singleton
class AuthService:
    def __init__(self, token: Live[JsonWebToken]) -> None:
        self._token = token   # LiveProxy — not the token itself

    def get_user_id(self) -> str:
        # Re-resolves from the active request scope on every call
        return self._token.get().subject

    async def get_user_id_async(self) -> str:
        token = await self._token.aget()
        return token.subject
```

Works as a class-level annotation too:

```python
@Singleton
class AuthService:
    token: Live[JsonWebToken]   # set after construction, re-resolves per request
```

`Live` also accepts `qualifier=` and `priority=` via `Annotated` + `LiveMeta`:

```python
from typing import Annotated
from providify import LiveMeta

token: Annotated[JsonWebToken, LiveMeta(qualifier="bearer")]
```

**`Lazy[T]` vs `Live[T]` at a glance:**

| | `Lazy[T]` | `Live[T]` |
|---|---|---|
| First `.get()` | Resolves and **caches** | Resolves (no cache) |
| Subsequent `.get()` | Returns **cached** instance | Re-resolves every time |
| Circular deps | ✅ Breaks A→B→A cycles | ❌ Does not help |
| Scoped deps in singletons | ❌ Stale after first access | ✅ Always fresh |

---

## Scope contexts

`@RequestScoped` and `@SessionScoped` bindings require an active scope context.

```python
# Sync request scope
with container.request():
    svc = container.get(RequestLogger)   # same instance within this block

# Async request scope
async with container.arequest():
    svc = await container.aget(RequestLogger)

# Session scope — provide a stable ID to share state across multiple requests
with container.session("user-abc"):
    profile = container.get(UserProfile)

# Resume the same session later
with container.session("user-abc"):
    profile = container.get(UserProfile)   # same cached instance

# Destroy a session on logout
container.invalidate_session("user-abc")

# scope_context property — still available for advanced use or direct cache access
with container.scope_context.request():   # equivalent to container.request()
    ...
```

> Resolving a `@RequestScoped` or `@SessionScoped` binding outside an active context
> raises `RuntimeError` immediately.

---

## Scope safety

The container detects scope leaks at `validate_bindings()` time (triggered by the first `get()` call) and raises before any instance is created.

A **scope leak** occurs when a longer-lived component holds a direct reference to a shorter-lived one, causing it to silently serve a stale instance across scope boundaries.

### LiveInjectionRequiredError

Raised when a `@Singleton` (or `@SessionScoped`) injects a `@RequestScoped` or `@SessionScoped` dep via `Inject[T]`, `Lazy[T]`, or a bare type annotation — all of which capture one instance at construction time:

```python
@Singleton
class Bad:
    def __init__(self, ctx: RequestContext) -> None:  # ❌ captured once, stale forever
        self.ctx = ctx
```

Fix: wrap with `Live[T]` so the dep is re-resolved on every access:

```python
@Singleton
class Good:
    def __init__(self, ctx: Live[RequestContext]) -> None:  # ✅ re-resolves per request
        self._ctx = ctx
```

Scope safety is checked for **both** constructor parameters and class-level annotations:

```python
@Singleton
class AlsoDetected:
    ctx: Inject[RequestContext]   # ❌ also caught — same rule applies to class-level attrs
```

### ScopeViolationDetectedError

Raised for other scope leaks — e.g. a `@Singleton` holding a `@Component` (DEPENDENT) dep directly. This is less critical but still signals a design issue: the singleton pins one `@Component` instance for its entire lifetime instead of getting a fresh one.

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

### Auto-scan at construction time

Pass `scan=` to the `DIContainer` constructor to scan one or more modules
**immediately when the container is created** — before any `bind()` / `get()` call.
This keeps the bootstrap code declarative and ensures every decorated class is
registered before any other code interacts with the container.

```python
from providify import DIContainer

# Single package — all sub-packages walked recursively (default)
container = DIContainer(scan="myapp")

# Explicit list — both packages scanned, left-to-right
container = DIContainer(scan=["myapp.services", "myapp.repositories"])

# Opt out of recursive walking
container = DIContainer(scan="myapp.services", recursive=False)
```

| Parameter | Type | Default | Meaning |
|-----------|------|---------|---------|
| `scan` | `str \| list[str] \| None` | `None` | Module(s) to scan at construction. `None` = no auto-scan (backward-compatible). |
| `recursive` | `bool` | `True` | Walk sub-packages recursively. Forwarded to each `scan()` call. |

> **`ModuleNotFoundError`** is raised at construction time if a module name
> cannot be imported — errors surface at the point of misconfiguration rather
> than at the first `get()` call.

The constructor `scan=` is purely additive — you can still call `container.scan()`
manually afterward to register additional modules.

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

### Resolving by qualifier and priority

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

## Generic types

The container resolves parameterised generic types — bind and get `Repository[User]`
as a distinct interface from `Repository[Post]`.

```python
from typing import Generic, TypeVar
from abc import ABC, abstractmethod
from providify import Component

T = TypeVar("T")

class Repository(ABC, Generic[T]):
    @abstractmethod
    def find(self, id: int) -> T: ...

@Component
class UserRepository(Repository[User]):
    def find(self, id: int) -> User: ...

@Component
class PostRepository(Repository[Post]):
    def find(self, id: int) -> Post: ...

container.bind(Repository[User], UserRepository)
container.bind(Repository[Post], PostRepository)

user_repo = container.get(Repository[User])   # UserRepository
post_repo = container.get(Repository[Post])   # PostRepository
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
| `test_scopes.py` | SINGLETON, DEPENDENT, REQUEST, SESSION, scope violation detection, class-level attr scope safety |
| `test_inject.py` | `Inject[T]`, `InjectInstances[T]`, `optional=True/False`, `Optional[T]` / `T \| None` / `Union[T1, T2]`, class-level attribute injection |
| `test_lazy.py` | `LazyProxy` unit tests, `Lazy[T]` injection, circular-via-lazy |
| `test_live.py` | `LiveProxy` unit tests, `Live[T]` injection, always-fresh resolution |
| `test_instance.py` | `InstanceProxy` unit tests, `Instance[T]` injection, `is_resolvable()`, scope-safety, async |
| `test_lifecycle.py` | `@PostConstruct`, `@PreDestroy`, `shutdown`, `ashutdown` |
| `test_async.py` | `aget`, `aget_all`, async providers, async context manager |
| `test_configuration.py` | `@Configuration`, `install()`, `ainstall()`, Spring-style injection |
| `test_circular.py` | `CircularDependencyError`, diamond dependency, `Lazy` cycle-break |
| `test_generics.py` | Generic[T] binding and resolution, parameterised interfaces |
| `test_scoped_providers.py` | `@Provider(scope=Scope.REQUEST/SESSION)` — factory result cached per scope |
| `test_warmup.py` | `warm_up()`, `awarm_up()`, all-or-nothing guard, qualifier/priority filter |
| `test_decorators.py` | `@Named`, `@Priority`, `@Inheritable`, stacking, error paths |
| `test_scanner.py` | `scan()`, recursive scan, ABC auto-binding, idempotency, `DIContainer(scan=...)` constructor |
| `test_describe.py` | `BindingDescriptor`, `ClassBinding.describe()`, ASCII tree output |
| `test_localns_cache.py` | `_build_localns()` caching and invalidation on `bind`/`register`/`provide` |

---

## Instance[T] — programmatic lookup handle

`Instance[T]` is the Jakarta CDI-inspired alternative when you need full
programmatic control over resolution at call time. Unlike `Inject[T]` (resolves
once, eager) or `InjectInstances[T]` (resolves all, eager), an `Instance[T]`
injects an `InstanceProxy` that defers every lookup to the call site — and
accepts qualifier / priority as **call-time arguments**, not annotation-time
metadata.

```python
from providify import Instance

@Singleton
class NotificationRouter:
    def __init__(self, senders: Instance[Sender]) -> None:
        self._senders = senders   # InstanceProxy — nothing resolved yet

    def route(self, msg: str, channel: str) -> None:
        # Qualifier chosen at runtime — same proxy, different filter each call
        sender = self._senders.get(qualifier=channel)
        sender.send(msg)

    def broadcast(self, msg: str) -> None:
        for sender in self._senders.get_all():
            sender.send(msg)

    def has_channel(self, channel: str) -> bool:
        # Side-effect-free check — never creates an instance
        return self._senders.resolvable(qualifier=channel)
```

### InstanceProxy methods

| Method | Description |
|--------|-------------|
| `.get(qualifier=None, priority=None)` | Resolve highest-priority match (sync) |
| `.get_all(qualifier=None)` | Resolve all matches sorted by priority (sync) |
| `.aget(qualifier=None, priority=None)` | Same as `.get()`, async |
| `.aget_all(qualifier=None)` | Same as `.get_all()`, async |
| `.resolvable(qualifier=None, priority=None)` | `True` if at least one binding matches — no instance created |

`get_all()` and `aget_all()` return `[]` (never raise) when no bindings match,
making them safe for the "zero or more" pattern.

### Scope safety

`Instance[T]` **always passes scope validation** — even `Instance[RequestScoped]`
inside a `@Singleton`. Because resolution is deferred to call time, the proxy
naturally fetches the current request's instance on each `.get()` call without
requiring an explicit `Live[T]` wrapper.

This exemption applies equally to the `ClassVar[Instance[T]]` form.

```python
@Singleton
class AuthGateway:
    # ✅ No LiveInjectionRequiredError — Instance[T] is inherently scope-safe
    def __init__(self, token: Instance[JwtToken]) -> None:
        self._token = token

    def verify(self) -> bool:
        return self._token.get().is_valid()   # re-resolves per request automatically
```

### `Lazy[T]` vs `Live[T]` vs `Instance[T]`

| | `Lazy[T]` | `Live[T]` | `Instance[T]` |
|---|---|---|---|
| Resolution time | First `.get()` call | Every `.get()` call | Every `.get()` call |
| Caches result | ✅ Yes | ❌ No | ❌ No |
| Qualifier at call time | ❌ Fixed at annotation | ❌ Fixed at annotation | ✅ Chosen per call |
| Breaks circular deps | ✅ Yes | ❌ No | ❌ No |
| Scope-safe in singletons | ❌ Stale after first access | ✅ Yes | ✅ Yes |
| `resolvable()` check | ❌ No | ❌ No | ✅ Yes |

---

## container.is_resolvable()

Check whether a type can be resolved **without creating any instances**:

```python
if container.is_resolvable(Notifier, qualifier="sms"):
    sms = container.get(Notifier, qualifier="sms")

# Reflects live binding state — re-evaluated on every call
container.bind(Notifier, SmsNotifier)
assert container.is_resolvable(Notifier) is True
```

---

## container.set_scoped()

Register a pre-built instance into the **currently active** scope cache.
Useful when an instance is created outside the container (e.g. deserialized
from a session cookie) and should be returned for subsequent `get()` calls
within the same scope block.

```python
with container.request():
    token = JwtToken.decode(raw_header)
    container.set_scoped(JwtToken, token)   # register into request cache

    # All code inside this block that resolves JwtToken gets this instance
    svc = container.get(AuthService)        # AuthService.token == token ✅
```

- Calling `set_scoped()` outside an active scope raises `RuntimeError` immediately.
- Calling it twice with the same type overwrites the cache entry.
- Works inside both `request()` and `session()` blocks.

---

## Scope reference

| Decorator | Lifetime |
|-----------|----------|
| `@Component` | New instance on every `get()` |
| `@Singleton` | One instance per container — shared for the container's lifetime |
| `@RequestScoped` | One instance per `container.request()` block |
| `@SessionScoped` | One instance per `container.session(id)` — survives across requests |
