"""Tests for F14: run_in_request / run_in_session utilities."""

from __future__ import annotations

import pytest

from providify import (
    DIContainer,
    RequestScoped,
    SessionScoped,
)


@RequestScoped
class RequestCtx:
    def __init__(self) -> None:
        self.counter = 0

    def inc(self) -> int:
        self.counter += 1
        return self.counter


@SessionScoped
class SessionCtx:
    def __init__(self) -> None:
        self.value = "initial"


def test_run_in_request_activates_scope(container: DIContainer):
    container.register(RequestCtx)

    def work():
        return container.get(RequestCtx)

    ctx = container.run_in_request(work)
    assert isinstance(ctx, RequestCtx)


def test_run_in_request_scope_torn_down_after(container: DIContainer):
    container.register(RequestCtx)

    def work():
        return container.get(RequestCtx)

    container.run_in_request(work)

    import pytest

    with pytest.raises(RuntimeError):
        container.get(RequestCtx)


def test_run_in_request_returns_fn_result(container: DIContainer):
    container.register(RequestCtx)

    def work():
        return 42

    result = container.run_in_request(work)
    assert result == 42


def test_run_in_session_activates_scope(container: DIContainer):
    container.register(SessionCtx)

    def work():
        return container.get(SessionCtx)

    ctx = container.run_in_session("sid1", work)
    assert isinstance(ctx, SessionCtx)


def test_run_in_session_same_id_same_instance(container: DIContainer):
    container.register(SessionCtx)

    def work():
        return container.get(SessionCtx)

    ctx1 = container.run_in_session("sid1", work)
    ctx2 = container.run_in_session("sid1", work)
    assert ctx1 is ctx2


def test_run_in_session_different_ids_different_instances(container: DIContainer):
    container.register(SessionCtx)

    def work():
        return container.get(SessionCtx)

    ctx1 = container.run_in_session("sid1", work)
    ctx2 = container.run_in_session("sid2", work)
    assert ctx1 is not ctx2


@pytest.mark.asyncio
async def test_arun_in_request_async(container: DIContainer):
    container.register(RequestCtx)

    async def work():
        return container.get(RequestCtx)

    ctx = await container.arun_in_request(work)
    assert isinstance(ctx, RequestCtx)


@pytest.mark.asyncio
async def test_arun_in_session_async(container: DIContainer):
    container.register(SessionCtx)

    async def work():
        return container.get(SessionCtx)

    ctx = await container.arun_in_session("sid", work)
    assert isinstance(ctx, SessionCtx)


def test_run_in_request_passes_args(container: DIContainer):
    container.register(RequestCtx)

    def work(multiplier: int):
        return multiplier * 2

    result = container.run_in_request(work, 5)
    assert result == 10


def test_run_in_session_passes_kwargs(container: DIContainer):
    container.register(SessionCtx)

    def work(value: int = 0):
        return value + 1

    result = container.run_in_session("s1", work, value=10)
    assert result == 11
