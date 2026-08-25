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


class _AltProviderWidget:
    """Module-level sentinel — @Provider return types resolve via
    fn.__globals__, so this must not be a locally-scoped test class."""


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


# ─────────────────────────────────────────────────────────────────
#  Plan 005 — @Profile / @Alternative composition (Step 13)
#
#  These tests target code that does not exist yet (`Profile` decorator).
#  They must not interfere with the 6 tests above, which must keep passing
#  unchanged once @Profile lands.
# ─────────────────────────────────────────────────────────────────


def test_alternative_without_profile_still_requires_enable_call(container: DIContainer):
    """Invariant: a container with no @Profile anywhere behaves exactly as today."""
    container.bind(PaymentGateway, RealGateway)
    container.bind(PaymentGateway, MockGateway)

    gw = container.get(PaymentGateway)
    assert isinstance(gw, RealGateway)


def test_alternative_with_matching_profile_resolves_without_enable_call():
    from providify import Alternative, Profile, Singleton

    class Db:
        pass

    @Singleton
    class RealDb(Db):
        pass

    # priority=10: without an explicit tiebreaker both bindings are equally
    # eligible once "test" is active (@Priority rules apply to ties per
    # plan 005 §Edge cases) — matches the priority=10 convention the
    # pre-existing MockGateway fixture above uses for the same reason.
    @Profile("test")
    @Alternative
    @Singleton(priority=10)
    class FakeDb(Db):
        pass

    c = DIContainer(profiles=("test",))
    c.bind(Db, RealDb)
    c.bind(Db, FakeDb)

    assert isinstance(c.get(Db), FakeDb)


def test_alternative_with_non_matching_profile_filtered_out():
    from providify import Alternative, Profile, Singleton

    class Db:
        pass

    @Singleton
    class RealDb(Db):
        pass

    @Profile("test")
    @Alternative
    @Singleton
    class FakeDb(Db):
        pass

    c = DIContainer(profiles=("prod",))
    c.bind(Db, RealDb)
    c.bind(Db, FakeDb)

    assert isinstance(c.get(Db), RealDb)


def test_alternative_provider_function_is_disabled_by_default():
    """Behaviour change (§Risks): @Alternative on a @Provider fn no longer no-ops."""
    from providify import Alternative, Provider

    @Alternative
    @Provider(singleton=True)
    def mock_widget() -> _AltProviderWidget:
        return _AltProviderWidget()

    c = DIContainer()
    c.provide(mock_widget)
    with pytest.raises(LookupError):
        c.get(_AltProviderWidget)
