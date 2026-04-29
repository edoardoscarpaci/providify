"""Tests for F3: @Alternative — deployment-time bean replacement."""

from __future__ import annotations

import pytest

from providify import (
    Alternative,
    DIContainer,
    Singleton,
)


class PaymentGateway:
    pass


@Singleton
class RealGateway(PaymentGateway):
    pass


@Singleton(priority=10)
@Alternative
class MockGateway(PaymentGateway):
    pass


def test_alternative_excluded_by_default(container: DIContainer):
    container.bind(PaymentGateway, RealGateway)
    container.bind(PaymentGateway, MockGateway)

    gw = container.get(PaymentGateway)
    assert isinstance(gw, RealGateway)


def test_alternative_included_after_enable(container: DIContainer):
    container.bind(PaymentGateway, RealGateway)
    container.bind(PaymentGateway, MockGateway)

    container.enable_alternative(MockGateway)
    gw = container.get(PaymentGateway)
    assert isinstance(gw, MockGateway)


def test_alternative_excluded_again_after_disable(container: DIContainer):
    container.bind(PaymentGateway, RealGateway)
    container.bind(PaymentGateway, MockGateway)

    container.enable_alternative(MockGateway)
    container.disable_alternative(MockGateway)

    gw = container.get(PaymentGateway)
    assert isinstance(gw, RealGateway)


def test_enable_non_alternative_still_resolves(container: DIContainer):
    container.bind(PaymentGateway, RealGateway)
    container.enable_alternative(RealGateway)
    gw = container.get(PaymentGateway)
    assert isinstance(gw, RealGateway)


def test_alternative_marker_stamped():
    assert hasattr(MockGateway, "__di_alternative__")


def test_alternative_raises_if_no_fallback(container: DIContainer):
    container.bind(PaymentGateway, MockGateway)
    with pytest.raises(LookupError):
        container.get(PaymentGateway)
