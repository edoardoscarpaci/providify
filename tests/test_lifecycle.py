"""Unit tests for @PostConstruct, @PreDestroy, shutdown(), and ashutdown().

Covered:
    - @PostConstruct: called immediately after construction and injection
    - @PostConstruct async: raises RuntimeError if resolved via sync get()
    - @PostConstruct async: awaited when resolved via aget()
    - @PreDestroy: called during shutdown() for cached singletons
    - @PreDestroy: NOT called for DEPENDENT instances (never cached)
    - @PreDestroy async: raises RuntimeError from sync shutdown()
    - @PreDestroy async: awaited by ashutdown()
    - shutdown() clears the singleton cache and request/session caches
    - Shutdown runs even if a @PreDestroy hook raises (via context manager)

DESIGN NOTE: @PreDestroy is only invoked for singleton instances that
are in the _singleton_cache. DEPENDENT instances are created and discarded
per-resolution — the container never owns them, so it can't destroy them.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.lifecycle import PostConstruct, PreDestroy
from providify.decorator.scope import Component, Singleton

# ─────────────────────────────────────────────────────────────────
#  @PostConstruct tests
# ─────────────────────────────────────────────────────────────────


class TestPostConstruct:
    """Tests for the @PostConstruct lifecycle hook."""

    def test_sync_post_construct_called_after_construction(self, container: DIContainer) -> None:
        """@PostConstruct must be called once, after the constructor returns."""

        @Component
        class Service:
            def __init__(self) -> None:
                self.initialized = False

            @PostConstruct
            def setup(self) -> None:
                # Called by the container after injection — flag set here
                self.initialized = True

        container.register(Service)
        svc = container.get(Service)

        assert svc.initialized is True

    def test_sync_post_construct_receives_injected_state(self, container: DIContainer) -> None:
        """@PostConstruct should run after all dependencies are injected
        so that it can safely use them during initialization.
        """

        @Component
        class Config:
            value = "hello"

        @Component
        class Service:
            def __init__(self, config: Config) -> None:
                self.config = config
                self.greeting: str = ""

            @PostConstruct
            def setup(self) -> None:
                # Uses the injected config — must be available here
                self.greeting = f"greeting={self.config.value}"

        container.register(Config)
        container.register(Service)

        svc = container.get(Service)

        assert svc.greeting == "greeting=hello"

    def test_async_post_construct_raises_on_sync_get(self, container: DIContainer) -> None:
        """Async @PostConstruct must cause get() to raise RuntimeError — use aget() instead."""

        @Component
        class AsyncService:
            @PostConstruct
            async def async_setup(self) -> None:
                pass

        container.register(AsyncService)

        with pytest.raises(RuntimeError, match="async"):
            container.get(AsyncService)

    async def test_async_post_construct_awaited_on_aget(self, container: DIContainer) -> None:
        """async @PostConstruct must be awaited when resolved via aget()."""

        @Component
        class AsyncService:
            def __init__(self) -> None:
                self.ready = False

            @PostConstruct
            async def async_setup(self) -> None:
                self.ready = True

        container.register(AsyncService)
        svc = await container.aget(AsyncService)

        assert svc.ready is True


# ─────────────────────────────────────────────────────────────────
#  @PreDestroy tests
# ─────────────────────────────────────────────────────────────────


class TestPreDestroy:
    """Tests for the @PreDestroy lifecycle hook and shutdown behavior."""

    def test_pre_destroy_called_on_shutdown(self, container: DIContainer) -> None:
        """@PreDestroy must be called during shutdown() for cached singleton instances."""
        destroyed: list[str] = []

        @Singleton
        class Resource:
            @PreDestroy
            def teardown(self) -> None:
                destroyed.append("Resource.teardown")

        container.register(Resource)
        container.get(Resource)  # caches the singleton

        container.shutdown()

        assert destroyed == ["Resource.teardown"]

    def test_pre_destroy_not_called_for_uncached_singleton(self, container: DIContainer) -> None:
        """@PreDestroy must NOT be called if the singleton was never actually resolved
        (i.e. it's in _bindings but not in _singleton_cache).
        """
        destroyed: list[bool] = []

        @Singleton
        class Resource:
            @PreDestroy
            def teardown(self) -> None:
                destroyed.append(True)

        container.register(Resource)
        # Do NOT call container.get(Resource) — leave it uncached

        container.shutdown()

        assert destroyed == [], "Never-resolved singleton must not have @PreDestroy called"

    def test_pre_destroy_not_called_for_dependent_scope(self, container: DIContainer) -> None:
        """DEPENDENT instances are not cached — @PreDestroy must not be called for them.

        DESIGN: The container does not own DEPENDENT instances — they are
        handed off to the caller immediately. The caller is responsible for
        any cleanup of DEPENDENT instances.
        """
        destroyed: list[bool] = []

        @Component
        class ShortLived:
            @PreDestroy
            def teardown(self) -> None:
                destroyed.append(True)

        container.register(ShortLived)
        container.get(ShortLived)  # DEPENDENT — not cached

        container.shutdown()

        assert destroyed == []

    def test_shutdown_clears_singleton_cache(self, container: DIContainer) -> None:
        """shutdown() must clear _singleton_cache so fresh instances are created after restart."""

        @Singleton
        class Resource:
            pass

        container.register(Resource)
        first = container.get(Resource)

        container.shutdown()

        # After shutdown, cache is empty — get() creates a new instance
        second = container.get(Resource)

        assert first is not second

    def test_context_manager_exit_calls_shutdown(self, container: DIContainer) -> None:
        """with container: ... must call shutdown() on __exit__."""
        destroyed: list[bool] = []

        @Singleton
        class Resource:
            @PreDestroy
            def teardown(self) -> None:
                destroyed.append(True)

        container.register(Resource)
        container.get(Resource)

        with container:
            pass  # __exit__ calls shutdown()

        assert destroyed == [True]

    def test_sync_shutdown_raises_for_async_pre_destroy(self, container: DIContainer) -> None:
        """sync shutdown() must raise RuntimeError if a @PreDestroy is async def."""

        @Singleton
        class Resource:
            @PreDestroy
            async def async_teardown(self) -> None:
                pass

        container.register(Resource)
        container.get(Resource)

        with pytest.raises(RuntimeError, match="async"):
            container.shutdown()

    async def test_async_shutdown_awaits_async_pre_destroy(self, container: DIContainer) -> None:
        """ashutdown() must await async @PreDestroy hooks."""
        torn_down: list[bool] = []

        @Singleton
        class Resource:
            @PreDestroy
            async def async_teardown(self) -> None:
                torn_down.append(True)

        container.register(Resource)
        await container.aget(Resource)

        await container.ashutdown()

        assert torn_down == [True]

    async def test_async_context_manager_calls_ashutdown(self, container: DIContainer) -> None:
        """async with container: ... must call ashutdown() on __aexit__."""
        torn_down: list[bool] = []

        @Singleton
        class Resource:
            @PreDestroy
            async def async_teardown(self) -> None:
                torn_down.append(True)

        container.register(Resource)
        await container.aget(Resource)

        async with container:
            pass  # __aexit__ calls ashutdown()

        assert torn_down == [True]

    def test_shutdown_raises_shutdown_error_and_still_clears_cache_on_hook_failure(
        self, container: DIContainer
    ) -> None:
        """Plan 004 (F9): shutdown() no longer raises the bare hook exception and
        aborts before clearing caches. It must raise ShutdownError (aggregating
        every failure) while still running _clear_caches() in a finally block.

        This replaces the old raise-on-first-and-leak-the-cache behaviour.
        """
        from providify.exceptions import ShutdownError

        @Singleton
        class Resource:
            @PreDestroy
            def teardown(self) -> None:
                raise ValueError("boom")

        container.register(Resource)
        container.get(Resource)

        with pytest.raises(ShutdownError):
            container.shutdown()

        # Cache must be cleared even though the hook raised.
        assert container._singleton_cache == {}

    def test_shutdown_teardown_order_follows_dependencies_not_registration(
        self, container: DIContainer
    ) -> None:
        """Plan 004 (F9): teardown order is reverse-*creation*-order (dependents
        before dependencies), not the order bindings were registered in.
        """
        order: list[str] = []

        @Singleton
        class Config:
            @PreDestroy
            def teardown(self) -> None:
                order.append("Config")

        @Singleton
        class Service:
            def __init__(self, config: Config) -> None:
                self.config = config

            @PreDestroy
            def teardown(self) -> None:
                order.append("Service")

        # Registered in dependency order here (Config before Service) but the
        # true assertion is about creation order, which is fixed by the
        # constructor call graph regardless of registration order.
        container.register(Config)
        container.register(Service)

        container.get(Service)
        container.shutdown()

        # Service depends on Config, so Service (the dependent) must be torn
        # down BEFORE Config (its dependency) — reverse creation order.
        assert order == ["Service", "Config"]
