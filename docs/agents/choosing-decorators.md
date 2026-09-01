# Choosing Decorators — The Decision That Matters Most

This is where agents most often misuse Providify by importing Spring habits. The
library is **Jakarta CDI first**: *beans are classes*; producers fill the gaps.

Full treatment: **[`../../PROVIDERS.md`](../../PROVIDERS.md)**. This page is the
condensed decision procedure.

---

## The one question

> **Do I own the class and can I put a decorator on it?**

### Yes → annotate the class. This is the default. Reach here first.

```python
from providify import Singleton

@Singleton
class OrderService:
    def __init__(self, repo: OrderRepository) -> None:   # injected
        self._repo = repo
```

Pick the scope decorator by lifetime:

| Decorator | Scope | Lifetime | Jakarta equivalent |
|-----------|-------|----------|--------------------|
| `@Component` | `DEPENDENT` | new instance every `get()` | `@Dependent` |
| `@Singleton` | `SINGLETON` | one per container | `@ApplicationScoped` |
| `@RequestScoped` | `REQUEST` | one per `request()` block | `@RequestScoped` |
| `@SessionScoped` | `SESSION` | one per `session(id)` block | `@SessionScoped` |

### No → write a `@Provider` (= Jakarta `@Produces`)

Use a producer **only** when you cannot annotate the class:

- **Third-party / stdlib types** — `redis.Redis`, `httpx.AsyncClient`, a SQLAlchemy
  `Engine`, `pathlib.Path`.
- **Interface where the concrete type is chosen at runtime.**
- **Values needing imperative construction** — read an env var, open a connection.

```python
from providify import Provider

@Provider(singleton=True)                 # the return type is the registered interface
def db_pool(cfg: AppConfig) -> ConnectionPool:   # cfg injected from the container
    return ConnectionPool(cfg.db_url, size=cfg.pool_size)
```

### Several related producers → *optionally* group in `@Configuration`

```python
from providify import Configuration, Provider

@Configuration
class InfraModule:
    @Provider(singleton=True)
    def pool(self, cfg: AppConfig) -> ConnectionPool:
        return ConnectionPool(cfg.db_url)

    @Provider(singleton=True)
    def cache(self, cfg: AppConfig) -> Cache:
        return Cache(cfg.redis_url)
```

`@Configuration` is **grouping sugar only**. A standalone `@Provider` function found
by `scan()` is equally valid. `scan()` auto-installs `@Configuration` classes — no
separate `install()` needed.

---

## Provider method injection — no `__init__` boilerplate

Each `@Provider` method's parameters are resolved from the container; `self` is
already bound. Declare dependencies as parameters so the container hands you the
**cached** instance (respecting `singleton=True`). Do **not** call sibling producer
methods directly — that re-runs the factory and breaks caching.

If many producers need the *same* injected object, you *may* inject it once via the
module's `__init__` and reach it via `self` — a convenience, not the norm.

---

## Translation table for Spring/CDI users

| Providify | Jakarta CDI | Spring |
|-----------|-------------|--------|
| `@Component` / `@Singleton` on a class | a (scoped) bean | `@Component` / `@Service` |
| `@Provider` method/function | `@Produces` method | `@Bean` method |
| `@Configuration` class | *(no equivalent)* | `@Configuration` |
| `@Disposes` method | `@Disposes` | `@Bean(destroyMethod=...)` |
| `@Named` / `qualifier=` | `@Named` | `@Qualifier` |

**Takeaway:** Jakarta CDI has no `@Configuration`. Treating it as the Spring "config
object where all beans go" is the anti-pattern this guide exists to prevent.

**On `@Disposes`:** the Spring column (`@Bean(destroyMethod=...)`) is misleading if
read too literally — Spring *also* runs `@PreDestroy` on `@Bean`-produced singleton
and request-scoped instances, so Spring migrants have two working teardown paths.
In CDI (and providify), `@Disposes` is the **only** teardown path for
`@Produces`/`@Provider`-produced instances — `@PreDestroy` is never invoked on them.

---

## Advanced CDI features (when you actually need them)

These exist for parity; reach for them only when the simpler model doesn't fit. See
SKILL.md / README.md for full examples.

- **Qualifiers & stereotypes** — `@Named(name=...)` / `qualifier=` to disambiguate
  multiple bindings of one type; `@Stereotype(...)` to define a reusable composed
  decorator; `@Alternative` / `@Default` for selection.
- **Events** — inject `Event[T]`, fire with `.fire(e)` / `.afire(e)`; observe with a
  method decorated `@Observes(T)`.
- **Interceptors** — `@Interceptor` + `@AroundInvoke(ctx)` calling `ctx.proceed()`.
- **Decorators (bean delegation)** — `@Decorator` class receiving the wrapped bean
  via `Annotated[T, DelegateMeta()]` (`Delegate[T]`).
- **`InjectionPoint`** — type a `@Provider`/constructor param as `InjectionPoint` to
  learn who is requesting the dependency (declaring class, param name, qualifier).
