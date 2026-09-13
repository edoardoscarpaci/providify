from __future__ import annotations

from collections.abc import Callable
from typing import Any

_INTERCEPTOR_BINDING_ATTR = "__di_interceptor_binding__"
_INTERCEPTOR_ATTR = "__di_interceptor__"
_AROUND_INVOKE_ATTR = "__di_around_invoke__"
#: Field-advice marker attributes (plan 010, F8) — AspectJ get/set pointcuts
#: (research-F8 §AspectJ), NOT a CDI feature; see providify/field.py's module
#: docstring for the full framing.
_AROUND_GET_ATTR = "__di_around_get__"
_AROUND_SET_ATTR = "__di_around_set__"


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


class AroundGetMarker:
    """Marks a method as the around-get field-advice body (plan 010, F8).

    AspectJ `get` pointcut analogue (research-F8 §AspectJ) — advises reads of
    `Advised(...)` fields on beans that share an `@InterceptorBinding` with
    this interceptor. Not a CDI concept; see `providify/field.py`.
    """

    __slots__ = ("fn_name",)

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.fn_name = fn.__name__


class AroundSetMarker:
    """Marks a method as the around-set field-advice body (plan 010, F8).

    AspectJ `set` pointcut analogue (research-F8 §AspectJ) — see
    `AroundGetMarker` and `providify/field.py` for the full framing.
    """

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
    ``@AroundInvoke`` method (mirrors Jakarta's rule), and at most one each
    of ``@AroundGet``/``@AroundSet`` (plan 010, F8 — AspectJ get/set pointcut
    analogues, never a CDI concept; see ``providify/field.py``). The
    at-most-one checks run eagerly, right here, so a misconfigured
    interceptor fails at decoration time rather than at first resolution.

    Equivalent to Jakarta's @Interceptor.

    Raises:
        TypeError: *cls* declares more than one ``@AroundGet`` or more than
            one ``@AroundSet`` method.
    """
    # Eager validation — see docstring. @AroundInvoke intentionally keeps its
    # pre-existing lazy (first-resolution-time) check to avoid any behaviour
    # change for existing callers (plan 010 step 17).
    _get_around_get_method(cls)
    _get_around_set_method(cls)
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


def AroundGet(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Marks a method as the around-get field-advice body of an interceptor.

    AspectJ `get` pointcut analogue (research-F8 §AspectJ), implemented via
    Python's descriptor protocol rather than bytecode weaving — see
    `providify/field.py`'s module docstring for the full framing. This is
    **not** a CDI feature: Jakarta Interceptors 2.1 has no field-level
    interception in either profile.

    The method receives a `FieldAccessContext` and must call `ctx.proceed()`
    to continue the chain (or skip it to short-circuit a read).

    Usage::

        @AroundGet
        def on_get(self, ctx: FieldAccessContext) -> Any:
            print(f"reading {ctx.field}")
            return ctx.proceed()
    """
    fn.__dict__[_AROUND_GET_ATTR] = AroundGetMarker(fn)
    return fn


def AroundSet(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Marks a method as the around-set field-advice body of an interceptor.

    AspectJ `set` pointcut analogue (research-F8 §AspectJ) — see
    `AroundGet` and `providify/field.py` for the full framing. Not a CDI
    feature.

    The method receives a `FieldAccessContext` and must call `ctx.proceed()`
    to let the write land (skipping it vetoes the write; the old value
    stands).

    Usage::

        @AroundSet
        def on_set(self, ctx: FieldAccessContext) -> None:
            print(f"writing {ctx.field} = {ctx.value!r}")
            ctx.proceed()
    """
    fn.__dict__[_AROUND_SET_ATTR] = AroundSetMarker(fn)
    return fn


def _is_interceptor(cls: type) -> bool:
    """Returns True if the class is decorated with @Interceptor."""
    return isinstance(getattr(cls, _INTERCEPTOR_ATTR, None), InterceptorMarker)


def _is_interceptor_binding(cls: type) -> bool:
    """Returns True if the class is decorated with @InterceptorBinding."""
    return isinstance(getattr(cls, _INTERCEPTOR_BINDING_ATTR, None), InterceptorBindingMarker)


def _get_marked_method(cls: type, attr: str, marker_cls: type) -> Callable[..., Any] | None:
    """Return the single method on *cls* (MRO-walked) marked with *marker_cls*, or None.

    Shared implementation behind `_get_around_invoke_method`,
    `_get_around_get_method` and `_get_around_set_method` — only the marker
    attribute name and marker class differ between the three call/set/get
    variants (plan 010 step 17).

    Args:
        cls:        The interceptor class to scan.
        attr:       The `__dict__` key the marker decorator stamps on the
                    method (e.g. `_AROUND_INVOKE_ATTR`).
        marker_cls: The marker class the stamped value must be an instance
                    of (e.g. `AroundInvokeMarker`).

    Returns:
        The marked method (an unbound function, as found in `vars(base)`),
        or `None` if no method on *cls* or any of its bases carries the
        marker.

    Raises:
        TypeError: More than one method carries the marker — "at most one
            of each" (plan 010 §Design F8.5).
    """
    found: list[Callable[..., Any]] = []
    for base in cls.__mro__:
        for name, val in vars(base).items():
            if callable(val) and isinstance(getattr(val, "__dict__", {}).get(attr), marker_cls):
                found.append(val)
    if len(found) > 1:
        names = ", ".join(fn.__name__ for fn in found)
        raise TypeError(
            f"{cls.__name__} declares {len(found)} methods marked with the "
            f"same interceptor marker ({names}) — at most one is allowed "
            f"per interceptor class."
        )
    return found[0] if found else None


def _get_around_invoke_method(cls: type) -> str | None:
    """
    Returns the name of the @AroundInvoke method on an interceptor class,
    or None if not found. Walks the MRO.
    """
    fn = _get_marked_method(cls, _AROUND_INVOKE_ATTR, AroundInvokeMarker)
    return fn.__name__ if fn is not None else None


def _get_around_get_method(cls: type) -> Callable[..., Any] | None:
    """Returns the @AroundGet method on an interceptor class, or None. Walks the MRO.

    AspectJ `get` pointcut analogue (research-F8 §AspectJ) — not CDI. Unlike
    `_get_around_invoke_method`, this returns the callable itself (not its
    name) — `_apply_interceptors` only needs the name to build a chain entry,
    which it derives via `.__name__`.
    """
    return _get_marked_method(cls, _AROUND_GET_ATTR, AroundGetMarker)


def _get_around_set_method(cls: type) -> Callable[..., Any] | None:
    """Returns the @AroundSet method on an interceptor class, or None. Walks the MRO.

    AspectJ `set` pointcut analogue (research-F8 §AspectJ) — not CDI. See
    `_get_around_get_method`.
    """
    return _get_marked_method(cls, _AROUND_SET_ATTR, AroundSetMarker)


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
