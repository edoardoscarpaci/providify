# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed

- `@Disposes` wiring attached to the first matching `ProviderBinding` in the
  whole container instead of the installing module's own — with two
  `@Configuration`s providing the same interface, the second module's
  disposer overwrote the first's and the second module's instance was never
  torn down (silent leak on `shutdown()`/`ashutdown()`). Wiring is now scoped
  to the bindings the same `install()`/`ainstall()` registered.
  **Behaviour change:** a `@Disposes(X)` on a module that declares no `X`
  provider no longer attaches to another module's binding; `validate()` now
  reports it as `UNMATCHED_DISPOSER`. (P24-DISPOSES-FIRSTMATCH, plan 014.)

### Added

- `IssueKind.DISPOSER_OVERWRITTEN`, `IssueKind.UNMATCHED_DISPOSER` — both
  `WARNING`; can newly make `report.ok` `False`.
- `DIContainer.provide()` now returns the `ProviderBinding` it registered
  (was `None`).
- `@Requires(condition=..., env=..., value=...)` — gates a class or
  `@Provider` function on a predicate evaluated lazily at resolve time, the
  third conjunct of binding activation beside `@Profile` and `@Alternative`.
  `RequiresMarker` (the stamped, frozen dataclass) and
  `ConditionEvaluationError` (raised when a predicate raises, from every
  public lookup path and from `validate()`) are new public names. See
  `plans/015-requires-conditional-registration.md`.
- `Severity.INFO` and `IssueKind.CONDITION_INACTIVE` — `container.validate()`
  reports one `CONDITION_INACTIVE` (`INFO`) issue per binding whose
  `@Requires` currently evaluates `False`; `ValidationReport.infos` is the
  new third partition alongside `.errors`/`.warnings`. `INFO` issues never
  raise and never affect `report.ok`. `repr(report)` now has a third
  `[INFO]` tier — a report with only `INFO` issues renders them where
  before they were silently omitted. `DIContainer._validated` is **not**
  set when only `INFO` issues are present, matching the existing
  errors-and-warnings-only rule.
- `@Fallback` and `FallbackMarker` — marks a class or `@Provider`
  function/method as a default binding: a candidate **only when no active
  non-fallback binding matches the same request** `(interface, qualifier,
  priority)`. Evaluated lazily, at resolve time — the same moment
  `@Profile`/`@Alternative`/`@Requires` are checked — so a shadowing binding
  registered after the fallback still wins on the next lookup. Honoured by
  `get()`, `aget()`, `get_all()`, `aget_all()`, `is_resolvable()`,
  `get_binding()`, `get_all_bindings()`, and `list[T]` multibinding
  collection — a shadowed fallback is excluded from every one of them. An
  unqualified fallback yields to a qualified non-fallback sibling only for
  an unqualified request; a fallback with its own qualifier still wins its
  own qualified request (the `qualifier="in_memory"` escape hatch keeps
  working). Mirrors Quarkus's `@DefaultBean`, adapted to providify's
  resolve-time evaluation model. See `plans/017-fallback-binding.md`.
- `IssueKind.FALLBACK_SHADOWED` — `container.validate()` reports one
  `FALLBACK_SHADOWED` (`INFO`) issue per active `@Fallback` binding whose own
  natural request is currently won by an active non-fallback binding — the
  feature working as declared, not a defect. `ValidationIssue.shadowed_by`
  (new field, emitted by `to_dict()`) names the winning binding's owner.
  Never raises, never affects `report.ok`, same tier as
  `CONDITION_INACTIVE`.
- **Open-generic provider bindings** — `container.provide(factory,
  returns=Repo[T])` (or `@Provider(returns=Repo[T])`, or a bare `-> Repo[T]`
  return annotation) whose alias args are ALL plain `TypeVar`s now registers
  an OPEN binding, served at resolve time by any closed request sharing its
  origin (`get(Repo[User])`, `get(Repo[Order])`, `get_all(Repo[User])`,
  `InjectInstances[Repo[User]]`, a bare `repo: Repo[User]` constructor
  parameter, `Lazy[Repo[User]]`, ...). Adapts Autofac's resolve-time factory
  model — Python has no runtime "instantiate the closed generic type with
  substituted parameters", so the factory receives the closed type as a
  *value* through a `type[T]`-annotated parameter, matched by TypeVar NAME
  (PEP 695-safe). Eight governing rules, all documented on
  `DIContainer.provide()`: (1) binding-side `TypeVar` is a wildcard, a
  request-side `TypeVar` and a bare request are not; (2) a closed binding
  always beats an open one, regardless of priority/order; (3) `type[T]`
  delivery by name, silently omitted for an ANNOTATED parameter that does
  not use this exact shape or name — except a factory with exactly one
  un-delivered closing value and a completely UNANNOTATED, no-default
  parameter (`lambda entity: Repo(entity)`), which gets it positionally
  (the single-`TypeVar` "positional by TypeVar" idiom); (4) `singleton=True`
  caches ONE instance PER CLOSED ALIAS — never one
  instance shared across every closed type, the "dangerous anti-pattern" a
  naive cache-by-binding implementation would default to; (5) `get_all`/
  `InjectInstances` include the open binding once per closed request, never
  expand a bare request; (6) `validate()` skips the `type[T]` parameter as a
  graph edge and checks closed requests against open bindings; (7) a
  `bound=`/constraint violation is a non-match, not a resolve-time error;
  (8) every alias arg must be all-`TypeVar` (open) or all-concrete (closed)
  — a mixed alias (`Repo[list[T]]`, `Pair[str, T]`) raises `TypeError` at
  registration. See `plans/016-open-generic-binding.md` and the README's
  "Open-generic providers" section.

