"""Unit tests for Live[T] always-fresh injection and LiveProxy.

Live[T] is the correct injection form for @RequestScoped and @SessionScoped
dependencies held by longer-lived components (@Singleton, @SessionScoped).
Unlike Inject[T] or Lazy[T], Live[T] NEVER caches — every .get() / .aget()
call delegates back to the container, which routes through the active
ScopeContext and returns the instance for the *current* scope.

Covered:
    LiveProxy unit (isolated from container):
    - Live[T] in a constructor creates a LiveProxy, not the dep itself
    - LiveProxy.get() calls container.get() on every invocation (no caching)
    - LiveProxy.aget() calls container.aget() on every invocation (no caching)
    - LiveProxy.__repr__ reflects the wrapped type

    Integration — request scope:
    - @Singleton with Live[RequestScoped] sees the current request's instance
    - @Singleton sees a fresh instance in each new request context (no stale cache)
    - Two distinct request contexts produce distinct instances via the same proxy

    Integration — session scope:
    - @Singleton with Live[SessionScoped] sees the current session's instance
    - @Singleton sees a fresh instance after the session changes

    Validation:
    - Inject[T] for a REQUEST/SESSION dep raises LiveInjectionRequiredError
    - Lazy[T]  for a REQUEST/SESSION dep raises LiveInjectionRequiredError
    - Live[T]  for a REQUEST/SESSION dep passes validation (correct usage)
    - Live(T, qualifier=...) forwards qualifier to container.get()
"""

from __future__ import annotations

from typing import Annotated

import pytest

from providify.container import DIContainer
from providify.decorator.scope import RequestScoped, SessionScoped, Singleton
from providify.exceptions import LiveInjectionRequiredError
from providify.type import Inject, Lazy, Live, LiveMeta, LiveProxy


# ─────────────────────────────────────────────────────────────────
#  Domain types
# ─────────────────────────────────────────────────────────────────


@RequestScoped
class RequestToken:
    """REQUEST scope — new instance per request context.

    instance_count tracks how many instances have been created across
    all requests, letting tests verify that the proxy re-resolves rather
    than returning a cached value from a previous request.
    """

    instance_count: int = 0

    def __init__(self) -> None:
        RequestToken.instance_count += 1
        # Each instance remembers its own creation-order index so tests can
        # distinguish "first request's token" from "second request's token".
        self.index = RequestToken.instance_count

    @classmethod
    def reset(cls) -> None:
        """Reset counter between tests to keep assertions independent."""
        cls.instance_count = 0


@SessionScoped
class SessionToken:
    """SESSION scope — one instance per active session ID."""

    instance_count: int = 0

    def __init__(self) -> None:
        SessionToken.instance_count += 1
        self.index = SessionToken.instance_count

    @classmethod
    def reset(cls) -> None:
        SessionToken.instance_count = 0


# ─────────────────────────────────────────────────────────────────
#  LiveProxy unit tests (direct construction, no real container)
# ─────────────────────────────────────────────────────────────────


