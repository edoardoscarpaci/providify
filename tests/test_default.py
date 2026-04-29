"""Tests for F2: @Default — explicit default qualifier."""

from __future__ import annotations

from providify import (
    Component,
    Default,
    DIContainer,
    InjectMeta,
    Singleton,
)
from typing import Annotated


class Service:
    pass


@Singleton(qualifier=Default)
class DefaultService(Service):
    pass


@Singleton
class AnotherService(Service):
    pass


def test_default_qualifed_bean_resolves_without_qualifier(container: DIContainer):
    container.bind(Service, DefaultService)
    svc = container.get(Service)
    assert isinstance(svc, DefaultService)


def test_default_resolves_same_as_none_qualifier(container: DIContainer):
    container.bind(Service, DefaultService)
    svc_none = container.get(Service, qualifier=None)
    svc_default = container.get(Service, qualifier=Default)
    # Both should resolve to the same type
    assert type(svc_none) is DefaultService
    assert type(svc_default) is DefaultService


def test_inject_meta_with_default(container: DIContainer):
    container.bind(Service, DefaultService)

    @Component
    class Consumer:
        def __init__(
            self, svc: Annotated[Service, InjectMeta(qualifier=Default)]
        ) -> None:
            self.svc = svc

    container.register(Consumer)
    c = container.get(Consumer)
    assert isinstance(c.svc, DefaultService)


def test_default_is_qualifier_annotated():
    assert hasattr(Default, "__di_qualifier_marker__")
