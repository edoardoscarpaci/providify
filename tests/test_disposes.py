"""Tests for F9: @Disposes — provider teardown methods."""

from __future__ import annotations

from providify import (
    DIContainer,
    Disposes,
    DisposesMarker,
    Provider,
)
from providify.decorator.module import Configuration
from providify.metadata import Scope as _Scope


class Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@Configuration
class InfraModule:
    @Provider(scope=_Scope.SINGLETON)
    def make_conn(self) -> Connection:
        return Connection()

    @Disposes(Connection)
    def close_conn(self, conn: Connection) -> None:
        conn.close()


def test_disposes_called_on_singleton_shutdown(container: DIContainer):
    container.install(InfraModule)
    conn = container.get(Connection)
    assert not conn.closed

    container.shutdown()
    assert conn.closed


def test_disposes_not_called_when_never_instantiated(container: DIContainer):
    container.install(InfraModule)
    # Never call container.get(Connection)
    container.shutdown()  # Should not raise


def test_disposes_marker_stamped():
    from providify.decorator.lifecycle import _get_disposes_marker

    @Disposes(Connection)
    def teardown(self, conn: Connection) -> None:
        pass

    marker = _get_disposes_marker(teardown)
    assert isinstance(marker, DisposesMarker)
    assert marker.disposed_type is Connection


class Cursor:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_disposes_multiple_providers(container: DIContainer):
    @Configuration
    class MultiModule:
        @Provider(scope=_Scope.SINGLETON)
        def make_conn(self) -> Connection:
            return Connection()

        @Disposes(Connection)
        def close_conn(self, conn: Connection) -> None:
            conn.close()

        @Provider(scope=_Scope.SINGLETON)
        def make_cursor(self) -> Cursor:
            return Cursor()

        @Disposes(Cursor)
        def close_cursor(self, cursor: Cursor) -> None:
            cursor.close()

    container.install(MultiModule)
    conn = container.get(Connection)
    cursor = container.get(Cursor)

    container.shutdown()

    assert conn.closed
    assert cursor.closed