class TestLiveProxyUnit:
    """Unit tests for LiveProxy in isolation — backed by a call-counting fake container.

    DESIGN: A FakeContainer that increments a counter on every .get() call is
    the most direct proof that LiveProxy never caches — if the counter grows
    proportionally to .get() calls on the proxy, caching is absent.
    """

    def _make_proxy(
        self,
        resolved_value: object,
        qualifier: str | None = None,
    ) -> tuple[LiveProxy, list[tuple[type, str | None]]]:
        """Helper — creates a LiveProxy backed by a call-recording fake container.

        Returns:
            A (proxy, calls) pair. Every call to fake_container.get() appends
            (type, qualifier) to *calls* so tests can assert on count and args.
        """
        calls: list[tuple[type, str | None]] = []

        class FakeContainer:
            def get(
                self,
                tp: type,
                *,
                qualifier: str | None = None,
                priority: int | None = None,
            ) -> object:
                calls.append((tp, qualifier))
                return resolved_value

        # type: ignore — FakeContainer satisfies the Any-typed _container field
        proxy = LiveProxy(
            FakeContainer(),  # type: ignore[arg-type]
            RequestToken,
            qualifier=qualifier,
        )
        return proxy, calls

    def test_live_injection_creates_proxy_not_dep(self, container: DIContainer) -> None:
        """Live[T] in a constructor must inject a LiveProxy, not the resolved dep.

        DESIGN: The proxy is what gives Live[T] its always-fresh semantics —
        the actual instance is resolved lazily on every .get() call, not eagerly
        at construction time the way Inject[T] would.
        """

        @Singleton
        class AuthService:
            def __init__(self, token: Live[RequestToken]) -> None:
                # At construction time this should be a proxy, NOT a RequestToken
                self.token = token

        container.register(RequestToken)
        container.register(AuthService)

        with container.scope_context.request():
            svc = container.get(AuthService)

        # The stored attribute must be the proxy itself — callers reach through it
        assert isinstance(svc.token, LiveProxy)

    def test_proxy_calls_container_every_time(self) -> None:
        """LiveProxy.get() must call container.get() on every invocation — no caching.

        DESIGN: This is the core contract of Live[T].  Unlike LazyProxy, which
        sets _resolved = True after the first call and short-circuits, LiveProxy
        has no _resolved guard — every call goes back to the container.
        """
        token = RequestToken()
        proxy, calls = self._make_proxy(token)

        proxy.get()
        proxy.get()
        proxy.get()

        # Three proxy.get() calls must produce exactly three container.get() calls
        assert len(calls) == 3

    def test_proxy_does_not_cache_between_calls(self) -> None:
        """LiveProxy.get() must return the container's current value every time.

        If the fake container returns a different object each call, the proxy
        must faithfully relay each one — proving there is no cached instance
        being returned after the first call.
        """
        values = [object(), object(), object()]
        call_index = 0

        class RotatingContainer:
            def get(
                self,
                tp: type,
                *,
                qualifier: str | None = None,
                priority: int | None = None,
            ) -> object:
                nonlocal call_index
                result = values[call_index % len(values)]
                call_index += 1
                return result

        proxy = LiveProxy(RotatingContainer(), RequestToken)  # type: ignore[arg-type]

        results = [proxy.get(), proxy.get(), proxy.get()]

        # Each call must return the value that the container returned for that call
        assert results[0] is values[0]
        assert results[1] is values[1]
        assert results[2] is values[2]

    def test_proxy_forwards_qualifier(self) -> None:
        """LiveProxy must forward the qualifier to container.get() on every call."""
        token = RequestToken()
        proxy, calls = self._make_proxy(token, qualifier="bearer")

        proxy.get()
        proxy.get()

        # Both calls must carry the qualifier — not just the first one
        assert all(q == "bearer" for _, q in calls)

    def test_proxy_repr(self) -> None:
        """LiveProxy.__repr__ must include the wrapped type name."""
        proxy, _ = self._make_proxy(object())

        # repr should identify both the proxy type and the wrapped class
        assert "LiveProxy" in repr(proxy)
        assert "RequestToken" in repr(proxy)


# ─────────────────────────────────────────────────────────────────
#  Integration — request scope
# ─────────────────────────────────────────────────────────────────


