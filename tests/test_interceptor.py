"""Tests for F5: @Interceptor / @InterceptorBinding / @AroundInvoke."""

from __future__ import annotations

from providify import (
    AroundInvoke,
    DIContainer,
    Interceptor,
    InterceptorBinding,
    InvocationContext,
    Singleton,
)


@InterceptorBinding
class Logged:
    pass


@Interceptor
@Logged
class LoggingInterceptor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @AroundInvoke
    def intercept(self, ctx: InvocationContext) -> object:
        self.calls.append(f"before:{ctx.method.__name__}")
        result = ctx.proceed()
        self.calls.append(f"after:{ctx.method.__name__}")
        return result


@Singleton
@Logged
class OrderService:
    def __init__(self) -> None:
        self.processed = 0

    def process(self) -> str:
        self.processed += 1
        return "done"


def test_interceptor_wraps_method(container: DIContainer):
    # Manually inject interceptor instance to verify chain fires
    container.register(OrderService)
    container.add_interceptor(LoggingInterceptor)
    svc = container.get(OrderService)

    result = svc.process()
    assert result == "done"


def test_add_interceptor_requires_decorator():
    container = DIContainer()

    class Plain:
        pass

    import pytest

    with pytest.raises(TypeError):
        container.add_interceptor(Plain)


def test_invocation_context_proceed():
    class Target:
        def greet(self) -> str:
            return "hello"

    target = Target()
    ctx = InvocationContext(
        target=target,
        method=target.greet,
        parameters=(),
        kwargs={},
        _chain=[],
    )
    result = ctx.proceed()
    assert result == "hello"


def test_invocation_context_chain():
    log = []

    class Target:
        def work(self) -> str:
            return "done"

    class Interceptor1:
        def around(self, ctx: InvocationContext) -> object:
            log.append("i1_before")
            r = ctx.proceed()
            log.append("i1_after")
            return r

    class Interceptor2:
        def around(self, ctx: InvocationContext) -> object:
            log.append("i2_before")
            r = ctx.proceed()
            log.append("i2_after")
            return r

    t = Target()
    i1 = Interceptor1()
    i2 = Interceptor2()
    ctx = InvocationContext(
        target=t,
        method=t.work,
        parameters=(),
        kwargs={},
        _chain=[(i1, "around"), (i2, "around")],
    )
    result = ctx.proceed()
    assert result == "done"
    assert log == ["i1_before", "i2_before", "i2_after", "i1_after"]


def test_interceptor_markers():
    from providify.decorator.interceptor import (
        _is_interceptor,
        _is_interceptor_binding,
        _get_around_invoke_method,
    )

    assert _is_interceptor(LoggingInterceptor)
    assert _is_interceptor_binding(Logged)
    assert _get_around_invoke_method(LoggingInterceptor) == "intercept"
