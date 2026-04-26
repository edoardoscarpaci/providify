# Providify — Project Skill Reference

> Portable memory document. Import this into any AI assistant to get full context on the Providify codebase without re-exploration.

---

## 1. What Is Providify?

**Providify** is a zero-dependency Python dependency injection (DI) container library inspired by Jakarta CDI and Spring Framework. It automates constructor injection via type hints, manages component lifecycles across multiple scopes, and supports both synchronous and asynchronous resolution patterns.

- **Version:** 0.1.7 (dev — see pyproject.toml for current tag)
- **Python:** 3.12+
- **License:** Apache-2.0
- **Dependencies:** None (stdlib only)
- **Repository:** https://github.com/edoardoscarpaci/providify

---

## 2. Core Mental Model

### Two-Phase Operation

```
Phase 1 — Registration:  bind() / register() / provide() / scan() / install()
Phase 2 — Resolution:    get() / aget() / get_all() / aget_all()
```

On the **first** resolution call, `validate_bindings()` fires automatically. After that, the container enters a "live" state where instances are created and cached per scope.

### Key Design Decisions

| Decision | Why |
|---|---|
| Metadata stored in `cls.__dict__` | Picklable, GC-safe, multiprocess-safe (avoids `WeakKeyDictionary` fragility) |
| `ContextVar` for resolution stack and scope IDs | Each `asyncio.Task` gets its own isolated context — no state bleed across concurrent coroutines |
| `threading.Lock` + `asyncio.Lock` for caches | Dual locking supports both sync and async callers of the same container |
| `Annotated[T, InjectMeta(...)]` for injection hints | Carries qualifier/priority metadata without changing the visible type `T` |
| Abstract `Binding` base class | `ClassBinding` vs `ProviderBinding` are pluggable strategies — new binding types can be added without touching the container |
| `Live[T]` proxy re-resolves on every call | Safely injects narrow-scoped (REQUEST/SESSION) deps into wide-scoped (SINGLETON) components |

---

## 3. Directory Map

```
providify/
├── __init__.py          — Public API surface (exports everything users need)
├── container.py         — DIContainer, ScopeContext, _ScopedContainer (core orchestration)
├── binding.py           — Binding ABC, ClassBinding, ProviderBinding (creation strategies)
├── metadata.py          — DIMetadata, ProviderMetadata, Scope enum, accessors
├── scope.py             — ScopeContext: request/session instance caching via ContextVar
├── resolution.py        — _resolution_stack (ContextVar), cycle detection, _UNRESOLVED sentinel
├── type.py              — Inject, InjectInstances, Lazy, Live aliases; proxies and metadata classes; _unwrap_classvar()
├── utils.py             — Generic type utilities: _type_name, _is_generic_subtype, _interface_matches
├── descriptor.py        — BindingDescriptor, DIContainerDescriptor (serializable snapshots / ASCII trees)
├── exceptions.py        — All custom exceptions
└── decorator/
    ├── scope.py         — @Component, @Singleton, @RequestScoped, @SessionScoped, @Provider, @Named, @Priority, @Inheritable
    ├── lifecycle.py     — @PostConstruct, @PreDestroy
    └── module.py        — @Configuration
```

---

## 4. Public API Reference

### Container Setup

```python
from providify import DIContainer

container = DIContainer()

container.bind(Interface, Implementation)       # explicit interface → implementation
container.register(ConcreteClass)               # self-bind a concrete class
container.provide(provider_fn)                  # register a @Provider factory function
container.scan("my.module", recursive=True)     # auto-discover decorated members
container.install(MyConfigModule)               # install a @Configuration class (sync)
await container.ainstall(MyConfigModule)        # install async

# ── Mutation (useful in tests) ────────────────────────────────────
container.override(Interface, MockImpl)         # replace all bindings for Interface in-place; evicts singleton cache
container.reset_binding(Interface)              # remove all bindings for Interface; returns count removed

# ── Introspection (no instantiation) ──────────────────────────────
binding  = container.get_binding(Interface)     # returns best-match AnyBinding; raises LookupError if absent
bindings = container.get_all_bindings(Interface) # returns list[AnyBinding]; [] if none registered
```

### Container as Context Manager

```python
# Sync — calls shutdown() on exit
with DIContainer() as container:
    container.register(MyService)
    svc = container.get(MyService)

# Async — calls ashutdown() on exit
async with DIContainer() as container:
    container.register(MyService)
    svc = await container.aget(MyService)
```

