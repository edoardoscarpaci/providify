"""Tests for F11: NamedMeta — injection-point qualifier by name."""

from __future__ import annotations

import pytest

from providify import (
    Component,
    DIContainer,
    InjectMeta,
    NamedMeta,
    Singleton,
)
from typing import Annotated


class Cache:
    pass


@Singleton(qualifier="redis")
class RedisCache(Cache):
    pass


@Singleton(qualifier="memory")
class MemoryCache(Cache):
    pass


def test_named_meta_resolves_by_name(container: DIContainer):
    container.bind(Cache, RedisCache)
    container.bind(Cache, MemoryCache)

    @Component
    class Consumer:
        def __init__(self, cache: Annotated[Cache, NamedMeta("redis")]) -> None:
            self.cache = cache

    container.register(Consumer)
    c = container.get(Consumer)
    assert isinstance(c.cache, RedisCache)


def test_named_meta_same_as_inject_meta_with_qualifier(container: DIContainer):
    container.bind(Cache, RedisCache)
    container.bind(Cache, MemoryCache)

    @Component
    class C1:
        def __init__(self, cache: Annotated[Cache, NamedMeta("memory")]) -> None:
            self.cache = cache

    @Component
    class C2:
        def __init__(
            self, cache: Annotated[Cache, InjectMeta(qualifier="memory")]
        ) -> None:
            self.cache = cache

    container.register(C1)
    container.register(C2)

    c1 = container.get(C1)
    c2 = container.get(C2)
    assert isinstance(c1.cache, MemoryCache)
    assert isinstance(c2.cache, MemoryCache)


def test_named_meta_missing_qualifier_returns_unresolved(container: DIContainer):
    container.bind(Cache, RedisCache)

    @Component
    class BadConsumer:
        def __init__(self, cache: Annotated[Cache, NamedMeta("nonexistent")]) -> None:
            self.cache = cache

    container.register(BadConsumer)
    with pytest.raises(LookupError):
        container.get(BadConsumer)


def test_named_meta_str_field():
    meta = NamedMeta(name="test")
    assert meta.name == "test"
