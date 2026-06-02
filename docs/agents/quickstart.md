# Quickstart — Minimal Correct Usage

Copy-paste-correct skeletons. Both follow the [usage rules](usage-rules.md).

---

## Sync

```python
from providify import DIContainer, Singleton, Component

# 1. Annotate the classes you own (R1). Constructor params are injected by type.
@Singleton
class Database:
    def __init__(self) -> None:
        self.dsn = "postgres://localhost/app"

@Component                      # DEPENDENT — a new instance on every get()
class UserRepository:
    def __init__(self, db: Database) -> None:   # db injected from the container
        self._db = db

# 2. Register, then resolve. The context manager guarantees shutdown() runs (R11).
with DIContainer() as container:
    container.register(Database)
    container.register(UserRepository)

    repo = container.get(UserRepository)        # fully constructed + injected
```

---

## Async

```python
import asyncio
from providify import DIContainer, Singleton, PostConstruct, PreDestroy

@Singleton
class ConnectionPool:
    # Lifecycle hooks may be async. @PostConstruct runs after construction,
    # @PreDestroy on shutdown / scope exit.
    @PostConstruct
    async def open(self) -> None:
        self._conn = await _connect()

    @PreDestroy
    async def close(self) -> None:
        await self._conn.aclose()

async def main() -> None:
    # Async container → ashutdown() (and async @PreDestroy hooks) on exit.
    async with DIContainer() as container:
        container.register(ConnectionPool)
        pool = await container.aget(ConnectionPool)   # async resolution (R6)

asyncio.run(main())
```

> If **any** provider in the dependency graph is `async def`, you must resolve with
> `aget()` — calling `get()` raises `RuntimeError`.

---

## Discovery instead of manual registration

For anything beyond a toy app, prefer `scan()` over hand-registering each class:

```python
with DIContainer() as container:
    container.scan("myapp", recursive=True)   # finds decorated classes/functions
                                              # AND auto-installs @Configuration (R9)
    svc = container.get(MyService)
```

---

## Binding an interface to an implementation

```python
from abc import ABC, abstractmethod
from providify import DIContainer, Singleton

class Mailer(ABC):
    @abstractmethod
    def send(self, msg: str) -> None: ...

@Singleton
class SmtpMailer(Mailer):
    def send(self, msg: str) -> None: ...

with DIContainer() as container:
    container.bind(Mailer, SmtpMailer)     # resolve Mailer → get an SmtpMailer
    mailer = container.get(Mailer)
```

`scan()` also auto-binds a class to the ABCs it implements, so explicit `bind()` is
often unnecessary — but note one class implementing several ABCs is registered once
per ABC, which affects `get_all()` results.
