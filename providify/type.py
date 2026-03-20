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
    get_args,
)

# TYPE_CHECKING guard — DIContainer is only imported for the type checker.
# At runtime, LazyProxy stores the container as Any to avoid a circular import
# (container.py imports from type.py; type.py cannot import from container.py).
if TYPE_CHECKING:
    from .container import DIContainer

T = TypeVar("T")


class _providify:
    """
    Marker base class for all injection metadata types in this library.

    Subclass this to tag a dataclass as recognized injection metadata
    (e.g. InjectMeta, LazyMeta). The container's resolution methods use
    isinstance(hint, _providify) to dispatch injection handling.

    No logic, state, or required methods — presence in the MRO is the
    entire contract.

    Thread safety:  ✅ Safe — no state whatsoever.
    Async safety:   ✅ Safe — same reason.

    Example:
        @dataclass
        class MyMeta(_providify):
            qualifier: str | None = None  # will be detected by the container
    """

    __slots__ = ()  # Lightweight — subclasses define their own fields


@dataclass
class InjectMeta(_providify):
    """Marker placed inside Annotated[T, InjectMeta(...)] by the Inject alias.

    Detected by the container's _resolve_hint_sync/_async methods to control
    how the dependency is resolved.

    ⚠️  INJECTION TIMING — resolved ONCE at construction time:
        Inject[T] resolves T immediately when the owning component is constructed
        and passes the instance directly to __init__. The instance is stored as a
        plain attribute — no proxy, no deferred logic, no re-resolution.

        For @Singleton-scoped owners this means the dep is bound for the
        container's lifetime: every future call to the owning component reuses
        the exact same T instance that was resolved at construction.

        Consequence for scoped dependencies:
            A @Singleton injecting a @RequestScoped T via Inject[T] will capture
            one request's T instance at startup and serve it stale to ALL subsequent
            requests — a correctness error and potential data leak.
            Use Live[T] instead: it re-resolves T on every access from the
            currently active scope context.

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
    priority: int | None = None
    all: bool = False
    # DESIGN: optional=False by default — fail-fast is safer than silently
    # injecting None. Callers must explicitly opt in to optional injection.
    optional: bool = False


# ─────────────────────────────────────────────────────────────────
#  Type aliases — sugar over Annotated[T, Inject(...)]
#  These are purely type-hint constructs, zero runtime overhead
# ─────────────────────────────────────────────────────────────────
class _InjectedAlias:
    """Sugar over Annotated[T, InjectMeta(...)].

    Supports both subscript and call syntax:
        Inject[NotificationService]                          ← subscript
        Inject(NotificationService, qualifier="smtp")        ← call with options

    ⚠️  Resolved ONCE at the owning component's construction time.
        The resolved instance is stored as a plain attribute — not a proxy.
        For @Singleton owners this means the dep is frozen for the container's
        entire lifetime.  Do NOT use Inject[T] for @RequestScoped or
        @SessionScoped dependencies held by longer-lived components —
        use Live[T] instead, which re-resolves on every access.

    Thread safety:  ✅ Safe — stateless singleton, no mutable state.
    Async safety:   ✅ Safe — stateless singleton.
    """

    @overload
    def __getitem__(
        self, tp: type[T]
    ) -> type[T]: ...  # Injected[T] → type[T] for checker

    @overload
    def __getitem__(self, tp: Any) -> Any: ...  # fallback for complex types

    def __getitem__(self, tp: Any) -> Any:  # ✅ Any — Annotated can't satisfy type[T]
        return Annotated[tp, InjectMeta()]

    @overload
    def __call__(
        self,
        tp: type[T],
        *,
        qualifier: str | None = ...,
        priority: int | None = ...,
        optional: bool = ...,
    ) -> type[T]: ...
    @overload
    def __call__(
        self,
        tp: Any,
        *,
        qualifier: str | None = ...,
        priority: int | None = ...,
        optional: bool = ...,
    ) -> Any: ...
    def __call__(
        self,
        tp: Any,
        *,
        qualifier: str | None = None,
        priority: int | None = None,
        # optional=True: return None instead of raising LookupError when the
        # binding is absent. Useful for truly optional collaborators (e.g. a
        # metrics reporter that may not be wired in all environments).
        optional: bool = False,
    ) -> Any:
        return Annotated[
            tp, InjectMeta(qualifier=qualifier, priority=priority, optional=optional)
        ]


class _InjectedInstancesAlias:
    """
    Supports both call and subscript syntax:
        InjectInstances[NotificationService]              ← subscript
        InjectInstances(NotificationService, qualifier=X) ← call with options
    """

    @overload
    def __getitem__(
        self, tp: type[T]
    ) -> Type[list[T]]: ...  # InjectedInstances[T] → list[T] for checker
    @overload
    def __getitem__(self, tp: Any) -> Any: ...  # fallback
    def __getitem__(
        self, tp: Any
    ) -> Any:  # Any — Annotated[list[T], ...] != Type[list[T]]
        return Annotated[list[tp], InjectMeta(all=True)]

    @overload
    def __call__(
        self, tp: type[T], *, qualifier: str | None = ...
    ) -> Type[list[T]]: ...
    @overload
    def __call__(self, tp: Any, *, qualifier: str | None = ...) -> Any: ...

    def __call__(  # Any on implementation
        self,
        tp: Any,
        *,
        qualifier: str | None = None,
    ) -> Any:
        return Annotated[list[tp], InjectMeta(all=True, qualifier=qualifier)]


# DESIGN: TYPE_CHECKING split — Pylance and mypy only see the class stubs
# below; the runtime path keeps the original alias singleton instances.
#
# Without this, Pylance fires reportInvalidTypeForm on every annotation like
# `svc: Inject[Database]` because it sees Inject as a plain value (an instance
# of _InjectedAlias), not a type.  By presenting a class with __class_getitem__
# under TYPE_CHECKING, the type checker treats Inject[T] as a generic
# specialization and infers the correct resolved type from the overload return.
#
# TYPE_CHECKING is False at runtime — the class stubs never exist at runtime.
# Only the else branch runs: Inject = _InjectedAlias(), unchanged from before.
# This is the same pattern used above for `from .container import DIContainer`.
#
# The __new__ overloads cover the call syntax: Inject(T, qualifier="x").
# Returning T from __new__ is non-standard; # type: ignore[misc] silences mypy.
if TYPE_CHECKING:

    class Inject:
        """Type-checker stub — see _InjectedAlias for runtime behaviour."""

        @overload
        @classmethod
        def __class_getitem__(cls, tp: type[T]) -> T: ...

        @overload
        @classmethod
        def __class_getitem__(cls, tp: Any) -> Any: ...

        @classmethod
        def __class_getitem__(
            cls, tp: Any
        ) -> Any: ...  # implementation required in .py files

        def __new__(
            cls,
            _tp: type[T],
            *,
            qualifier: str | None = None,
            priority: int | None = None,
            optional: bool = False,
        ) -> T: ...  # type: ignore[misc]

    class InjectInstances:
        """Type-checker stub — see _InjectedInstancesAlias for runtime behaviour."""

        @overload
        @classmethod
        def __class_getitem__(cls, tp: type[T]) -> list[T]: ...

        @overload
        @classmethod
        def __class_getitem__(cls, tp: Any) -> Any: ...

        @classmethod
        def __class_getitem__(
            cls, tp: Any
        ) -> Any: ...  # implementation required in .py files

        def __new__(
            cls,
            _tp: type[T],
            *,
            qualifier: str | None = None,
        ) -> list[T]: ...  # type: ignore[misc]

else:
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
class LazyMeta(_providify):
    """Marker placed inside Annotated[T, LazyMeta(...)] by the Lazy alias.

    Detected by the container's _resolve_hint_sync/_async methods to
    construct a LazyProxy instead of resolving T immediately.

    Attributes:
        qualifier: Optional named qualifier forwarded to container.get().
        priority:  Optional priority forwarded to container.get().
    """

    qualifier: str | None = None
    priority: int | None = None


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
        self._tp = tp
        self._qualifier = qualifier
        self._priority = priority
        # _instance is None until first resolution — not the same as a None binding.
        # _resolved tracks whether resolution has occurred, since None is a valid result.
        self._instance: T | None = None
        self._resolved: bool = False

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


if TYPE_CHECKING:
    # DESIGN: Lazy is a TypeAlias for LazyProxy under TYPE_CHECKING.
    # LazyProxy is already a proper Generic[T] class with .get() / .aget().
    # Aliasing to it gives the type checker full knowledge:
    #   proxy: Lazy[Database]  →  proxy: LazyProxy[Database]
    #   proxy.get()            →  returns Database   ✅
    # This is simpler and more correct than a custom stub class.
    Lazy = LazyProxy
else:
    # DESIGN: module-level singleton — same pattern as Inject / InjectInstances.
    # Users import Lazy and use it as a type alias factory; they never instantiate
    # _LazyAlias directly. This keeps the usage surface minimal and consistent.
    Lazy = _LazyAlias()


# ─────────────────────────────────────────────────────────────────
#  Live[T] — always-fresh injection proxy
#
#  DESIGN: Live[T] solves the "stale singleton holding a scoped dep"
#  problem that Inject[T] cannot handle.
#
#  When a @Singleton is constructed, Inject[T] resolves T once and
#  stores the instance forever — so all requests share one JWT, one
#  RequestContext, etc. That is wrong and often a security issue.
#
#  Live[T] gives the singleton a LiveProxy instead.  The proxy holds
#  no instance of its own; every .get() / .aget() call delegates
#  directly to container.get(T), which routes through the active
#  ScopeContext and returns the instance for the *current* request.
#
#  Contrast with Lazy[T]:
#    Lazy[T]  — defers first resolution, then caches forever. ❌ wrong for scoped deps.
#    Live[T]  — never caches; re-resolves on every access.    ✅ correct for scoped deps.
#
#  Jakarta CDI solves this with bytecode-generated subclass proxies
#  that intercept every method call.  LiveProxy is the Python equivalent:
#  it re-resolves from the container (which checks the active scope
#  ContextVar) instead of generating bytecode.
#
#  DESIGN: LiveProxy stores the container as Any at runtime to avoid
#  a circular import — same pattern as LazyProxy.
# ─────────────────────────────────────────────────────────────────


@dataclass
class LiveMeta(_providify):
    """Marker placed inside Annotated[T, LiveMeta(...)] by the Live alias.

    Detected by the container's _resolve_hint_sync/_async methods to
    construct a LiveProxy instead of resolving T immediately.

    Unlike LazyMeta, LiveMeta signals that *every* access to the proxy
    must re-resolve T — there is never a cached instance.

    Attributes:
        qualifier: Optional named qualifier forwarded to container.get().
        priority:  Optional priority forwarded to container.get().
    """

    qualifier: str | None = None
    priority: int | None = None


class LiveProxy(Generic[T]):
    """Always-fresh proxy — re-resolves T on every .get() or .aget() call.

    Unlike LazyProxy, LiveProxy never caches the resolved instance.
    Suitable for REQUEST-scoped or SESSION-scoped dependencies held by
    a longer-lived component (e.g. a @Singleton holding a JsonWebToken).

    A @Singleton that holds a LiveProxy[T] is safe: the proxy always
    fetches the instance for the *currently active* scope context, so
    each request or session gets its own isolated T.

    Thread safety:  ✅ Safe — no shared mutable state. Each .get() call
                    is an independent lookup in the container. Concurrent
                    threads receive independent instances as dictated by
                    the scope rules of T.
    Async safety:   ✅ Safe — .aget() is a coroutine with no shared state.
                    Concurrent tasks each resolve from their own
                    ContextVar-isolated scope.

    Edge cases:
        - T not registered              → .get() raises LookupError on every call
        - T is async-only               → .get() raises RuntimeError; use .aget()
        - T is SINGLETON                → same instance every time (fine, correct)
        - T is DEPENDENT                → new instance created on every .get() call
        - T is REQUEST-scoped outside   → container raises RuntimeError; must be
          an active request context       inside scope_context.request() block
        - T is SESSION-scoped           → instance from the currently active session

    Usage:
        @Singleton
        class AuthService:
            def __init__(self, token: Live[JsonWebToken]) -> None:
                # token is a LiveProxy — not the token itself
                self._token = token

            def get_user_id(self) -> str:
                # Re-resolves from the current request scope every call
                return self._token.get().subject
    """

    def __init__(
        self,
        container: DIContainer,
        tp: type[T],
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> None:
        # Stored as Any at runtime — TYPE_CHECKING guard prevents circular import.
        # Same pattern as LazyProxy — container API is stable and narrow.
        self._container: Any = container
        self._tp = tp
        self._qualifier = qualifier
        self._priority = priority

    def get(self) -> T:
        """Re-resolve and return the wrapped instance synchronously.

        Unlike LazyProxy.get(), this method NEVER caches — every call
        delegates to container.get(T) so the result always reflects the
        currently active scope (request, session, etc.).

        Returns:
            A freshly resolved instance of T for the current scope.

        Raises:
            LookupError:   If T has no registered binding.
            RuntimeError:  If T's provider is async — use .aget() instead.
            RuntimeError:  If T is REQUEST/SESSION-scoped and no scope
                           context is currently active.
        """
        # No _resolved guard — intentional.  Always delegate so that the
        # container's ScopeContext routing applies on every call.
        return self._container.get(
            self._tp,
            qualifier=self._qualifier,
            priority=self._priority,
        )

    async def aget(self) -> T:
        """Re-resolve and return the wrapped instance asynchronously.

        Async mirror of .get() — re-fetches on every call, no caching.

        Returns:
            A freshly resolved instance of T for the current scope.

        Raises:
            LookupError:  If T has no registered binding.
            RuntimeError: If T is REQUEST/SESSION-scoped and no scope
                          context is currently active.
        """
        # Same intentional no-cache design as .get() above.
        return await self._container.aget(
            self._tp,
            qualifier=self._qualifier,
            priority=self._priority,
        )

    def __repr__(self) -> str:
        qualifier_str = f", qualifier={self._qualifier!r}" if self._qualifier else ""
        return f"LiveProxy[{self._tp.__name__}](live{qualifier_str})"


class _LiveAlias:
    """Supports both subscript and call syntax for Live[T].

    Subscript:  Live[JsonWebToken]
    Call:       Live(JsonWebToken, qualifier="bearer", priority=1)

    Both forms expand to Annotated[T, LiveMeta(...)], which the container
    detects in _resolve_hint_sync/_async and converts to a LiveProxy.

    Thread safety:  ✅ Safe — stateless singleton, no mutable state.
    Async safety:   ✅ Safe — stateless singleton.
    """

    def __getitem__(self, tp: Any) -> Any:
        # Subscript form — no options, plain live injection
        return Annotated[tp, LiveMeta()]

    def __call__(
        self,
        tp: Any,
        *,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> Any:
        # Call form — qualifier / priority forwarded to LiveMeta
        return Annotated[tp, LiveMeta(qualifier=qualifier, priority=priority)]


if TYPE_CHECKING:
    # DESIGN: Live is a TypeAlias for LiveProxy under TYPE_CHECKING.
    # LiveProxy is already a proper Generic[T] class with .get() / .aget().
    # Aliasing to it gives the type checker full knowledge:
    #   token: Live[JsonWebToken]  →  token: LiveProxy[JsonWebToken]
    #   token.get()                →  returns JsonWebToken   ✅
    #   token.aget()               →  returns JsonWebToken   ✅
    # This is simpler and more correct than a custom stub class.
    Live = LiveProxy
else:
    # DESIGN: module-level singleton — same pattern as Lazy / Inject / InjectInstances.
    # Users import Live and use it as a type alias factory; they never instantiate
    # _LiveAlias directly.
    Live = _LiveAlias()


def _has_providify_metadata(hint: Any) -> bool:
    """
    Return True if a type hint contains any _providify metadata in its Annotated args.

    Designed as a fast pre-flight check — call this before the more expensive
    _resolve_hint_sync/_async to avoid processing hints that carry no injection
    metadata at all.

    Args:
        hint: Any type hint — bare types (int, str, MyClass), Annotated[T, ...],
              or complex generics (list[T], Optional[T]) are all accepted.

    Returns:
        True  — hint is Annotated[T, ..., <_providify>, ...] with at least
                one _providify instance among the metadata args.
        False — hint is a bare type, a non-Annotated generic, or an Annotated
                type whose metadata contains no _providify instances.

    Edge cases:
        - Bare type (int, MyClass)         → False, no Annotated wrapper
        - Annotated with no _providify    → False (e.g. Annotated[int, "doc"])
        - Annotated with multiple metadata → True if ANY arg is _providify
        - Nested Annotated                 → False — outer origin must be
                                             Annotated; inner nesting is not walked
        - hint is None                     → False, get_origin(None) is not Annotated

    Example:
        _has_providify_metadata(int)                          # False
        _has_providify_metadata(Inject[MyService])            # True
        _has_providify_metadata(Annotated[int, "just a doc"]) # False
        _has_providify_metadata(Lazy[MyService])              # True
    """
    return _get_providify_metadata(hint) is not None


def _get_providify_metadata(hint: Any) -> _providify | None:
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        return next((a for a in args[1:] if isinstance(a, _providify)), None)
    return None
