---
name: providify-architecture-snapshot
description: Architectural snapshot of providify v0.1.7 — layer map, key design decisions, and known gaps as of 2026-04-26.
type: project
---

Providify is a zero-dependency Python DI container (Python 3.12+, Apache-2.0). Version 0.1.7 as of 2026-04-26.

**Why:** Pure codebase scan requested by user. No explicit feature priorities were given.

**How to apply:** Use this as baseline for future release planning sessions to avoid re-scanning.

## Key architectural facts

- Single package: `providify/` — no sub-packages except `decorator/`
- Two binding strategies: `ClassBinding` (constructor injection) and `ProviderBinding` (factory function)
- Four scopes: DEPENDENT, SINGLETON, REQUEST, SESSION
- Four injection types: `Inject[T]`, `Lazy[T]`, `Live[T]`, `Instance[T]`
- Class-level annotations supported via `get_type_hints()` + MRO walk
- `ContextVar`-based scope isolation (safe for async)
- Abstract `ContainerScanner` + `DefaultContainerScanner` (pluggable scan strategy)

## Known gaps as of 2026-04-26

1. **Priority semantics bug**: README/docstrings say "lower number wins" but `_get_best_candidate` uses `max()` so HIGHER number wins. The test assertion confirms higher wins, but the test docstring contradicts it. Either fix `max()` → `min()` or fix all docs. Priority: High.

2. **`@PreDestroy` not called for REQUEST/SESSION scoped bindings**: `shutdown()` only iterates `_singleton_cache`. Request/session scoped beans with `@PreDestroy` never get their hook called when the scope exits. Jakarta CDI calls `@PreDestroy` on scope exit.

3. **Uncommitted work in dev branch**: `Inject[T | None]`, `Lazy[T | None]`, `Live[T | None]` optional injection unwrapping is implemented in `container.py` and `type.py` but not yet committed to main. `LazyMeta.optional` field added.

4. **No binding override/replace API**: Once a binding is registered, there's no `container.override(Interface, NewImpl)` for testing or conditional wiring. Tests rely on creating fresh containers.

5. **Concurrent singleton instantiation race**: docstring explicitly notes "A singleton provider called concurrently may be invoked more than once — the last write wins." No double-check locking for singleton creation.

6. **No `@EventListener` or event bus**: No event-driven inter-component communication pattern. No Jakarta-style `@Observes` equivalent.

7. **No conditional binding** (`@ConditionalOnProperty`, `@Profile`): No environment-aware binding selection at scan time.

8. **`ProviderBinding.validate()` is a no-op**: Provider functions are not scope-leak validated — a singleton provider returning a request-scoped object is silently accepted.

9. **No `describe()` in README docs section**: The `describe()` / `DIContainerDescriptor` API is tested but not prominently documented in README (only in test table).

10. **No type stubs (.pyi) files**: `py.typed` marker exists so mypy/pyright read inline annotations. No `.pyi` stub files are generated.

11. **No integration with ASGI middleware**: No helper for wiring `container.arequest()` as FastAPI/Starlette middleware. Documented with example but no utility class.

12. **`_get_best_candidate` uses `max()` but comment says lower wins**: The test `test_highest_priority_wins_without_filter` asserts `PushFallbackNotifier` (priority=2) wins. This confirms higher-number-wins semantics, but README says "Lower number wins". Needs resolution.
