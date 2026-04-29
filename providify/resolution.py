from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

# ─────────────────────────────────────────────────────────────────
#  Resolution stack — tracks the current dependency chain per task/thread
#
#  DESIGN: ContextVar instead of threading.local because async tasks
#  share a thread — threading.local would bleed state across concurrent
#  coroutines on the same thread. Each asyncio.Task (and each OS thread)
#  gets its own isolated copy via contextvars.copy_context().
#
#  Thread safety:  ✅ ContextVar is safe — each thread has its own copy.
#  Async safety:   ✅ Each asyncio Task inherits a copy on creation,
#                  so concurrent resolutions never see each other's stack.
# ─────────────────────────────────────────────────────────────────

_resolution_stack: ContextVar[list[type]] = ContextVar(
    "resolution_stack",
    default=[],
)


def _current_stack() -> list[type]:
    """Return the current resolution stack for this thread/task.

    Returns:
        The list of types currently being constructed, outermost first.
        An empty list when no resolution is in progress.
    """
    return _resolution_stack.get()


def _format_cycle(stack: list[type], cls: type) -> str:
    """Format a human-readable description of the detected dependency cycle.

    Args:
        stack: The current resolution stack — types already being constructed
               (outermost to innermost).
        cls:   The type whose resolution would close the cycle.

    Returns:
        A string like ``"A → B → C → A"`` where the last element is *cls*.

    Example:
        >>> _format_cycle([A, B], C)
        'A → B → C'
    """
    chain = stack + [cls]
    return " → ".join(c.__name__ for c in chain)


# ─────────────────────────────────────────────────────────────────
#  Unresolved sentinel
#
#  DESIGN: plain object() instead of None — None is a valid resolved value
#  (e.g. an Optional dependency intentionally bound to None).
#  Final prevents accidental reassignment; the identity check
#  (resolved_value is _UNRESOLVED) must remain stable for the lifetime
#  of the process.
# ─────────────────────────────────────────────────────────────────

_UNRESOLVED: Final[object] = object()


# ─────────────────────────────────────────────────────────────────
#  Current injection point — set during _collect_kwargs so that
#  InjectionPoint can be injected into constructor parameters.
#
#  DESIGN: ContextVar so concurrent async tasks each see their own
#  injection context — same reasoning as _resolution_stack.
# ─────────────────────────────────────────────────────────────────

if TYPE_CHECKING:
    from .type import InjectionPoint

_current_injection_point: ContextVar[InjectionPoint | None] = ContextVar(
    "current_injection_point",
    default=None,
)