### Resolution

```python
svc = container.get(Service)                            # single instance (sync) — highest priority wins
svc = container.get(Service, qualifier="email")         # with qualifier
svc = container.get(Service, priority=1)                # with priority override
svcs = container.get_all(Service)                       # all matching, sorted by priority value ascending (highest-priority last)

svc = await container.aget(Service)                     # single instance (async)
svcs = await container.aget_all(Service)                # all matching (async)

# Side-effect-free check — never creates an instance
ok = container.is_resolvable(Service, qualifier="email")
```

### Lifecycle Management

```python
container.warm_up()                                     # pre-create all singletons (sync)
container.warm_up(qualifier="x", priority=1)            # filtered warm-up
await container.awarm_up()                              # pre-create all singletons (async)
container.shutdown()                                    # destroy singletons, call @PreDestroy hooks
await container.ashutdown()                             # async shutdown
```

### Global Container Pattern

```python
# Singleton accessor — creates on first call
container = DIContainer.current()
container = await DIContainer.acurrent()

# Reset the global container
DIContainer.reset()

# Temporarily replace the global container (useful for tests)
with DIContainer.scoped() as c:
    c.register(MockService)
    result = c.get(Service)
# original global is restored here
```

### Scope Contexts

Scope methods are available both directly on the container and via `container.scope_context`:

```python
# Request scope — new instance per request block
with container.request():
    svc = container.get(RequestScoped)

async with container.arequest():
    svc = await container.aget(RequestScoped)

# Session scope — instance survives multiple requests for same session ID
with container.session("user-abc"):
    svc = container.get(SessionScoped)

async with container.asession("user-abc"):
    svc = await container.aget(SessionScoped)

container.invalidate_session("user-abc")    # destroy session cache

# Register a pre-built instance into the active scope
container.set_scoped(MyService, my_instance)

# Also accessible via scope_context property (older API, still works)
with container.scope_context.request():
    ...
container.scope_context.invalidate_session("user-abc")
```

---

## 5. Decorators Reference

### Scope Decorators

| Decorator | Scope | Instance lifetime |
|---|---|---|
| `@Component` | `DEPENDENT` | New instance on every `get()` call |
| `@Singleton` | `SINGLETON` | One instance for the container's lifetime |
| `@RequestScoped` | `REQUEST` | One instance per `request()` block |
| `@SessionScoped` | `SESSION` | One instance per `session(id)` block |

All scope decorators accept optional inline arguments:

```python
from providify import Component, Singleton, RequestScoped, SessionScoped

# Plain usage
@Singleton
class DatabasePool:
    pass

# With inline qualifier, priority, and/or inherited flag
@Singleton(qualifier="primary", priority=10, inherited=True)
class PrimaryDatabase:
    pass

@RequestScoped(qualifier="audit")
class AuditContext:
    pass
```

### Qualifier and Priority

Can be set inline on the scope decorator **or** via separate modifier decorators:

```python
from providify import Singleton, Named, Priority

# Option A — inline (preferred)
@Singleton(qualifier="smtp", priority=10)
class SmtpMailer:
    pass

# Option B — stacked decorators
@Singleton
@Named(name="smtp")    # name= is required — bare @Named("smtp") raises TypeError
@Priority(10)
class SmtpMailer:
    pass

# Resolved with:
mailer = container.get(Mailer, qualifier="smtp")
```

### Inheritable

```python
from providify import Singleton, Inheritable

@Singleton
@Inheritable
class BaseRepository:
    pass

# Subclasses automatically inherit the SINGLETON scope
class UserRepository(BaseRepository):
    pass
```

### Provider Functions

```python
from providify import Provider

@Provider
def create_db_pool(config: Inject[Config]) -> DatabasePool:
    return DatabasePool(config.dsn)

@Provider(singleton=True)
async def create_cache(config: Inject[Config]) -> Redis:
    return await Redis.connect(config.redis_url)

# Full signature:
@Provider(qualifier=None, priority=0, singleton=False, scope=None)
def my_factory(...) -> MyType: ...
```

### Lifecycle Hooks

```python
from providify import Singleton, PostConstruct, PreDestroy

@Singleton
class Database:
    @PostConstruct
    async def connect(self) -> None:
        self._pool = await create_pool()

    @PreDestroy
    async def disconnect(self) -> None:
        await self._pool.close()
```