class TestLiveWithRequestScope:
    """Integration tests: @Singleton with Live[RequestScoped] sees fresh instances.

    DESIGN: These tests prove the end-to-end contract:
      1. The proxy stored in the singleton is a LiveProxy (not the dep itself).
      2. Calling .get() inside a request context returns that request's instance.
      3. Calling .get() in a NEW request context returns a DIFFERENT instance.
    The key assertion is always `first_token is not second_token` — not equality,
    identity — proving no stale instance was returned from a private cache.
    """

    def setup_method(self) -> None:
        """Reset instance counter before each test for clean assertions."""
        RequestToken.reset()

    def test_proxy_resolves_current_request_instance(
        self, container: DIContainer
    ) -> None:
        """LiveProxy.get() inside a request context must return that request's instance.

        The proxy must route through the container's ScopeContext, which caches
        one instance per request — so two .get() calls within the SAME request
        must return the identical object (scoped caching still applies).
        """

        @Singleton
        class AuthService:
            def __init__(self, token: Live[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(AuthService)

        with container.scope_context.request():
            svc = container.get(AuthService)
            # Two .get() calls in the same request — same RequestToken instance
            first = svc.token.get()
            second = svc.token.get()

        # Same request context → same scoped instance, but resolved fresh each time
        assert first is second

    def test_different_requests_yield_different_instances(
        self, container: DIContainer
    ) -> None:
        """The SAME proxy must return different instances across request contexts.

        This is the critical Live[T] contract: the singleton is constructed once,
        but svc.token.get() in request A returns A's token, and in request B
        returns B's token — the proxy never freezes on a single instance.
        """

        @Singleton
        class AuthService:
            def __init__(self, token: Live[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(AuthService)

        svc = None

        with container.scope_context.request():
            svc = container.get(AuthService)
            first_token = svc.token.get()

        with container.scope_context.request():
            # svc is the SAME singleton instance — token is the SAME LiveProxy
            # but .get() routes to the new request's ScopeContext
            second_token = svc.token.get()

        assert first_token is not second_token
        # Each request created exactly one RequestToken — total = 2
        assert RequestToken.instance_count == 2

    def test_inject_would_freeze_but_live_does_not(
        self, container: DIContainer
    ) -> None:
        """Live[T] must produce a new instance per request; Inject[T] would not.

        This test documents the exact defect that Live[T] was designed to fix.
        We can't easily demonstrate Inject[T] here (validation blocks it), but
        we can prove Live[T]'s index differs across requests — the smoking gun
        for freshness.
        """

        @Singleton
        class AuthService:
            def __init__(self, token: Live[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(AuthService)

        with container.scope_context.request():
            svc = container.get(AuthService)
            index_request_1 = svc.token.get().index

        with container.scope_context.request():
            index_request_2 = svc.token.get().index

        # Each request got a distinct RequestToken instance
        assert index_request_1 != index_request_2

    def test_multiple_proxies_in_same_request_share_scoped_instance(
        self, container: DIContainer
    ) -> None:
        """Two Live[RequestToken] proxies in the same singleton must see the same instance.

        REQUEST scope caches ONE instance per request context.  Two different
        proxies that resolve the same type inside the same request context must
        both receive that single cached instance — they should not create two.
        """

        @Singleton
        class AuthService:
            def __init__(
                self,
                token_a: Live[RequestToken],
                token_b: Live[RequestToken],
            ) -> None:
                self.token_a = token_a
                self.token_b = token_b

        container.register(RequestToken)
        container.register(AuthService)

        with container.scope_context.request():
            svc = container.get(AuthService)
            resolved_a = svc.token_a.get()
            resolved_b = svc.token_b.get()

        # REQUEST scope — one instance per request, shared across all resolutions
        assert resolved_a is resolved_b
        assert RequestToken.instance_count == 1


# ─────────────────────────────────────────────────────────────────
#  Integration — session scope
# ─────────────────────────────────────────────────────────────────


class TestLiveWithSessionScope:
    """Integration tests: @Singleton with Live[SessionScoped] sees fresh instances per session."""

    def setup_method(self) -> None:
        SessionToken.reset()

    def test_proxy_resolves_current_session_instance(
        self, container: DIContainer
    ) -> None:
        """LiveProxy.get() must return the instance for the currently active session."""

        @Singleton
        class UserService:
            def __init__(self, token: Live[SessionToken]) -> None:
                self.token = token

        container.register(SessionToken)
        container.register(UserService)

        with container.scope_context.session("user-abc"):
            svc = container.get(UserService)
            first = svc.token.get()
            second = svc.token.get()

        # Same session — scoped cache returns the same instance both times
        assert first is second

    def test_different_sessions_yield_different_instances(
        self, container: DIContainer
    ) -> None:
        """The same proxy must return different instances for different session IDs."""

        @Singleton
        class UserService:
            def __init__(self, token: Live[SessionToken]) -> None:
                self.token = token

        container.register(SessionToken)
        container.register(UserService)

        with container.scope_context.session("user-abc"):
            svc = container.get(UserService)
            abc_token = svc.token.get()

        with container.scope_context.session("user-xyz"):
            xyz_token = svc.token.get()

        # Different session IDs → different SessionToken instances
        assert abc_token is not xyz_token
        assert SessionToken.instance_count == 2


# ─────────────────────────────────────────────────────────────────
#  Async path
# ─────────────────────────────────────────────────────────────────


class TestLiveProxyAsync:
    """Verifies that LiveProxy.aget() also never caches."""

    async def test_aget_calls_container_every_time(self) -> None:
        """LiveProxy.aget() must call container.aget() on every invocation.

        Mirrors test_proxy_calls_container_every_time for the async path.
        """
        call_count = 0

        class FakeAsyncContainer:
            async def aget(
                self,
                tp: type,
                *,
                qualifier: str | None = None,
                priority: int | None = None,
            ) -> object:
                nonlocal call_count
                call_count += 1
                return RequestToken()

        proxy = LiveProxy(FakeAsyncContainer(), RequestToken)  # type: ignore[arg-type]

        await proxy.aget()
        await proxy.aget()
        await proxy.aget()

        assert call_count == 3

    async def test_aget_different_requests_yield_different_instances(
        self, container: DIContainer
    ) -> None:
        """LiveProxy.aget() must return the current request's instance each time."""
        RequestToken.reset()

        @Singleton
        class AsyncAuthService:
            def __init__(self, token: Live[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(AsyncAuthService)

        async with container.scope_context.arequest():
            svc = await container.aget(AsyncAuthService)
            first_token = await svc.token.aget()

        async with container.scope_context.arequest():
            second_token = await svc.token.aget()

        assert first_token is not second_token


# ─────────────────────────────────────────────────────────────────
#  Validation tests
# ─────────────────────────────────────────────────────────────────


class TestLiveValidation:
    """Verifies that the container enforces Live[T] for REQUEST/SESSION scoped deps."""

    def test_inject_for_request_scoped_raises(self, container: DIContainer) -> None:
        """Inject[T] for a REQUEST-scoped dep inside a @Singleton must raise.

        DESIGN: The container detects this in _check_scope_violation during
        validate_bindings().  The error tells the developer exactly which
        parameter to change and what to change it to.
        """

        @Singleton
        class BadService:
            def __init__(self, token: Inject[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(BadService)

        with pytest.raises(LiveInjectionRequiredError) as exc_info:
            container.get(BadService)

        # Error must identify the offending parameter name
        assert "token" in str(exc_info.value)

    def test_lazy_for_request_scoped_raises(self, container: DIContainer) -> None:
        """Lazy[T] for a REQUEST-scoped dep inside a @Singleton must also raise.

        DESIGN: Lazy[T] caches after the first .get() call — it's equally
        dangerous as Inject[T] for scoped deps.  The validation treats both
        the same: neither is Live[T], so both are rejected.
        """

        @Singleton
        class AlsoBadService:
            def __init__(self, token: Lazy[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(AlsoBadService)

        with pytest.raises(LiveInjectionRequiredError):
            container.get(AlsoBadService)

    def test_inject_for_session_scoped_raises(self, container: DIContainer) -> None:
        """Inject[T] for a SESSION-scoped dep inside a @Singleton must raise."""

        @Singleton
        class BadService:
            def __init__(self, token: Inject[SessionToken]) -> None:
                self.token = token

        container.register(SessionToken)
        container.register(BadService)

        with pytest.raises(LiveInjectionRequiredError):
            container.get(BadService)

    def test_live_for_request_scoped_passes_validation(
        self, container: DIContainer
    ) -> None:
        """Live[T] for a REQUEST-scoped dep must pass validation without error.

        This is the positive counterpart to the error tests above — confirms
        that the correct form is accepted and resolves without raising.
        """

        @Singleton
        class GoodService:
            def __init__(self, token: Live[RequestToken]) -> None:
                self.token = token

        container.register(RequestToken)
        container.register(GoodService)

        # Validation must pass — no exception raised on get()
        with container.scope_context.request():
            svc = container.get(GoodService)

        assert isinstance(svc.token, LiveProxy)

    def test_live_for_session_scoped_passes_validation(
        self, container: DIContainer
    ) -> None:
        """Live[T] for a SESSION-scoped dep must pass validation without error."""

        @Singleton
        class GoodService:
            def __init__(self, token: Live[SessionToken]) -> None:
                self.token = token

        container.register(SessionToken)
        container.register(GoodService)

        with container.scope_context.session("any-session"):
            svc = container.get(GoodService)

        assert isinstance(svc.token, LiveProxy)

    def test_error_message_names_the_offending_parameter(
        self, container: DIContainer
    ) -> None:
        """LiveInjectionRequiredError message must include the parameter name and fix hint.

        DESIGN: The error is actionable — it names the exact constructor parameter,
        its scope, and the exact rename to apply.  This test verifies those key
        strings are present so the developer doesn't have to read a stack trace.
        """

        @Singleton
        class ServiceWithBadDep:
            def __init__(self, jwt: Inject[RequestToken]) -> None:
                self.jwt = jwt

        container.register(RequestToken)
        container.register(ServiceWithBadDep)

        with pytest.raises(LiveInjectionRequiredError) as exc_info:
            container.get(ServiceWithBadDep)

        error_text = str(exc_info.value)
        assert "jwt" in error_text  # parameter name present
        assert "Live[" in error_text  # fix suggestion present
        assert "REQUEST" in error_text  # dep scope present


# ─────────────────────────────────────────────────────────────────
#  Feature 2: Live[T | None] — optional unwrapping inside wrapper
# ─────────────────────────────────────────────────────────────────


class UnboundLiveService:
    """NOT decorated — used to test optional Live resolution of unbound types."""

    pass


class TestLiveOptional:
    """Tests for Live[T | None] and Annotated[T, LiveMeta(optional=True)]."""

    def test_live_t_or_none_returns_none_when_not_bound_sync(
        self, container: DIContainer
    ) -> None:
        """Live[T | None] proxy .get() returns None when T has no binding.

        Args:
            container: Fresh container with NO binding for UnboundLiveService.
        """

        @Singleton
        class Consumer:
            def __init__(self, svc: Live[UnboundLiveService | None]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(Consumer)
        # UnboundLiveService is NOT registered

        consumer = container.get(Consumer)

        assert isinstance(consumer.svc, LiveProxy)
        result = consumer.svc.get()
        assert result is None, "Live[T | None] must return None when T is not bound"

    def test_live_t_or_none_returns_instance_when_bound_sync(
        self, container: DIContainer
    ) -> None:
        """Live[T | None] proxy .get() returns the resolved instance when T is bound.

        Args:
            container: Container with RequestToken registered.
        """

        @Singleton
        class Consumer:
            def __init__(self, svc: Live[RequestToken | None]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(RequestToken)
        container.register(Consumer)

        with container.scope_context.request():
            consumer = container.get(Consumer)
            result = consumer.svc.get()

        assert isinstance(result, RequestToken)

    def test_live_meta_optional_true_returns_none_when_not_bound(
        self, container: DIContainer
    ) -> None:
        """Annotated[T, LiveMeta(optional=True)] must also return None when not bound.

        Args:
            container: Fresh container with no binding for UnboundLiveService.
        """

        @Singleton
        class Consumer:
            def __init__(
                self, svc: Annotated[UnboundLiveService, LiveMeta(optional=True)]
            ) -> None:
                self.svc = svc

        container.register(Consumer)

        consumer = container.get(Consumer)
        result = consumer.svc.get()

        assert result is None, "LiveMeta(optional=True) must return None when not bound"

    async def test_live_t_or_none_returns_none_async_path(
        self, container: DIContainer
    ) -> None:
        """Live[T | None] proxy .aget() returns None when T has no binding (async path).

        Args:
            container: Container with no binding for UnboundLiveService.
        """

        @Singleton
        class Consumer:
            def __init__(self, svc: Live[UnboundLiveService | None]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(Consumer)

        consumer = await container.aget(Consumer)
        result = await consumer.svc.aget()

        assert (
            result is None
        ), "Live[T | None] .aget() must return None when T is not bound"

    async def test_live_meta_optional_true_async_path(
        self, container: DIContainer
    ) -> None:
        """Annotated[T, LiveMeta(optional=True)] .aget() returns None when not bound.

        Args:
            container: Container with no binding for UnboundLiveService.
        """

        @Singleton
        class Consumer:
            def __init__(
                self, svc: Annotated[UnboundLiveService, LiveMeta(optional=True)]
            ) -> None:
                self.svc = svc

        container.register(Consumer)

        consumer = await container.aget(Consumer)
        result = await consumer.svc.aget()

        assert result is None
