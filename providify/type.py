from __future__ import annotations

from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
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

    Three equivalent forms — choose based on what options you need:

        # 1. Subscript — no options, cleanest syntax, Pylance shows Inject[T]
        store: Inject[Storage]

        # 2. Call — options available, but requires # type: ignore[valid-type]
        #    because a call expression is not valid in annotation position.
        store: Inject(Storage, qualifier="cloud")  # type: ignore[valid-type]

        # 3. Annotated — recommended when options are needed.
        #    Fully valid Python; Pylance hover shows bare `Storage`; no ignore comment.
        from providify import InjectMeta
        from typing import Annotated
        store: Annotated[Storage, InjectMeta(qualifier="cloud")]
        store: Annotated[Storage, InjectMeta(qualifier="cloud", optional=True)]
        store: Annotated[Storage, InjectMeta(priority=2)]

    The Annotated form is the underlying expansion that Inject[...] produces
    at runtime — it is not a second code path, just a more explicit notation.
    Use it whenever qualifier / priority / optional options are needed and
    you want proper Pylance hover without ignore comments.

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
    """Sugar over Annotated[list[T], InjectMeta(all=True, ...)].

    Three equivalent forms — choose based on what options you need:

        # 1. Subscript — no options, cleanest syntax, Pylance shows InjectInstances[T]
        stores: InjectInstances[Storage]

        # 2. Call — options available, but requires # type: ignore[valid-type]
        #    because a call expression is not valid in annotation position.
        stores: InjectInstances(Storage, qualifier="cloud")  # type: ignore[valid-type]

        # 3. Annotated — recommended when qualifier is needed.
        #    Fully valid Python; Pylance hover shows bare `list[Storage]`; no ignore comment.
        from providify import InjectMeta
        from typing import Annotated
        stores: Annotated[list[Storage], InjectMeta(all=True, qualifier="cloud")]

    The Annotated form is the underlying expansion that InjectInstances[...] produces
    at runtime — same resolution path, just explicit notation.
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


# DESIGN: Python 3.12 `type` statement (PEP 695) for type-checker visibility.
#
# The previous approach defined class stubs with __class_getitem__ returning T,
# hoping type checkers would propagate that return type into annotation context.
# They don't. Pylance and mypy treat `Inject[MyService]` in an annotation as a
# parameterised generic form of the Inject class — the __class_getitem__ return
# type is only used for *expression* context, not annotation context. Result:
# `param: Inject[MyService]` showed as `Inject[MyService]` in hover/completion,
# not the useful `MyService` — breaking autocomplete for the injected dependency.
#
# `type Inject[T] = T` creates a TypeAliasType. When a type checker resolves
# `param: Inject[MyService]`, it substitutes T=MyService in the alias and
# arrives at `MyService` directly — giving correct completions and hover types.
#
# Tradeoffs:
#   ✅ `param: Inject[MyService]`         → linter shows MyService
#   ✅ `param: InjectInstances[MyService]` → linter shows list[MyService]
#   ❌ Call form Inject(MyService, qualifier="x") loses type-checker support
#      (TypeAliasType is not callable — type checkers flag it as an error).
#      For qualified injection with full type precision, use:
#          Annotated[MyService, InjectMeta(qualifier="x")]
#      The container resolves it identically at runtime.
#   ✅ Runtime unaffected — TYPE_CHECKING is always False at runtime;
#      the else branch still provides the callable _InjectedAlias() singleton.
#
# Alternative considered: class stub with __class_getitem__ (previous approach)
# Rejected: type checkers resolve ClassName[T] in annotations as a parameterised
# generic, ignoring __class_getitem__ return types in that context.
if TYPE_CHECKING:
    type Inject[T] = T
    type InjectInstances[T] = list[T]
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
        optional:  When True the proxy's .get() / .aget() return None instead
                   of raising LookupError when no binding is found.
                   Set automatically when the type annotation is Lazy[T | None]
                   or Lazy[Optional[T]]; can also be set explicitly via
                   Annotated[T, LazyMeta(optional=True)].
    """

    qualifier: str | None = None
    priority: int | None = None
    # DESIGN: optional=False by default — fail-fast is safer than silently
    # injecting None. Callers must explicitly opt in, either by using
    # Lazy[T | None] / Lazy[Optional[T]] or Annotated[T, LazyMeta(optional=True)].
    optional: bool = False


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
        - T not registered, optional=False → .get() raises LookupError (deferred to call time)
        - T not registered, optional=True  → .get() returns None and caches that result
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

        # Optional form — returns None if B is not registered:
        @Component
        class A:
            def __init__(self, b: Lazy[B | None]) -> None:
                self._b = b          # LazyProxy with optional=True

            def do_work(self) -> None:
                result = self._b.get()   # None if B not bound, B instance otherwise
    """

    def __init__(
        self,
        container: DIContainer,
        tp: type[T],
        qualifier: str | None = None,
        priority: int | None = None,
        optional: bool = False,
    ) -> None:
        # Stored as Any at runtime — TYPE_CHECKING guard prevents circular import.
        # DIContainer is only used via self._container.get() / .aget() — both are
        # public methods with stable signatures, so the Any cast is safe here.
        self._container: Any = container
        self._tp = tp
        self._qualifier = qualifier
        self._priority = priority
        # When True, LookupError on first resolution is swallowed and None is
        # cached instead. Set automatically when the annotation is Lazy[T | None].
        self._optional = optional
        # _instance is None until first resolution — not the same as a None binding.
        # _resolved tracks whether resolution has occurred, since None is a valid result
        # (either from a None-returning binding or from optional injection of an absent dep).
        self._instance: T | None = None
        self._resolved: bool = False

    def get(self) -> T | None:
        """Resolve and return the wrapped instance synchronously.

        On first call, delegates to container.get(T).
        Subsequent calls return the cached result without re-resolving.

        Returns:
            The resolved instance of T, or None if optional=True and T has
            no registered binding.

        Raises:
            LookupError:   If T has no registered binding and optional=False.
            RuntimeError:  If T's provider is async — use .aget() instead.
        """
        if not self._resolved:
            try:
                self._instance = self._container.get(
                    self._tp,
                    qualifier=self._qualifier,
                    priority=self._priority,
                )
            except LookupError:
                # optional=True: cache None so subsequent .get() calls are cheap
                # and do not re-hit the container unnecessarily.
                # optional=False: re-raise so the caller sees the real error.
                if not self._optional:
                    raise
                self._instance = None
            # Set after assignment — so a concurrent caller that reads
            # _resolved=True will also see the completed _instance.
            self._resolved = True
        return self._instance  # type: ignore[return-value]

    async def aget(self) -> T | None:
        """Resolve and return the wrapped instance asynchronously.

        Async mirror of .get(). Handles both sync and async providers —
        the container decides whether to await.

        Returns:
            The resolved instance of T, or None if optional=True and T has
            no registered binding.

        Raises:
            LookupError: If T has no registered binding and optional=False.
        """
        if not self._resolved:
            try:
                self._instance = await self._container.aget(
                    self._tp,
                    qualifier=self._qualifier,
                    priority=self._priority,
                )
            except LookupError:
                if not self._optional:
                    raise
                self._instance = None
            self._resolved = True
        return self._instance  # type: ignore[return-value]

    def __repr__(self) -> str:
        if self._resolved:
            return f"LazyProxy[{self._tp.__name__}](resolved={self._instance!r})"
        optional_part = ", optional" if self._optional else ""
        return f"LazyProxy[{self._tp.__name__}](unresolved{optional_part})"


class _LazyAlias:
    """Sugar over Annotated[T, LazyMeta(...)].

    Three equivalent forms — choose based on what options you need:

        # 1. Subscript — no options, cleanest syntax, Pylance shows LazyProxy[T]
        store: Lazy[Storage]

        # 2. Call — options available, but requires # type: ignore[valid-type]
        #    because a call expression is not valid in annotation position.
        store: Lazy(Storage, qualifier="cloud")  # type: ignore[valid-type]

        # 3. Annotated — recommended when qualifier / priority are needed.
        #    Fully valid Python; Pylance hover shows bare `LazyProxy[Storage]`; no ignore comment.
        from providify import LazyMeta
        from typing import Annotated
        store: Annotated[Storage, LazyMeta(qualifier="cloud")]
        store: Annotated[Storage, LazyMeta(priority=2)]

    Both expand to Annotated[T, LazyMeta(...)], which the container
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
        optional:  When True the proxy's .get() / .aget() return None instead
                   of raising LookupError when no binding is found.
                   Set automatically when the annotation is Live[T | None]
                   or Live[Optional[T]]; can also be set explicitly via
                   Annotated[T, LiveMeta(optional=True)].
    """

    qualifier: str | None = None
    priority: int | None = None
    # DESIGN: optional=False by default — fail-fast is safer than silently
    # injecting None. Callers must explicitly opt in, either by using
    # Live[T | None] / Live[Optional[T]] or Annotated[T, LiveMeta(optional=True)].
    optional: bool = False


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
        - T not registered, optional=False → .get() raises LookupError on every call
        - T not registered, optional=True  → .get() returns None on every call
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

        # Optional form — returns None if JsonWebToken is not registered:
        @Singleton
        class AuthService:
            def __init__(self, token: Live[JsonWebToken | None]) -> None:
                self._token = token   # LiveProxy with optional=True

            def get_user_id(self) -> str | None:
                tok = self._token.get()   # None if not bound
                return tok.subject if tok is not None else None
    """

    def __init__(
        self,
        container: DIContainer,
        tp: type[T],
        qualifier: str | None = None,
        priority: int | None = None,
        optional: bool = False,
    ) -> None:
        # Stored as Any at runtime — TYPE_CHECKING guard prevents circular import.
        # Same pattern as LazyProxy — container API is stable and narrow.
        self._container: Any = container
        self._tp = tp
        self._qualifier = qualifier
        self._priority = priority
        # When True, LookupError from .get() / .aget() is swallowed and None is
        # returned instead. Set automatically when the annotation is Live[T | None].
        self._optional = optional

    def get(self) -> T | None:
        """Re-resolve and return the wrapped instance synchronously.

        Unlike LazyProxy.get(), this method NEVER caches — every call
        delegates to container.get(T) so the result always reflects the
        currently active scope (request, session, etc.).

        Returns:
            A freshly resolved instance of T for the current scope, or None
            if optional=True and T has no registered binding.

        Raises:
            LookupError:   If T has no registered binding and optional=False.
            RuntimeError:  If T's provider is async — use .aget() instead.
            RuntimeError:  If T is REQUEST/SESSION-scoped and no scope
                           context is currently active.
        """
        # No _resolved guard — intentional.  Always delegate so that the
        # container's ScopeContext routing applies on every call.
        try:
            return self._container.get(
                self._tp,
                qualifier=self._qualifier,
                priority=self._priority,
            )
        except LookupError:
            # optional=True: return None every call — no caching since the
            # binding might be added later (unlike LazyProxy which is one-shot).
            # optional=False (default): re-raise so the caller sees the real error.
            if self._optional:
                return None
            raise

    async def aget(self) -> T | None:
        """Re-resolve and return the wrapped instance asynchronously.

        Async mirror of .get() — re-fetches on every call, no caching.

        Returns:
            A freshly resolved instance of T for the current scope, or None
            if optional=True and T has no registered binding.

        Raises:
            LookupError:  If T has no registered binding and optional=False.
            RuntimeError: If T is REQUEST/SESSION-scoped and no scope
                          context is currently active.
        """
        # Same intentional no-cache design as .get() above.
        try:
            return await self._container.aget(
                self._tp,
                qualifier=self._qualifier,
                priority=self._priority,
            )
        except LookupError:
            if self._optional:
                return None
            raise

    def __repr__(self) -> str:
        qualifier_str = f", qualifier={self._qualifier!r}" if self._qualifier else ""
        optional_str = ", optional" if self._optional else ""
        return f"LiveProxy[{self._tp.__name__}](live{qualifier_str}{optional_str})"