- Only one `@PostConstruct` and one `@PreDestroy` per class (raises `TypeError` if multiple).
- Supports both sync and async methods.
- Hooks are detected via MRO walk and are inheritable.
- `@PreDestroy` fires in two situations: (1) `shutdown()` / `ashutdown()` for every cached singleton, and (2) automatically when a `request()` / `session()` scope block exits for any `@RequestScoped` / `@SessionScoped` instance cached in that scope.
- `DEPENDENT` instances are never tracked by the container — `@PreDestroy` is never called on them.
- Async `@PreDestroy` hooks on scoped (REQUEST/SESSION) instances are silently skipped if the scope exits via the **sync** `request()` / `session()` context manager. Use `arequest()` / `asession()` when async teardown is needed.

### Configuration Modules (Spring-style)

```python
from providify import Configuration, Provider, Inject

@Configuration
class InfraConfig:
    def __init__(self, settings: Inject[Settings]) -> None:
        self._settings = settings

    @Provider(singleton=True)
    def database(self) -> Database:
        return Database(self._settings.db_url)

    @Provider
    def mailer(self) -> Mailer:
        return SmtpMailer(self._settings.smtp_host)

# Install the config module:
container.install(InfraConfig)
await container.ainstall(InfraConfig)
```

---

## 6. Injection Type Annotations

These are used as constructor parameter type hints to control how the container resolves dependencies.

```python
from typing import Annotated
from providify import Inject, InjectInstances, Lazy, Live, Instance, InjectMeta, LiveMeta

class AlertService:
    def __init__(
        self,
        notifier: Inject[Notifier],                                          # required, single binding
        sms:      Annotated[Notifier, InjectMeta(qualifier="sms")],          # with qualifier ✅
        opt:      Annotated[Notifier, InjectMeta(optional=True)],            # None if not bound ✅
        all_n:    InjectInstances[Notifier],                                  # list of all matching bindings
        lazy_svc: Lazy[HeavyService],                                        # deferred — resolved once on first access
        req_ctx:  Live[RequestContext],                                      # live — re-resolved on every access
        senders:  Instance[Sender],                                          # programmatic handle — qualifier chosen at call time
    ) -> None:
        ...
```

> **Why `Annotated[T, InjectMeta(...)]` for options?**
> `Inject[T]` subscript only accepts a single type argument — `Inject[T, qualifier="x"]` is invalid Python and will raise a `TypeError` at import time.
> The call form `Inject(T, qualifier="x")` works at runtime but type checkers (Pylance, mypy) cannot infer the return type — hover shows `Unknown`.
> `Annotated[T, InjectMeta(...)]` is the correct form: fully valid Python, type-checker-safe, and the underlying representation `Inject[T]` expands to anyway.

### `Inject[T]`

Resolves a single binding eagerly at construction time.

```python
# ✅ Subscript — no options needed (clean, type-checker-safe):
dep: Inject[MyService]

# ✅ With options — use Annotated + InjectMeta (type-checker-safe):
from typing import Annotated
from providify import InjectMeta
dep:  Annotated[MyService, InjectMeta(qualifier="x")]
dep:  Annotated[MyService, InjectMeta(optional=True)]
dep:  Annotated[MyService, InjectMeta(priority=1, qualifier="primary")]

# ❌ Subscript with options — INVALID, raises TypeError at import time:
dep: Inject[MyService, qualifier="x"]      # ← do NOT use

# ❌ Call form — works at runtime but type checkers show Unknown:
dep: Inject(MyService, qualifier="x")      # ← not recommended
```

### `InjectInstances[T]`

Injects all matching bindings as a `list[T]`.

```python
all_handlers: InjectInstances[EventHandler]

# With qualifier:
from typing import Annotated
from providify import InjectMeta
all_handlers: Annotated[list[EventHandler], InjectMeta(all=True, qualifier="fast")]
```

### `Lazy[T]`

Deferred proxy — resolves once on first `.get()` / `.aget()` call, then caches.
**Use this to break circular dependencies**: A → B → A can be resolved if one side uses `Lazy[A]`.