### Changed

- `utils._interface_matches()` — the both-generic-alias branch now also
  matches an open binding-side alias (`Repo[T]`) against a closed request
  (`Repo[User]`) via a new wildcard check (`utils._closing_args`), on top of
  the existing literal `get_args() == get_args()` comparison. A **bare**
  request (`Repo`) still matches an open binding structurally (unchanged,
  fourth branch) — the new "an open binding needs closing args to actually
  serve a request" rule lives one layer up, in the lookup-only
  `DIContainer._binding_serves()`, so `@Disposes(Repo)`/`reset_binding(Repo)`
  keep wiring to open bindings exactly as before.
- `utils._type_name()` now renders a parameterised generic alias with SHORT
  names at every level (`"Repository[User]"`) instead of Python's own fully
  module-qualified `str()` (`"mymod.Repository[mymod.User]"`) — every
  `LookupError`/`CircularDependencyError`/`validate()` message that names a
  generic alias is correspondingly shorter and less noisy.

⚠️ **Two narrow behaviour changes**, both on registrations that were never
resolvable for a closed request before this release:

- A provider whose interface is an open alias (`returns=Repo[T]`) is no
  longer served for a **bare** origin request (`get(Repo)`) — previously it
  was served with an uninformed factory call (no type information at all);
  now it raises `LookupError`, matching `is_resolvable(Repo) is False`. A
  coexisting closed `Repo[User]` binding is unaffected and still resolves
  `get(Repo)` normally.
- A partially-open alias (`Repo[list[T]]`, `Pair[str, T]`) now raises
  `TypeError` at registration (`provide()`/`@Provider`/`install()`/`scan()`)
  instead of silently registering a binding that could never match any
  closed request.

## [2.0.1] — 2026-09-01

### Added

- `IssueKind.UNREACHABLE_PRE_DESTROY` — `container.validate()` now reports a
  `WARNING` when a `SINGLETON`-scoped `@Provider` has no `@Disposes` disposer
  and the type it produces carries a `@PreDestroy` hook that will therefore
  never run. This mirrors Jakarta CDI: instances returned from a producer
  method receive no lifecycle callbacks, so `@Disposes` is the only teardown
  path for them. See `plans/012-unreachable-pre-destroy-validation.md`.
  ⚠️ This can newly make a previously-clean `report.ok` `False` for containers
  that have this pattern — it is a `WARNING`, so `validate(raise_on_error=True)`
  (the default) still does not raise for it; a gate that inspects
  `report.errors` only will not see it.

### Changed

- `@PreDestroy` / `@Disposes` docstrings (`providify/decorator/lifecycle.py`)
  and the README corrected to state that `@PreDestroy` applies to class
  bindings only, and that `@Disposes` is the teardown path for
  provider-produced instances.

---

## [2.0.0] — 2026-08-25

