from __future__ import annotations

import inspect
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])
# ─────────────────────────────────────────────────────────────────
#  LifecycleMarker — plain typed object, NOT a descriptor
#  Stored directly on the function via __dict__
#  Same pattern as DIMetadata and ProviderMetadata
# ─────────────────────────────────────────────────────────────────


class LifecycleMarker:
    """
    Plain typed metadata object — marks a method as a lifecycle hook.

    NOT a descriptor — does not intercept attribute access.
    Stored directly on the function object via __dict__,
    exactly like ProviderMetadata.

    Discovery uses vars(cls) + isinstance() — same as before,
    but now __get__ never fires because we're not a descriptor.
    """

    __slots__ = ("fn_name", "is_async", "fn_module")

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn_name = fn.__name__  # store name only — not the function itself
        self.fn_module = fn.__module__
        self.is_async = inspect.iscoroutinefunction(fn)

    def __getstate__(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in self.__slots__}

    def __setstate__(self, state: dict[str, Any]) -> None:
        for slot, value in state.items():
            object.__setattr__(self, slot, value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LifecycleMarker):
            return NotImplemented
        return self.fn_name == other.fn_name and self.is_async == other.is_async

    def __hash__(self) -> int:
        return hash(self.fn_name + self.fn_module)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.fn_name!r}, is_async={self.is_async})"


class PostConstructMarker(LifecycleMarker):
    """Marks a method as @PostConstruct."""

    __slots__ = ()


class PreDestroyMarker(LifecycleMarker):
    """Marks a method as @PreDestroy."""

    __slots__ = ()


# ─────────────────────────────────────────────────────────────────
#  Single attribute name — storage slot, not semantic key
#  Stored on the FUNCTION, not on the class
# ─────────────────────────────────────────────────────────────────

_LIFECYCLE_ATTR = "__di_lifecycle__"


def _get_lifecycle_marker(fn: Callable[..., Any]) -> LifecycleMarker | None:
    """Reads LifecycleMarker from a function's own __dict__.

    Uses getattr(..., None) instead of fn.__dict__ directly because
    C-level callables found while walking the MRO (e.g. object.__new__,
    classmethod_descriptors in vars(object)) are builtin_function_or_method
    objects that do NOT expose __dict__ — accessing it raises AttributeError.
    Module-level builtins (len, print) do have __dict__; class-level C methods
    do not. getattr is the safe fallback for both cases.
    """
    d = getattr(fn, "__dict__", None)
    if d is None:
        return None
    val = d.get(_LIFECYCLE_ATTR)
    return val if isinstance(val, LifecycleMarker) else None


def _set_lifecycle_marker(fn: Callable[..., Any], marker: LifecycleMarker) -> None:
    """Stamps LifecycleMarker onto a function."""
    fn.__dict__[_LIFECYCLE_ATTR] = marker


def _find_lifecycle_hook(
    cls: type,
    marker_type: type[LifecycleMarker],
) -> LifecycleMarker | None:
    """
    Finds a lifecycle hook of the given marker type on a class.

    Iterates vars(base).values() — raw function objects, no __get__ firing.
    Checks fn.__dict__ for the marker via isinstance().
    Returns (method_name, marker) tuple — container uses name to call it.

    Walks MRO — subclasses inherit parent lifecycle hooks.
    Raises if more than one of the same type exists on the same class.
    """
    found: list[tuple[type, str, LifecycleMarker]] = []

    for base in cls.__mro__:
        for name, val in vars(base).items():
            if not callable(val):
                continue
            marker = _get_lifecycle_marker(val)  # ✅ reads from fn.__dict__
            if isinstance(marker, marker_type):  # ✅ isinstance — type is signal
                found.append((base, name, marker))

    if not found:
        return None

    own = [(name, marker) for base, name, marker in found if base is cls]
    if len(own) > 1:
        raise TypeError(
            f"{cls.__name__} defines more than one "
            f"@{marker_type.__name__.replace('Marker', '')}. "
            f"Only one is allowed per class."
        )

    _, name, marker = found[0]
    return marker


def _find_post_construct(cls: type) -> LifecycleMarker | None:
    return _find_lifecycle_hook(cls, PostConstructMarker)


