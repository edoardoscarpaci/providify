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

            _set_metadata(c,
                existing.merge(                      # merge if already decorated
                    scope     = scope,
                    qualifier = qualifier,
                    priority  = priority,
                    inherited = inherited,
                ) if existing is not None else
                DIMetadata(                          # fresh if first decorator
                    scope     = scope,
                    qualifier = qualifier,
                    priority  = priority,
                    inherited = inherited,
                )
            )
            return c

        if __cls is not None:
            return stamp(__cls)
        return stamp

    return decorator


# ─────────────────────────────────────────────────────────────────
#  Public scope decorators — each is just _make_decorator(Scope.X)
# ─────────────────────────────────────────────────────────────────

Component     = _make_decorator(Scope.DEPENDENT)
Singleton     = _make_decorator(Scope.SINGLETON)
RequestScoped = _make_decorator(Scope.REQUEST)
SessionScoped = _make_decorator(Scope.SESSION)
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
    require_args=True,      # @Named without name= is always a mistake
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
) -> Callable[[Callable[..., R]], Callable[..., R]]: ...

def Provider(
    __fn: Any = None,
    *,
    qualifier: str | None = None,
    priority: int = 0,
    singleton: bool = False,
) -> Any:
    """
    Marks a function as a DI provider.
    Return type hint determines the provided type.

    Supports both sync and async functions —
    is_async is detected once at decoration time via inspect,
    so ProviderBinding never needs to call inspect at resolution time.

    Equivalent to Jakarta's @Produces / @Bean.

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
    """
    def decorator(fn: Callable[..., R]) -> Callable[..., R]:
        existing = _get_provider_metadata(fn)

        _set_provider_metadata(fn,
            existing.merge(                      # merge if already decorated
                singleton  = singleton,
                qualifier = qualifier,
                priority  = priority,
                is_async   = inspect.iscoroutinefunction(fn),  # detected once at decoration time
            ) if existing is not None else
            ProviderMetadata(                          # fresh if first decorator
                singleton  = singleton,
                qualifier = qualifier,
                priority  = priority,
                is_async   = inspect.iscoroutinefunction(fn),  # detected once at decoration
            )
        )

        return fn

    if __fn is not None:
        return decorator(__fn)
    return decorator