First stable release. The version jumps 1.1.1 → 2.0.0 because PyPI requires a
monotonically increasing version and 1.x was already published under an Alpha
classifier — **this release contains no breaking API changes**. Interim
releases 1.0.x–1.1.1 were unannounced; their contents are consolidated into
this entry. From here the public API is committed; see the versioning and
deprecation policy in [CONTRIBUTING.md](CONTRIBUTING.md#versioning-and-deprecation-policy).

### Added

#### Pytest integration — `di_container` / `di_overrides` / `di_global` fixtures
- Installing providify now registers a `pytest11` entry-point plugin
  (`providify/pytest_plugin.py`) exposing four function-scoped, `yield`-based
  fixtures — no conftest boilerplate required: `di_container` (a fresh
  `DIContainer()`, `shutdown()` at teardown), `di_acontainer` (async mirror,
  `ashutdown()`), `di_overrides` (a `ContainerOverrides` bound to
  `di_container`, undone at teardown), and `di_global` (installs
  `di_container` as `DIContainer.current()` for the test). None are
  autouse — the plugin has zero effect until a fixture is explicitly
  requested, and a consumer conftest redefining `di_container` wins over the
  plugin's default.
  ```python
  def test_checkout(di_container, di_overrides):
      di_container.scan("myapp")
      di_overrides.instance(Clock, FrozenClock("2026-01-01"))
      di_overrides.bind(Notifier, FakeNotifier)
      di_overrides.remove(PaymentGateway)
      assert di_container.get(Checkout).run() == "ok"
      # every override undone automatically at teardown
  ```
- New class `ContainerOverrides` (`providify/testing.py`, exported from
  `providify`) — usable without pytest, as a context manager, from
  `unittest`/scripts/REPL: `instance(iface, obj)`, `bind(iface, impl)`,
  `factory(iface, fn)`, `remove(iface, *, qualifier=None)`,
  `profiles(*names)`, `alternative(cls)`, and explicit `reset()`. Undo is
  snapshot/restore (via the new `container.snapshot()`/`restore()`), taken
  lazily on the first mutation, not an inverse-operation log.
- New `DIContainer.snapshot() -> ContainerSnapshot` and
  `DIContainer.restore(snapshot)` — capture and roundtrip a container's
  bindings, singleton cache/order, enabled alternatives, active profiles, and
  registered interceptor classes. `ContainerSnapshot` is exported from
  `providify`. ⚠️ Instances created after the snapshot are dropped without
  teardown on `restore()` — configuration is restored, not lifecycle.
- `DIContainer.scoped()` gains an optional `container: DIContainer | None`
  parameter — `DIContainer.scoped(existing)` installs `existing` as the
  global for the block instead of always creating a fresh one, and does
  **not** shut it down on exit. Backward compatible — `scoped()` with no
  argument is unchanged.
- No new runtime dependency: `import providify` still never imports
  `pytest` — the plugin lives entirely in `providify/pytest_plugin.py`,
  which is not imported by `providify/__init__.py`.

#### `container.validate()` — startup-time full graph validation
- New method `container.validate(*, raise_on_error: bool = True) -> ValidationReport`
  walks the **entire declared dependency graph** once, without instantiating
  anything, and reports every wiring defect in one shot: missing bindings,
  ambiguous bindings, dependency cycles, scope leaks, `Live[T]` violations, and
  unresolvable annotations. By default it raises a single aggregate
  `ContainerValidationError` when the report contains any `ERROR`-severity
  issue, so a misconfigured app fails at boot instead of on the first
  production request; pass `raise_on_error=False` to inspect the report
  instead.
  ```python
  container.scan("myapp")
  container.validate()                                # raises on any error
  report = container.validate(raise_on_error=False)   # or inspect it
  for issue in report.errors:
      log.error("%s", issue.message)
  ```
- New module `providify/validation.py` with the report/issue types, all
  exported from `providify` and added to `__all__`:
  `ValidationReport` (`.issues`, `.checked_bindings`, `.errors`, `.warnings`,
  `.ok`, `.to_dict()`), `ValidationIssue` (`.kind`, `.severity`, `.owner`,
  `.message`, `.param_name`, `.requested`, `.qualifier`, `.candidates`),
  `IssueKind` (`MISSING_BINDING`, `MISSING_BINDING_DEFAULTED`,
  `MISSING_BINDING_DEFERRED`, `AMBIGUOUS_BINDING`, `CIRCULAR_DEPENDENCY`,
  `SCOPE_LEAK`, `LIVE_REQUIRED`, `UNRESOLVED_ANNOTATION`), and `Severity`
  (`ERROR`, `WARNING`).
- New exception `ContainerValidationError` (a `ValidationError` subclass, so
  existing `except ValidationError` handlers still catch it), exported from
  `providify`. Carries `.report`, the full `ValidationReport` that triggered
  the raise; its message is a grouped, multi-line, one-line-per-issue block.
- **`validate_bindings()` and `validate_all()` are unchanged** — they keep
  their exact current behaviour (scope-leak tier only: `Binding.validate()`
  reused verbatim, still auto-triggered on the first `get()`/`aget()`/
  `get_all()`/`aget_all()`). `validate()` is purely additive on top of them
  and never instantiates anything, so it is safe to call at any point after
  registration without side effects (no `create()`, no cache write, no
  `@PostConstruct`).

### Changed

#### Annotations are now resolved per parameter / per class attribute
- Every constructor parameter, `@Provider` parameter, and class-level annotation is
  now evaluated **individually** instead of resolving the entire signature in one
  `get_type_hints()` call. An unresolvable annotation on a parameter or attribute
  that is **not** an injection point (a `TYPE_CHECKING`-only import, a defaulted
  local type) no longer has any effect at all — no warning, nothing to configure.
  This removes most of the `AnnotationResolutionError` failures introduced by the
  scope-leak-validation change below, which promised exactly this: "the next
  release removes most of these failures — an unresolvable annotation on a
  parameter that is not an injection point will stop being fatal at all."

#### ⚠️ An unresolvable annotation on an actual injection point now raises, naming the parameter
- If a parameter or class attribute IS (or plausibly is) an injection point —
  annotated `Inject[T]`, `Lazy[T]`, `Live[T]`, `Instance[T]`, `InjectInstances[T]`,
  or a `ClassVar` wrapping one of those — and its annotation cannot be evaluated,
  the container now raises `AnnotationResolutionError` naming the exact parameter
  and the unresolvable name. This replaces two previous outcomes:
  - the old warning-and-skip, which silently injected nothing and left the
    instance half-constructed with no error at all;
  - the misleading `TypeError: missing N required positional arguments` that
    surfaced when a whole-signature resolution failure wiped out every
    parameter's hints, not just the unresolvable one.
  Migration: import the annotated type at runtime instead of guarding it behind
  `TYPE_CHECKING`, or move locally-defined types to module level — same fix as
  before, now scoped to only the injection points that actually need it.
- `AnnotationResolutionError` gained an optional `param_name` attribute, set
  whenever the failure can be attributed to one parameter or class attribute
  rather than an entire signature.

### Fixed

#### A self-referential singleton deadlocked instead of raising
- `container.get()` / `aget()` hung **forever** when a `SINGLETON`-scoped binding
  resolved back to itself during its own construction. Both singleton paths hold a
  non-reentrant per-key lock (`threading.Lock` / `asyncio.Lock`) across
  `create()` / `acreate()`, and cycle detection runs *inside* that call — so the
  re-entrant resolution blocked on a lock its own thread/task already held, before
  any cycle could be detected. It now raises `CircularDependencyError`, matching
  what the `DEPENDENT` path already did.
  The easiest way to hit this was a parameter annotated with a bare `object` (or
  `object | None`): `object` is a supertype of every registered interface, so it
  matches every binding — including the one being created. Any self-referential
  singleton reached the same lock, though.
  Affects sync and async, class bindings and `@Provider` bindings alike.

#### Provider registration no longer fails on an unrelated parameter
- `ProviderBinding.__init__` resolved a provider's return type with a
  whole-signature `get_type_hints(fn)`, so a `@Provider` with a `TYPE_CHECKING`-only
  or function-local **parameter** annotation could fail to register even when its
  return annotation was perfectly resolvable — it relied on a broad `except` and a
  hand-rolled `eval` fallback to paper over this. The return annotation is now
  evaluated on its own, so parameter annotations cannot affect registration.
  Resolved types are unchanged; `Annotated[T, ...]` returns still yield `T`, and a
  missing return annotation still raises `TypeError`.

### Removed

- Internal `NameError` tolerance in the injection path (`_resolve_hints_or_warn`
  and its whole-signature warn-and-skip behaviour) — replaced by per-parameter
  resolution above. Not a public API; no caller-visible removal beyond the
  behaviour change described above.

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

#### `returns=` — explicit interface override for `@Provider` and `provide()`
- `DIContainer.provide(fn, *, returns=None)` and `@Provider(returns=None)` accept an
  explicit binding interface: a type, a parameterised generic alias (e.g.
  `Repository[User]`), an `Annotated[...]` wrapper (unwrapped automatically), or a
  zero-arg callable evaluated once at registration time (`ProviderBinding`
  construction), not at decoration time.
- When `returns=` is given, the factory's return annotation is not read, not
  evaluated, and not validated — it can be `-> Any`, an unresolvable forward ref, or
  absent entirely. This removes the only reason a caller ever had to mutate
  `factory.__annotations__["return"]` before registering, e.g. for an interface only
  nameable as a generic alias built inside a loop.
- Precedence, highest first: `provide(fn, returns=...)` call-site override, then
  `@Provider(returns=...)` decoration-time override, then `fn`'s resolved return
  annotation.
- Omitting `returns=` changes nothing: behaviour and error messages on the
  annotation-derived path are unchanged.

### Changed

#### ⚠️ Scope-leak validation now raises instead of silently reporting clean
- `_check_scope_violation` / `_check_provider_scope_violation` / `_collect_class_var_hints`
  now raise `AnnotationResolutionError` (a `ValidationError` subclass) when a binding's
  annotations cannot be evaluated by `get_type_hints()` — instead of swallowing the failure
  and reporting the binding clean. "I don't know" is no longer reported as "you're fine".
  This can break downstream apps whose `__init__`/provider annotations are `TYPE_CHECKING`-only
  imports or otherwise unresolvable at runtime; they will now fail at container wiring
  (`validate_bindings()` / the first `get()`/`aget()` call) instead of silently passing
  validation.
  Migration: import the annotated type at runtime instead of guarding it behind
  `TYPE_CHECKING`, or move locally-defined types to module level.
  The next release (per-parameter annotation resolution) removes most of these failures —
  an unresolvable annotation on a parameter that is *not* an injection point will stop being
  fatal at all.

#### Priority direction — documentation corrected
- **Higher priority value wins** when multiple candidates match a `container.get()` call. The `priority` field on `BindingDescriptor` and all documentation previously stated "lower value wins" — this was incorrect. The code (`max()` in `_get_best_candidate`) was always correct; only the docs have been updated.
- `get_all()` returns bindings sorted **ascending** by priority (lowest first), so the highest-priority binding is last — consistent with `max()` selection in `get()`.

#### `__repr__`
- `DIContainer.__repr__` now reports scope counts and validation state:
  `DIContainer(singleton=3, request=2, dependent=6, validated=True)`

#### `@Provider` return-type resolution — fails fast instead of silently
- ⚠️ A `@Provider` whose return annotation is a quoted forward reference that
  cannot be resolved from the function's module globals (e.g. a
  `TYPE_CHECKING`-only import, or a locally-defined type) now raises
  `TypeError` at registration time, naming the provider and the offending
  annotation. Previously such a provider silently registered the *string*
  itself as the binding interface, which then corrupted dependency injection
  for every other binding in the container (see Fixed below). Callers whose
  providers previously appeared to work by accident must import the
  annotated type at runtime instead of guarding it behind `TYPE_CHECKING`.

