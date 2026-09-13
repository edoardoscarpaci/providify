from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final

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


# ─────────────────────────────────────────────────────────────────
#  Singleton creation guard — tracks which singleton cache keys the
#  CURRENT thread/task is already inside `create()` for.
#
#  WHY THIS EXISTS (and why _resolution_stack is not enough):
#  `_instantiate_sync`/`_instantiate_async` acquire a per-key lock and
#  hold it across `binding.create()`. Cycle detection (`_check_cycle`)
#  runs *inside* create(), so a singleton that resolves back to itself
#  re-entered the lock before any cycle could be detected — and both
#  `threading.Lock` and `asyncio.Lock` are non-reentrant, so the call
#  blocked on a lock its own thread/task already held. A permanent hang,
#  not an error.
#
#  DESIGN: a ContextVar of (container id, cache key) pairs.
#  Tradeoffs:
#    ✅ Isolated per thread AND per asyncio Task, so it flags only
#       SAME-context re-entry — a different thread legitimately waiting
#       on the per-key lock is untouched, preserving double-check locking.
#    ✅ Leaves the fast lock non-reentrant, so the "exactly one instance"
#       guarantee is unchanged (an RLock would instead let a re-entrant
#       call construct a second instance and cache the wrong one).
#    ❌ One extra ContextVar set/reset per cold singleton creation —
#       negligible next to constructing the object, and skipped entirely
#       on the cache-hit fast path.
#
#  The container's `id()` is used rather than the container itself so the
#  guard never keeps a container alive; the id is only compared while that
#  container is on the stack, so recycling cannot cause a false match.
# ─────────────────────────────────────────────────────────────────

_singleton_in_progress: ContextVar[frozenset[tuple[int, Any]]] = ContextVar(
    "singleton_in_progress",
    default=frozenset(),
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

    Edge cases:
        - A stack entry is a parameterised generic alias (e.g. an open
          binding's closed cycle-key override, ``Repo[User]`` — plan 016)
          → rendered via ``_type_name`` as ``"Repo[User]"``, not bare
          ``c.__name__`` — a generic alias delegates ``__name__`` to its
          origin type via ``__getattr__``, so a plain ``c.__name__`` would
          silently collapse ``Repo[User]`` and ``Repo[Order]`` to the same
          misleading ``"Repo"`` in the message.

    Example:
        >>> _format_cycle([A, B], C)
        'A → B → C'
    """
    # Local import — resolution.py is a low-level module imported by both
    # container.py and utils.py; importing _type_name from utils here (not
    # at module top) avoids inflating this tiny module's import surface for
    # a helper used only in this one error-formatting path.
    from .utils import _type_name

    chain = stack + [cls]
    return " → ".join(_type_name(c) for c in chain)


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
