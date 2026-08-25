"""Unit tests for ClassBinding and ProviderBinding.

These tests verify that bindings are constructed correctly from decorated
classes / provider functions, and that they raise the right errors on
invalid inputs. They do NOT involve the container — bindings are tested
in isolation.

Covered:
    - ClassBinding: success, undecorated class, wrong subtype
    - ProviderBinding: success, async detection, undecorated function,
      missing return annotation
    - __repr__ output sanity checks
"""

from __future__ import annotations

import pytest

from providify.binding import ClassBinding, ProviderBinding
from providify.decorator.scope import Component, Provider, Singleton
from providify.exceptions import (
    ClassBindingNotDecoratedError,
    ProviderBindingNotDecoratedError,
)
from providify.metadata import Scope

# ─────────────────────────────────────────────────────────────────
#  Domain types used across binding tests
# ─────────────────────────────────────────────────────────────────


class Notifier:
    """Abstract-style base — used as interface in binding tests."""


@Component
class EmailNotifier(Notifier):
    """Concrete @Component implementation of Notifier."""


@Singleton
class SMSNotifier(Notifier):
    """Concrete @Singleton implementation of Notifier."""


class UndecoNotifier(Notifier):
    """No DI decorator — binding should refuse this class."""


# ─────────────────────────────────────────────────────────────────
#  ClassBinding tests
# ─────────────────────────────────────────────────────────────────


class TestClassBinding:
    """Tests for ClassBinding construction and validation."""

    def test_creates_with_decorated_implementation(self) -> None:
        """Happy path: interface → decorated implementation."""
        b = ClassBinding(Notifier, EmailNotifier)

        assert b.interface is Notifier
        assert b.implementation is EmailNotifier
        assert b.scope == Scope.DEPENDENT  # @Component maps to DEPENDENT

    def test_singleton_scope_detected(self) -> None:
        """@Singleton decorator must produce Scope.SINGLETON on the binding."""
        b = ClassBinding(Notifier, SMSNotifier)

        assert b.scope == Scope.SINGLETON

    def test_self_registration_is_valid(self) -> None:
        """interface == implementation (self-registration via register()) must work.

        ClassBinding only checks issubclass() — a class is always a subclass
        of itself, so this is a valid edge case.
        """
        b = ClassBinding(EmailNotifier, EmailNotifier)

        assert b.interface is EmailNotifier
        assert b.implementation is EmailNotifier

    def test_raises_if_implementation_not_subclass(self) -> None:
        """Implementation must be a subclass of the interface — raises TypeError otherwise."""

        class Unrelated:
            pass

        @Component
        class DecoratedUnrelated(Unrelated):
            pass

        # DecoratedUnrelated is not a subclass of Notifier
        with pytest.raises(TypeError, match="must be a subclass"):
            ClassBinding(Notifier, DecoratedUnrelated)

    def test_raises_if_implementation_not_decorated(self) -> None:
        """Undecorated class (no @Component / @Singleton) must raise ClassBindingNotDecoratedError."""
        with pytest.raises(ClassBindingNotDecoratedError):
            ClassBinding(Notifier, UndecoNotifier)

    def test_repr_contains_class_names_and_scope(self) -> None:
        """__repr__ should include interface name, implementation name, and scope."""
        b = ClassBinding(Notifier, EmailNotifier)
        r = repr(b)

        assert "Notifier" in r
        assert "EmailNotifier" in r
        assert "DEPENDENT" in r

    def test_qualifier_included_in_repr(self) -> None:
        """qualifier should appear in __repr__ when set."""

        @Component(qualifier="email")
        class QualifiedNotifier(Notifier):
            pass

        b = ClassBinding(Notifier, QualifiedNotifier)
        r = repr(b)

        assert "email" in r


# ─────────────────────────────────────────────────────────────────
#  ProviderBinding tests
# ─────────────────────────────────────────────────────────────────


class TestProviderBinding:
    """Tests for ProviderBinding construction, async detection, and error paths."""

    def test_creates_from_provider_function(self) -> None:
        """Happy path: @Provider function with return annotation."""

        @Provider
        def make_notifier() -> Notifier:
            return EmailNotifier()

        b = ProviderBinding(make_notifier)

        assert b.interface is Notifier
        assert b.scope == Scope.DEPENDENT  # singleton=False is the default

    def test_singleton_provider_scope(self) -> None:
        """@Provider(singleton=True) must produce Scope.SINGLETON."""

        @Provider(singleton=True)
        def make_singleton_notifier() -> Notifier:
            return EmailNotifier()

        b = ProviderBinding(make_singleton_notifier)

        assert b.scope == Scope.SINGLETON

    def test_is_async_false_for_sync_provider(self) -> None:
        """Sync provider function must have is_async=False."""

        @Provider
        def make_notifier() -> Notifier:
            return EmailNotifier()

        b = ProviderBinding(make_notifier)

        assert b.is_async is False

    def test_is_async_true_for_async_provider(self) -> None:
        """async def provider must have is_async=True — detected at registration time."""

        @Provider
        async def make_notifier_async() -> Notifier:
            return EmailNotifier()

        b = ProviderBinding(make_notifier_async)

        assert b.is_async is True

    def test_qualifier_and_priority_stored(self) -> None:
        """qualifier and priority from @Provider metadata are stored on the binding."""

        @Provider(qualifier="sms", priority=5)
        def make_sms() -> Notifier:
            return SMSNotifier()

        b = ProviderBinding(make_sms)

        assert b.qualifier == "sms"
        assert b.priority == 5

    def test_raises_if_not_decorated(self) -> None:
        """Function without @Provider must raise ProviderBindingNotDecoratedError."""

        def bare_fn() -> Notifier:
            return EmailNotifier()

        with pytest.raises(ProviderBindingNotDecoratedError):
            ProviderBinding(bare_fn)

    def test_raises_if_no_return_annotation(self) -> None:
        """Provider with no return type hint must raise TypeError.

        The return annotation is used as the binding interface — without it
        the container has no idea what type this factory produces.
        """

        @Provider
        def no_return():  # type: ignore[return]
            return EmailNotifier()

        with pytest.raises(TypeError, match="return type hint"):
            ProviderBinding(no_return)

    def test_repr_contains_interface_and_function_name(self) -> None:
        """__repr__ should include interface name, function name, and scope."""

        @Provider
        def make_notifier() -> Notifier:
            return EmailNotifier()

        b = ProviderBinding(make_notifier)
        r = repr(b)

        assert "Notifier" in r
        assert "make_notifier" in r
        assert "DEPENDENT" in r