### Fixed

- `test_live.py`: imports of `Annotated`, `LiveMeta`, `LiveProxy` moved to module level — locally-scoped imports inside test functions were invisible to `get_type_hints()` under `from __future__ import annotations`, causing silent `NameError` that left injected parameters unresolved.
- `_check_provider_scope_violation` passes `localns=self._build_localns()` to `get_type_hints()` — without this, types defined inside test/setup functions were silently dropped, causing scope-leak detection to produce false negatives.
- `@Provider` with a quoted return annotation (e.g. `-> "ProfilingSettings"`) plus an unresolvable parameter annotation could register the plain `str` `'ProfilingSettings'` as the binding interface instead of the class. `ProviderBinding` now resolves nested forward references properly and raises `TypeError` if the annotation still cannot be resolved to a type or generic alias, rather than silently accepting a string.
- `DIContainer._build_localns` no longer raises `AttributeError` and aborts building the container-wide type-hint namespace when one binding's interface is not a real type (e.g. left over from the bug above) — the malformed binding is now skipped with a `logger.warning`, and every other binding still resolves correctly.
- `_collect_kwargs_sync` / `_collect_kwargs_async` no longer swallow *every* exception from `get_type_hints()` into an empty hints dict. Only `NameError` (a genuinely unresolvable annotation) is now tolerated and logged; any other exception propagates. Previously an unrelated bug elsewhere (such as the `AttributeError` above) could silently zero out all injected keyword arguments for a completely unrelated provider or constructor, surfacing as a confusing `TypeError: ... missing N required positional arguments`.
- `_check_scope_violation` now passes `localns=self._build_localns()` to `get_type_hints()` for a `ClassBinding`'s `__init__` — its provider twin (`_check_provider_scope_violation`) already did this. Previously, a class whose `__init__` referenced a locally-defined dependency type raised `NameError`, which was swallowed and reported as "no leaks found" — a genuine `SINGLETON` → `REQUEST`/`SESSION` scope leak validated clean.
- `_inject_class_vars_sync` / `_inject_class_vars_async` no longer swallow every exception from `get_type_hints()`; only `NameError` is tolerated (and logged), matching `_collect_kwargs_*`. Previously an unrelated error left annotated class attributes unset, and the failure surfaced much later as an `AttributeError` in unrelated code.
- `_collect_dependencies` / `_get_provider_return_type` / the class-var lookup inside `_get_dependencies` now log a `logger.warning` when they swallow a `get_type_hints()` failure while building the dependency graph (`describe()`). Previously the graph silently lost nodes with no trace.

