"""Unit tests for @Configuration modules and container.install() / container.ainstall().

Covered:
    - @Configuration stamps __di_module__ on the class
    - install(): basic module with @Provider methods registers all providers
    - install(): Spring-style — module constructor receives injected dependencies
    - install(): module providers are callable as bound methods (self is live instance)
    - install(): raises TypeError when class is not decorated with @Configuration
    - ainstall(): async mirror — works when module needs async-resolved constructor deps
    - Provider metadata (qualifier, priority, singleton) is preserved through install()
    - Multiple @Provider methods in one module — all registered
    - Module subclass is NOT treated as a module (own __dict__ check only)

DESIGN NOTE: Spring-style means the @Configuration class can declare constructor
parameters that are resolved by the container at install() time. The module instance
is then passed as `self` to every @Provider method as a bound method. This is the
key difference from Guice-style where modules are plain objects with no injection.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.module import Configuration
from providify.decorator.scope import Provider, Singleton
from providify.metadata import _has_configuration_module

# ─────────────────────────────────────────────────────────────────
#  @Configuration decorator tests
# ─────────────────────────────────────────────────────────────────


class TestConfigurationDecorator:
    """Tests for the @Configuration marker decorator."""

    def test_stamps_module_marker_on_class(self) -> None:
        """@Configuration must set __di_module__ = True on the class."""

        @Configuration
        class MyModule:
            pass

        assert _has_configuration_module(MyModule) is True

    def test_undecorated_class_is_not_a_module(self) -> None:
        """_has_configuration_module() must return False for a plain class."""

        class NotAModule:
            pass

        assert _has_configuration_module(NotAModule) is False

    def test_returns_same_class(self) -> None:
        """@Configuration must return the original class unchanged (no wrapping)."""

        class Original:
            pass

        result = Configuration(Original)

        assert result is Original

    def test_subclass_is_not_a_module(self) -> None:
        """Module marker is checked on the class's own __dict__ only.
        A subclass of a @Configuration class is NOT automatically a module.
        """

        @Configuration
        class Base:
            pass

        class Sub(Base):
            pass

        assert _has_configuration_module(Base) is True
        assert _has_configuration_module(Sub) is False


# ─────────────────────────────────────────────────────────────────
#  Domain types for module tests
# ─────────────────────────────────────────────────────────────────


class Repository:
    """Abstract interface for a data repository."""


class Cache:
    """Abstract interface for a cache."""


# ─────────────────────────────────────────────────────────────────
#  install() tests
# ─────────────────────────────────────────────────────────────────


class TestInstall:
    """Tests for container.install() with @Configuration modules."""

    def test_basic_provider_method_registered(self, container: DIContainer) -> None:
        """install() must register each @Provider method as a binding."""

        @Configuration
        class DataModule:
            @Provider
            def make_repo(self) -> Repository:
                return object.__new__(Repository)

        container.install(DataModule)
        result = container.get(Repository)

        assert isinstance(result, Repository)

    def test_multiple_providers_in_one_module(self, container: DIContainer) -> None:
        """install() must register ALL @Provider methods in the module."""

        @Configuration
        class InfraModule:
            @Provider
            def make_repo(self) -> Repository:
                return object.__new__(Repository)

            @Provider
            def make_cache(self) -> Cache:
                return object.__new__(Cache)

        container.install(InfraModule)

        # Both providers must be registered
        assert isinstance(container.get(Repository), Repository)
        assert isinstance(container.get(Cache), Cache)

    def test_spring_style_constructor_injection(self, container: DIContainer) -> None:
        """Module constructor deps must be injected — Spring-style.

        The module instance is created with its own dependencies resolved
        from the container. This is the key Spring-style feature.
        """

        @Singleton
        class Config:
            prefix = "prod"

        @Configuration
        class DataModule:
            def __init__(self, config: Config) -> None:
                # config is injected at install() time
                self._config = config

            @Provider
            def make_repo(self) -> Repository:
                # Uses the injected config — this is why Spring-style is useful
                repo = object.__new__(Repository)
                repo.prefix = self._config.prefix  # type: ignore[attr-defined]
                return repo

        container.register(Config)
        container.install(DataModule)

        repo = container.get(Repository)

        assert repo.prefix == "prod"  # type: ignore[attr-defined]

    def test_provider_metadata_preserved(self, container: DIContainer) -> None:
        """Provider qualifier, priority, and singleton=True must survive install()."""

        @Configuration
        class DataModule:
            @Provider(qualifier="primary", priority=1, singleton=True)
            def make_primary_repo(self) -> Repository:
                return object.__new__(Repository)

            @Provider(qualifier="replica", priority=2)
            def make_replica_repo(self) -> Repository:
                return object.__new__(Repository)

        container.install(DataModule)

        primary = container.get(Repository, qualifier="primary")
        replica = container.get(Repository, qualifier="replica")

        assert isinstance(primary, Repository)
        assert isinstance(replica, Repository)
        # They are different bindings
        assert primary is not replica

    def test_singleton_provider_caches_result(self, container: DIContainer) -> None:
        """@Provider(singleton=True) in a module must cache the result."""
        call_count = 0

        @Configuration
        class DataModule:
            @Provider(singleton=True)
            def make_repo(self) -> Repository:
                nonlocal call_count
                call_count += 1
                return object.__new__(Repository)

        container.install(DataModule)

        container.get(Repository)
        container.get(Repository)

        assert call_count == 1

    def test_raises_type_error_for_non_module(self, container: DIContainer) -> None:
        """install() must raise TypeError when the class has no @Configuration decorator."""

        class BareClass:
            @Provider
            def make_repo(self) -> Repository:
                return object.__new__(Repository)

        with pytest.raises(TypeError, match="@Configuration"):
            container.install(BareClass)

    def test_module_without_init_installed_correctly(self, container: DIContainer) -> None:
        """Module with no __init__ (default) must install without error."""

        @Configuration
        class SimpleModule:
            @Provider
            def make_cache(self) -> Cache:
                return object.__new__(Cache)

        container.install(SimpleModule)

        assert isinstance(container.get(Cache), Cache)


# ─────────────────────────────────────────────────────────────────
#  ainstall() tests
# ─────────────────────────────────────────────────────────────────


class TestAinstall:
    """Tests for container.ainstall() — async module installation."""

    async def test_ainstall_registers_providers(self, container: DIContainer) -> None:
        """ainstall() must register @Provider methods just like sync install()."""

        @Configuration
        class DataModule:
            @Provider
            def make_cache(self) -> Cache:
                return object.__new__(Cache)

        await container.ainstall(DataModule)

        result = await container.aget(Cache)
        assert isinstance(result, Cache)

    async def test_ainstall_raises_for_non_module(self, container: DIContainer) -> None:
        """ainstall() must raise TypeError when class is not @Configuration decorated."""

        class BareClass:
            @Provider
            def make_cache(self) -> Cache:
                return object.__new__(Cache)

        with pytest.raises(TypeError, match="@Configuration"):
            await container.ainstall(BareClass)

    async def test_ainstall_spring_style_with_async_dep(self, container: DIContainer) -> None:
        """ainstall() enables modules with deps that require aget() to resolve.

        This is the main reason ainstall() exists: when the container has
        async-only providers (e.g. db pool init), the module's constructor deps
        can only be resolved via the async path.
        """

        @Provider(singleton=True)
        async def make_config() -> Cache:
            # Simulates an async resource (e.g. connection pool startup)
            cache = object.__new__(Cache)
            cache.ready = True  # type: ignore[attr-defined]
            return cache

        @Configuration
        class AppModule:
            def __init__(self, cache: Cache) -> None:
                self._cache = cache

            @Provider
            def make_repo(self) -> Repository:
                repo = object.__new__(Repository)
                repo.cache = self._cache  # type: ignore[attr-defined]
                return repo

        container.provide(make_config)
        await container.ainstall(AppModule)

        repo = await container.aget(Repository)

        # The injected cache dep was resolved correctly via async path
        assert repo.cache.ready is True  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────
#  Plan 002 — returns= discovery through @Configuration / install()
# ─────────────────────────────────────────────────────────────────


class _ReturnsRepository:
    """Interface bound only via returns= — never named by any annotation below."""


class TestConfigurationReturnsOverride:
    """@Configuration must carry `@Provider(returns=...)` through install() for free.

    Covers Test coverage item 16: both a plain method provider and a
    @property field provider must register under the explicit interface,
    since install() copies effective_fn.__dict__ / proxies to __func__ per
    the plan's discovery-path note (container.py:3823/3826).
    """

    def test_method_provider_with_returns_registers_under_explicit_interface(
        self, container: DIContainer
    ) -> None:
        @Configuration
        class MethodReturnsModule:
            @Provider(returns=_ReturnsRepository)
            def make_repo(self):
                return object.__new__(_ReturnsRepository)

        container.install(MethodReturnsModule)

        assert isinstance(container.get(_ReturnsRepository), _ReturnsRepository)

    def test_property_provider_with_returns_registers_under_explicit_interface(
        self, container: DIContainer
    ) -> None:
        @Configuration
        class PropertyReturnsModule:
            @property
            @Provider(returns=_ReturnsRepository)
            def repo(self):
                return object.__new__(_ReturnsRepository)

        container.install(PropertyReturnsModule)

        assert isinstance(container.get(_ReturnsRepository), _ReturnsRepository)
