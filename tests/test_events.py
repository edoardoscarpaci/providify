"""Tests for F7: Event[T] / @Observes — event bus."""

from __future__ import annotations

import pytest

from providify import (
    DIContainer,
    Event,
    EventMeta,
    EventProxy,
    Observes,
    ObservesMarker,
    Singleton,
)
from typing import Annotated


class OrderPlaced:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id


class OrderShipped(OrderPlaced):
    pass


@Singleton
class AuditLog:
    def __init__(self) -> None:
        self.events: list[object] = []

    @Observes(OrderPlaced)
    def on_order(self, event: OrderPlaced) -> None:
        self.events.append(event)


@Singleton
class OrderService:
    def __init__(self, events: Annotated[OrderPlaced, EventMeta()]) -> None:
        self._events = events

    def place(self, order_id: str) -> None:
        self._events.fire(OrderPlaced(order_id))


def test_fire_reaches_observer(container: DIContainer):
    container.register(AuditLog)
    container.register(OrderService)

    audit = container.get(AuditLog)
    svc = container.get(OrderService)
    svc.place("ORD-1")

    assert len(audit.events) == 1
    assert audit.events[0].order_id == "ORD-1"


def test_subtype_events_match_supertype_observer(container: DIContainer):
    container.register(AuditLog)

    audit = container.get(AuditLog)
    proxy = EventProxy(container, OrderShipped)
    proxy.fire(OrderShipped("SHIP-1"))

    assert len(audit.events) == 1
    assert audit.events[0].order_id == "SHIP-1"


def test_event_alias_produces_annotated():
    ann = Event[OrderPlaced]
    from typing import get_origin, get_args, Annotated

    assert get_origin(ann) is Annotated
    args = get_args(ann)
    assert args[0] is OrderPlaced
    assert isinstance(args[1], EventMeta)


def test_eventproxy_repr():
    container = DIContainer()
    proxy = EventProxy(container, OrderPlaced)
    assert "OrderPlaced" in repr(proxy)


def test_observes_marker_stamped():
    @Observes(OrderPlaced)
    def handler(self, event: OrderPlaced) -> None:
        pass

    from providify.decorator.lifecycle import _get_observes_marker

    marker = _get_observes_marker(handler)
    assert isinstance(marker, ObservesMarker)
    assert marker.event_type is OrderPlaced


def test_dispatch_with_no_observers(container: DIContainer):
    proxy = EventProxy(container, OrderPlaced)
    # Should not raise even if nobody is observing
    proxy.fire(OrderPlaced("X"))


@pytest.mark.asyncio
async def test_afire_reaches_async_observer(container: DIContainer):
    received = []

    @Singleton
    class AsyncListener:
        @Observes(OrderPlaced)
        async def on_event(self, event: OrderPlaced) -> None:
            received.append(event)

    container.register(AsyncListener)
    container.get(AsyncListener)

    proxy = EventProxy(container, OrderPlaced)
    await proxy.afire(OrderPlaced("ASYNC-1"))

    assert len(received) == 1
    assert received[0].order_id == "ASYNC-1"
