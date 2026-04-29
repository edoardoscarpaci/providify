from __future__ import annotations

from typing import Any, Callable

_INTERCEPTOR_BINDING_ATTR = "__di_interceptor_binding__"
_INTERCEPTOR_ATTR = "__di_interceptor__"
_AROUND_INVOKE_ATTR = "__di_around_invoke__"


class InterceptorBindingMarker:
    """Marks a class as an interceptor binding annotation."""

    __slots__ = ()


class InterceptorMarker:
    """Marks a class as an interceptor implementation."""

    __slots__ = ()


class AroundInvokeMarker:
    """Marks a method as the around-invoke interceptor body."""

    __slots__ = ("fn_name",)

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn_name = fn.__name__


def InterceptorBinding(cls: type) -> type:
    """
    Marks a class as an interceptor binding annotation.

    An interceptor binding links interceptor implementations to the beans
    they should intercept. Apply the same binding annotation to both the
    interceptor class and the target bean class.

    The resulting class can be used as a class decorator (``@Transactional``)
    to stamp itself as an attribute on the target, which ``_apply_interceptors``
    then reads to build the interceptor chain.

    Usage::

        @InterceptorBinding
        class Transactional:
            pass

        @Interceptor
        @Transactional        # stamps Transactional on TxInterceptor
        class TxInterceptor:
            @AroundInvoke
            def intercept(self, ctx):
                ...
                return ctx.proceed()

        @Component
        @Transactional        # stamps Transactional on OrderService
        class OrderService:
            ...

    Equivalent to Jakarta's @InterceptorBinding.
    """
    setattr(cls, _INTERCEPTOR_BINDING_ATTR, InterceptorBindingMarker())

    # Allow @Logged to be used as a class decorator. When Python evaluates
    # @Logged on a class, it calls Logged(target_cls). We override __new__ so
    # that if the argument is a class (not an instantiation), the binding
    # annotation is stamped on the target and the target is returned unchanged.
    # type.__call__ skips __init__ when __new__ returns a non-instance.
    def _new(mcs: Any, maybe_target: Any = None) -> Any:
        if isinstance(maybe_target, type) and maybe_target is not mcs:
            setattr(maybe_target, mcs.__name__, mcs)
            return maybe_target
        return object.__new__(mcs)

    cls.__new__ = _new  # type: ignore[method-assign]
    return cls


def Interceptor(cls: type) -> type:
    """
    Marks a class as an interceptor.

    The class must also carry an ``@InterceptorBinding`` annotation to
    declare which beans it intercepts, and must have exactly one
    ``@AroundInvoke`` method.

    Equivalent to Jakarta's @Interceptor.
    """
    setattr(cls, _INTERCEPTOR_ATTR, InterceptorMarker())
    return cls


def AroundInvoke(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Marks a method as the around-invoke body of an interceptor.

    The method receives an ``InvocationContext`` and must call
    ``ctx.proceed()`` to continue the chain (or skip it to short-circuit).

    Usage::

        @AroundInvoke
        def intercept(self, ctx: InvocationContext) -> Any:
            print("before")
            result = ctx.proceed()
            print("after")
            return result

    Equivalent to Jakarta's @AroundInvoke.
    """
    fn.__dict__[_AROUND_INVOKE_ATTR] = AroundInvokeMarker(fn)
    return fn


def _is_interceptor(cls: type) -> bool:
    """Returns True if the class is decorated with @Interceptor."""
    return isinstance(getattr(cls, _INTERCEPTOR_ATTR, None), InterceptorMarker)


def _is_interceptor_binding(cls: type) -> bool:
    """Returns True if the class is decorated with @InterceptorBinding."""
    return isinstance(
        getattr(cls, _INTERCEPTOR_BINDING_ATTR, None), InterceptorBindingMarker
    )


def _get_around_invoke_method(cls: type) -> str | None:
    """
    Returns the name of the @AroundInvoke method on an interceptor class,
    or None if not found. Walks the MRO.
    """
    for base in cls.__mro__:
        for name, val in vars(base).items():
            if callable(val) and isinstance(
                getattr(val, "__dict__", {}).get(_AROUND_INVOKE_ATTR),
                AroundInvokeMarker,
            ):
                return name
    return None


def _get_interceptor_bindings(cls: type) -> list[type]:
    """
    Returns all interceptor binding annotation classes applied to ``cls``.

    Walks the class's own annotations (decorators that are themselves
    @InterceptorBinding-marked classes).
    """
    bindings: list[type] = []
    for attr_val in vars(cls).values():
        if isinstance(attr_val, type) and _is_interceptor_binding(attr_val):
            bindings.append(attr_val)
    # Also check class-level attributes set by applying binding decorators
    for name in dir(cls):
        val = getattr(cls, name, None)
        if isinstance(val, type) and _is_interceptor_binding(val):
            if val not in bindings:
                bindings.append(val)
    return bindings