### Added

- `AnnotationResolutionError` (a `ValidationError` subclass), exported from `providify`. Raised by the scope-leak validators when a binding's annotations cannot be resolved — names the exact binding, the original exception, and how to fix it.

### Added

#### Graceful shutdown — reverse-dependency-order teardown
- `container.shutdown()` / `await container.ashutdown()` now tear down cached
  singletons in **reverse dependency (creation) order** — every dependent's
  `@PreDestroy` hook / `@Disposes` disposer runs before the dependencies it may
  still reference — matching Spring, .NET's `IServiceProvider`, and
  Quarkus/CDI. `@RequestScoped` / `@SessionScoped` teardown on scope exit gets
  the same guarantee.
- New `ShutdownError(providifyError)`, raised when one or more teardown hooks
  fail, and `ShutdownFailure` (frozen dataclass: `owner: str`,
  `exception: BaseException`), both exported from `providify`. Every failure is
  captured and reported together via `exc.failures: list[ShutdownFailure]`;
  `exc.__cause__` is chained to the exception of the *earliest-created* failing
  component (typically the most foundational one, e.g. a DB pool), not simply
  the first hook encountered during the reversed teardown walk.
  ```python
  try:
      container.shutdown()
  except ShutdownError as exc:
      for failure in exc.failures:
          log.error("teardown failed: %s", failure.owner, exc_info=failure.exception)
  ```

### Changed

#### ⚠️ `shutdown()` / `ashutdown()` now aggregate ALL teardown failures instead of raising on the first one
- Previously, the first `@PreDestroy` hook or `@Disposes` disposer to raise
  aborted `shutdown()` immediately: every remaining hook was skipped and the
  singleton caches were **never cleared**, leaking every still-cached instance.
  `shutdown()` / `ashutdown()` now run every hook regardless of earlier
  failures, **always** clear caches (even when hooks fail), and raise a single
  aggregated `ShutdownError` at the end instead of propagating the first raw
  exception.
  Migration: callers doing `try: container.shutdown() except MyDbError: ...`
  must now catch `ShutdownError` and inspect `exc.failures` (or rely on
  `exc.__cause__`, which is still chained to a root-cause exception) to find
  the original error.
