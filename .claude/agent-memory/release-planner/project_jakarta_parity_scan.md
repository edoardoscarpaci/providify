---
name: Jakarta CDI parity scan — v0.3.0 gap analysis
description: Which Jakarta CDI features exist, are partial, or are absent in Providify as of 2026-04-29
type: project
---

Scan performed 2026-04-29 against the dev branch (477 tests passing after v0.2.0 features).

## PRESENT (full implementation)

- `@Inject` equivalent — constructor + class-level attribute injection via type hints, `Inject[T]`, `InjectInstances[T]`
- `@Named` — `Named(name=...)` decorator; `qualifier=` on scope decorators
- `@Qualifier`-style — `qualifier` field on `DIMetadata`/`ProviderMetadata`; no custom annotation form yet
- `@Singleton` / `@ApplicationScoped` — `@Singleton` decorator maps to `Scope.SINGLETON`
- `@RequestScoped` / `@SessionScoped` — full `ScopeContext` with ContextVar isolation
- `@Dependent` scope — `@Component` maps to `Scope.DEPENDENT`
- `@Produces` factory methods — `@Provider` (standalone + in `@Configuration` modules)
- `@PostConstruct` / `@PreDestroy` — full lifecycle hooks (sync + async)
- `Instance<T>` programmatic lookup — `Instance[T]` / `InstanceProxy` (get, get_all, resolvable)
- `@Priority` — `Priority(priority=...)` decorator; `priority=` on scope decorators
- Circular dependency detection — `CircularDependencyError` with chain message
- Lazy vs eager — `Lazy[T]` (deferred, cached), `Live[T]` (always-fresh), eager via `warm_up()`
- Scope safety validation — `ScopeViolationDetectedError`, `LiveInjectionRequiredError`
- `@Alternative`-style selection — `priority=` field achieves the same binding-override semantics

## PARTIAL (exists but incomplete)

- `@Qualifier` (custom qualifier annotations) — only string-based qualifiers; no annotation-level `@Qualifier` meta-annotation for user-defined typed qualifiers like `@SMTP`
- `@Alternative` with `@Priority` for disabling/enabling beans — `priority=` works for selection but there is no `@Alternative` marker that disables a bean by default and enables it for a specific deployment
- `@Default` qualifier — no explicit `@Default` marker; the container uses absence-of-qualifier as the implicit default
- `@Disposes` — `@PreDestroy` covers basic teardown but there is no explicit `@Disposes` linked to a specific `@Provider` parameter (Jakarta's `@Disposes` tags a parameter in a disposer method)

## ABSENT (not implemented)

- `@Interceptor` / `@InterceptorBinding` — no AOP-style interceptor mechanism; no method interception at all
- `@Decorator` pattern — no decorator wrapping/delegation support
- `Event<T>` / `@Observes` event system — no event bus, no observer registration
- `@Stereotype` (composed annotations) — no meta-annotation support for bundling multiple annotations
- `InjectionPoint` metadata — injection point context not passed to providers or constructors
- `@Default` explicit qualifier decorator — present implicitly, not as an exported symbol

## Why: Primary goal is Jakarta CDI feature parity for v0.3.0.
## How to apply: Use this map when prioritising features for the release plan.