class _LiveAlias:
    """Sugar over Annotated[T, LiveMeta(...)].

    Three equivalent forms — choose based on what options you need:

        # 1. Subscript — no options, cleanest syntax, Pylance shows LiveProxy[T]
        token: Live[JsonWebToken]

        # 2. Call — options available, but requires # type: ignore[valid-type]
        #    because a call expression is not valid in annotation position.
        token: Live(JsonWebToken, qualifier="bearer")  # type: ignore[valid-type]

        # 3. Annotated — recommended when qualifier / priority are needed.
        #    Fully valid Python; Pylance hover shows bare `LiveProxy[JsonWebToken]`; no ignore comment.
        from providify import LiveMeta
        from typing import Annotated
        token: Annotated[JsonWebToken, LiveMeta(qualifier="bearer")]
        token: Annotated[JsonWebToken, LiveMeta(priority=1)]

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


# ─────────────────────────────────────────────────────────────────
#  Instance[T] — Jakarta CDI-inspired programmatic lookup handle
#
#  DESIGN: Instance[T] is the "programmatic injection" pattern from
#  Jakarta CDI (jakarta.enterprise.inject.Instance<T>).
#
#  Compared to the other injection types:
#    Inject[T]          — eager, single, raises if absent.
#    InjectInstances[T] — eager, all, resolved once at construction.
#    Lazy[T]            — deferred single, cached on first access.
#    Live[T]            — always-fresh single, never cached.
#    Instance[T]        — deferred handle; caller chooses get() or
#                         get_all() at call time; exposes resolvable()
#                         for optional-style guards.
#
#  Key differences from Inject/InjectInstances:
#    ✅ Single object to inject; caller decides single vs. all.
#    ✅ resolvable() lets callers guard optional dependencies without
#       needing Inject[T, optional=True] or a try/except block.
#    ✅ get_all() returns [] instead of raising on empty — natural
#       for optional multi-binding scenarios.
#    ❌ One extra method call vs. plain Inject[T] — small overhead.
#
#  DESIGN: InstanceProxy stores the container as Any at runtime to avoid
#  a circular import — same pattern as LazyProxy and LiveProxy.
# ─────────────────────────────────────────────────────────────────


@dataclass
class InstanceMeta(_providify):
    """Marker placed inside Annotated[T, InstanceMeta()] by the Instance alias.

    Detected by the container's _resolve_hint_sync/_async methods to
    construct an InstanceProxy instead of resolving T immediately.

    Unlike InjectMeta, the proxy is handed to the owner un-resolved;
    the owner calls .get() / .get_all() / .resolvable() at call time,
    giving full control over single-vs-all and optional-vs-required semantics.

    DESIGN: InstanceMeta carries NO qualifier or priority — filtering is
    intentionally deferred to call time on the proxy methods.  This keeps
    the annotation site clean and lets callers use the same proxy handle
    with different filters in different code paths (e.g. same Instance[T]
    used once with qualifier="sms" and once with qualifier="email").

    Tradeoffs:
        ✅ One proxy handles multiple qualifier / priority combinations.
        ✅ Annotation stays minimal — no Annotated[T, InstanceMeta(qualifier=...)] needed.
        ❌ Qualifier / priority are not visible at the injection site — callers
           must remember to pass them at the call site (or intend None = any).

    Alternative considered: storing qualifier/priority on InstanceMeta like
    InjectMeta does — rejected because it locks every call through the proxy
    to one fixed filter, losing the flexibility advantage over plain Inject[T].
    """

    # Empty marker — no fields.  The dataclass decorator still generates
    # __init__, __eq__, __repr__, and __hash__ automatically.
    pass


class InstanceProxy(Generic[T]):
    """Jakarta CDI-inspired programmatic injection handle for type T.

    A single proxy object that gives the owner full control over how and
    when to resolve dependencies.  Unlike :class:`Inject` (eager, single)
    or :class:`InjectInstances` (eager, all), ``InstanceProxy`` is:

    - **Deferred** — no resolution occurs at construction time.
    - **Dual-mode** — ``.get()`` for a single best-priority instance,
      ``.get_all()`` for the full ranked list.
    - **Optional-friendly** — ``.resolvable()`` returns ``bool`` without
      side-effects, so callers can guard optional deps without try/except.

    Thread safety:  ✅ Safe — no mutable state on the proxy itself.
                    Each call delegates to the container; thread-safety
                    guarantees are the same as for ``container.get()``.
    Async safety:   ✅ Safe — ``.aget()`` / ``.aget_all()`` are coroutines
                    with no shared state. Each call is an independent
                    delegation into the container's async path.

    Edge cases:
        - T not registered            → .get() raises LookupError.
        - T not registered            → .get_all() returns [] (no raise).
        - T is async-only             → .get() raises RuntimeError; use .aget().
        - T is REQUEST/SESSION-scoped → .get() raises if no scope is active.
        - qualifier narrows to zero   → .get() raises; .resolvable() → False.
        - Multiple bindings, no prio  → .get() returns highest-priority match.
        - resolvable() re-evaluates every call — not cached; safe to call
          repeatedly even after new bindings are added.

    Usage:
        @Component
        class AlertService:
            def __init__(self, notifiers: Instance[Notifier]) -> None:
                self._notifiers = notifiers

            def notify(self, msg: str) -> None:
                # Optional single dependency — guard before calling
                if self._notifiers.resolvable():
                    self._notifiers.get().send(msg)

            def broadcast(self, msg: str) -> None:
                # All registered notifiers, sorted by priority
                for n in self._notifiers.get_all():
                    n.send(msg)
    """

    def __init__(
        self,
        container: DIContainer,
        tp: type[T],
    ) -> None:
        # Stored as Any at runtime — TYPE_CHECKING guard prevents circular import.
        # Same pattern as LazyProxy and LiveProxy; container API is stable and narrow.
        self._container: Any = container
        self._tp = tp
        # DESIGN: no qualifier/priority stored on the proxy — all filtering is
        # deferred to call time on get() / get_all() / resolvable() so a single
        # InstanceProxy handle can serve multiple filter combinations.

    def get(
        self,
        *,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> T:
        """Resolve and return the single best-priority matching instance synchronously.

        Delegates to ``container.get(T)`` with the caller-supplied qualifier and priority.
        Unlike ``.get_all()``, this method raises if no binding is found — use
        ``.resolvable()`` with the same qualifier/priority first if the dep is optional.

        Args:
            qualifier: Named qualifier to narrow the candidates — only bindings
                       registered with this qualifier are considered. ``None`` matches
                       any qualifier (default).
            priority:  Exact priority to match. ``None`` means the highest-priority
                       candidate wins (default).

        Returns:
            The highest-priority resolved instance of T that satisfies the filter.

        Raises:
            LookupError:   If no binding matches T with the given qualifier/priority.
            RuntimeError:  If T's provider is async — use ``.aget()`` instead.
            RuntimeError:  If T is REQUEST/SESSION-scoped and no scope is active.

        Example:
            svc = proxy.get()
            svc = proxy.get(qualifier="email")
            svc = proxy.get(qualifier="sms", priority=10)

        Edge cases:
            - No binding registered → raises LookupError immediately.
            - qualifier narrows to zero → raises LookupError.
            - Multiple bindings, qualifier=None → highest priority wins.
        """
        return self._container.get(
            self._tp,
            qualifier=qualifier,
            priority=priority,
        )

    async def aget(
        self,
        *,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> T:
        """Resolve and return the single best-priority matching instance asynchronously.

        Async mirror of ``.get()``.  Handles both sync and async providers —
        the container decides whether to await.

        Args:
            qualifier: Named qualifier to narrow the candidates. ``None`` matches any.
            priority:  Exact priority to match. ``None`` means best priority wins.

        Returns:
            The highest-priority resolved instance of T that satisfies the filter.

        Raises:
            LookupError:  If no binding matches T with the given qualifier/priority.
            RuntimeError: If T is REQUEST/SESSION-scoped and no scope is active.

        Edge cases:
            - No binding → raises LookupError immediately.
        """
        return await self._container.aget(
            self._tp,
            qualifier=qualifier,
            priority=priority,
        )

    def get_all(
        self,
        *,
        qualifier: str | None = None,
    ) -> list[T]:
        """Resolve all matching instances synchronously, sorted by ascending priority.

        Unlike ``container.get_all()``, this method returns an empty list instead
        of raising when no bindings are registered — safe for optional multi-dep scenarios.

        Args:
            qualifier: Named qualifier to narrow candidates. ``None`` returns all
                       bindings for T regardless of qualifier (default).

        Returns:
            A list of resolved instances, sorted by ascending priority (lowest first).
            Returns ``[]`` if no binding matches — never raises LookupError.

        Raises:
            RuntimeError: If any matching provider is async — use ``.aget_all()`` instead.

        Edge cases:
            - No binding registered → returns [] without raising
              (⚠️ differs from container.get_all() which raises LookupError).
            - qualifier narrows to zero → returns [].
            - DEPENDENT scope → each binding yields a new instance on every call.
        """
        try:
            return self._container.get_all(self._tp, qualifier=qualifier)
        except LookupError:
            # DESIGN: return [] instead of propagating — the whole point of
            # Instance[T].get_all() is to handle the "zero or more" case
            # without requiring a try/except at the call site.
            return []

    async def aget_all(
        self,
        *,
        qualifier: str | None = None,
    ) -> list[T]:
        """Resolve all matching instances asynchronously, sorted by ascending priority.

        Async mirror of ``.get_all()``. Returns an empty list when no bindings match.

        Args:
            qualifier: Named qualifier to narrow candidates. ``None`` returns all
                       bindings for T regardless of qualifier (default).

        Returns:
            A list of resolved instances, sorted by ascending priority (lowest first).
            Returns ``[]`` if no binding matches — never raises LookupError.

        Edge cases:
            - No binding → returns [] without raising.
        """
        try:
            return await self._container.aget_all(self._tp, qualifier=qualifier)
        except LookupError:
            # Same design as get_all() — swallow to support optional multi-dep pattern.
            return []

    def resolvable(
        self,
        *,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> bool:
        """Return True if at least one binding matches — safe to call ``.get()``.

        Performs a side-effect-free check via ``container.is_resolvable()``;
        no instances are created or cached.  Analogous to Jakarta CDI's
        ``Instance.isResolvable()`` (adapted: Providify uses priority to break ties,
        so "resolvable" means "at least one candidate exists", not "exactly one").

        Pass the same qualifier/priority you intend to use in the subsequent
        ``.get()`` call so the guard matches the actual resolution filter.

        Args:
            qualifier: Named qualifier to narrow the check. ``None`` matches any.
            priority:  Exact priority to match. ``None`` accepts any priority.

        Returns:
            True  — one or more bindings match; ``.get(qualifier=..., priority=...)``
                    will not raise LookupError.
            False — no bindings match; ``.get()`` with the same filter would raise.

        Thread safety:  ✅ Safe — reads only; no writes to container state.

        Edge cases:
            - Called before any binding is registered → False.
            - Called after container.reset() → False (registry cleared).
            - qualifier/priority narrow to zero → False.
            - Re-evaluated on every call — not cached. ✅ safe after dynamic binding.

        Example:
            if proxy.resolvable(qualifier="email"):
                svc = proxy.get(qualifier="email")
        """
        # DESIGN: delegate to container.is_resolvable() — a public method — rather
        # than calling _filter() directly.  This keeps InstanceProxy on the public
        # API surface of the container and avoids coupling to private internals.
        return self._container.is_resolvable(
            self._tp,
            qualifier=qualifier,
            priority=priority,
        )

    def __repr__(self) -> str:
        # No qualifier/priority to show — filtering is call-time, not construction-time.
        return f"InstanceProxy[{self._tp.__name__}](unresolved)"


class _InstanceAlias:
    """Sugar over Annotated[T, InstanceMeta(...)].

    Three equivalent forms — choose based on what options you need:

        # 1. Subscript — no options, cleanest syntax, Pylance shows InstanceProxy[T]
        notifiers: Instance[Notifier]

        # 2. Call — options available, but requires # type: ignore[valid-type]
        #    because a call expression is not valid in annotation position.
        notifiers: Instance(Notifier, qualifier="sms")  # type: ignore[valid-type]

        # 3. Annotated — recommended when qualifier / priority are needed.
        #    Fully valid Python; Pylance hover shows bare InstanceProxy[Notifier].
        from providify import InstanceMeta
        from typing import Annotated
        notifiers: Annotated[Notifier, InstanceMeta(qualifier="sms")]

    Both forms expand to Annotated[T, InstanceMeta(...)], which the container
    detects in _resolve_hint_sync/_async and converts to an InstanceProxy.

    Thread safety:  ✅ Safe — stateless singleton, no mutable state.
    Async safety:   ✅ Safe — stateless singleton.
    """

    def __getitem__(self, tp: Any) -> Any:
        # Subscript form — no options, plain instance handle injection
        return Annotated[tp, InstanceMeta()]

    def __call__(self, tp: Any) -> Any:
        # Call form — equivalent to subscript form since InstanceMeta has no options.
        # Kept for API consistency with Inject / Lazy / Live call forms.
        return Annotated[tp, InstanceMeta()]


if TYPE_CHECKING:
    # DESIGN: Instance is a TypeAlias for InstanceProxy under TYPE_CHECKING.
    # InstanceProxy is a proper Generic[T] class — aliasing to it gives the
    # type checker full knowledge:
    #   notifiers: Instance[Notifier]  →  notifiers: InstanceProxy[Notifier]
    #   notifiers.get()                →  returns Notifier   ✅
    #   notifiers.get_all()            →  returns list[Notifier]   ✅
    # This is simpler and more correct than a custom stub class.
    Instance = InstanceProxy
else:
    # DESIGN: module-level singleton — same pattern as Lazy / Live / Inject.
    # Users import Instance and use it as a type alias factory; they never
    # instantiate _InstanceAlias directly.
    Instance = _InstanceAlias()


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


def _unwrap_classvar(hint: Any) -> Any:
    """Strip a ``ClassVar[X]`` wrapper and return the inner type ``X``.

    Used by the container's class-var injection paths so that annotations
    like ``ClassVar[Instance[T]]`` are treated identically to plain
    ``Instance[T]`` during resolution and scope-violation checks.

    Args:
        hint: Any type hint — ClassVar-wrapped or not.

    Returns:
        The inner type arg when ``hint`` is ``ClassVar[X]``; the original
        hint unchanged for every other form (bare type, ``Annotated[...]``,
        ``list[T]``, etc.).

    Edge cases:
        - Bare ``ClassVar`` with no args (shouldn't appear in practice) → hint
          returned unchanged rather than crashing.
        - Already-unwrapped hint                                         → no-op.
    """
    if get_origin(hint) is ClassVar:
        inner_args = get_args(hint)
        # Guard against bare ClassVar with no args — extremely rare in practice
        # but avoids an IndexError in pathological annotation cases.
        return inner_args[0] if inner_args else hint
    return hint


def _get_providify_metadata(hint: Any) -> _providify | None:
    # ClassVar[Instance[T]] expands to ClassVar[Annotated[T, InstanceMeta()]].
    # get_origin() returns ClassVar — not Annotated — so the Annotated check
    # below would miss it entirely.  Unwrap one level so the real inner type
    # (e.g. Annotated[T, InstanceMeta()]) is what we actually inspect.
    if get_origin(hint) is ClassVar:
        inner_args = get_args(hint)
        hint = inner_args[0] if inner_args else hint
    if get_origin(hint) is Annotated:
        args = get_args(hint)
        return next((a for a in args[1:] if isinstance(a, _providify)), None)
    return None
