"""Unit tests for Lazy[T] deferred injection and LazyProxy.

Covered:
    - Lazy[T] in a constructor parameter creates a LazyProxy (not the dep itself)
    - LazyProxy.get() resolves the dep on first call
    - LazyProxy.get() returns the cached value on subsequent calls (no re-resolution)
    - _resolved sentinel — correctly handles None as a valid resolved value
    - Lazy[T] breaks a circular dependency cycle (A → B → A)
    - LazyProxy.__repr__ reflects resolved/unresolved state
    - Lazy(T, qualifier=...) forwards qualifier to container.get()
    - LazyProxy.aget() async resolution path
    - Lazy[T | None] returns None when T is not bound (optional unwrapping)
    - Annotated[T, LazyMeta(optional=True)] also works
    - Both sync and async paths for optional unwrapping
"""

from __future__ import annotations

from typing import Annotated

from providify.container import DIContainer
from providify.decorator.scope import Component, Singleton
from providify.type import Lazy, LazyMeta, LazyProxy


# ─────────────────────────────────────────────────────────────────
#  Domain types
# ─────────────────────────────────────────────────────────────────


@Singleton
class ExpensiveService:
    """Singleton used to verify LazyProxy caches the resolved instance."""

    instance_count: int = 0

    def __init__(self) -> None:
        ExpensiveService.instance_count += 1

    @classmethod
    def reset(cls) -> None:
        cls.instance_count = 0


# ─────────────────────────────────────────────────────────────────
#  LazyProxy unit tests (direct construction, no container)
# ─────────────────────────────────────────────────────────────────


class TestLazyProxyUnit:
    """Unit tests for LazyProxy in isolation — uses a mock container."""

    def _make_proxy(self, resolved_value: object) -> LazyProxy:
        """Helper — creates a LazyProxy backed by a fake container."""

        class FakeContainer:
            def get(self, tp: type, *, qualifier=None, priority=None) -> object:
                return resolved_value

        # type: ignore — FakeContainer satisfies the Any typed _container field
        return LazyProxy(FakeContainer(), ExpensiveService)  # type: ignore[arg-type]

    def test_unresolved_proxy_repr(self) -> None:
        """__repr__ should say 'unresolved' before .get() is called."""
        proxy = self._make_proxy(ExpensiveService())

        assert "unresolved" in repr(proxy)

    def test_resolved_proxy_repr(self) -> None:
        """__repr__ should say 'resolved=...' after .get() is called."""
        proxy = self._make_proxy(ExpensiveService())
        proxy.get()

        assert "resolved=" in repr(proxy)

    def test_get_resolves_on_first_call(self) -> None:
        """proxy.get() should return the value from the backing container."""
        instance = ExpensiveService()
        proxy = self._make_proxy(instance)

        result = proxy.get()

        assert result is instance

    def test_get_caches_after_first_call(self) -> None:
        """proxy.get() must return the same object on subsequent calls without
        re-calling the container — the container is called exactly once.
        """
        call_count = 0
        instance = ExpensiveService()

        class CountingContainer:
            def get(self, tp: type, *, qualifier=None, priority=None) -> object:
                nonlocal call_count
                call_count += 1
                return instance

        proxy: LazyProxy = LazyProxy(CountingContainer(), ExpensiveService)  # type: ignore[arg-type]

        proxy.get()
        proxy.get()
        proxy.get()

        assert call_count == 1

    def test_resolved_sentinel_handles_none_as_valid_value(self) -> None:
        """_resolved sentinel must be used — not `_instance is None`.

        When the backing container returns None (e.g. optional injection),
        the proxy must NOT re-call the container on the second get().
        """
        call_count = 0

        class NoneReturningContainer:
            def get(self, tp: type, *, qualifier=None, priority=None) -> None:
                nonlocal call_count
                call_count += 1
                return None  # valid resolved value

        proxy: LazyProxy = LazyProxy(NoneReturningContainer(), ExpensiveService)  # type: ignore[arg-type]

        result1 = proxy.get()
        result2 = proxy.get()

        assert result1 is None
        assert result2 is None
        assert (
            call_count == 1
        ), "Container must be called only once even when result is None"


# ─────────────────────────────────────────────────────────────────
#  Lazy[T] end-to-end tests through the container
# ─────────────────────────────────────────────────────────────────


