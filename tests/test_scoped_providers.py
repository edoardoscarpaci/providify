"""Unit tests for @Provider(scope=Scope.REQUEST) and @Provider(scope=Scope.SESSION).

Mirroring Jakarta CDI's @Produces @RequestScoped pattern — a factory function
whose result is cached for the duration of the active scope, then discarded
when the scope ends.

Covered:
    REQUEST scope:
    - Factory runs exactly once per request block (not per .get() call)
    - Two distinct request blocks call the factory twice — no cross-request caching
    - Provider with injected deps (@Inject[T]) reads them from the active scope
    - Live[T] in a @Singleton using a scoped provider always sees the current value

    SESSION scope:
    - Factory runs exactly once per session ID
    - Same session ID across multiple request blocks reuses the cached value
    - Different session IDs produce different factory invocations

    Backward compatibility:
    - @Provider(singleton=True) still produces SINGLETON scope (no regression)
    - @Provider with no args still produces DEPENDENT scope (no regression)
    - Bare @Provider decorator still works (no regression)

    Async:
    - async def @Provider(scope=Scope.REQUEST) caches correctly per request
    - async def @Provider(scope=Scope.SESSION) caches correctly per session

    Integration — Live[T] + scoped provider:
    - @Singleton using Live[T] of a scoped-provider type always sees current scope's value
"""

from __future__ import annotations


from providify.container import DIContainer
from providify.decorator.scope import (
    Singleton,
)
from providify.decorator.scope import Provider
from providify.metadata import Scope
from providify.type import Inject, Live, LiveProxy


# ─────────────────────────────────────────────────────────────────
#  Domain types — shared across test classes
# ─────────────────────────────────────────────────────────────────


class JWTToken:
    """Plain (undecorated) class — scope comes from the @Provider factory.

    DESIGN: The token itself carries no DI decorator because it is produced
    exclusively via a factory — mirroring how Jakarta's @Produces works.
    The factory decides the scope; the class is scope-agnostic.
    """

    def __init__(self, subject: str) -> None:
        self.subject = subject


class SessionContext:
    """Plain class produced per-session by a @Provider factory."""

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


# ─────────────────────────────────────────────────────────────────
#  REQUEST-scoped provider tests
# ─────────────────────────────────────────────────────────────────


