---
name: providify-architecture-snapshot
description: Architectural snapshot of providify v0.1.7 dev branch — layer map, key design decisions, public API gaps, and known missing features as of 2026-04-26.
type: project
---

Providify is a zero-dependency Python DI container (Python 3.12+, Apache-2.0). Version 0.1.7 (dev branch ahead of 0.2.0).

**Why:** Pure codebase scan requested by user to identify production-readiness gaps.

**How to apply:** Use this as baseline for future release planning sessions to avoid re-scanning.

## Key architectural facts

- Single package: `providify/` — no sub-packages except `decorator/`
- Two binding strategies: `ClassBinding` (constructor injection) and `ProviderBinding` (factory function)
- Four scopes: DEPENDENT, SINGLETON, REQUEST, SESSION
- Four injection types: `Inject[T]`, `Lazy[T]`, `Live[T]`, `Instance[T]` plus `InjectInstances[T]`
- Class-level annotations supported via `get_type_hints()` + MRO walk
- `ContextVar`-based scope isolation (safe for async)
- Abstract `ContainerScanner` + `DefaultContainerScanner` (pluggable scan strategy)
- `@Configuration` class support for Spring-style module grouping
- `DIContainerDescriptor` + `BindingDescriptor` for dependency visualization
- Per-key double-check locking (both threading.Lock and asyncio.Lock) for singleton instantiation
- `set_scoped(tp, instance)` — inject pre-built value into active scope cache
- `override(interface, impl)` and `reset_binding(interface)` for test support (IMPLEMENTED, no tests yet)
- `get_binding()` and `get_all_bindings()` read-only introspection (IMPLEMENTED, no tests yet)

## What is IMPLEMENTED but in dev/unreleased (CHANGELOG [Unreleased] v0.2.0)

1. `container.override(interface, implementation)` — documented in README, implemented in container.py, NO TESTS
2. `container.reset_binding(interface, *, qualifier=None) -> int` — documented in README, implemented, NO TESTS
3. `container.get_binding(interface, ...) -> AnyBinding` — documented in README, implemented, NO TESTS
4. `container.get_all_bindings(interface, ...) -> list[AnyBinding]` — documented in README, implemented, NO TESTS
5. `ProviderBinding.validate()` now checks scope leaks — unreleased
6. `Lazy[T | None]` / `Live[T | None]` optional forms — unreleased
7. `ScopeContext` @PreDestroy on scope exit — unreleased
8. Per-key singleton locks (double-check locking) — unreleased
9. `DIContainer.__repr__` — unreleased

## Public API gaps

1. **`Priority`, `Scope`, `providifyError`, `ScopeViolationDetectedError`, `CircularDependencyError`, `BindingDescriptor`, `DIContainerDescriptor`, `AnyBinding`** are NOT exported from `providify/__init__.py` — users must import from internal modules.

2. **`set_scoped()`** is documented in container.py docstring with a FastAPI JWT example but NOT in README.

3. **`Priority` decorator** is in README but NOT in `__init__.py.__all__`.

4. **`Scope` enum** is required by `@Provider(scope=Scope.REQUEST)` but not importable from `providify` directly.

5. **No test files for `override`, `reset_binding`, `get_binding`, `get_all_bindings`** — these are fully implemented but completely untested.

## Known gaps as of 2026-04-26 (dev scan)

1. **No test file `test_mutation.py`**: `override()`, `reset_binding()`, `get_binding()`, `get_all_bindings()` have zero test coverage despite being implemented and documented in CHANGELOG + README.

2. **No ASGI/FastAPI integration helper**: `set_scoped()` is documented only in container.py. No `ProvidifyMiddleware` or lifespan helper class.

3. **No conditional binding** (`@ConditionalOnProperty`, `@Profile`): No environment-aware binding selection at scan/registration time.

4. **No event bus / @EventListener**: No inter-component event-driven communication.

5. **`LazyProxy` thread-safety gap**: Two concurrent threads calling `.get()` on the same unresolved proxy may each call `container.get()` — last write wins. Documented but unfixed.

6. **`session.invalidate_session()` does NOT run @PreDestroy**: The docstring explicitly notes "Does NOT run @PreDestroy hooks". Session invalidation on logout silently skips teardown.

7. **`scan()` does NOT auto-discover `@Configuration` classes**: README correctly says "Not picked up by scan() — use container.install() instead" but `DefaultContainerScanner._scan_module` DOES call `_autoregister_configurator`. The behavior is actually implemented but the README doc is incorrect/misleading.

8. **No `py.typed`-backed `.pyi` stubs**: `py.typed` exists but no `.pyi` stub files for generated type aliases (Inject, Lazy, Live, Instance behave differently under TYPE_CHECKING vs runtime).

9. **No `test_mutation.py`** covering the new mutation/introspection API.

10. **`@Named`, `@Priority`, `@Inheritable` not exported from `__init__`** — only `Named` and `Inheritable` are in `__all__`. `Priority` is missing. `Scope` enum missing. All exception types missing.
