from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Generic,
    Type,
    TypeVar,
    overload,
    get_origin,
    get_args
)

# TYPE_CHECKING guard — DIContainer is only imported for the type checker.
# At runtime, LazyProxy stores the container as Any to avoid a circular import
# (container.py imports from type.py; type.py cannot import from container.py).
if TYPE_CHECKING:
    from .container import DIContainer

T = TypeVar("T")

class _Injectable:
    """
    Marker base class for all injection metadata types in this library.

    Subclass this to tag a dataclass as recognized injection metadata
    (e.g. InjectMeta, LazyMeta). The container's resolution methods use
    isinstance(hint, _Injectable) to dispatch injection handling.

    No logic, state, or required methods — presence in the MRO is the
    entire contract.

    Thread safety:  ✅ Safe — no state whatsoever.
    Async safety:   ✅ Safe — same reason.

    Example:
        @dataclass
        class MyMeta(_Injectable):
            qualifier: str | None = None  # will be detected by the container
    """

    __slots__ = ()  # Lightweight — subclasses define their own fields

@dataclass
class InjectMeta(_Injectable):
    """Marker placed inside Annotated[T, InjectMeta(...)] by the Inject alias.

    Detected by the container's _resolve_hint_sync/_async methods to control
    how the dependency is resolved.

    Attributes:
        qualifier: Named qualifier forwarded to container.get() / get_all().
        priority:  Exact priority forwarded to container.get().
        all:       When True, resolves every matching binding as a list
                   (maps to InjectInstances[T]).
        optional:  When True, returns None instead of raising LookupError
                   if no binding is found. Ignored when all=True since an
                   empty list already signals "nothing found".
    """
    qualifier: str | None = None
    priority:  int | None = None
    all:       bool       = False
    # DESIGN: optional=False by default — fail-fast is safer than silently
    # injecting None. Callers must explicitly opt in to optional injection.
    optional:  bool       = False

# ─────────────────────────────────────────────────────────────────
#  Type aliases — sugar over Annotated[T, Inject(...)]
#  These are purely type-hint constructs, zero runtime overhead
# ─────────────────────────────────────────────────────────────────
class _InjectedAlias:
    """
    Supports both call and subscript syntax:
        Inject[NotificationService]           ← subscript
        Inject(NotificationService, priority=1) ← call with options
    """

    @overload
    def __getitem__(self, tp: type[T]) -> type[T]: ...          # Injected[T] → type[T] for checker

    @overload
    def __getitem__(self, tp: Any) -> Any: ...                  # fallback for complex types

    def __getitem__(self, tp: Any) -> Any:                      # ✅ Any — Annotated can't satisfy type[T]
        return Annotated[tp, InjectMeta()]

    @overload
    def __call__(self, tp: type[T], *, qualifier: str | None = ..., priority: int | None = ..., optional: bool = ...) -> type[T]: ...
    @overload
    def __call__(self, tp: Any, *, qualifier: str | None = ..., priority: int | None = ..., optional: bool = ...) -> Any: ...
    def __call__(
        self,
        tp: Any,
        *,
        qualifier: str | None = None,
        priority:  int | None = None,
        # optional=True: return None instead of raising LookupError when the
        # binding is absent. Useful for truly optional collaborators (e.g. a
        # metrics reporter that may not be wired in all environments).
        optional:  bool       = False,
    ) -> Any:
        return Annotated[tp, InjectMeta(qualifier=qualifier, priority=priority, optional=optional)]

class _InjectedInstancesAlias:
    """
    Supports both call and subscript syntax:
        InjectInstances[NotificationService]              ← subscript
        InjectInstances(NotificationService, qualifier=X) ← call with options
    """
    @overload
    def __getitem__(self, tp: type[T]) -> Type[list[T]]: ...    # InjectedInstances[T] → list[T] for checker
    @overload
    def __getitem__(self, tp: Any) -> Any: ...                  # fallback
    def __getitem__(self, tp: Any) -> Any:                      # Any — Annotated[list[T], ...] != Type[list[T]]
        return Annotated[list[tp], InjectMeta(all=True)]

    @overload
    def __call__(self, tp: type[T], *, qualifier: str | None = ...) -> Type[list[T]]: ...
    @overload
    def __call__(self, tp: Any, *, qualifier: str | None = ...) -> Any: ...

    def __call__(                                               # Any on implementation
        self,
        tp: Any,
        *,
        qualifier: str | None = None,
    ) -> Any:
        return Annotated[list[tp], InjectMeta(all=True, qualifier=qualifier)]


