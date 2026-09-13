# Injection Cheatsheet

Every injection annotation, the **only correct option-bearing form**, plus scopes
and lifecycle. All forms go on **constructor parameters** (or `@Provider` method
parameters), typed as hints the container reads.

---

## The golden rule for options

`Inject[T]` / `Lazy[T]` / `Live[T]` subscripts accept **one** type argument. To pass
`qualifier`, `priority`, `optional`, or `all`, use `Annotated[T, <Meta>(...)]`:

```python
from typing import Annotated
from providify import Inject, InjectMeta, Lazy, LazyMeta, Live, LiveMeta

ok1: Inject[Service]                                   # ✅ no options
ok2: Annotated[Service, InjectMeta(qualifier="x")]     # ✅ options via Annotated

bad1: Inject[Service, qualifier="x"]   # ❌ TypeError at import time
bad2: Inject(Service, qualifier="x")   # ❌ runs, but type checkers report "Unknown"
```

---

## The injection types

| Annotation | Resolves | When to use |
|------------|----------|-------------|
| `Inject[T]` | one binding, **eagerly at construction** | the normal case |
| `InjectInstances[T]` | `list[T]` of **all** matching bindings | plugin lists, handler sets |
| `list[T]` (bare) | `list[T]` of **all** matching bindings — **requires `T` to be a declared collection point first** (`container.multibind(T)` or `@Multibound`) | same as `InjectInstances[T]`, but `[]` instead of `LookupError` on zero matches |
| `Lazy[T]` | one binding, **once on first `.get()`**, then cached | break circular deps; defer expensive construction |
| `Live[T]` | one binding, **re-resolved every `.get()`** | inject narrow scope into wider scope (R5) |
| `Instance[T]` | a programmatic handle; nothing resolved until you call it | choose qualifier/priority at call time |
| `Event[T]` | a dispatch handle (`EventProxy`) | fire CDI events to `@Observes` methods |
| `entity: type[T]` (in an OPEN `@Provider`, plan 016) | filled with the CLOSED type argument, **not resolved from the container** (name-matched; an unannotated single-`TypeVar` parameter gets it positionally instead) | replace a per-type `provide()` loop with one open registration — see `usage-rules.md` R10 |

### `Inject[T]` / `InjectInstances[T]`

```python
notifier: Inject[Notifier]                                       # required, single
sms:      Annotated[Notifier, InjectMeta(qualifier="sms")]       # qualified
opt:      Annotated[Notifier, InjectMeta(optional=True)]         # None if unbound
all_n:    InjectInstances[Notifier]                              # list of all matches
fast:     Annotated[list[Notifier], InjectMeta(all=True, qualifier="fast")]
```

`InjectMeta` fields: `qualifier`, `priority`, `all`, `optional`.

```python
container.multibind(Notifier)                        # declare the collection point once
list_n: list[Notifier]                                # then a bare list[T] works — same as InjectInstances[T]
```

### `Advised` — field-level interceptors (NOT a CDI feature)

```python
from providify import Advised, AroundGet, AroundSet, FieldAccessContext

class Account:
    balance: float = Advised(0.0)   # declared join point — only advised fields cost anything
```

Fires `@AroundGet`/`@AroundSet` advice on read/write, but **only** for
container-managed instances, **only** after construction completes (never
during `__init__`/class-var injection/`@PostConstruct`), and **never** for
dataclasses/pydantic/attrs targets (rejected with `TypeError`). This goes
beyond Jakarta CDI (method-only interception) — modelled on AspectJ `get`/
`set` pointcuts, implemented via Python's descriptor protocol.

### `Lazy[T]` — break cycles / defer

```python
heavy: Lazy[HeavyService]                                # resolved on first access
opt:   Lazy[HeavyService | None]                         # .get() → None if unbound
q:     Annotated[HeavyService, LazyMeta(qualifier="h")]  # with options

instance = heavy.get()          # sync
instance = await heavy.aget()   # async — must match context (R7)
```

A → B → A resolves if one side uses `Lazy`. `LazyMeta` fields: `qualifier`,
`priority`, `optional`.

### `Live[T]` — narrow scope into wider scope

```python
ctx: Live[RequestContext]                                # re-resolved every call
opt: Live[OptionalContext | None]                        # .get() → None if unbound
q:   Annotated[RequestContext, LiveMeta(qualifier="audit")]

current = ctx.get()             # always the active scope's instance; .aget() in async
```

Required to inject `@RequestScoped` / `@SessionScoped` into `@Singleton` — otherwise
`LiveInjectionRequiredError` at validation. `LiveMeta` fields: `qualifier`,
`priority`, `optional`.

### `Instance[T]` — programmatic, call-time qualifier