```python
# ✅ Subscript — no options:
lazy_svc: Lazy[HeavyService]

# ✅ Optional — proxy.get() returns None when T is not bound:
lazy_svc: Lazy[HeavyService | None]                              # pipe-union form
lazy_svc: Annotated[HeavyService, LazyMeta(optional=True)]       # equivalent explicit form

# ✅ With options — use Annotated + LazyMeta:
from typing import Annotated
from providify import LazyMeta
lazy_svc: Annotated[HeavyService, LazyMeta(qualifier="heavy")]

# Access:
instance = lazy_svc.get()           # sync
instance = await lazy_svc.aget()    # async — must match context
```

### `Instance[T]`  *(new)*

Programmatic lookup handle — Jakarta CDI `Instance<T>` style. Injects an `InstanceProxy` that defers all resolution to call time. Qualifier and priority are **NOT baked in** — passed as call-time arguments, so one proxy can serve multiple filter combinations.

```python
@Singleton
class NotificationRouter:
    def __init__(self, senders: Instance[Sender]) -> None:
        self._senders = senders   # InstanceProxy — nothing resolved yet

    def route(self, channel: str, msg: str) -> None:
        self._senders.get(qualifier=channel).send(msg)   # qualifier chosen at runtime

    def all(self) -> list[Sender]:
        return self._senders.get_all()

    def has_channel(self, ch: str) -> bool:
        return self._senders.resolvable(qualifier=ch)    # no instance created
```

**Methods on `InstanceProxy`:** `.get()`, `.get_all()`, `.aget()`, `.aget_all()`, `.resolvable()`.

**Scope safety:** Always passes validation — even `Instance[RequestScoped]` in a `@Singleton`. Re-resolves per call like `Live[T]` without needing the wrapper. This exemption applies equally to the `ClassVar[Instance[T]]` form.

**`ClassVar` form:** `ClassVar[Instance[T]]` is fully supported — the container unwraps the `ClassVar` before dispatching:
```python
@Singleton
class AlertService:
    emailer: ClassVar[Instance[Emailer]]   # ✅ identical to plain Instance[Emailer]
```

### `Live[T]`

Live proxy — **re-resolves on every `.get()` / `.aget()` call**. Never caches.
**Use this to safely inject narrow-scoped dependencies into wide-scoped components** (e.g., `@RequestScoped` into `@Singleton`). Without `Live[T]`, the validator raises `LiveInjectionRequiredError`.

```python
@Singleton
class RequestProcessor:
    def __init__(self, ctx: Live[RequestContext]) -> None:
        self._ctx = ctx

    def process(self) -> None:
        context = self._ctx.get()           # always returns the current request's instance
        await self._ctx.aget()              # async variant
```

```python
# Optional — proxy.get() returns None when T is not bound:
ctx: Live[OptionalContext | None]                                 # pipe-union form
ctx: Annotated[OptionalContext, LiveMeta(optional=True)]          # equivalent explicit form

# With qualifier:
from typing import Annotated
from providify import LiveMeta
ctx: Annotated[RequestContext, LiveMeta(qualifier="audit")]
```

### Proxy Classes

| Class | Behaviour |
|---|---|
| `LazyProxy[T]` | Resolves once, caches. Methods: `.get()`, `.aget()` |
| `LiveProxy[T]` | Re-resolves on every call. Methods: `.get()`, `.aget()` |

### Metadata Classes (for `Annotated` form)

| Class | Fields |
|---|---|
| `InjectMeta` | `qualifier`, `priority`, `all`, `optional` |
| `LazyMeta` | `qualifier`, `priority`, `optional` — `optional=True` makes `.get()` return `None` when T is unbound |
| `LiveMeta` | `qualifier`, `priority`, `optional` — `optional=True` makes `.get()` return `None` when T is unbound |
| `InstanceMeta` | *(no fields)* — signals container to inject an `InstanceProxy` instead of the type |

---

## 7. Generic Type Support

Providify supports binding and resolving parameterized generic types:

```python
from typing import Generic, TypeVar
from abc import ABC, abstractmethod

T = TypeVar("T")

class Repository(ABC, Generic[T]):
    @abstractmethod
    def find(self, id: int) -> T: ...

@Component
class UserRepository(Repository[User]):
    def find(self, id: int) -> User: ...

# Container resolves the parameterized generic:
repo = container.get(Repository[User])
```

**How it works:** `utils.py` provides `_is_generic_subtype()` and `_interface_matches()` that handle all four matching cases: concrete↔concrete, concrete↔generic, generic↔generic, generic↔concrete.

---

## 8. Scopes Deep Dive