Inject = _InjectedAlias()
InjectInstances = _InjectedInstancesAlias()

# ─────────────────────────────────────────────────────────────────
#  Lazy[T] — deferred injection
#
#  DESIGN: Lazy[T] solves two problems simultaneously:
#    1. Circular dependencies — A depends on B, B depends on A.
#       Without Lazy, the container enters infinite recursion.
#       With Lazy[B], A receives a proxy at construction time and
#       resolves B only when A first calls .get() — by which point
#       both constructors have returned.
#    2. Scope leaks — a SINGLETON holding a REQUEST-scoped dep.
#       The proxy re-resolves on every .get() call, so the singleton
#       always gets the *current* request instance, not a stale one.
#       (Scope-leak validation still fires a warning via validate_bindings,
#        but this pattern makes it safe in practice.)
#
#  DESIGN: LazyProxy stores the container as Any at runtime to avoid
#  a circular import. DIContainer is only referenced via TYPE_CHECKING.
# ─────────────────────────────────────────────────────────────────

@dataclass
class LazyMeta(_Injectable):
    """Marker placed inside Annotated[T, LazyMeta(...)] by the Lazy alias.

    Detected by the container's _resolve_hint_sync/_async methods to
    construct a LazyProxy instead of resolving T immediately.

    Attributes:
        qualifier: Optional named qualifier forwarded to container.get().
        priority:  Optional priority forwarded to container.get().
    """
    qualifier: str | None = None
    priority:  int | None = None