- An async `@PreDestroy` hook reached from sync `shutdown()` is unaffected by
  this change: it still raises `RuntimeError` immediately (unchanged message)
  and is **not** aggregated into `ShutdownError`.

### Added

#### `@Profile` — deployment-time bean activation (env-driven `@Alternative`)
- New decorator `@Profile(*expressions: str)`, usable on classes **and**
  `@Provider` functions (including `@Configuration` bound methods and
  `@Provider @property`), gates a bean on the container's active profile
  set. Multiple expressions are OR'd (`@Profile("dev", "test")`); a leading
  `"!"` negates a single literal (`@Profile("!prod")`). Profile names are
  normalised (stripped, lower-cased) on both sides of the match. Raises
  `ValueError` at decoration time for an empty argument list or an
  empty/whitespace-only/bare-`"!"` literal.
  ```python
  @Profile("prod")
  @Singleton
  class RealMailer(Mailer): ...

  @Profile("dev", "test")
  @Singleton
  class ConsoleMailer(Mailer): ...
  ```
- The container's active profile set is resolved from an explicit
  `DIContainer(profiles=...)` argument, the `PROVIDIFY_PROFILES`
  environment variable (comma-separated, parsed via the new
  `providify.profiles` module), or imperative calls — in that precedence
  order (an explicit argument, even `()`, always wins over the environment
  variable):
  ```python
  container = DIContainer(profiles=("prod",))
  container = DIContainer()                    # reads PROVIDIFY_PROFILES
  container.activate_profile("debug")
  container.deactivate_profile("debug")
  container.active_profiles                    # frozenset[str], read-only snapshot
  ```
- `@Profile` composes with `@Alternative`: an `@Alternative` bean that also
  carries `@Profile` is governed by its profile instead of requiring an
  imperative `enable_alternative()` call — the "env-driven `@Alternative`"
  half of this feature. The profile is a hard AND-gate; `enable_alternative()`
  cannot activate a bean whose profile does not match.
- A profile-gated binding is invisible to `get()`, `get_all()`,
  `is_resolvable()`, and `validate()` when its profile doesn't match —
  `validate()` reports `MISSING_BINDING` for an interface whose only
  provider is gated by a currently-inactive profile. This is intentional
  (the graph is validated as it will actually be wired) but is worth
  calling out explicitly; see `docs/agents/usage-rules.md`.
- New `ProfileMetadata` (frozen dataclass, `.expressions: tuple[str, ...]`)
  and `Profile`, both exported from `providify`.

### Changed

#### ⚠️ `@Alternative` on a `@Provider` function is now genuinely disabled by default
- Previously, `@Alternative` stamped on a `@Provider` **function** was
  silently ignored: the activation check only inspected `ClassBinding`, so
  such a provider was *always* active — the exact opposite of what the
  decorator's own docstring promises ("disabled by default"). This is now
  fixed: `@Alternative` on a provider function behaves the same as on a
  class, and requires `enable_alternative(fn)` (or a matching `@Profile`) to
  become resolvable. Anything that stamped `@Alternative` on a provider
  function and relied on it doing nothing will now see `LookupError` until
  the provider is explicitly enabled or given a matching `@Profile`.

### Added

#### `@ConfigProperties` — typed configuration binding (env / YAML / JSON / TOML)
- New decorator `@ConfigProperties(*, prefix=None, sources=None)` marks a class
  (a dataclass, a plain class with an annotated `__init__`, or a pydantic
  `BaseModel`) as a typed settings target, and `container.bind_config(cls, *,
  sources=None)` binds it into a singleton — no hand-written `@Provider` that
  reads `os.environ` needed.
  ```python
  from dataclasses import dataclass
  from providify import DIContainer, ConfigProperties, EnvSource, YamlSource

  @ConfigProperties(prefix="db", sources=(EnvSource(), YamlSource("config.yaml", required=False)))
  @dataclass(frozen=True)
  class DbSettings:
      url: str
      pool_size: int = 5

  container = DIContainer()
  container.bind_config(DbSettings)   # …or container.scan("myapp") discovers it
  container.get(DbSettings)           # DB__URL / DB__POOL_SIZE / config.yaml's db: section
  ```
- New sources, all exported from `providify`: `EnvSource` (`PREFIX__FIELD` env
  nesting), `JsonSource`, `TomlSource` (stdlib `tomllib`), `YamlSource`
  (requires the new `providify[yaml]` extra), and `DictSource` (in-memory, the
  test seam) — plus the `ConfigSource` `Protocol` for third-party sources.
  Sources are loaded in order and deep-merged (later wins, keys normalised
  case-insensitively); `prefix` selects a subtree of the merged mapping.
- If the target class exposes `model_validate` (pydantic v2 or anything
  API-compatible), providify hands it the merged mapping verbatim and performs
  **no** coercion of its own — full pydantic validation without providify
  depending on pydantic. Otherwise, declared fields are coerced per a bounded
  stdlib coercion table (`str`, `int`, `float`, `bool`, `Path`, sequences,
  `dict`, `X | None`, `Enum`, `Literal`, nested dataclasses).