```
DEPENDENT   → No caching. New instance on every get() call.
SINGLETON   → Cached in container._singleton_cache. Lives until shutdown().
REQUEST     → Cached in ScopeContext per ContextVar token. Lives until request() block exits.
SESSION     → Cached in ScopeContext keyed by session ID. Survives request() blocks within same session.
```

**Scope violation detection:** The container validates that short-lived dependencies are not injected directly into longer-lived components (e.g., a `REQUEST` scoped dep injected directly into a `SINGLETON`). This fires during `validate_bindings()` and raises `ScopeViolationDetectedError`.

**Resolution:** Wrap the narrow-scoped dep in `Live[T]` — this satisfies the validator and guarantees safe re-resolution per call.

**`@Inheritable`:** Marks DI metadata as inheritable via MRO. Without this, subclasses don't automatically inherit scope from their parent.

---

## 9. Module Auto-Discovery

```python
# Scan a module path — discovers all @Component, @Singleton, etc. decorated classes/functions
container.scan("myapp.services")
container.scan("myapp", recursive=True)   # all submodules recursively
```

**What the scanner does:**
- Inspects all module members for DI metadata
- Auto-binds to ABCs when a class implements abstract base classes
- Skips private members (prefixed `_`) and re-exports (imported from elsewhere)
- Idempotent — safe to call multiple times on the same module

---

## 10. Dependency Visualization

```python
# Full container snapshot — returns a DIContainerDescriptor
descriptor = container.describe()
print(descriptor)
# [SINGLETON]
# └── AlertService [SINGLETON] → AlertService
#     └── Notifier [SINGLETON] → EmailNotifier

# Grouped by scope
descriptor.singleton_bindings   # list[BindingDescriptor]
descriptor.request_bindings
descriptor.session_bindings
descriptor.dependent_bindings

import json
print(json.dumps(descriptor.to_dict(), indent=2))
```

`BindingDescriptor.scope_leak` — `True` if the binding directly injects a shorter-lived dependency.

> **Important:** `describe()` only builds dependency trees for `Inject[T]` / `Live[T]` / `Lazy[T]`
> annotated parameters. Plain type hints (`dep: MyClass`) are invisible to `_collect_dependencies`.

---

## 11. Error Types

| Exception | When raised |
|---|---|
| `CircularDependencyError` | A → B → A cycle detected during resolution |
| `ScopeViolationDetectedError` | Short-lived dep injected directly into long-lived component |
| `LiveInjectionRequiredError` | REQUEST/SESSION dep injected into SINGLETON without `Live[T]` wrapper |
| `ClassBindingNotDecoratedError` | `register(cls)` called with an undecorated class |
| `ProviderBindingNotDecoratedError` | `provide(fn)` called with an undecorated function |
| `NotDecoratedError` | Generic "not decorated" base error |
| `ClassAlreadyDecorated` | Decorator applied twice to the same class |
| `ProviderAlreadyDecorated` | `@Provider` applied twice to the same function |
| `BindingError` | Base class for binding-level errors |
| `ValidationError` | Base class for validation errors |
| `providifyError` | Base class for ALL providify errors |

---

## 12. Testing Patterns

Tests live in `tests/`. Each test class is fully self-contained.

**Key fixtures (in `conftest.py`):**
- `container` — fresh `DIContainer` instance per test
- `reset_global_container` (autouse) — resets the global singleton before/after every test

**Test file map:**

| File | What it covers |
|---|---|
| `test_container.py` | bind/register/provide/get/get_all/current/scoped |
| `test_scopes.py` | All four scopes, scope violation, @Inheritable |
| `test_inject.py` | Inject[T], InjectInstances[T], optional, ClassVar[Inject/Live/Lazy] |
| `test_lazy.py` | LazyProxy, Lazy[T], circular-via-lazy |
| `test_live.py` | LiveProxy, Live[T], scope-safe injection |
| `test_instance.py` | InstanceProxy, Instance[T], is_resolvable(), scope-safety, async, ClassVar[Instance[T]] |
| `test_lifecycle.py` | @PostConstruct, @PreDestroy, shutdown |
| `test_async.py` | aget/aget_all, async providers, async context managers |
| `test_configuration.py` | @Configuration, install/ainstall |
| `test_circular.py` | CircularDependencyError, diamond deps |
| `test_generics.py` | Generic[T] binding and resolution |
| `test_scanner.py` | scan(), recursive, ABC auto-binding |
| `test_describe.py` | BindingDescriptor, ASCII trees, JSON |
| `test_warmup.py` | warm_up/awarm_up, validation |

