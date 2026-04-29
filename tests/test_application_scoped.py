"""Tests for F13: ApplicationScoped — alias for Singleton."""

from __future__ import annotations

from providify import (
    ApplicationScoped,
    DIContainer,
    Scope,
    Singleton,
)
from providify.metadata import _get_metadata


@ApplicationScoped
class AppScopedService:
    pass


@Singleton
class SingletonService:
    pass


def test_application_scoped_is_singleton_alias():
    assert ApplicationScoped is Singleton


def test_application_scoped_resolves_to_singleton_scope():
    meta = _get_metadata(AppScopedService)
    assert meta is not None
    assert meta.scope == Scope.SINGLETON


def test_application_scoped_shares_instance(container: DIContainer):
    container.register(AppScopedService)
    a = container.get(AppScopedService)
    b = container.get(AppScopedService)
    assert a is b


def test_application_scoped_with_kwargs(container: DIContainer):
    @ApplicationScoped(qualifier="primary", priority=5)
    class PrimaryService:
        pass

    meta = _get_metadata(PrimaryService)
    assert meta.scope == Scope.SINGLETON
    assert meta.qualifier == "primary"
    assert meta.priority == 5

    container.register(PrimaryService)
    a = container.get(PrimaryService, qualifier="primary")
    b = container.get(PrimaryService, qualifier="primary")
    assert a is b
