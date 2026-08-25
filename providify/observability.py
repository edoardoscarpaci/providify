"""Container observability events (Plan 009 / F6) — creation, disposal, scope.

DESIGN — module isolation (mirrors `validation.py`'s isolation rule, see that
module's header comment): this is a **pure** module — stdlib +
`providify.metadata` (for `Scope`) only. It must NEVER import `container.py`.
Import direction is one-way: `container.py` -> `observability.py`. The four
event dataclasses below are plain, inert value objects; all the logic that
constructs, times and dispatches them (`add_hook()`, `remove_hook()`,
`_emit()`, and every instrumentation call site) lives in `container.py`,
which is the only thing that has the state (locks, caches, teardown plans)
these events describe.

DESIGN — why frozen dataclasses, not positional args: research
`design/observability-hooks/research/001-otel-hook-patterns.md` §4 compares
positional-arg callbacks (OTel HTTP instrumentation's
`request_hook(span, request)`) against frozen dataclass event objects and,
per its Librarian's Note, lands on frozen dataclasses: *"expose synchronous
hooks as frozen dataclass event objects (single parameter, extensible,
async-safe by context, aligns with providify's existing
ValidationIssue/ShutdownFailure pattern)."* `@dataclass(frozen=True,
slots=True, kw_only=True)` is research 001 §4's named emerging best
practice — frozen + slots keeps the objects small and immutable (safe to
hand to arbitrary consumer code, including across threads), kw_only means a
future optional field with a default stays a backward-compatible addition
rather than a positional-signature break.

DESIGN — this is container TELEMETRY, not the `Event[T]` / `@Observes`
application-event system (`container.py`'s `Event[T]`/`@Observes` dispatcher,
documented around `container.py:1464-1543`). That system resolves *observer
beans* through the container and dispatches *domain* events the application
defines. This module's events describe what the CONTAINER itself did
(instance creation/disposal, scope transitions) and are delivered to *plain
callables* registered via `container.add_hook()` — never through container
resolution. The two systems share no code, no state, and deliberately avoid
overlapping names (`ContainerEvent` is the one place this module uses the
word "event" in a type name, and it is a type alias, not a decorator).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .metadata import Scope


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceCreated:
    """Emitted once, right after a binding produces a new instance.

    Never emitted for a cache hit — only actual creation. See
    `container.py`'s `_instantiate_sync`/`_instantiate_async` for the two
    (sync/async) emission sites and `container.py`:1938-1974/1976-2060 for
    the guarded-timer shape this event's `duration_ns` comes from.

    Attributes:
        interface: The bound interface (a type or a parameterised generic
            alias) that was resolved. Carried as an object, not a name — a
            consumer can filter with `e.interface is DatabasePool`; a label
            is one `getattr(e.interface, "__name__", str(e.interface))` away.
        implementation: The concrete class for a `ClassBinding`, or the
            factory callable for a `ProviderBinding`. `None` only if a
            future binding kind has no single implementation object.
        scope: The `Scope` the binding was registered under.
        qualifier: The binding's qualifier, if any (`str | type | None`).
        duration_ns: Wall-clock nanoseconds spent in `binding.create()` /
            `binding.acreate()` plus post-construct hooks and interceptors
            — the guarded window documented in `container.py`'s
            `_instantiate_sync`/`_instantiate_async`.
        is_async: `True` when produced via `aget()`/`acreate()`, `False`
            when produced via `get()`/`create()`.
    """

    interface: Any
    implementation: type | Callable[..., Any] | None
    scope: Scope
    qualifier: str | type | None
    duration_ns: int
    is_async: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceDisposed:
    """Emitted once per torn-down instance, whether or not its teardown hook raised.

    Emission sites: `shutdown()`/`ashutdown()` for SINGLETON-scoped
    instances, and `_run_pre_destroy_for_scope`/`_arun_pre_destroy_for_scope`
    for REQUEST/SESSION-scoped instances. A binding with no `@PreDestroy`/
    `@Disposes` hook emits nothing — there was nothing to dispose.

    Attributes:
        interface: The bound interface that was torn down.
        implementation: The concrete class, or the factory callable for a
            `ProviderBinding`.
        scope: The `Scope` the disposed instance belonged to.
        owner: The same `"ClassName.hook_name"` / `"@Disposes(fn_name)"`
            label `ShutdownFailure.owner` uses (`container.py`'s
            `_owner_label`, ~3627-3641) — deliberately reused, not
            reformatted, so a hook consumer and a `ShutdownError.failures`
            consumer see identical labels for the same component.
        duration_ns: Wall-clock nanoseconds spent running the teardown hook
            or disposer.
        error: The exception the teardown hook raised, or `None` on a clean
            teardown. Carrying the exception (rather than just a bool) lets
            a hook log/report the failure without re-deriving it.
    """

    interface: Any
    implementation: type | Callable[..., Any] | None
    scope: Scope
    owner: str
    duration_ns: int
    error: BaseException | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeEntered:
    """Emitted when a `request()`/`arequest()`/`session()`/`asession()` frame is entered.

    Emitted from the container façade (`container.py`'s `request()` etc.,
    ~3330-3397), never from `scope.py`'s `ScopeContext` — `ScopeContext` is
    deliberately kept generic (`scope.py:43-46`); container-side telemetry
    stays in the container. Calling `container.scope_context.request()`
    directly bypasses this instrumentation entirely — documented, not a bug.

    Attributes:
        kind: `"request"` or `"session"` — which façade produced this frame.
        scope_id: The id yielded by the underlying `ScopeContext` context
            manager (a fresh id for `request()`, the given/generated session
            id for `session()`).
    """

    kind: Literal["request", "session"]
    scope_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeExited:
    """Emitted when a `request()`/`arequest()`/`session()`/`asession()` frame is exited.

    Always emitted — including when the `with`/`async with` block raises
    (emitted from a `finally`) — so every `ScopeEntered` has a matching
    `ScopeExited`.

    Attributes:
        kind: `"request"` or `"session"` — matches the paired `ScopeEntered`.
        scope_id: Matches the paired `ScopeEntered.scope_id`.
        duration_ns: Wall-clock nanoseconds the frame was open for.
    """

    kind: Literal["request", "session"]
    scope_id: str
    duration_ns: int


# One callback per exact event TYPE (container.add_hook(InstanceCreated, cb))
# dispatches on `type(event)`, never `isinstance` — see container.py's
# `_emit()` DESIGN comment for why (a hook for `object` must not receive
# `InstanceCreated` events).
ContainerEvent: TypeAlias = (  # noqa: UP040 — matches binding.py's AnyBinding precedent
    InstanceCreated | InstanceDisposed | ScopeEntered | ScopeExited
)
