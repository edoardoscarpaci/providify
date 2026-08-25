"""`@Multibound` — declare a type as a multibinding collection point (plan 010, F7).

Mirrors `decorator/interceptor.py`'s marker-class shape (`InterceptorBinding`,
`_is_interceptor_binding`) exactly: a private stamped attribute plus a
predicate helper, with `container.py` as the only consumer that cares.

`@Multibound` is one of the two equivalent ways to declare a collection point
(plan 010 §Design F7.2) — the other is `container.multibind(cls)`. Putting the
marker on the interface itself (rather than only on the container instance)
lets the declaration travel with the type across modules, and works for types
discovered by `container.scan()` without a matching explicit `multibind()`
call at the call site.
"""

from __future__ import annotations

_MULTIBOUND_ATTR = "__di_multibound__"


class MultiboundMarker:
    """Sentinel stamped on a class by `@Multibound` — see `_is_multibound`."""

    __slots__ = ()


def Multibound(cls: type) -> type:
    """Declare *cls* as a multibinding collection point (plan 010 §Design F7.2).

    Equivalent to calling ``container.multibind(cls)`` for every container that
    later resolves this type — the marker travels with the class itself, so it
    also works for types discovered via ``container.scan()`` where there is no
    single call site to add an explicit ``multibind()`` call.

    Usage::

        @Multibound
        class Handler(ABC):
            @abstractmethod
            def handle(self) -> str: ...

        @Component
        class Dispatcher:
            def __init__(self, handlers: list[Handler]) -> None:
                self.handlers = handlers  # every registered Handler impl

    Args:
        cls: The interface (or base class) that contributions will be
            registered against (via `bind()`, `register()`, or `provide()`).

    Returns:
        *cls*, unmodified except for the stamped marker attribute.

    Example:
        >>> @Multibound
        ... class Plugin: ...
    """
    setattr(cls, _MULTIBOUND_ATTR, MultiboundMarker())
    return cls


def _is_multibound(cls: object) -> bool:
    """Return True if *cls* was declared a collection point via `@Multibound`.

    Args:
        cls: Any object — non-classes always return False.

    Returns:
        True if `@Multibound` was applied to *cls* (checked via `getattr`,
        so the marker is inherited by subclasses the same way `isinstance`
        checks are — consistent with `_is_interceptor_binding`'s lookup).
    """
    return isinstance(getattr(cls, _MULTIBOUND_ATTR, None), MultiboundMarker)
