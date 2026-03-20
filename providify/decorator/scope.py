from __future__ import annotations

import inspect
from typing import (
    Callable,
    Any,
    TypeVar,
    overload,
)

from ..exceptions import NotDecoratedError
from ..metadata import (
    DIMetadata,
    ProviderMetadata,
    Scope,
    _is_decorated,
    _get_own_metadata,
    _set_metadata,
    _get_provider_metadata,
    _set_provider_metadata,
)

T = TypeVar("T")
R = TypeVar("R")


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────
def _is_function_provider(obj: Any) -> bool:
    """
    Returns True if obj is a callable but not a class.
    Distinguishes @Provider functions from @Component classes.
    """
    return callable(obj) and not isinstance(obj, type)


# ─────────────────────────────────────────────────────────────────
#  _make_decorator — factory for all scope decorators
#  Eliminates duplication across @Component, @Singleton,
#  @RequestScoped, @SessionScoped — only the Scope value differs
# ─────────────────────────────────────────────────────────────────
def _make_decorator(scope: Scope) -> Any:
    @overload
    def decorator(__cls: type[T]) -> type[T]: ...

    @overload
    def decorator(
        __cls: None = ...,
        *,
        qualifier: str | None = None,
        priority: int = 0,
        inherited: bool = False,
    ) -> Callable[[type[T]], type[T]]: ...

    def decorator(
        __cls: Any = None,
        *,
        qualifier: str | None = None,
        priority: int = 0,
        inherited: bool = False,
    ) -> Any:
        def stamp(c: type[T]) -> type[T]:
            existing = _get_own_metadata(c)

            _set_metadata(
                c,
                (
                    existing.merge(  # merge if already decorated
                        scope=scope,
                        qualifier=qualifier,
                        priority=priority,
                        inherited=inherited,
                    )
                    if existing is not None
                    else DIMetadata(  # fresh if first decorator
                        scope=scope,
                        qualifier=qualifier,
                        priority=priority,
                        inherited=inherited,
                    )
                ),
            )
            return c

        if __cls is not None:
            return stamp(__cls)
        return stamp

    return decorator


# ─────────────────────────────────────────────────────────────────
#  Public scope decorators — explicit @overload wrappers around
#  _make_decorator so that linters (pyright / mypy) can see the
#  kwargs (qualifier, priority, inherited) instead of just `Any`.
#
#  DESIGN: We intentionally keep _make_decorator as the single
#  source of truth for the runtime logic, but expose public names
#  as thin wrapper functions that carry the typed @overload stubs.
#
#  Without this, `Singleton = _make_decorator(Scope.SINGLETON)`
#  produces a name of type `Any` — overloads defined inside the
#  closure are invisible to the type checker.
#
#  Tradeoffs:
#    ✅ Linters see qualifier / priority / inherited kwargs
#    ✅ Type narrowing works: @Singleton(cls) → type[T]
#    ✅ Runtime behaviour is identical — delegates to _make_decorator
#    ❌ Four thin wrappers to maintain if the signature ever changes
#    ❌ Slightly more boilerplate — acceptable given the clear upside
#
#  Alternative considered: Protocol with overloaded __call__.
#  Rejected because pyright's support for @overload inside Protocol
#  bodies is inconsistent across versions, making it unreliable.
# ─────────────────────────────────────────────────────────────────

# Private implementations — carry the actual runtime logic.
_component_impl = _make_decorator(Scope.DEPENDENT)
_singleton_impl = _make_decorator(Scope.SINGLETON)
_request_impl = _make_decorator(Scope.REQUEST)
_session_impl = _make_decorator(Scope.SESSION)


# ── Component ──────────────────────────────────────────────────────


@overload
def Component(__cls: type[T]) -> type[T]: ...


