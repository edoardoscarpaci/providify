"""Tests for F12: Field-level @Provider — @property combined with @Provider."""

from __future__ import annotations

from providify import (
    DIContainer,
    Provider,
)
from providify.decorator.module import Configuration
from providify.metadata import Scope as _Scope


class Config:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


class Cache:
    def __init__(self, url: str) -> None:
        self.url = url


@Configuration
class AppModule:
    """Configuration using @property @Provider pattern (field-level providers)."""

    @property
    @Provider
    def config(self) -> Config:
        return Config("postgres://localhost/app")

    @property
    @Provider(scope=_Scope.SINGLETON)
    def cache(self) -> Cache:
        return Cache("redis://localhost")


def test_property_provider_registered_at_install(container: DIContainer):
    container.install(AppModule)
    cfg = container.get(Config)
    assert isinstance(cfg, Config)
    assert cfg.dsn == "postgres://localhost/app"


def test_singleton_property_provider_shares_instance(container: DIContainer):
    container.install(AppModule)
    a = container.get(Cache)
    b = container.get(Cache)
    assert a is b


def test_multiple_property_providers(container: DIContainer):
    container.install(AppModule)
    cfg = container.get(Config)
    cache = container.get(Cache)
    assert isinstance(cfg, Config)
    assert isinstance(cache, Cache)


class _AltConfig:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


def test_property_provider_independent_of_method_provider(container: DIContainer):
    @Configuration
    class MixedModule:
        @property
        @Provider
        def config_field(self) -> _AltConfig:
            return _AltConfig("sqlite:///test")

        @Provider
        def provide_cache(self) -> Cache:
            return Cache("memcached://localhost")

    container.install(MixedModule)
    cfg = container.get(_AltConfig)
    cache = container.get(Cache)
    assert cfg.dsn == "sqlite:///test"
    assert cache.url == "memcached://localhost"