class TestLazyInjection:
    """End-to-end tests for Lazy[T] as a constructor parameter."""

    def setup_method(self) -> None:
        """Reset the instance counter before each test."""
        ExpensiveService.reset()

    def test_lazy_parameter_receives_proxy_not_instance(
        self, container: DIContainer
    ) -> None:
        """Constructor with Lazy[T] must receive a LazyProxy, not the resolved type."""

        @Component
        class Consumer:
            def __init__(self, svc: Lazy[ExpensiveService]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(ExpensiveService)
        container.register(Consumer)

        consumer = container.get(Consumer)

        # Must be a proxy — the service is NOT yet instantiated
        assert isinstance(consumer.svc, LazyProxy)
        assert (
            ExpensiveService.instance_count == 0
        ), "Service must not be created until .get() is called"

    def test_proxy_resolves_on_get(self, container: DIContainer) -> None:
        """Calling .get() on the proxy must return the actual resolved service."""

        @Component
        class Consumer:
            def __init__(self, svc: Lazy[ExpensiveService]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(ExpensiveService)
        container.register(Consumer)

        consumer = container.get(Consumer)
        resolved = consumer.svc.get()

        assert isinstance(resolved, ExpensiveService)

    def test_lazy_breaks_circular_dependency(self, container: DIContainer) -> None:
        """Lazy[T] must allow A → B → A cycles without raising CircularDependencyError.

        DESIGN: The circular dep is broken because LazyProxy is created
        eagerly (no resolution), so both constructors can return before
        either circular dep is actually resolved.
        """

        @Singleton
        class A:
            def __init__(self, b: Lazy["B"]) -> None:  # type: ignore[valid-type]
                self.b = b

        @Singleton
        class B:
            def __init__(self, a: A) -> None:
                self.a = a

        container.register(A)
        container.register(B)

        # Should NOT raise — Lazy[B] defers B's resolution past A's construction
        a_instance = container.get(A)
        assert isinstance(a_instance, A)

        # And B is resolvable normally
        b_instance = container.get(B)
        assert isinstance(b_instance, B)

    def test_lazy_with_qualifier_forwards_to_container(
        self, container: DIContainer
    ) -> None:
        """Lazy(T, qualifier='x') must forward the qualifier when resolving."""

        class DB:
            pass

        @Singleton(qualifier="primary")
        class PrimaryDB(DB):
            pass

        @Singleton(qualifier="replica")
        class ReplicaDB(DB):
            pass

        @Component
        class Service:
            def __init__(
                self,
                db: Lazy(DB, qualifier="replica"),  # type: ignore[valid-type]
            ) -> None:
                self.db = db

        container.bind(DB, PrimaryDB)
        container.bind(DB, ReplicaDB)
        container.register(Service)

        svc = container.get(Service)
        resolved = svc.db.get()

        assert isinstance(resolved, ReplicaDB)


# ─────────────────────────────────────────────────────────────────
#  Async LazyProxy tests
# ─────────────────────────────────────────────────────────────────


class TestLazyProxyAsync:
    """Async resolution via LazyProxy.aget()."""

    async def test_aget_resolves_instance(self, container: DIContainer) -> None:
        """LazyProxy.aget() should resolve the dep via container.aget()."""

        @Component
        class Consumer:
            def __init__(self, svc: Lazy[ExpensiveService]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(ExpensiveService)
        container.register(Consumer)

        consumer = await container.aget(Consumer)
        resolved = await consumer.svc.aget()

        assert isinstance(resolved, ExpensiveService)


# ─────────────────────────────────────────────────────────────────
#  Feature 2: Lazy[T | None] — optional unwrapping inside wrapper
# ─────────────────────────────────────────────────────────────────


class UnboundService:
    """NOT decorated — used to test optional resolution of unbound types."""

    pass


class TestLazyOptional:
    """Tests for Lazy[T | None] and Annotated[T, LazyMeta(optional=True)]."""

    def test_lazy_t_or_none_returns_none_when_not_bound_sync(
        self, container: DIContainer
    ) -> None:
        """Lazy[T | None] proxy .get() returns None when T has no binding.

        The union annotation Lazy[T | None] expands at runtime to
        Annotated[T | None, LazyMeta()].  The container should unwrap
        the Optional[T] and treat the binding as optional.

        Args:
            container: Fresh container with NO binding for UnboundService.
        """

        @Component
        class Consumer:
            def __init__(self, svc: Lazy[UnboundService | None]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(Consumer)
        # UnboundService is NOT registered — proxy must return None on .get()

        consumer = container.get(Consumer)

        # Proxy itself should be a LazyProxy with optional=True
        assert isinstance(consumer.svc, LazyProxy)
        result = consumer.svc.get()
        assert result is None, "Lazy[T | None] must return None when T is not bound"

    def test_lazy_t_or_none_returns_instance_when_bound_sync(
        self, container: DIContainer
    ) -> None:
        """Lazy[T | None] proxy .get() returns the resolved instance when T is bound.

        Args:
            container: Container with ExpensiveService registered.
        """

        @Component
        class Consumer:
            def __init__(self, svc: Lazy[ExpensiveService | None]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(ExpensiveService)
        container.register(Consumer)

        consumer = container.get(Consumer)
        result = consumer.svc.get()

        assert isinstance(result, ExpensiveService)

    def test_lazy_meta_optional_true_returns_none_when_not_bound(
        self, container: DIContainer
    ) -> None:
        """Annotated[T, LazyMeta(optional=True)] must also return None when not bound.

        Explicit Annotated form should behave identically to the Lazy[T | None] sugar.

        Args:
            container: Fresh container with no binding for UnboundService.
        """

        @Component
        class Consumer:
            def __init__(
                self, svc: Annotated[UnboundService, LazyMeta(optional=True)]
            ) -> None:
                self.svc = svc

        container.register(Consumer)

        consumer = container.get(Consumer)
        result = consumer.svc.get()

        assert result is None, "LazyMeta(optional=True) must return None when not bound"

    async def test_lazy_t_or_none_returns_none_async_path(
        self, container: DIContainer
    ) -> None:
        """Lazy[T | None] proxy .aget() returns None when T has no binding (async path).

        Args:
            container: Container with no binding for UnboundService.
        """

        @Component
        class Consumer:
            def __init__(self, svc: Lazy[UnboundService | None]) -> None:  # type: ignore[valid-type]
                self.svc = svc

        container.register(Consumer)

        consumer = await container.aget(Consumer)
        result = await consumer.svc.aget()

        assert (
            result is None
        ), "Lazy[T | None] .aget() must return None when T is not bound"

    async def test_lazy_meta_optional_true_async_path(
        self, container: DIContainer
    ) -> None:
        """Annotated[T, LazyMeta(optional=True)] .aget() returns None when not bound.

        Args:
            container: Container with no binding for UnboundService.
        """

        @Component
        class Consumer:
            def __init__(
                self, svc: Annotated[UnboundService, LazyMeta(optional=True)]
            ) -> None:
                self.svc = svc

        container.register(Consumer)

        consumer = await container.aget(Consumer)
        result = await consumer.svc.aget()

        assert result is None
