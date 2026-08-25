"""Tests for F6: @Decorator — interface-level delegation."""

from __future__ import annotations

from typing import Annotated

from providify import (
    Decorator,
    Delegate,
    DelegateMeta,
    DIContainer,
    Singleton,
)


class Notifier:
    def notify(self, msg: str) -> str:
        return msg


@Singleton
class EmailNotifier(Notifier):
    def notify(self, msg: str) -> str:
        return f"email:{msg}"


@Singleton(priority=10)
@Decorator
class LoggingNotifier(Notifier):
    def __init__(self, delegate: Annotated[Notifier, DelegateMeta()]) -> None:
        self._delegate = delegate

    def notify(self, msg: str) -> str:
        result = self._delegate.notify(msg)
        return f"logged:{result}"


def test_decorator_wraps_delegate(container: DIContainer):
    container.bind(Notifier, EmailNotifier)
    container.bind(Notifier, LoggingNotifier)

    notifier = container.get(Notifier)
    # LoggingNotifier has higher priority (10) so it wraps EmailNotifier
    assert isinstance(notifier, LoggingNotifier)
    result = notifier.notify("hello")
    assert result == "logged:email:hello"


def test_delegate_meta_resolves_inner_bean(container: DIContainer):
    container.bind(Notifier, EmailNotifier)
    container.bind(Notifier, LoggingNotifier)

    notifier = container.get(Notifier)
    assert isinstance(notifier._delegate, EmailNotifier)


def test_decorator_marker_stamped():
    assert hasattr(LoggingNotifier, "__di_decorator__")


def test_delegate_alias_produces_annotated():
    ann = Delegate[Notifier]
    from typing import Annotated, get_args, get_origin

    assert get_origin(ann) is Annotated
    args = get_args(ann)
    assert args[0] is Notifier
    assert isinstance(args[1], DelegateMeta)
