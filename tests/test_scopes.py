"""Unit tests for scope behaviors: SINGLETON, DEPENDENT, REQUEST, SESSION.

Also covers scope-violation detection (SINGLETON holding REQUEST-scoped dep)
which fires at validate_bindings() time on the first get() call.

Covered:
    - SINGLETON: same instance returned every time
    - DEPENDENT: new instance on every resolution
    - REQUEST: one instance per active request context, new instance across contexts
    - SESSION: one instance per active session, shared within the same session
    - Scope isolation: two concurrent request contexts get different instances
    - Scope violation: SINGLETON depending on REQUEST raises ScopeViolationDetectedError
    - Resolving @RequestScoped outside a request context raises RuntimeError
    - Scope violation via class-level annotation (var: Inject[T]) is also detected
    - Live[T] class-level annotation does NOT trigger scope violation
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component, RequestScoped, SessionScoped, Singleton
from providify.exceptions import LiveInjectionRequiredError
from providify.type import Inject, Live


# ─────────────────────────────────────────────────────────────────
#  Domain types
# ─────────────────────────────────────────────────────────────────


@Component
class DependentService:
    """DEPENDENT scope — new instance on every resolution."""


@Singleton
class SingletonService:
    """SINGLETON scope — shared instance for the lifetime of the container."""


@RequestScoped
class RequestService:
    """REQUEST scope — one instance per active request context."""


@SessionScoped
class SessionService:
    """SESSION scope — one instance per session context."""


# ─────────────────────────────────────────────────────────────────
#  Singleton scope tests
# ─────────────────────────────────────────────────────────────────


class TestSingletonScope:
    """Verifies that @Singleton returns the same cached instance."""

    def test_same_instance_on_repeated_get(self, container: DIContainer) -> None:
        """Two calls to get() for the same @Singleton must return the identical object."""
        container.register(SingletonService)

        first = container.get(SingletonService)
        second = container.get(SingletonService)

        # Must be the exact same object, not just equal
        assert first is second

    def test_singleton_provider_caches_result(self, container: DIContainer) -> None:
        """@Provider(singleton=True) must cache its return value across calls."""
        from providify.decorator.scope import Provider

        call_count = 0

        @Provider(singleton=True)
        def make_singleton() -> SingletonService:
            nonlocal call_count
            call_count += 1
            return SingletonService()

        container.provide(make_singleton)

        container.get(SingletonService)
        container.get(SingletonService)

        # Provider must have been called exactly once — second call hits the cache
        assert call_count == 1


# ─────────────────────────────────────────────────────────────────
#  DEPENDENT scope tests
# ─────────────────────────────────────────────────────────────────


class TestDependentScope:
    """Verifies that @Component (DEPENDENT) creates a new instance each time."""

    def test_different_instances_on_repeated_get(self, container: DIContainer) -> None:
        """Two calls to get() for the same @Component must return different objects."""
        container.register(DependentService)

        first = container.get(DependentService)
        second = container.get(DependentService)

        # Must be distinct objects — no caching for DEPENDENT scope
        assert first is not second

    def test_dependent_provider_calls_factory_each_time(
        self, container: DIContainer
    ) -> None:
        """@Provider(singleton=False) must invoke the factory on every resolution."""
        from providify.decorator.scope import Provider

        call_count = 0

        @Provider
        def make_service() -> DependentService:
            nonlocal call_count
            call_count += 1
            return DependentService()

        container.provide(make_service)

        container.get(DependentService)
        container.get(DependentService)

        assert call_count == 2


# ─────────────────────────────────────────────────────────────────
#  REQUEST scope tests
# ─────────────────────────────────────────────────────────────────


class TestRequestScope:
    """Verifies @RequestScoped caching within a request context."""

    def test_same_instance_within_request(self, container: DIContainer) -> None:
        """Two get() calls inside the same request context should return the same instance."""
        container.register(RequestService)

        with container.scope_context.request():
            first = container.get(RequestService)
            second = container.get(RequestService)

        assert first is second

    def test_different_instances_across_requests(self, container: DIContainer) -> None:
        """Each request context must have its own isolated instance."""
        container.register(RequestService)

        with container.scope_context.request():
            first = container.get(RequestService)

        with container.scope_context.request():
            second = container.get(RequestService)

        assert first is not second

    def test_raises_outside_request_context(self, container: DIContainer) -> None:
        """Resolving @RequestScoped outside a request context must raise RuntimeError."""
        container.register(RequestService)

        with pytest.raises(RuntimeError, match="outside of an active request context"):
            container.get(RequestService)


# ─────────────────────────────────────────────────────────────────
#  SESSION scope tests
# ─────────────────────────────────────────────────────────────────


class TestSessionScope:
    """Verifies @SessionScoped caching within a named session."""

    def test_same_instance_within_session(self, container: DIContainer) -> None:
        """Two get() calls in the same session context should return the same instance."""
        container.register(SessionService)

        with container.scope_context.session("user-1"):
            first = container.get(SessionService)
            second = container.get(SessionService)

        assert first is second

    def test_different_instances_across_sessions(self, container: DIContainer) -> None:
        """Different session IDs must produce different cached instances."""
        container.register(SessionService)

        with container.scope_context.session("user-1"):
            first = container.get(SessionService)

        with container.scope_context.session("user-2"):
            second = container.get(SessionService)

        assert first is not second

    def test_same_session_id_reuses_instance(self, container: DIContainer) -> None:
        """Re-entering the same session ID must return the same cached instance."""
        container.register(SessionService)

        with container.scope_context.session("user-abc"):
            first = container.get(SessionService)

        # Same session ID — cache is preserved between contexts
        with container.scope_context.session("user-abc"):
            second = container.get(SessionService)

        assert first is second


# ─────────────────────────────────────────────────────────────────
#  Scope violation tests
# ─────────────────────────────────────────────────────────────────


class TestScopeViolation:
    """Verifies that scope leaks (SINGLETON depending on REQUEST) are detected."""

    def test_singleton_depending_on_request_scoped_raises(
        self, container: DIContainer
    ) -> None:
        """A SINGLETON that holds a REQUEST-scoped dep without Live[T] raises LiveInjectionRequiredError.

        DESIGN: Using Inject[T] or a bare type annotation for a REQUEST-scoped dep
        inside a SINGLETON captures one instance at construction time — that instance
        becomes stale across request boundaries. The container detects this during
        validate_bindings() and requires the developer to use Live[T] instead.
        """

        @Singleton
        class BadSingleton:
            def __init__(self, svc: RequestService) -> None:
                self.svc = svc

        container.register(RequestService)
        container.register(BadSingleton)

        # First get() triggers validate_bindings() which calls ClassBinding.validate().
        # LiveInjectionRequiredError is raised because Inject[T]/bare type is unsafe
        # for REQUEST-scoped deps in longer-lived components.
        with pytest.raises(LiveInjectionRequiredError):
            container.get(BadSingleton)


# ─────────────────────────────────────────────────────────────────
#  Async REQUEST scope tests
# ─────────────────────────────────────────────────────────────────


class TestAsyncRequestScope:
    """Verifies arequest() async context manager isolates REQUEST-scoped instances."""

    async def test_same_instance_within_async_request(
        self, container: DIContainer
    ) -> None:
        """Two aget() calls inside the same arequest() context must return the same instance."""
        container.register(RequestService)

        async with container.scope_context.arequest():
            first = await container.aget(RequestService)
            second = await container.aget(RequestService)

        assert first is second

    async def test_different_instances_across_async_requests(
        self, container: DIContainer
    ) -> None:
        """Each arequest() context must produce a fresh, isolated instance."""
        container.register(RequestService)

        async with container.scope_context.arequest():
            first = await container.aget(RequestService)

        async with container.scope_context.arequest():
            second = await container.aget(RequestService)

        assert first is not second

    async def test_async_request_cache_cleaned_up_on_exit(
        self, container: DIContainer
    ) -> None:
        """arequest() must remove its cache entry from _request_caches after the block.

        DESIGN: Each request context creates a new UUID keyed entry in
        _request_caches. On exit, that entry must be removed to prevent
        unbounded memory growth in long-running servers.
        """
        async with container.scope_context.arequest() as request_id:
            assert request_id in container.scope_context._request_caches

        # Cache entry must be gone after the context exits
        assert request_id not in container.scope_context._request_caches

    async def test_nested_async_requests_are_isolated(
        self, container: DIContainer
    ) -> None:
        """Nested arequest() contexts must each get their own independent cache.

        DESIGN: ContextVar.set() returns a Token used to restore the previous
        value on reset(). Nested contexts stack correctly — the inner context
        does not overwrite the outer one after exit.
        """
        container.register(RequestService)

        async with container.scope_context.arequest():
            outer = await container.aget(RequestService)

            async with container.scope_context.arequest():
                inner = await container.aget(RequestService)

            # Back in outer context — must still see the outer instance
            outer_again = await container.aget(RequestService)

        # Inner and outer are isolated — different instances
        assert inner is not outer
        # Outer context is still active while nested, then restored
        assert outer_again is outer


# ─────────────────────────────────────────────────────────────────
#  Async SESSION scope tests
# ─────────────────────────────────────────────────────────────────


class TestAsyncSessionScope:
    """Verifies asession() async context manager isolates SESSION-scoped instances."""

    async def test_same_instance_within_async_session(
        self, container: DIContainer
    ) -> None:
        """Two aget() calls in the same asession() context must return the same instance."""
        container.register(SessionService)

        async with container.scope_context.asession("async-user-1"):
            first = await container.aget(SessionService)
            second = await container.aget(SessionService)

        assert first is second

    async def test_different_instances_across_async_sessions(
        self, container: DIContainer
    ) -> None:
        """Different session IDs in asession() must produce different instances."""
        container.register(SessionService)

        async with container.scope_context.asession("async-user-1"):
            first = await container.aget(SessionService)

        async with container.scope_context.asession("async-user-2"):
            second = await container.aget(SessionService)

        assert first is not second

    async def test_same_session_id_reuses_cache_across_async_contexts(
        self, container: DIContainer
    ) -> None:
        """Re-entering the same session ID via asession() must reuse the cached instance."""
        container.register(SessionService)

        async with container.scope_context.asession("persistent-session"):
            first = await container.aget(SessionService)

        async with container.scope_context.asession("persistent-session"):
            second = await container.aget(SessionService)

        assert first is second


# ─────────────────────────────────────────────────────────────────
#  ScopeContext.invalidate_session() tests
# ─────────────────────────────────────────────────────────────────


class TestInvalidateSession:
    """Verifies that invalidate_session() clears the named session cache."""

    def test_invalidate_session_removes_cached_instance(
        self, container: DIContainer
    ) -> None:
        """After invalidate_session(), the next get() in that session must create a fresh instance.

        DESIGN: This is the server-side logout pattern — destroy the session
        cache so stale instances (e.g. UserProfile) are not served after logout.
        """
        container.register(SessionService)

        with container.scope_context.session("logout-user"):
            first = container.get(SessionService)

        # Invalidate the session cache
        container.scope_context.invalidate_session("logout-user")

        with container.scope_context.session("logout-user"):
            second = container.get(SessionService)

        # Cache was cleared — second is a fresh instance
        assert first is not second

    def test_invalidate_nonexistent_session_is_safe(
        self, container: DIContainer
    ) -> None:
        """invalidate_session() on an unknown session ID must not raise."""
        # Should not raise — idempotent / defensive behaviour
        container.scope_context.invalidate_session("no-such-session-id")


# ─────────────────────────────────────────────────────────────────
#  Scope violation via class-level annotations
# ─────────────────────────────────────────────────────────────────


class TestClassVarScopeViolation:
    """Verifies that scope-leak detection covers class-level Inject[T] annotations.

    Before this feature, only ``__init__`` parameters were validated.  A SINGLETON
    could silently hold a stale REQUEST-scoped dep via a class-level annotation
    and no error would be raised at validate_bindings() time.
    """

    def test_classvar_inject_request_scoped_in_singleton_raises(
        self, container: DIContainer
    ) -> None:
        """A SINGLETON with a class-level Inject[RequestScoped] must raise LiveInjectionRequiredError.

        The class-level annotation ``svc: Inject[RequestService]`` is semantically
        identical to the constructor pattern ``def __init__(self, svc: Inject[...])``:
        both capture a single instance at construction time, which becomes stale
        across request boundaries.  validate_bindings() must catch both.
        """

        @Singleton
        class BadSingleton:
            # Class-level injection — NOT safe for REQUEST-scoped deps in a SINGLETON.
            # The container must detect this the same way it detects the __init__ form.
            svc: Inject[RequestService]  # type: ignore[valid-type]

        container.register(RequestService)
        container.register(BadSingleton)

        # First get() triggers validate_bindings() — must raise, not silently inject.
        with pytest.raises(LiveInjectionRequiredError):
            container.get(BadSingleton)

    def test_classvar_inject_session_scoped_in_singleton_raises(
        self, container: DIContainer
    ) -> None:
        """A SINGLETON with a class-level Inject[SessionScoped] must also raise."""

        @Singleton
        class BadSingleton:
            svc: Inject[SessionService]  # type: ignore[valid-type]

        container.register(SessionService)
        container.register(BadSingleton)

        with pytest.raises(LiveInjectionRequiredError):
            container.get(BadSingleton)

    def test_classvar_live_request_scoped_in_singleton_is_safe(
        self, container: DIContainer
    ) -> None:
        """A SINGLETON with a class-level Live[RequestScoped] must NOT raise.

        Live[T] re-resolves on every .get() call — it never captures a stale instance.
        The validator must distinguish Inject[T] (unsafe) from Live[T] (safe).
        """

        @Singleton
        class GoodSingleton:
            # Live[T] is the correct pattern — proxy re-resolves per request.
            svc: Live[RequestService]  # type: ignore[valid-type]

        container.register(RequestService)
        container.register(GoodSingleton)

        # Should NOT raise — Live[T] is explicitly allowed for scoped deps.
        with container.scope_context.request():
            instance = container.get(GoodSingleton)
        assert isinstance(instance, GoodSingleton)

    def test_mixed_init_and_classvar_violations_both_reported(
        self, container: DIContainer
    ) -> None:
        """Both __init__-param and class-var violations must be caught in one error.

        LiveInjectionRequiredError aggregates all violations so the developer
        sees everything at once — not just the first bad parameter.
        """

        @Singleton
        class DoublyBad:
            # Class-level violation
            class_svc: Inject[RequestService]  # type: ignore[valid-type]

            # Constructor-level violation (same pattern, both should appear)
            def __init__(self, init_svc: RequestService) -> None:
                self.init_svc = init_svc

        container.register(RequestService)
        container.register(DoublyBad)

        with pytest.raises(LiveInjectionRequiredError) as exc_info:
            container.get(DoublyBad)

        # Both violations must be captured — not just the first one encountered.
        assert len(exc_info.value.violations) == 2


# ─────────────────────────────────────────────────────────────────
#  invalidate_session() / ainvalidate_session() @PreDestroy tests
# ─────────────────────────────────────────────────────────────────


class TestInvalidateSessionPreDestroy:
    """Tests that invalidate_session() and ainvalidate_session() run @PreDestroy hooks."""

    def test_invalidate_session_runs_sync_pre_destroy(
        self, container: DIContainer
    ) -> None:
        """invalidate_session() must call @PreDestroy on session-scoped instances."""
        destroyed: list[str] = []

        from providify.decorator.lifecycle import PreDestroy

        @SessionScoped
        class SessionResource:
            @PreDestroy
            def teardown(self) -> None:
                destroyed.append("destroyed")

        container.register(SessionResource)

        with container.session("test-session") as _:
            container.get(SessionResource)

        # After the session() context exits, @PreDestroy already fired.
        # Reset so we can test invalidate_session() independently.
        destroyed.clear()

        # Re-enter the session to put a new instance in the cache
        with container.session("test-session2"):
            resource = container.get(SessionResource)
            assert isinstance(resource, SessionResource)

        # Reset again — context manager exit ran @PreDestroy
        destroyed.clear()

        # Now use invalidate_session() to destroy a fresh session
        with container.session("user-logout"):
            container.get(SessionResource)

        # Above context manager already ran @PreDestroy.  Use invalidate_session()
        # directly on a session that was created but not yet invalidated via context.
        # Create a session cache without a context manager by manually registering:
        container.scope_context._session_caches["manual-sid"] = {}
        container.scope_context._session_id.set("manual-sid")
        container.get(SessionResource)
        container.scope_context._session_id.set(None)  # type: ignore[arg-type]

        destroyed.clear()
        container.invalidate_session("manual-sid")

        assert "destroyed" in destroyed

    def test_invalidate_session_noop_for_unknown_session(
        self, container: DIContainer
    ) -> None:
        """invalidate_session() on an unknown session_id must not raise."""
        # Should not raise even for completely unknown IDs
        container.invalidate_session("nonexistent-session-id")

    async def test_ainvalidate_session_runs_async_pre_destroy(
        self, container: DIContainer
    ) -> None:
        """ainvalidate_session() must await async @PreDestroy on session-scoped instances."""
        destroyed: list[str] = []

        from providify.decorator.lifecycle import PreDestroy

        @SessionScoped
        class AsyncSessionResource:
            @PreDestroy
            async def teardown(self) -> None:
                destroyed.append("async-destroyed")

        container.register(AsyncSessionResource)

        # Create session cache manually
        container.scope_context._session_caches["async-sid"] = {}
        container.scope_context._session_id.set("async-sid")
        await container.aget(AsyncSessionResource)
        container.scope_context._session_id.set(None)  # type: ignore[arg-type]

        await container.ainvalidate_session("async-sid")

        assert "async-destroyed" in destroyed

    async def test_ainvalidate_session_noop_for_unknown_session(
        self, container: DIContainer
    ) -> None:
        """ainvalidate_session() on an unknown session_id must not raise."""
        await container.ainvalidate_session("nonexistent")
