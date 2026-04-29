"""Tests for F8: InjectionPoint — injection context metadata."""

from __future__ import annotations

import logging

from providify import (
    DIContainer,
    InjectionPoint,
    Provider,
    Singleton,
)
from providify.decorator.module import Configuration


@Singleton
class Dependent:
    def __init__(self, ip: InjectionPoint) -> None:
        self.ip = ip


def test_injection_point_injected_in_constructor(container: DIContainer):
    container.register(Dependent)
    dep = container.get(Dependent)
    assert dep.ip is not None
    assert isinstance(dep.ip, InjectionPoint)


def test_injection_point_param_name(container: DIContainer):
    @Singleton
    class Svc:
        def __init__(self, ip: InjectionPoint) -> None:
            self.ip = ip

    container.register(Svc)
    svc = container.get(Svc)
    assert svc.ip.param_name == "ip"


def test_injection_point_declaring_class_is_class(container: DIContainer):
    @Singleton
    class MyService:
        def __init__(self, ip: InjectionPoint) -> None:
            self.ip = ip

    container.register(MyService)
    svc = container.get(MyService)
    # declaring_class is the last item on the resolution stack at injection time
    # For a top-level resolution it will be MyService
    assert svc.ip.declaring_class is MyService


def test_injection_point_annotation_is_hint(container: DIContainer):
    @Singleton
    class Svc2:
        def __init__(self, ip: InjectionPoint) -> None:
            self.ip = ip

    container.register(Svc2)
    svc = container.get(Svc2)
    assert svc.ip.annotation is InjectionPoint


def test_injection_point_in_provider(container: DIContainer):
    """InjectionPoint can be injected into @Provider methods."""

    @Configuration
    class Module:
        @Provider
        def provide_logger(self, ip: InjectionPoint) -> logging.Logger:
            name = ip.declaring_class.__name__ if ip.declaring_class else "root"
            return logging.getLogger(name)

    container.install(Module)
    logger = container.get(logging.Logger)
    assert logger is not None


def test_injection_point_not_injected_without_hint(container: DIContainer):
    @Singleton
    class NoIP:
        def __init__(self) -> None:
            pass

    container.register(NoIP)
    obj = container.get(NoIP)
    assert obj is not None
