# Anti-Patterns — What Not To Do

Each entry: the mistake, why it's wrong, the fix, and the symptom it produces.

---

## 1. Wrapping a class you own in a `@Provider`

```python
@Provider
def make_repo(db: Database) -> UserRepository:   # ❌ pointless factory
    return UserRepository(db)
```

**Why wrong:** `@Provider` is for types you *can't* annotate. The container already
injects `db` if you just decorate the class.
**Fix:** `@Singleton class UserRepository: ...` and let the container build it.
**Symptom:** boilerplate that drifts out of sync; reviewers flag Spring-think.

---

## 2. Treating `@Configuration` as "where all beans live"

**Why wrong:** Jakarta CDI has no `@Configuration`; it's just optional grouping
sugar. Most beans are decorated classes found by `scan()`.
**Fix:** annotate classes; reserve `@Configuration` for the handful of real
producers that belong together.

---

## 3. Calling a sibling producer method directly

```python
@Provider
def repo(self) -> UserRepository:
    return UserRepository(self.pool())   # ❌ re-runs pool(), defeats singleton=True
```

**Why wrong:** bypasses the container, so caching/scoping is lost.
**Fix:** declare it as a parameter — `def repo(self, pool: ConnectionPool)` — and the
container hands you the cached instance.
**Symptom:** "singleton" objects are silently created more than once.

---

## 4. Putting options inside the subscript

```python
dep: Inject[Service, qualifier="x"]   # ❌ TypeError at import time
dep: Inject(Service, qualifier="x")   # ❌ runs, but type checker shows "Unknown"
```

**Fix:** `Annotated[Service, InjectMeta(qualifier="x")]` (and `LazyMeta` / `LiveMeta`
for those proxies).
**Symptom:** import-time crash, or silently untyped code.

---

## 5. Injecting a narrow scope into a wider scope without `Live[T]`

```python
@Singleton
class Processor:
    def __init__(self, ctx: RequestContext) -> None: ...   # ❌ REQUEST into SINGLETON
```

**Why wrong:** the singleton would capture one request's instance forever.
**Fix:** `ctx: Live[RequestContext]` (re-resolves per call) or `Instance[T]`.
**Symptom:** `LiveInjectionRequiredError` at first resolution.

---

## 6. Mixing sync/async resolution

```python
svc = container.get(AsyncBuiltService)   # ❌ async provider, sync call → RuntimeError
proxy.get()                              # ❌ inside async code → use proxy.aget()
```

**Fix:** use `aget()` / `aget_all()` and proxy `.aget()` inside async code; `get()` /
`.get()` in sync code.
**Symptom:** `RuntimeError`, or awaiting something that isn't a coroutine.

---

## 7. Adding bindings after the first resolution and expecting validation

**Why wrong:** `validate_bindings()` runs once, on the first `get()`/`aget()`. Later
`bind()` calls are not re-validated.
**Fix:** register everything before the first resolution, or call
`container.validate_bindings()` manually after late binding.
**Symptom:** scope violations / missing-binding errors that don't surface until use.

---

## 8. Forgetting the container is a resource

```python
container = DIContainer()      # ❌ no shutdown → @PreDestroy never runs
```

**Fix:** use it as a context manager — `with DIContainer() as container:` (or
`async with` → `ashutdown()`). Then `@PreDestroy` and singleton teardown run.
**Symptom:** leaked connections/pools; cleanup hooks silently never fire.

---

## 9. Relying on `@Inheritable` implicitly

**Why wrong:** subclasses do **not** inherit a parent's scope decorator unless the
parent is also `@Inheritable`.
**Fix:** add `@Inheritable` to the base, or decorate each subclass explicitly.
**Symptom:** a subclass resolves as `DEPENDENT` when you expected `SINGLETON`.

---

## 10. Misusing `@Named`

```python
@Named            # ❌ TypeError
@Named("smtp")    # ❌ TypeError (positional)
```

**Fix:** `@Named(name="smtp")` — or just use the inline `@Singleton(qualifier="smtp")`
form, which is equivalent and preferred.

---

## 11. Annotating an injection point with a type that doesn't exist at runtime

```python
if TYPE_CHECKING:
    from .internal import Tracer

@Singleton
class Worker:
    def __init__(self, dep: Inject[Tracer]) -> None: ...   # ❌ Tracer unresolvable
```

**Why wrong:** annotations are resolved per parameter, but a parameter carrying an
`Inject[T]` / `Lazy[T]` / `Live[T]` / `Instance[T]` / `InjectInstances[T]` marker (or
a `ClassVar` wrapping one) IS an injection point — the container must be able to
evaluate its type at runtime to know what to construct.
**Fix:** import the annotated type at runtime instead of guarding it behind
`TYPE_CHECKING`, or move a function-local type to module level.
**Symptom:** `AnnotationResolutionError` naming the exact parameter, at construction
or the first `get()`/`aget()`/`validate_bindings()` call.

**Not an anti-pattern any more:** a *defaulted, non-injected* parameter (no
providify marker) may reference anything, including a `TYPE_CHECKING`-only import —
the container resolves each annotation independently, so an unresolvable one on a
parameter nobody injects never affects any other parameter on the same signature.
There is no need to import a type at runtime, or restructure a signature, just to
protect an unrelated sibling parameter — that whole-signature hint loss no longer
happens.

---

## Quick self-check before finishing

- [ ] Every class I own is decorated, not wrapped in a needless `@Provider`.
- [ ] `@Configuration` only groups real producers (or I used bare `@Provider`).
- [ ] Producer deps are parameters, not `self.sibling()` calls.
- [ ] Options use `Annotated[T, …Meta(...)]`, never subscript args.
- [ ] Narrow-into-wide scope injections use `Live[T]` / `Instance[T]`.
- [ ] Async graphs resolved with `aget()`; proxies use matching `.get`/`.aget`.
- [ ] Container used as a context manager so teardown runs.
- [ ] Every actual injection point (`Inject[T]`/`Lazy[T]`/`Live[T]`/`Instance[T]`/
      `InjectInstances[T]`, or a `ClassVar` wrapping one) annotates a type that
      exists at runtime — not a `TYPE_CHECKING`-only import.