- Every field failure is aggregated into a single `ConfigBindingError`
  (`.target`, `.issues: list[ConfigIssue]`) rather than failing on the first
  bad field. Binding is **lazy**: sources are read and errors surface on the
  first `get(cls)`, not at `bind_config()` registration time — call
  `container.warm_up()` to catch a bad config value at startup (`validate()`
  does not instantiate anything, so it will not catch this).
- New optional install extra: `pip install providify[yaml]` (`PyYAML>=6.0`)
  for `YamlSource`. The core install remains dependency-free; pydantic, if
  used as a `@ConfigProperties` target, is never a providify dependency
  (`model_validate` is duck-typed by attribute name only).

### Added

#### Multi-module startup/shutdown ordering — `@Configuration(depends_on=...)` (F5)
- `@Configuration` is now dual-form: `@Configuration` (bare), `@Configuration()`
  (empty parens), and `@Configuration(depends_on=[OtherModule])` all work, matching
  the shape of `@Provider`/`@Component`. `depends_on` accepts a single class, a
  list, or a tuple — all normalise to `tuple[type, ...]`.
  ```python
  @Configuration
  class InfraModule:
      @Provider(singleton=True)
      def pool(self) -> DatabasePool: ...

  @Configuration(depends_on=[InfraModule])
  class RepoModule:
      def __init__(self, pool: DatabasePool) -> None: ...   # resolvable
  ```
- `container.install()` / `ainstall()` now install a module's `depends_on`
  closure **transitively**, in a deterministic order (deps before dependents,
  shared dependencies installed exactly once) — new pure module
  `providify/modules.py` (`resolve_install_order()`, `module_dependencies()`)
  does the DFS post-order sort, imports nothing from `container.py`.
- New `ModuleCycleError` (exported from `providify`) — raised when
  `depends_on` edges form a cycle, naming every class in the cycle in order
  (`A → B → C → A`). Raised **before** any module is instantiated, so a
  cycle leaves the container completely untouched — no partial installation.
- A module's `@PostConstruct` hook now runs once, at install time, after
  `__init__` and before its `@Provider` methods are registered.
- A module's `@PreDestroy` hook now runs at `shutdown()`/`ashutdown()`, in
  **exact reverse install order**, strictly *after* every singleton has
  already been torn down (a second, later teardown phase — plan 004's
  singleton teardown is completely unchanged and still runs first). Failures
  are aggregated into the same `ShutdownError` as singleton teardown
  failures, with `owner == "ModuleClassName.hook_name"`. An `async def`
  module `@PreDestroy` reached from sync `shutdown()` raises `RuntimeError`
  pointing at `ashutdown()`, matching the existing singleton-hook guard.
  `container.copy()` inherits install-order/dedup history but never the
  original's module instances' ownership — a copy's `shutdown()` runs no
  module `@PreDestroy` hooks.

### Fixed

#### `scan()` after an explicit `install()` no longer double-registers a module
- Dedup authority for `@Configuration` modules moved from the scanner (a
  same-session-only `set[type]`) to the container's new
  `_installed_modules` dict. Previously, `container.install(M)` followed by
  `container.scan(...)` covering `M` registered every `@Provider` on `M`
  **twice** — the scanner's dedup set had no visibility into the earlier
  explicit `install()` call. `install()`/`ainstall()`/`scan()` now all check
  the same container-level record, so installing (or discovering) a module
  more than once is a no-op past the first time.

### Changed

#### ⚠️ Module `@PostConstruct`/`@PreDestroy` hooks now actually run
- Previously, a `@Configuration` module's `@PostConstruct` and `@PreDestroy`
  hooks were silently never invoked — `install()` never called
  `_run_post_construct_sync`, and the container kept no reference to the
  module instance after registering its providers, so `shutdown()` could
  not find it to run `@PreDestroy` either. Both now run (see Added above).
  Any module that already carried one of these hooks — written in the
  expectation it would run, or copy-pasted from a class that has one — will
  now execute it. This is the intended fix, but it is a runtime behaviour
  change on existing code.

### Added

#### Multibinding — `container.multibind()` / `@Multibound` / bare `list[T]` (F7)
- A bare `list[T]` annotation now injects every active binding for `T`,
  priority-ascending, once `T` has been explicitly declared a collection
  point via `container.multibind(T)` or the new `@Multibound` class
  decorator. Contributions need no marker of their own — register them
  normally with `bind()`, `register()`, or `provide()`.
  ```python
  container.multibind(Handler)          # or: @Multibound on class Handler
  container.bind(Handler, HandlerA)
  container.bind(Handler, HandlerB)

  @Component
  class Dispatcher:
      def __init__(self, handlers: list[Handler]) -> None: ...   # all impls
  ```
- A declared collection point with zero contributions resolves to `[]`
  instead of raising — deliberately different from `get_all()`/
  `InjectInstances[T]`, which keep raising `LookupError` on zero matches;
  declaring the collection point is itself the statement that zero is a
  legal count.
