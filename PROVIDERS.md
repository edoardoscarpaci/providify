# Providers & Configuration — the Jakarta CDI way

> **TL;DR** — Annotate the classes *you* own with `@Component` / `@Singleton` /
> `@RequestScoped` / `@SessionScoped` and let the container build them. Reach for
> `@Provider` (providify's name for Jakarta CDI's `@Produces`) **only** for the
> things you *can't* annotate. `@Configuration` is just an optional namespace for
> grouping related `@Provider` methods — not a Spring-style "config bean" you reach
> for by default.

providify is **inspired by Jakarta CDI** first and Spring second. If you have used
Spring, the `@Configuration` name will tempt you to put every object behind a
`@Bean`/`@Provider` factory. Don't. In the CDI model, *beans are classes*, and
producers exist for the gaps. This guide explains the gap.

---

## The decision rule

Ask one question: **do I own the class and can I put a decorator on it?**

### 1. Yes → annotate the class. This is the default.

```python
from providify import Singleton, Component

@Singleton
class UserRepository:
    def __init__(self, db: Database) -> None:   # db injected by the container
        self._db = db
```

The container constructs it, injects its constructor dependencies, and manages its
scope. No factory, no `@Configuration`, nothing to wire by hand. **Reach here first.**

| Decorator | Scope | Lifetime |
|-----------|-------|----------|
| `@Component` | `DEPENDENT` | new instance every `get()` |
| `@Singleton` (= `@ApplicationScoped`) | `SINGLETON` | one per container |
| `@RequestScoped` | `REQUEST` | one per request scope |
| `@SessionScoped` | `SESSION` | one per session scope |

### 2. No → write a `@Provider`.

Use a `@Provider` (= Jakarta `@Produces`) when you **cannot** annotate the class:

- **Third-party / stdlib types** you don't control — `redis.Redis`, `httpx.AsyncClient`,
  a SQLAlchemy `Engine`, a `pathlib.Path`.
- **Interfaces / protocols** where the concrete implementation is chosen at runtime.
- **Values that need imperative construction** — read an env var, open a connection,
  build from settings.

```python
from providify import Provider

# A type you don't own — annotate it via a producer instead.
@Provider(singleton=True)
def db_pool(cfg: AppConfig) -> ConnectionPool:   # cfg is injected
    return ConnectionPool(cfg.db_url, size=cfg.pool_size)
```

