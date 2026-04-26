# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — v0.2.0

### Added

#### Container mutation & introspection
- `container.override(interface, implementation)` — replaces **all** existing bindings for an interface in-place, evicts the singleton cache, and resets the validated flag. Useful for test overrides and hot-swap scenarios.
- `container.reset_binding(interface, *, qualifier=None) -> int` — removes matching bindings, evicts cache entries, and returns the number of bindings removed.
- `container.get_binding(interface, *, qualifier, priority) -> AnyBinding` — pure-read lookup; raises `LookupError` if no match.
- `container.get_all_bindings(interface, *, qualifier=None) -> list[AnyBinding]` — pure-read; returns an empty list instead of raising when no bindings exist.

#### Thread safety
- Per-key `threading.Lock` instances (`_singleton_locks`) with a guard lock (`_singleton_lock_guard`) implement double-check locking for singleton instantiation, preventing double-construction under concurrent access.

#### Lifecycle hooks on scope exit
- `ScopeContext` now accepts `on_scope_exit` and `on_scope_exit_async` callbacks. The container wires these to call `@PreDestroy` hooks when a `request()` or `session()` scope exits (both sync and async variants). Previously `@PreDestroy` only fired on full container shutdown.

#### Optional proxy types
- `Lazy[T | None]` — resolves to `None` instead of raising `LookupError` when no binding is registered for `T`.
- `Live[T | None]` — same optional behaviour for live (request/session-scoped) proxies.
- Both pipe-union forms (`T | None`) map to `optional=True` on `LazyMeta` / `LiveMeta`.

#### Provider scope-leak detection
- `ProviderBinding.validate()` now inspects `@Provider` function parameters for scope leaks (e.g. a `SINGLETON`-scoped provider that directly injects a `REQUEST`-scoped dependency). Previously only class-based bindings were validated.
- Uses `localns=container._build_localns()` when calling `get_type_hints()` so locally-defined types resolve correctly.

#### `@Named` improved error message
- `@Named("smtp")` (positional string instead of `name=`) now raises:
  `TypeError: @Named requires a keyword argument: use @Named(name='smtp') instead of @Named('smtp').`
  Previously the runtime produced an opaque `TypeError: 'str' object is not callable`.

### Changed

#### Priority direction — documentation corrected
- **Higher priority value wins** when multiple candidates match a `container.get()` call. The `priority` field on `BindingDescriptor` and all documentation previously stated "lower value wins" — this was incorrect. The code (`max()` in `_get_best_candidate`) was always correct; only the docs have been updated.
- `get_all()` returns bindings sorted **ascending** by priority (lowest first), so the highest-priority binding is last — consistent with `max()` selection in `get()`.

#### `__repr__`
- `DIContainer.__repr__` now reports scope counts and validation state:
  `DIContainer(singleton=3, request=2, dependent=6, validated=True)`

### Fixed

- `test_live.py`: imports of `Annotated`, `LiveMeta`, `LiveProxy` moved to module level — locally-scoped imports inside test functions were invisible to `get_type_hints()` under `from __future__ import annotations`, causing silent `NameError` that left injected parameters unresolved.
- `_check_provider_scope_violation` passes `localns=self._build_localns()` to `get_type_hints()` — without this, types defined inside test/setup functions were silently dropped, causing scope-leak detection to produce false negatives.

---

## [0.1.7] — 2026-04-26

### Changed
- Improved documentation on inheritance and MRO-based resolution behaviour.
- `DefaultContainerScanner` now self-binds concrete classes so they are directly resolvable without an explicit abstract base.

---

## [0.1.6] — 2026-04-25

### Added
- `@Configuration` class support in `DefaultContainerScanner` — scanner discovers and registers `@Provider`-decorated methods on `@Configuration` classes automatically.
- Scan idempotency — calling `scan()` multiple times on the same package no longer registers duplicates.

### Fixed
- `DefaultContainerScanner` now checks for provider metadata before attempting registration, preventing false positives on plain methods.
- Configurator classes are auto-registered so their `@Provider` methods are reachable at resolution time.

---

## [0.1.5] — 2026-04-24

### Added
- Union and `Optional[T]` type hint resolution — `container.get(T | None)` resolves to `T` when a binding exists and returns `None` otherwise.

---

## [0.1.4a2] — 2026-04-20

### Added
- `Instance[T]` wrapper support.
- `container.is_resolvable(T)` — returns `True` if at least one binding exists for `T`.
- Class-level (`ClassVar`-wrapped) injection support.

---

## [0.1.4a1] — 2026-04-18

### Added
- `Live[T]` proxy for deferred resolution of request/session-scoped dependencies from a singleton context.

---

## [0.1.3] — 2026-04-10

### Added
- Generic type support: `container.bind(Repository[User], UserRepository)`.

---

[Unreleased]: https://github.com/edoardoscarpaci/providify/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/edoardoscarpaci/providify/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/edoardoscarpaci/providify/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/edoardoscarpaci/providify/compare/v0.1.4a2...v0.1.5
[0.1.4a2]: https://github.com/edoardoscarpaci/providify/compare/v0.1.4a1...v0.1.4a2
[0.1.4a1]: https://github.com/edoardoscarpaci/providify/compare/v0.1.3...v0.1.4a1
[0.1.3]: https://github.com/edoardoscarpaci/providify/releases/tag/v0.1.3