class TestRequestScopedProvider:
    """Verifies @Provider(scope=Scope.REQUEST) caches per request block.

    The factory is a plain function that increments a counter each time it runs.
    If the counter grows by 1 per request block (not per .get() call), the
    scope caching is working correctly.
    """

    def test_factory_runs_once_per_request_not_per_get(
        self, container: DIContainer
    ) -> None:
        """Factory must be called exactly once per request block, regardless of
        how many times the type is resolved within that block.

        DESIGN: This proves caching is active — if the factory ran on every .get()
        call, the counter would grow proportionally to get() calls, not to request
        blocks.
        """
        call_count = 0

        @Provider(scope=Scope.REQUEST)
        def jwt_factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject=f"user-{call_count}")

        container.provide(jwt_factory)

        with container.scope_context.request():
            # Three .get() calls — factory must run only once
            first = container.get(JWTToken)
            second = container.get(JWTToken)
            third = container.get(JWTToken)

        assert call_count == 1
        # All three resolutions must return the same cached instance
        assert first is second is third

    def test_factory_runs_again_for_each_new_request(
        self, container: DIContainer
    ) -> None:
        """A new request block must produce a fresh factory invocation.

        The REQUEST scope cache is cleared when the context manager exits,
        so the next request block starts with an empty cache and the factory
        is called again.
        """
        call_count = 0

        @Provider(scope=Scope.REQUEST)
        def jwt_factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject=f"user-{call_count}")

        container.provide(jwt_factory)

        with container.scope_context.request():
            token_1 = container.get(JWTToken)

        with container.scope_context.request():
            token_2 = container.get(JWTToken)

        # Two request blocks → two factory calls → two distinct instances
        assert call_count == 2
        assert token_1 is not token_2
        assert token_1.subject != token_2.subject

    def test_factory_with_injected_dependency(self, container: DIContainer) -> None:
        """@Provider(scope=Scope.REQUEST) factory can inject other dependencies.

        This mirrors Jakarta's @Produces method injecting @HttpServletRequest —
        the factory receives its own deps from the container and uses them to
        construct the scoped value.
        """

        @Singleton
        class HeaderExtractor:
            """Simulates a component that knows how to read the current request header."""

            def extract_subject(self) -> str:
                # In a real app this would read from the current HTTP request
                return "alice"

        @Provider(scope=Scope.REQUEST)
        def jwt_factory(extractor: Inject[HeaderExtractor]) -> JWTToken:
            # extractor is injected by the container — this is the Jakarta @Produces pattern
            return JWTToken(subject=extractor.extract_subject())

        container.register(HeaderExtractor)
        container.provide(jwt_factory)

        with container.scope_context.request():
            token = container.get(JWTToken)

        assert token.subject == "alice"

    def test_scoped_provider_combined_with_live_injection(
        self, container: DIContainer
    ) -> None:
        """@Singleton using Live[T] of a @Provider(scope=REQUEST) type sees current value.

        This is the complete Jakarta @Produces @RequestScoped pattern:
          - @Provider(scope=Scope.REQUEST) — builds the value once per request
          - Live[JWTToken] in a @Singleton — re-resolves per method call

        Together they guarantee the singleton always sees the current request's
        produced value without any cross-request caching.
        """
        call_count = 0

        @Provider(scope=Scope.REQUEST)
        def jwt_factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject=f"user-{call_count}")

        @Singleton
        class AuthService:
            def __init__(self, token: Live[JWTToken]) -> None:
                # token is a LiveProxy — not the JWTToken itself
                self._token = token

            def current_user(self) -> str:
                # Re-resolves on every call — hits the REQUEST scope cache
                return self._token.get().subject

        container.provide(jwt_factory)
        container.register(AuthService)

        with container.scope_context.request():
            svc = container.get(AuthService)
            # Factory runs once; both calls return the cached request token
            assert svc.current_user() == "user-1"
            assert svc.current_user() == "user-1"

        with container.scope_context.request():
            # New request — factory runs again; singleton is the same but token is new
            assert svc.current_user() == "user-2"

        # Total: one factory call per request block
        assert call_count == 2
        assert isinstance(svc._token, LiveProxy)


# ─────────────────────────────────────────────────────────────────
#  SESSION-scoped provider tests
# ─────────────────────────────────────────────────────────────────


class TestSessionScopedProvider:
    """Verifies @Provider(scope=Scope.SESSION) caches per session ID.

    Session scope is longer-lived than request scope — the cached instance
    survives multiple request blocks for the same session ID, but a
    different session ID triggers a fresh factory call.
    """

    def test_factory_runs_once_per_session(self, container: DIContainer) -> None:
        """Factory must be called once per unique session ID, not per get() call."""
        call_count = 0

        @Provider(scope=Scope.SESSION)
        def session_factory() -> SessionContext:
            nonlocal call_count
            call_count += 1
            return SessionContext(user_id=f"user-{call_count}")

        container.provide(session_factory)

        with container.scope_context.session("session-abc"):
            first = container.get(SessionContext)
            second = container.get(SessionContext)

        assert call_count == 1
        assert first is second

    def test_same_session_id_reuses_cached_value_across_requests(
        self, container: DIContainer
    ) -> None:
        """The same session ID must return the same factory result across request blocks.

        SESSION scope outlives REQUEST scope — re-entering the same session ID
        should return the existing cached instance, not invoke the factory again.
        """
        call_count = 0

        @Provider(scope=Scope.SESSION)
        def session_factory() -> SessionContext:
            nonlocal call_count
            call_count += 1
            return SessionContext(user_id="alice")

        container.provide(session_factory)

        with container.scope_context.session("session-abc"):
            first = container.get(SessionContext)

        # Re-enter the SAME session — cache should survive
        with container.scope_context.session("session-abc"):
            second = container.get(SessionContext)

        assert call_count == 1
        # Same session ID → same instance from the session cache
        assert first is second

    def test_different_session_ids_produce_different_instances(
        self, container: DIContainer
    ) -> None:
        """Different session IDs must each trigger a factory call and own their instance."""
        call_count = 0

        @Provider(scope=Scope.SESSION)
        def session_factory() -> SessionContext:
            nonlocal call_count
            call_count += 1
            return SessionContext(user_id=f"user-{call_count}")

        container.provide(session_factory)

        with container.scope_context.session("alice"):
            alice_ctx = container.get(SessionContext)

        with container.scope_context.session("bob"):
            bob_ctx = container.get(SessionContext)

        assert call_count == 2
        assert alice_ctx is not bob_ctx
        assert alice_ctx.user_id != bob_ctx.user_id