The **return-type annotation** is the registered interface. The provider's
parameters are resolved from the container — see
[Per-method injection](#per-method-injection-no-__init__-needed) below.

> ⚠️ **Gotcha:** the return annotation must resolve to a real type at import
> time, even when quoted (`-> "Foo"`). A type that only exists under
> `if TYPE_CHECKING:` or is defined inside a function cannot be resolved and
> now raises `TypeError` at registration — naming the provider and the
> annotation. Fix it by importing the type normally (not under
> `TYPE_CHECKING`) wherever the `@Provider` function is defined.

```python
# ❌ Wrong — Foo only exists for type checkers, never at runtime.
if TYPE_CHECKING:
    from mypkg.models import Foo

@Provider(singleton=True)
def make_foo() -> "Foo":
    ...

# ✅ Right — import Foo unconditionally so the annotation resolves.
from mypkg.models import Foo

@Provider(singleton=True)
def make_foo() -> Foo:
    ...
```

### 3. Several related producers → *optionally* group them in `@Configuration`.

`@Configuration` is **sugar for grouping** — nothing more. A bare `@Provider`
function discovered by `scan()` is just as valid. Group when the producers share
configuration or you simply want them in one place.

```python
from providify import Configuration, Provider

@Configuration
class InfraModule:
    @Provider(singleton=True)
    def db_pool(self, cfg: AppConfig) -> ConnectionPool:
        return ConnectionPool(cfg.db_url)

    @Provider(singleton=True)
    def cache(self, cfg: AppConfig) -> Cache:
        return Cache(cfg.redis_url)
```

---

## `@Provider` *is* Jakarta `@Produces`

This is the key mental shift. The decorator is literally documented as
*"Equivalent to Jakarta's @Produces / @Bean"* in
[`providify/decorator/scope.py`](providify/decorator/scope.py). It is **not** a
Spring `@Configuration`-bean mechanism. A producer is a typed factory for one
interface, callable anywhere a bean is — that's the whole concept.

| providify | Jakarta CDI | Spring |
|-----------|-------------|--------|
| `@Component` / `@Singleton` on a class | a bean (scoped) | `@Component` / `@Service` |
| `@Provider` method/function | `@Produces` method | `@Bean` method |
| `@Configuration` class | *(no equivalent — any bean may host `@Produces`)* | `@Configuration` |
| `@Disposes` method | `@Disposes` | `@Bean(destroyMethod=...)` |
| `@Named` / `qualifier=` | `@Named` | `@Qualifier` |

The takeaway: **Jakarta CDI has no `@Configuration`.** Producers live on ordinary
beans. providify offers `@Configuration` only as a convenience grouping; treating
it as "the Spring config object where all my beans go" is the anti-pattern this
guide exists to prevent.

---

## Per-method injection (no `__init__` needed)

A `@Provider` method's parameters are resolved from the container. When the module
is installed, each method is registered as a **bound method** — `self` is already
filled in, so only the remaining parameters are injected. The lean style:

```python
@Configuration
class DatabaseModule:
    @Provider(singleton=True)
    def connection_pool(self, cfg: AppConfig) -> ConnectionPool:
        return ConnectionPool(cfg.db_url, size=cfg.pool_size)

    @Provider
    def user_repo(self, pool: ConnectionPool) -> UserRepository:
        # `pool` comes from the container → respects the singleton above.
        return UserRepository(pool)
```

> ⚠️ **Don't call a sibling producer method directly** (`self.connection_pool()`).
> That bypasses the container and *re-runs* the factory, defeating `singleton=True`
> caching. Declare it as a parameter (`pool: ConnectionPool`) so the container
> hands you the cached instance.

### Optional: constructor injection for shared config

If many producers in a module need the *same* injected object, you may inject it
once via the module's `__init__` and reach it through `self`. This is a
**convenience, not the norm** — prefer per-method parameters unless the repetition
genuinely hurts.

```python
@Configuration
class DatabaseModule:
    def __init__(self, cfg: AppConfig) -> None:   # injected at install/scan time
        self._cfg = cfg

    @Provider(singleton=True)
    def connection_pool(self) -> ConnectionPool:
        return ConnectionPool(self._cfg.db_url)
```

The constructor's own dependencies must be registered before the module is
installed (they are resolved eagerly). For async-only constructor deps, the module
is installed via `ainstall()` (or `scan()` from an async context — see below).

---

## Registration: `register` vs `provide` vs `scan` vs `install`

| Call | Use for |
|------|---------|
| `container.register(Cls)` / `bind(Iface, Cls)` | a class you annotated (or want self-bound) |
| `container.provide(fn)` | a single `@Provider` function |
| `container.install(Module)` / `ainstall(Module)` | a `@Configuration` class — explicit, manual |
| `container.scan("pkg", recursive=True)` | auto-discover everything decorated in a package |

**`scan()` *does* auto-install `@Configuration` classes.** You do **not** need a
separate `install()` call after scanning — the scanner finds each `@Configuration`
and installs it for you (deduplicated by class identity, so scanning twice is
safe). `install()` / `ainstall()` remain available for the explicit, no-scan path.

```python
# These are equivalent for picking up InfraModule:
container.scan("myapp.infra")          # auto-installs every @Configuration found
# ...or, without scanning:
container.install(InfraModule)         # explicit
```

---

## Anti-patterns

- ❌ **Wrapping a class you own in a `@Provider` just to construct it.**
  ```python
  @Provider
  def make_repo(db: Database) -> UserRepository:   # pointless
      return UserRepository(db)
  ```
  ✅ Annotate it: `@Singleton class UserRepository: ...` — the container already
  injects `db`.

- ❌ **Treating `@Configuration` as the place where "all beans are defined."**
  Most beans are just decorated classes discovered by `scan()`. Reserve
  `@Configuration` for grouping the handful of real producers.

- ❌ **Calling a sibling producer method directly** (`self.cache()`), which
  re-runs the factory and breaks scope caching. Inject it as a parameter instead.

---

## See also

- `README.md` — the `@Provider` and `@Configuration modules` sections.
- `providify/decorator/scope.py` — `@Provider` / scope decorators, with the
  Jakarta CDI equivalences noted inline.
- `tests/test_configuration.py`, `tests/test_field_provider.py` — runnable examples.