def _find_pre_destroy(cls: type) -> LifecycleMarker | None:
    return _find_lifecycle_hook(cls, PreDestroyMarker)


# ─────────────────────────────────────────────────────────────────
#  Public decorators — stamp marker onto the function, return it unchanged
# ─────────────────────────────────────────────────────────────────
def PostConstruct(fn: F) -> F:
    """
    Marks a method to be called after full injection.

    Stamps a PostConstructMarker onto the function's __dict__ —
    the function itself is returned UNCHANGED.

    No descriptor, no __get__ interception — instance.setup()
    works exactly as if @PostConstruct wasn't there.

    Equivalent to Jakarta's @PostConstruct.
    """
    _set_lifecycle_marker(fn, PostConstructMarker(fn))
    return fn  # ✅ return original function — no wrapping, no descriptor


def PreDestroy(fn: F) -> F:
    """
    Marks a method to be called on shutdown or scope teardown.
    Stamps a PreDestroyMarker onto the function's __dict__.
    Returns the function unchanged.

    Equivalent to Jakarta's @PreDestroy.
    """
    _set_lifecycle_marker(fn, PreDestroyMarker(fn))
    return fn  # ✅ return original function — no wrapping, no descriptor


# ─────────────────────────────────────────────────────────────────
#  @Disposes — provider teardown methods
# ─────────────────────────────────────────────────────────────────

_DISPOSES_ATTR = "__di_disposes__"
_OBSERVES_ATTR = "__di_observes__"


class DisposesMarker(LifecycleMarker):
    """Marks a @Configuration method as the disposer for a provider-produced type."""

    __slots__ = ("disposed_type",)

    def __init__(self, fn: Callable[..., Any], disposed_type: type) -> None:
        super().__init__(fn)
        self.disposed_type = disposed_type

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state["disposed_type"] = self.disposed_type
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        super().__setstate__(state)
        object.__setattr__(self, "disposed_type", state["disposed_type"])


class ObservesMarker(LifecycleMarker):
    """Marks a method as an observer for a specific event type."""

    __slots__ = ("event_type",)

    def __init__(self, fn: Callable[..., Any], event_type: type) -> None:
        super().__init__(fn)
        self.event_type = event_type

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state["event_type"] = self.event_type
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        super().__setstate__(state)
        object.__setattr__(self, "event_type", state["event_type"])


def _get_disposes_marker(fn: Callable[..., Any]) -> DisposesMarker | None:
    d = getattr(fn, "__dict__", None)
    if d is None:
        return None
    val = d.get(_DISPOSES_ATTR)
    return val if isinstance(val, DisposesMarker) else None


def _get_observes_marker(fn: Callable[..., Any]) -> ObservesMarker | None:
    d = getattr(fn, "__dict__", None)
    if d is None:
        return None
    val = d.get(_OBSERVES_ATTR)
    return val if isinstance(val, ObservesMarker) else None


def Disposes(disposed_type: type) -> Callable[[F], F]:
    """
    Marks a @Configuration method as the teardown handler for provider-produced instances.

    Usage::

        @Configuration
        class MyModule:
            @Provider
            def produce_conn(self) -> Connection:
                return Connection()

            @Disposes(Connection)
            def close_conn(self, conn: Connection) -> None:
                conn.close()

    Equivalent to Jakarta's @Disposes parameter annotation.
    """

    def decorator(fn: F) -> F:
        fn.__dict__[_DISPOSES_ATTR] = DisposesMarker(fn, disposed_type)
        return fn

    return decorator


def Observes(event_type: type) -> Callable[[F], F]:
    """
    Marks a method as an observer for events of the given type.

    The method is called when an ``Event[T].fire(event)`` is invoked and
    ``event`` is an instance of ``event_type`` (or a subclass).

    Usage::

        @Component
        class AuditListener:
            @Observes(UserCreatedEvent)
            def on_user_created(self, event: UserCreatedEvent) -> None:
                print(f"User created: {event.user_id}")

    Equivalent to Jakarta's @Observes parameter annotation.
    """

    def decorator(fn: F) -> F:
        fn.__dict__[_OBSERVES_ATTR] = ObservesMarker(fn, event_type)
        return fn

    return decorator