# ─────────────────────────────────────────────────────────────────
#  Backward compatibility
# ─────────────────────────────────────────────────────────────────


class TestProviderBackwardCompatibility:
    """Verifies that existing @Provider usage is unaffected by the scope= parameter.

    DESIGN: scope= is additive — existing code that uses singleton=True or the
    bare @Provider decorator must continue to behave exactly as before.
    """

    def test_bare_provider_still_dependent(self, container: DIContainer) -> None:
        """@Provider with no arguments must still produce DEPENDENT scope (new instance per get)."""
        call_count = 0

        @Provider
        def factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject="anon")

        container.provide(factory)

        first = container.get(JWTToken)
        second = container.get(JWTToken)

        # DEPENDENT — factory runs on every get(), no caching
        assert call_count == 2
        assert first is not second

    def test_singleton_true_still_works(self, container: DIContainer) -> None:
        """@Provider(singleton=True) must still produce SINGLETON scope."""
        call_count = 0

        @Provider(singleton=True)
        def factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject="admin")

        container.provide(factory)

        first = container.get(JWTToken)
        second = container.get(JWTToken)

        # SINGLETON — factory runs once, result cached for container lifetime
        assert call_count == 1
        assert first is second

    def test_explicit_scope_overrides_singleton_flag(
        self, container: DIContainer
    ) -> None:
        """scope= takes priority over singleton= when both are provided.

        DESIGN: Explicit scope is always the most specific signal — it supersedes
        the boolean shorthand.  This priority is documented in ProviderMetadata.
        """
        call_count = 0

        @Provider(singleton=True, scope=Scope.DEPENDENT)
        def factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject="x")

        container.provide(factory)

        first = container.get(JWTToken)
        second = container.get(JWTToken)

        # scope=DEPENDENT wins over singleton=True — new instance every time
        assert call_count == 2
        assert first is not second


# ─────────────────────────────────────────────────────────────────
#  Async providers
# ─────────────────────────────────────────────────────────────────


class TestAsyncScopedProvider:
    """Verifies async def @Provider(scope=Scope.REQUEST/SESSION) works correctly.

    async providers go through ProviderBinding.acreate() → container._call_provider_async().
    The scope caching in _instantiate_async is identical to the sync path, so
    these tests confirm the async branch is wired correctly — not just the sync one.
    """

    async def test_async_request_scoped_provider_caches_per_request(
        self, container: DIContainer
    ) -> None:
        """async @Provider(scope=Scope.REQUEST) factory must run once per request block."""
        call_count = 0

        @Provider(scope=Scope.REQUEST)
        async def async_jwt_factory() -> JWTToken:
            nonlocal call_count
            call_count += 1
            return JWTToken(subject=f"async-user-{call_count}")

        container.provide(async_jwt_factory)

        async with container.scope_context.arequest():
            first = await container.aget(JWTToken)
            second = await container.aget(JWTToken)

        async with container.scope_context.arequest():
            third = await container.aget(JWTToken)

        # Two request blocks → two factory calls
        assert call_count == 2
        # Within the first block — same cached instance
        assert first is second
        # Across blocks — different instances
        assert first is not third

    async def test_async_session_scoped_provider_caches_per_session(
        self, container: DIContainer
    ) -> None:
        """async @Provider(scope=Scope.SESSION) factory must run once per session ID."""
        call_count = 0

        @Provider(scope=Scope.SESSION)
        async def async_session_factory() -> SessionContext:
            nonlocal call_count
            call_count += 1
            return SessionContext(user_id=f"async-user-{call_count}")

        container.provide(async_session_factory)

        async with container.scope_context.asession("s1"):
            first = await container.aget(SessionContext)

        async with container.scope_context.asession("s1"):
            # Same session ID — should reuse cache, NOT call factory again
            second = await container.aget(SessionContext)

        async with container.scope_context.asession("s2"):
            third = await container.aget(SessionContext)

        assert call_count == 2  # s1 once, s2 once
        assert first is second  # same session ID → same instance
        assert first is not third  # different session ID → different instance