- `list[T]` where `T` is undeclared still resolves to `_UNRESOLVED`
  (unchanged v1 behaviour) unless a literal binding for `list[T]` exists
  (e.g. `provide(fn, returns=list[T])`), which always takes precedence over
  collection behaviour. `validate_bindings()` now flags the case where both
  a collection point and a literal `list[T]` binding exist for the same
  type.
- `InjectInstances[T]` is unchanged and still the form to use when you
  cannot decorate `T` and did not call `multibind()`.
- New export: `Multibound` (`providify/decorator/multibinding.py`).

#### Field-level interceptors — `Advised` / `@AroundGet` / `@AroundSet` (F8)
- Interceptors can now advise field reads and writes, not just method calls.
  This is a **deliberate extension beyond Jakarta CDI**, not a gap being
  closed — Jakarta Interceptors 2.1 defines exactly five interception types
  (`@AroundInvoke`, `@AroundTimeout`, `@PostConstruct`, `@PreDestroy`,
  `@AroundConstruct`), none field-level, in either the Lite or Full profile.
  The reference model is AspectJ's `get`/`set` pointcuts, implemented here
  via Python's data-descriptor protocol — the same technique Django ORM
  fields, SQLAlchemy columns, and Traitlets use — rather than AspectJ's
  compile-time bytecode weaving.
  ```python
  @Interceptor
  @Audited
  class AuditInterceptor:
      @AroundSet
      def on_set(self, ctx: FieldAccessContext) -> None:
          log.info("%s.%s = %r", type(ctx.target).__name__, ctx.field, ctx.value)
          ctx.proceed()

  @Component
  @Audited
  class Account:
      balance: float = Advised(0.0)      # declared join point
  ```
- Only fields whose class body assigns `Advised(...)` are join points — no
  `__getattribute__`/`__setattr__` override is installed on the target
  class, so every other attribute costs nothing.
- Advice is armed only for container-managed instances, and only after
  `binding.create()` returns — writes performed by `__init__`, class-var
  injection, and `@PostConstruct` are never advised. An instance built
  directly (`Account()`, outside the container) behaves like a plain
  attribute.
- `dataclasses`, frozen `dataclasses`, pydantic `BaseModel`s, and `attrs`
  classes are rejected as `Advised` targets with a `TypeError` naming the
  class and field — a frozen dataclass's `object.__setattr__`-based
  `__init__` would otherwise bypass the descriptor and silently corrupt
  reads.
- No `__delete__` advice — `del obj.field` always passes through unadvised,
  matching AspectJ's `get`/`set`-only pointcuts.
- New exports: `Advised`, `FieldAccessContext` (`providify/field.py`),
  `AroundGet`, `AroundSet` (`providify/decorator/interceptor.py`).

### Changed

#### ⚠️ Interceptor instances are now shared, container-scoped singletons
- `_apply_interceptors()` previously constructed a **fresh instance of each
  matching `@Interceptor` class for every bean it wrapped**. It now resolves
  each interceptor class **once per container** (via the new
  `_resolve_interceptor_instance()` / `self._interceptor_instances` cache)
  and reuses that same instance across every bean it advises — needed so
  that a bean's method advice (`@AroundInvoke`) and its field advice
  (`@AroundGet`/`@AroundSet`, both introduced in this release) observe a
  consistent interceptor object, and so a `container.get(InterceptorClass)`
  caller sees the same instance the chain uses. If an existing interceptor
  keeps mutable per-bean state on `self` (uncommon, but possible), that state
  is now shared across every bean it advises instead of being reset per bean
  — audit any interceptor that assigns to `self` outside `__init__`.

#### Packaging
- PyPI classifier upgraded from `Development Status :: 3 - Alpha` to
  `Development Status :: 5 - Production/Stable`.

### Fixed

#### `_InterceptorProxy` now supports attribute writes and deletes
- A bean wrapped by `_InterceptorProxy` (i.e. carrying an `@AroundInvoke`
  interceptor) previously raised `AttributeError` on `proxy.attr = value` or
  `del proxy.attr` — the proxy's `__slots__` had no `__setattr__`/
  `__delattr__`, so writes never reached the wrapped target. Both now
  delegate through to `_target`. Pre-existing bug, exposed while wiring up
  field interceptors (F8) — writes to any intercepted bean's attributes now
  work as expected instead of raising.

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

[Unreleased]: https://github.com/edoardoscarpaci/providify/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/edoardoscarpaci/providify/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/edoardoscarpaci/providify/releases/tag/v2.0.0
[0.1.7]: https://github.com/edoardoscarpaci/providify/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/edoardoscarpaci/providify/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/edoardoscarpaci/providify/compare/v0.1.4a2...v0.1.5
[0.1.4a2]: https://github.com/edoardoscarpaci/providify/compare/v0.1.4a1...v0.1.4a2
[0.1.4a1]: https://github.com/edoardoscarpaci/providify/compare/v0.1.3...v0.1.4a1
[0.1.3]: https://github.com/edoardoscarpaci/providify/releases/tag/v0.1.3