class LazyProxy(Generic[T]):
    """Deferred wrapper — resolves T on the first .get() or .aget() call.

    The proxy is created eagerly (at construction time of the owning class)
    but the underlying dependency is resolved only when first accessed.
    Subsequent calls return the same cached instance.

    Thread safety:  ⚠️ Conditional — _resolved / _instance are not protected
                    by a lock. Two threads calling .get() simultaneously on
                    the same proxy may both call container.get() once each;
                    the last write wins. For singleton T this is harmless;
                    for DEPENDENT T it creates two separate instances.
                    If strict once-only semantics are needed, guard externally.
    Async safety:   ✅ Safe — .aget() is a coroutine; no shared async state.
                    Two concurrent tasks calling .aget() on an unresolved proxy
                    have the same race condition as the thread case above.

    Edge cases:
        - T not registered → .get() raises LookupError (deferred to call time)
        - T is async-only → .get() raises RuntimeError; use .aget() instead
        - Proxy re-used across request boundaries with DEPENDENT T → each
          .get() call resolves a fresh instance (no caching in the proxy)
          ⚠️ but _resolved is set True after the first, so subsequent calls
          return the *first* instance. Callers that want fresh instances
          per-access should call container.get(T) directly, not use Lazy[T].

    Usage:
        @Component
        class A:
            def __init__(self, b: Lazy[B]) -> None:
                self._b = b          # proxy stored, B not yet resolved

            def do_work(self) -> None:
                self._b.get().method()  # B resolved here (first access)
    """

    def __init__(
        self,
        container: DIContainer,
        tp: type[T],
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> None:
        # Stored as Any at runtime — TYPE_CHECKING guard prevents circular import.
        # DIContainer is only used via self._container.get() / .aget() — both are
        # public methods with stable signatures, so the Any cast is safe here.
        self._container: Any = container
        self._tp         = tp
        self._qualifier  = qualifier
        self._priority   = priority
        # _instance is None until first resolution — not the same as a None binding.
        # _resolved tracks whether resolution has occurred, since None is a valid result.
        self._instance: T | None = None
        self._resolved:  bool    = False

    def get(self) -> T:
        """Resolve and return the wrapped instance synchronously.

        On first call, delegates to container.get(T).
        Subsequent calls return the cached result without re-resolving.

        Returns:
            The resolved instance of T.

        Raises:
            LookupError:   If T has no registered binding.
            RuntimeError:  If T's provider is async — use .aget() instead.
        """
        if not self._resolved:
            self._instance = self._container.get(
                self._tp,
                qualifier=self._qualifier,
                priority=self._priority,
            )
            # Set after assignment — so a concurrent caller that reads
            # _resolved=True will also see the completed _instance.
            self._resolved = True
        return self._instance  # type: ignore[return-value]

    async def aget(self) -> T:
        """Resolve and return the wrapped instance asynchronously.

        Async mirror of .get(). Handles both sync and async providers —
        the container decides whether to await.

        Returns:
            The resolved instance of T.

        Raises:
            LookupError: If T has no registered binding.
        """
        if not self._resolved:
            self._instance = await self._container.aget(
                self._tp,
                qualifier=self._qualifier,
                priority=self._priority,
            )
            self._resolved = True
        return self._instance  # type: ignore[return-value]

    def __repr__(self) -> str:
        if self._resolved:
            return f"LazyProxy[{self._tp.__name__}](resolved={self._instance!r})"
        return f"LazyProxy[{self._tp.__name__}](unresolved)"


class _LazyAlias:
    """Supports both subscript and call syntax for Lazy[T].

    Subscript:  Lazy[NotificationService]
    Call:       Lazy(NotificationService, qualifier="sms", priority=1)

    Both forms expand to Annotated[T, LazyMeta(...)], which the container
    detects in _resolve_hint_sync/_async and converts to a LazyProxy.

    Thread safety:  ✅ Safe — stateless singleton, no mutable state.
    Async safety:   ✅ Safe — stateless singleton.
    """

    def __getitem__(self, tp: Any) -> Any:
        # Subscript form — no options, plain deferred injection
        return Annotated[tp, LazyMeta()]

    def __call__(
        self,
        tp: Any,
        *,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> Any:
        # Call form — qualifier / priority forwarded to LazyMeta
        return Annotated[tp, LazyMeta(qualifier=qualifier, priority=priority)]


# DESIGN: module-level singleton — same pattern as Inject / InjectInstances.
# Users import Lazy and use it as a type alias factory; they never instantiate
# _LazyAlias directly. This keeps the usage surface minimal and consistent.
Lazy = _LazyAlias()

def _has_injectable_metadata(hint: Any) -> bool:
    """
    Return True if a type hint contains any _Injectable metadata in its Annotated args.

    Designed as a fast pre-flight check — call this before the more expensive
    _resolve_hint_sync/_async to avoid processing hints that carry no injection
    metadata at all.

    Args:
        hint: Any type hint — bare types (int, str, MyClass), Annotated[T, ...],
              or complex generics (list[T], Optional[T]) are all accepted.

    Returns:
        True  — hint is Annotated[T, ..., <_Injectable>, ...] with at least
                one _Injectable instance among the metadata args.
        False — hint is a bare type, a non-Annotated generic, or an Annotated
                type whose metadata contains no _Injectable instances.

    Edge cases:
        - Bare type (int, MyClass)         → False, no Annotated wrapper
        - Annotated with no _Injectable    → False (e.g. Annotated[int, "doc"])
        - Annotated with multiple metadata → True if ANY arg is _Injectable
        - Nested Annotated                 → False — outer origin must be
                                             Annotated; inner nesting is not walked
        - hint is None                     → False, get_origin(None) is not Annotated

    Example:
        _has_injectable_metadata(int)                          # False
        _has_injectable_metadata(Inject[MyService])            # True
        _has_injectable_metadata(Annotated[int, "just a doc"]) # False
        _has_injectable_metadata(Lazy[MyService])              # True
    """
    return _get_injectable_metadata(hint) is not None

def _get_injectable_metadata(hint: Any) -> _Injectable | None:
    if get_origin(hint) is Annotated:
        args        = get_args(hint)
        return next((a for a in args[1:] if isinstance(a, _Injectable)), None)
    return None
