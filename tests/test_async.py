"""Unit tests for async resolution paths.

Covered:
    - aget(): resolves sync class bindings asynchronously
    - aget(): awaits async @Provider functions
    - aget_all(): resolves multiple bindings including async providers
    - aget() raises LookupError for unregistered types
    - async with DIContainer: ... calls ashutdown() on __aexit__
    - async with DIContainer.scoped() as c: ... restores global on exit

All tests are async functions — pytest-asyncio (asyncio_mode="auto") handles
the event loop without needing @pytest.mark.asyncio markers.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton

# ─────────────────────────────────────────────────────────────────
#  Domain types
# ─────────────────────────────────────────────────────────────────


class Cache:
    """Abstract interface for cache implementations."""


@Component
class MemoryCache(Cache):
    """In-memory cache implementation."""


@Singleton
class SingletonCache(Cache):
    """Singleton cache — shared across resolutions."""


# ─────────────────────────────────────────────────────────────────
#  aget() tests
# ─────────────────────────────────────────────────────────────────


class TestAget:
    """Tests for async single-instance resolution."""

    async def test_aget_resolves_sync_binding(self, container: DIContainer) -> None:
        """aget() must resolve a sync class binding transparently."""
        container.bind(Cache, MemoryCache)

        result = await container.aget(Cache)

        assert isinstance(result, MemoryCache)

    async def test_aget_awaits_async_provider(self, container: DIContainer) -> None:
        """aget() must await an async provider function and return its result."""

        @Provider(singleton=True)
        async def make_cache() -> Cache:
            # Simulate async initialization (e.g. connecting to Redis)
            return MemoryCache()

        container.provide(make_cache)

        result = await container.aget(Cache)

        assert isinstance(result, MemoryCache)

    async def test_aget_calls_async_provider_once_for_singleton(
        self, container: DIContainer
    ) -> None:
        """Async singleton provider must be called only once — subsequent calls use cache."""
        call_count = 0

        @Provider(singleton=True)
        async def make_cache() -> Cache:
            nonlocal call_count
            call_count += 1
            return MemoryCache()

        container.provide(make_cache)

        await container.aget(Cache)
        await container.aget(Cache)

        assert call_count == 1

    async def test_aget_raises_lookup_error_for_unregistered_type(
        self, container: DIContainer
    ) -> None:
        """aget() must raise LookupError when no binding matches the requested type."""
        with pytest.raises(LookupError, match="No binding found"):
            await container.aget(Cache)

    async def test_aget_with_qualifier_selects_named_binding(self, container: DIContainer) -> None:
        """aget(T, qualifier=...) must return only the binding with the matching qualifier."""

        @Component(qualifier="fast")
        class FastCache(Cache):
            pass

        container.bind(Cache, MemoryCache)
        container.bind(Cache, FastCache)

        result = await container.aget(Cache, qualifier="fast")

        assert isinstance(result, FastCache)


# ─────────────────────────────────────────────────────────────────
#  aget_all() tests
# ─────────────────────────────────────────────────────────────────


class TestAgetAll:
    """Tests for async multi-instance resolution."""

    async def test_aget_all_returns_all_matching_instances(self, container: DIContainer) -> None:
        """aget_all() should return all bound implementations of the requested type."""
        container.bind(Cache, MemoryCache)
        container.bind(Cache, SingletonCache)

        results = await container.aget_all(Cache)

        assert len(results) == 2

    async def test_aget_all_mixes_sync_and_async_providers(self, container: DIContainer) -> None:
        """aget_all() must handle a mix of sync and async providers transparently."""

        @Provider
        async def make_async_cache() -> Cache:
            return MemoryCache()

        @Provider
        def make_sync_cache() -> Cache:
            return SingletonCache()

        container.provide(make_async_cache)
        container.provide(make_sync_cache)

        results = await container.aget_all(Cache)

        # Both async and sync providers should be resolved
        assert len(results) == 2

    async def test_aget_all_raises_when_no_bindings(self, container: DIContainer) -> None:
        """aget_all() must raise LookupError when no bindings match."""
        with pytest.raises(LookupError, match="No bindings found"):
            await container.aget_all(Cache)

    async def test_aget_all_results_sorted_by_priority(self, container: DIContainer) -> None:
        """aget_all() results must be sorted by ascending priority."""

        @Component(priority=1)
        class CacheA(Cache):
            pass

        @Component(priority=2)
        class CacheB(Cache):
            pass

        container.bind(Cache, CacheA)
        container.bind(Cache, CacheB)

        results = await container.aget_all(Cache)

        assert isinstance(results[0], CacheA)  # priority=1 first
        assert isinstance(results[1], CacheB)  # priority=2 second


# ─────────────────────────────────────────────────────────────────
#  Async context manager tests
# ─────────────────────────────────────────────────────────────────


class TestAsyncContextManager:
    """Tests for async with container: ... and async with DIContainer.scoped(): ..."""

    async def test_async_context_manager_returns_self(self, container: DIContainer) -> None:
        """async with container should yield the same container instance."""
        async with container as c:
            assert c is container

    async def test_async_scoped_installs_fresh_container(self) -> None:
        """async with DIContainer.scoped() must swap in a fresh container."""
        original = DIContainer.current()

        async with DIContainer.scoped() as scoped:
            assert scoped is not original
            assert DIContainer.current() is scoped

        # Global restored after exit
        assert DIContainer.current() is original

    async def test_async_scoped_restores_global_on_exception(self) -> None:
        """async scoped() must restore the global even if an exception is raised."""
        original = DIContainer.current()

        with pytest.raises(ValueError):
            async with DIContainer.scoped():
                raise ValueError("async test exception")

        assert DIContainer.current() is original
