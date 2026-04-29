"""Tests for F1: @Qualifier — typed custom qualifier annotations."""

from __future__ import annotations

import pytest

from providify import (
    Component,
    DIContainer,
    InjectMeta,
    Qualifier,
    Singleton,
)
from typing import Annotated


@Qualifier
class Cloud:
    pass


@Qualifier
class Local:
    pass


class Storage:
    pass


@Singleton(qualifier=Cloud)
class CloudStorage(Storage):
    pass


@Singleton(qualifier=Local)
class LocalStorage(Storage):
    pass


def test_qualifier_marker_stamped():
    assert hasattr(Cloud, "__di_qualifier_marker__")
    assert hasattr(Local, "__di_qualifier_marker__")


def test_type_qualifier_binding_and_resolution(container: DIContainer):
    container.bind(Storage, CloudStorage)
    container.bind(Storage, LocalStorage)

    cloud = container.get(Storage, qualifier=Cloud)
    local = container.get(Storage, qualifier=Local)

    assert isinstance(cloud, CloudStorage)
    assert isinstance(local, LocalStorage)


def test_string_qualifier_still_works(container: DIContainer):
    @Component(qualifier="legacy")
    class LegacyService:
        pass

    container.register(LegacyService)
    svc = container.get(LegacyService, qualifier="legacy")
    assert isinstance(svc, LegacyService)


def test_inject_meta_with_type_qualifier(container: DIContainer):
    container.bind(Storage, CloudStorage)
    container.bind(Storage, LocalStorage)

    @Component
    class Consumer:
        def __init__(
            self, store: Annotated[Storage, InjectMeta(qualifier=Cloud)]
        ) -> None:
            self.store = store

    container.register(Consumer)
    c = container.get(Consumer)
    assert isinstance(c.store, CloudStorage)


def test_unqualified_get_returns_binding(container: DIContainer):
    # qualifier=None in _filter means "any qualifier" (no filter) — providify
    # resolves by priority when multiple bindings exist.
    container.bind(Storage, CloudStorage)
    # Single binding → always resolves
    result = container.get(Storage)
    assert isinstance(result, CloudStorage)


def test_wrong_qualifier_raises(container: DIContainer):
    container.bind(Storage, LocalStorage)
    with pytest.raises(LookupError):
        container.get(Storage, qualifier=Cloud)