@overload
def Component(
    __cls: None = ...,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def Component(
    __cls: Any = None,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Any:
    """
    Marks a class as a DI component with DEPENDENT (prototype) scope.

    Each injection creates a fresh instance — no shared state.
    Equivalent to Jakarta CDI's default (dependent) scope.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @Component decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @Component
        class EmailService(NotificationService): ...

        @Component(qualifier="sms", priority=2)
        class SmsService(NotificationService): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _component_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited
    )


# ── Singleton ──────────────────────────────────────────────────────


@overload
def Singleton(__cls: type[T]) -> type[T]: ...


@overload
def Singleton(
    __cls: None = ...,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def Singleton(
    __cls: Any = None,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Any:
    """
    Marks a class as a DI component with SINGLETON scope.

    One shared instance is created per container and reused for every
    injection — equivalent to Jakarta CDI's @ApplicationScoped.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @Singleton decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @Singleton
        class DatabasePool: ...

        @Singleton(qualifier="primary", priority=10)
        class PrimaryDatabase(Database): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _singleton_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited
    )


# ── RequestScoped ──────────────────────────────────────────────────


@overload
def RequestScoped(__cls: type[T]) -> type[T]: ...


@overload
def RequestScoped(
    __cls: None = ...,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def RequestScoped(
    __cls: Any = None,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Any:
    """
    Marks a class as a DI component with REQUEST scope.

    One instance is created per active request context and shared across
    all injections within that request — equivalent to Jakarta's @RequestScoped.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @RequestScoped decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @RequestScoped
        class RequestContext: ...

        @RequestScoped(qualifier="audit")
        class AuditRequestContext(RequestContext): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _request_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited
    )


# ── SessionScoped ──────────────────────────────────────────────────


@overload
def SessionScoped(__cls: type[T]) -> type[T]: ...


@overload
def SessionScoped(
    __cls: None = ...,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def SessionScoped(
    __cls: Any = None,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Any:
    """
    Marks a class as a DI component with SESSION scope.

    One instance is created per active session context and shared across
    all injections within that session — equivalent to Jakarta's @SessionScoped.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @SessionScoped decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @SessionScoped
        class UserSession: ...

        @SessionScoped(qualifier="admin")
        class AdminSession(UserSession): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _session_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited
    )


# ─────────────────────────────────────────────────────────────────
#  _make_updater — factory for single/multi field update decorators
#  Named / Priority / and any future field-update decorators
#
#  Accepts a builder callable that receives the decorator's kwargs
#  and returns the dict of fields to update — keeping _make_updater
#  itself generic and field-agnostic.
# ─────────────────────────────────────────────────────────────────


def _make_updater(
    builder: Callable[..., dict[str, Any]],
    *,
    require_args: bool = False,
) -> Any:
    def updater(__cls: Any = None, **kwargs: Any) -> Any:
        def decorator(c: Any) -> Any:
            if not _is_decorated(c):
                raise NotDecoratedError(c)

            updates = builder(**kwargs)

            if _is_function_provider(c):
                existing = _get_provider_metadata(c)
                if existing is not None:
                    _set_provider_metadata(c, existing.merge(**updates))
            else:
                existing = _get_own_metadata(c)
                if existing is not None:
                    _set_metadata(c, existing.merge(**updates))

            return c

        if __cls is not None:
            if require_args:
                raise TypeError(
                    f"This decorator requires keyword arguments — "
                    f"use it with parens: @{updater.__name__}(...)"
                )
            return decorator(__cls)
        return decorator

    return updater


# ─────────────────────────────────────────────────────────────────
#  Public updater decorators
# ─────────────────────────────────────────────────────────────────

Priority = _make_updater(
    # Single field — only updates priority, never touches qualifier/scope/etc.
    lambda *, priority: {"priority": priority},
)

Named = _make_updater(
    # Single field — only updates qualifier
    # name= maps to qualifier internally, matching Jakarta's @Named
    lambda *, name: {"qualifier": name},
    require_args=True,  # @Named without name= is always a mistake
)

Inheritable = _make_updater(
    # Updates inherited flag only
    lambda: {"inherited": True}
)


# ─────────────────────────────────────────────────────────────────
#  @Provider — standalone, not a _make_updater candidate
#  It stamps __di_provider__ from scratch, not updating an existing
#  metadata dict, and has unique async detection logic.
# ─────────────────────────────────────────────────────────────────
@overload
def Provider(__fn: Callable[..., R]) -> Callable[..., R]: ...


@overload
def Provider(
    __fn: None = ...,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    singleton: bool = False,
    scope: Scope | None = None,
) -> Callable[[Callable[..., R]], Callable[..., R]]: ...


def Provider(
    __fn: Any = None,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    singleton: bool = False,
    # scope — explicit Scope value, overrides singleton flag when set.
    # Enables @Provider to produce REQUEST or SESSION scoped values,
    # mirroring Jakarta CDI's @Produces @RequestScoped pattern.
    # Example: @Provider(scope=Scope.REQUEST)
    scope: Scope | None = None,
) -> Any:
    """
    Marks a function as a DI provider.
    Return type hint determines the provided type.

    Supports both sync and async functions —
    is_async is detected once at decoration time via inspect,
    so ProviderBinding never needs to call inspect at resolution time.

    Equivalent to Jakarta's @Produces / @Bean.

    Scope resolution priority:
        1. ``scope=Scope.REQUEST`` / ``scope=Scope.SESSION`` — explicit scope
        2. ``singleton=True``                                — Scope.SINGLETON
        3. default                                           — Scope.DEPENDENT

    Usage:
        @Provider
        def email_service() -> NotificationService:
            return EmailService(load_config())

        @Provider(qualifier="sms", priority=2, singleton=True)
        def sms_service() -> NotificationService:
            return SMSService(api_key="secret")

        @Provider(singleton=True)
        async def db_pool() -> DatabasePool:
            pool = DatabasePool()
            await pool.connect()    # async initialisation ✅
            return pool

        # Mimic Jakarta's @Produces @RequestScoped —
        # factory runs once per request, result cached for its duration.
        @Provider(scope=Scope.REQUEST)
        def jwt_token(header: Inject[AuthHeader]) -> JWTToken:
            return JWTToken.decode(header.value)
    """

    def decorator(fn: Callable[..., R]) -> Callable[..., R]:
        existing = _get_provider_metadata(fn)

        _set_provider_metadata(
            fn,
            (
                existing.merge(  # merge if already decorated
                    singleton=singleton,
                    qualifier=qualifier,
                    priority=priority,
                    scope=scope,
                    is_async=inspect.iscoroutinefunction(
                        fn
                    ),  # detected once at decoration time
                )
                if existing is not None
                else ProviderMetadata(  # fresh if first decorator
                    singleton=singleton,
                    qualifier=qualifier,
                    priority=priority,
                    scope=scope,
                    is_async=inspect.iscoroutinefunction(
                        fn
                    ),  # detected once at decoration
                )
            ),
        )

        return fn

    if __fn is not None:
        return decorator(__fn)
    return decorator