```python
from providify import Singleton, Instance

@Singleton
class Router:
    def __init__(self, senders: Instance[Sender]) -> None:
        self._senders = senders                          # nothing resolved yet

    def route(self, channel: str, msg: str) -> None:
        self._senders.get(qualifier=channel).send(msg)   # qualifier chosen at runtime
```

Methods: `.get()`, `.get_all()`, `.aget()`, `.aget_all()`, `.resolvable()`. Always
passes scope validation (re-resolves per call like `Live`). `ClassVar[Instance[T]]`
is also supported.

### `Event[T]` — fire-and-observe

```python
from providify import Singleton, Component, Event, Observes

@Singleton
class OrderService:
    def __init__(self, events: Event[OrderPlaced]) -> None:
        self._events = events

    def place(self, order: Order) -> None:
        self._events.fire(OrderPlaced(order))            # .afire(...) in async

@Component
class AuditListener:
    @Observes(OrderPlaced)
    def on_placed(self, e: OrderPlaced) -> None:
        ...
```

---

## `ClassVar[...]` form

`ClassVar[Inject[T]]`, `ClassVar[Live[T]]`, `ClassVar[Lazy[T]]`, and
`ClassVar[Instance[T]]` all work — the container unwraps the `ClassVar` at every
injection boundary, so behaviour is identical to the bare form.

---

## Scopes & lifetime

```
DEPENDENT   new instance on every get()              — @Component
SINGLETON   one per container, until shutdown()      — @Singleton
REQUEST     one per request() block                  — @RequestScoped
SESSION     one per session(id), survives requests   — @SessionScoped
```

Scope blocks:

```python
with container.request():                 # async: `async with container.arequest():`
    svc = container.get(MyRequestScoped)

with container.session("user-abc"):       # async: container.asession("user-abc")
    svc = container.get(MySessionScoped)
container.invalidate_session("user-abc")  # drop the session cache
```

**Scope violation:** injecting a shorter-lived dep directly into a longer-lived
component raises `ScopeViolationDetectedError` / `LiveInjectionRequiredError` at
validation. Fix with `Live[T]` (or `Instance[T]`).

---

## Lifecycle hooks

```python
from providify import Singleton, PostConstruct, PreDestroy

@Singleton
class Database:
    @PostConstruct                 # runs after construction (sync or async)
    async def connect(self) -> None: ...

    @PreDestroy                    # runs on shutdown / scope exit (sync or async)
    async def disconnect(self) -> None: ...
```

- One `@PostConstruct` and one `@PreDestroy` per class (else `TypeError`).
- Detected via MRO walk; inheritable.
- `@PreDestroy` never fires on `DEPENDENT` instances (they aren't tracked), and
  never fires for `@Provider`-produced instances — use `@Disposes` (matched
  within the same `@Configuration` only).
- Async `@PreDestroy` on a scoped instance is **skipped** if the scope exits via the
  **sync** `request()`/`session()` block — use `arequest()`/`asession()` for async
  teardown.

---

## Common errors → cause

| Error | Cause |
|-------|-------|
| `LiveInjectionRequiredError` | REQUEST/SESSION dep in SINGLETON without `Live[T]`/`Instance[T]` |
| `ScopeViolationDetectedError` | shorter-lived dep injected directly into longer-lived one |
| `CircularDependencyError` | A → B → A cycle — break it with `Lazy[T]` |
| `RuntimeError` (on `get()`) | async provider resolved from sync context — use `aget()` |
| `ClassBindingNotDecoratedError` | `register()` on an undecorated class |
| `ProviderBindingNotDecoratedError` | `provide()` on an undecorated function |
| `TypeError` from `@Named` | use `@Named(name="x")`, not bare/positional |
| `TypeError` naming a `@Provider` and its return annotation | the return type (even quoted, e.g. `-> "Foo"`) can't be resolved to a real type — `Foo` is `TYPE_CHECKING`-only or defined inside a function; import it at module level instead |
| `AnnotationResolutionError` | an annotation on an actual injection point (`Inject[T]`, `Lazy[T]`, `Live[T]`, `Instance[T]`, `InjectInstances[T]`, or a `ClassVar` wrapping one of those) couldn't be evaluated at runtime, naming the exact parameter or attribute — either at resolution time (`get()`/`aget()`, construction) or during scope-leak validation. Import the annotated type at runtime instead of under `TYPE_CHECKING`, or move locally-defined types to module level |

**The rule for annotations that aren't injection points:** a defaulted, non-injected
parameter (no `Inject[T]`/`Lazy[T]`/`Live[T]`/etc. marker) may reference any type —
even one that doesn't exist at runtime (`TYPE_CHECKING`-only import, a type local to
another function). The container resolves each annotation independently, so an
unresolvable one on a parameter nobody injects never affects anything else on the
same signature: no warning, nothing to configure. Only annotate **actual injection
points** with types that exist at runtime.