**Run tests:**
```bash
cd tests
poetry install
poetry run pytest
```

---

## 13. Architecture Layers (Bottom → Top)

```
┌──────────────────────────────────────────────────┐
│ Public API: decorators/, type.py, __init__.py    │  ← User-facing surface
├──────────────────────────────────────────────────┤
│ Container: container.py                          │  ← Registry + orchestration
├──────────────────────────────────────────────────┤
│ Bindings: binding.py                             │  ← Creation strategies
├──────────────────────────────────────────────────┤
│ Resolution: resolution.py, scope.py              │  ← Caching + cycle detection
├──────────────────────────────────────────────────┤
│ Metadata: metadata.py                            │  ← Type-safe metadata storage
├──────────────────────────────────────────────────┤
│ Utilities: utils.py, descriptor.py, scanner.py  │  ← Generic types + discovery
└──────────────────────────────────────────────────┘
```

---

## 14. Known Gotchas

- **Async providers called from sync context** → raises `RuntimeError`. Always use `aget()` for async providers.
- **`@PostConstruct` / `@PreDestroy` on non-singleton** → hooks won't fire on `DEPENDENT` instances (nothing to track). Only cached scopes are destroyed on `shutdown()`.
- **`Lazy[T]` `.get()` / `.aget()` must match context** → if resolved inside an async context, call `.aget()` on the proxy, not `.get()`.
- **`Live[T]` `.get()` / `.aget()` must match context** → same rule as `Lazy[T]`. Inside async code, always `.aget()`.
- **REQUEST/SESSION deps in SINGLETON without `Live[T]` or `Instance[T]`** → validation raises `LiveInjectionRequiredError`. Fix: wrap in `Live[T]` (always-fresh proxy) or use `Instance[T]` (programmatic handle).
- **`scan()` auto-binds to ABCs** → if a class implements multiple ABCs, it is registered once per ABC. This can cause surprising `get_all()` results.
- **`validate_bindings()` is called once** → after the first `get()` call, new bindings added via `bind()` are NOT re-validated automatically. Call `validate_bindings()` manually if you add bindings after the first resolution.
- **`@Inheritable` is opt-in** → subclasses do not inherit scope decorators unless the parent is also decorated with `@Inheritable`.
- **Inline decorator args vs stacked decorators** → `@Singleton(qualifier="x")` is equivalent to `@Singleton` + `@Named(name="x")`. Prefer the inline form.
- **`@Named` requires `name=` keyword** → both bare `@Named` and `@Named("smtp")` (positional string) raise `TypeError`. The error now points directly to the correct form: `@Named(name="smtp")`.
- **`Inject[T, qualifier=...]` is invalid Python** → subscript only accepts one type arg. Use `Annotated[T, InjectMeta(qualifier=...)]` instead. Same rule applies to `Lazy[T]` and `Live[T]` — use `LazyMeta` / `LiveMeta` via `Annotated`.
- **`Inject(T, qualifier=...)` call form** → works at runtime but type checkers (Pylance, mypy) cannot infer the return type. Use `Annotated[T, InjectMeta(...)]`.
- **`ClassVar[Instance[T]]` / `ClassVar[Live[T]]` / `ClassVar[Lazy[T]]` / `ClassVar[Inject[T]]` are all valid** → the container calls `_unwrap_classvar()` at every injection boundary before dispatching. All four injection types work identically in the `ClassVar[...]` form. Scope-violation detection and dependency-graph construction also see the unwrapped hint.
- **`@Provider` scope leaks are now validated** → `@Provider(singleton=True)` functions whose parameters include a `@RequestScoped` or `@SessionScoped` dep without `Live[T]` wrapping now raise `LiveInjectionRequiredError` during `validate_bindings()`, the same as `ClassBinding`. Wrap the parameter in `Live[T]` to fix it.
- **`Lazy[T | None]` and `Live[T | None]`** → the pipe-union form is supported as shorthand for `optional=True`. `proxy.get()` returns `None` when T is not bound instead of raising `LookupError`. Equivalent to `Annotated[T, LazyMeta(optional=True)]` / `Annotated[T, LiveMeta(optional=True)]`.
