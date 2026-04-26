"""Unit tests for Named, Priority, and Inheritable decorators.

Verifies that modifier decorators correctly update binding metadata
on both class-style and provider-style registrations, that combining
decorators works as expected, and that error paths are clear.

IMPORTANT — decorator ordering:
    Python applies decorators bottom-to-top, so the scope decorator (@Component,
    @Singleton, @Provider) must be the INNERMOST (bottom) decorator. Modifier
    decorators (@Named, @Priority, @Inheritable) are applied on top of it
    in any order.

        @Named(name="email")   # applied second — reads existing metadata
        @Component             # applied first  — stamps metadata
        class EmailSender: ...

    Reversing this order would raise NotDecoratedError because @Named checks
    for existing DI metadata before attempting to update it.

Covered:
    - Named: sets qualifier on @Component / @Singleton / @Provider
    - Named: requires parens with name= argument — bare @Named raises TypeError
    - Named: combined with scope decorator in any order
    - Priority: sets priority on @Component / @Singleton / @Provider
    - Priority: default priority is 0
    - Inheritable: marks subclasses to inherit metadata via MRO
    - Error: @Named / @Priority on undecorated class raises NotDecoratedError
    - Container integration: qualifier and priority route resolution correctly
"""

from __future__ import annotations

import pytest

from providify import (
    Component,
    DIContainer,
    Inheritable,
    Named,
    Provider,
    Singleton,
)
from providify.decorator.scope import Priority
from providify.exceptions import NotDecoratedError
from providify.metadata import _get_metadata, _get_provider_metadata


# ─────────────────────────────────────────────────────────────────
#  @Named — sets qualifier on classes and providers
# ─────────────────────────────────────────────────────────────────


class TestNamed:
    """Tests for the @Named qualifier decorator."""

    def test_named_sets_qualifier_on_component(self) -> None:
        """@Named(name=...) sets the qualifier field on a @Component class.

        Decorator order: @Named on top, @Component at bottom so the scope
        metadata is stamped first and @Named can then update it.
        """

        @Named(name="email")  # applied second — reads existing scope metadata
        @Component  # applied first  — stamps scope metadata
        class EmailSender:
            pass

        meta = _get_metadata(EmailSender)
        assert meta is not None
        assert meta.qualifier == "email"

    def test_named_sets_qualifier_on_singleton(self) -> None:
        """@Named(name=...) sets the qualifier field on a @Singleton class."""

        @Named(name="primary")
        @Singleton
        class PrimaryDB:
            pass

        meta = _get_metadata(PrimaryDB)
        assert meta is not None
        assert meta.qualifier == "primary"

    def test_named_sets_qualifier_on_provider(self) -> None:
        """@Named(name=...) sets the qualifier field on a @Provider function.

        Same bottom-to-top rule: @Provider stamps metadata first, then @Named
        reads and updates it.
        """

        @Named(name="sms")
        @Provider
        def make_sender() -> str:
            return "sms"

        meta = _get_provider_metadata(make_sender)
        assert meta is not None
        assert meta.qualifier == "sms"

    def test_named_without_parens_raises_type_error(self) -> None:
        """@Named used without parens must raise TypeError immediately.

        @Named has require_args=True — the guard fires before _is_decorated
        is checked, so the TypeError is raised regardless of whether the class
        is decorated or not.
        """
        with pytest.raises(TypeError, match="requires keyword arguments"):

            @Named  # ← bare @Named — no parens, no name= argument
            @Component
            class BadClass:
                pass

    def test_named_with_positional_string_raises_helpful_type_error(self) -> None:
        """@Named('smtp') must raise TypeError with a message pointing to name= form.

        Without this guard the runtime raises "TypeError: 'str' object is not callable"
        which gives no hint about the correct API.  The improved message should
        mention @Named(name='smtp') explicitly.

        Args:
            None
        """
        with pytest.raises(TypeError, match=r"@Named requires a keyword argument"):

            @Named("smtp")  # ← common mistake: positional string instead of name=
            @Component
            class BadClass:
                pass

    def test_named_on_undecorated_class_raises(self) -> None:
        """@Named on a class with no scope decorator must raise NotDecoratedError."""
        with pytest.raises(NotDecoratedError):

            @Named(name="oops")
            class Undecorated:
                pass

    def test_named_overrides_inline_qualifier(self) -> None:
        """@Named(name=...) applied on top of @Component(qualifier=...) overrides the qualifier.

        DESIGN: merge() replaces the field — last write wins. This is intentional
        so that stacking decorators refines rather than conflicts.
        Decorator order: @Named on top (applied second), @Component at bottom (applied first).
        """

        @Named(name="new")
        @Component(qualifier="old")
        class Refined:
            pass

        meta = _get_metadata(Refined)
        assert meta is not None
        assert meta.qualifier == "new"

    def test_named_used_in_resolution(self, container: DIContainer) -> None:
        """@Named qualifier routes container.get() to the correct implementation."""

        class Sender:
            pass

        @Named(name="email")
        @Component
        class EmailSender(Sender):
            pass

        @Named(name="sms")
        @Component
        class SmsSender(Sender):
            pass

        container.bind(Sender, EmailSender)
        container.bind(Sender, SmsSender)

        resolved = container.get(Sender, qualifier="email")
        assert isinstance(resolved, EmailSender)

        resolved_sms = container.get(Sender, qualifier="sms")
        assert isinstance(resolved_sms, SmsSender)


# ─────────────────────────────────────────────────────────────────
#  @Priority — sets priority on classes and providers
# ─────────────────────────────────────────────────────────────────


class TestPriority:
    """Tests for the @Priority decorator."""

    def test_priority_sets_value_on_component(self) -> None:
        """@Priority(priority=N) stores N in the class metadata."""

        @Priority(priority=3)
        @Component
        class LowPriorityService:
            pass

        meta = _get_metadata(LowPriorityService)
        assert meta is not None
        assert meta.priority == 3

    def test_priority_sets_value_on_singleton(self) -> None:
        """@Priority(priority=N) works identically on @Singleton classes."""

        @Priority(priority=10)
        @Singleton
        class HighPrioritySingleton:
            pass

        meta = _get_metadata(HighPrioritySingleton)
        assert meta is not None
        assert meta.priority == 10

    def test_priority_sets_value_on_provider(self) -> None:
        """@Priority(priority=N) works on @Provider functions."""

        @Priority(priority=7)
        @Provider
        def make_something() -> str:
            return "hello"

        meta = _get_provider_metadata(make_something)
        assert meta is not None
        assert meta.priority == 7

    def test_default_priority_is_zero(self) -> None:
        """Scope decorators default to priority=0 when no @Priority is applied."""

        @Component
        class DefaultPriorityService:
            pass

        meta = _get_metadata(DefaultPriorityService)
        assert meta is not None
        assert meta.priority == 0

    def test_priority_on_undecorated_class_raises(self) -> None:
        """@Priority on a class with no scope decorator must raise NotDecoratedError."""
        with pytest.raises(NotDecoratedError):

            @Priority(priority=5)
            class Undecorated:
                pass

    def test_priority_overrides_inline_priority(self) -> None:
        """@Priority applied on top of @Component(priority=...) overrides the priority.

        DESIGN: merge() replaces the field — last write wins.
        @Priority at top (applied second) overrides @Component(priority=1) at bottom.
        """

        @Priority(priority=99)
        @Component(priority=1)
        class Refined:
            pass

        meta = _get_metadata(Refined)
        assert meta is not None
        assert meta.priority == 99

    def test_priority_routes_get_all_ordering(self, container: DIContainer) -> None:
        """get_all() returns bindings sorted by priority value ascending — higher value wins on get()."""

        class Handler:
            pass

        @Component(priority=2)
        class SlowHandler(Handler):
            pass

        @Component(priority=0)
        class FastHandler(Handler):
            pass

        @Component(priority=1)
        class MidHandler(Handler):
            pass

        container.bind(Handler, FastHandler)
        container.bind(Handler, SlowHandler)
        container.bind(Handler, MidHandler)

        all_handlers = container.get_all(Handler)
        # Sorted by priority ascending — lowest number first
        assert type(all_handlers[0]) is FastHandler
        assert type(all_handlers[1]) is MidHandler
        assert type(all_handlers[2]) is SlowHandler


# ─────────────────────────────────────────────────────────────────
#  @Inheritable — subclasses inherit metadata via MRO
# ─────────────────────────────────────────────────────────────────


class TestInheritable:
    """Tests for the @Inheritable decorator.

    @Inheritable sets inherited=True on the metadata, instructing the
    container to search the MRO when resolving — so an undecorated subclass
    can be resolved through its parent's binding.
    """

    def test_inheritable_sets_inherited_flag(self) -> None:
        """@Inheritable must set inherited=True on the class metadata."""

        @Inheritable
        @Component
        class Base:
            pass

        meta = _get_metadata(Base)
        assert meta is not None
        assert meta.inherited is True

    def test_inheritable_default_is_false(self) -> None:
        """Without @Inheritable, inherited defaults to False."""

        @Component
        class Plain:
            pass

        meta = _get_metadata(Plain)
        assert meta is not None
        assert meta.inherited is False

    def test_inheritable_on_undecorated_class_raises(self) -> None:
        """@Inheritable on a class with no scope decorator must raise NotDecoratedError."""
        with pytest.raises(NotDecoratedError):

            @Inheritable
            class Undecorated:
                pass

    def test_inheritable_combined_with_named_and_priority(self) -> None:
        """Stacking @Inheritable with @Named and @Priority must preserve all fields.

        Decorator stack (outer to inner, top to bottom):
            @Inheritable  — applied last
            @Named        — applied third
            @Priority     — applied second
            @Singleton    — applied first (stamps fresh metadata)
        """

        @Inheritable
        @Named(name="base")
        @Priority(priority=2)
        @Singleton
        class ConfiguredBase:
            pass

        meta = _get_metadata(ConfiguredBase)
        assert meta is not None
        assert meta.inherited is True
        assert meta.qualifier == "base"
        assert meta.priority == 2


# ─────────────────────────────────────────────────────────────────
#  Stacking decorators — order independence
# ─────────────────────────────────────────────────────────────────


class TestDecoratorStacking:
    """Tests that scope + modifier decorators can be stacked correctly.

    Convention: scope decorator at bottom (innermost, applied first),
    modifier decorators layered on top in any order.
    """

    def test_scope_at_bottom_then_named_on_top(self) -> None:
        """@Component at bottom + @Named on top must produce the correct qualifier.

        This is the canonical order: scope establishes metadata, modifier refines it.
        """

        @Named(name="x")  # applied second
        @Component  # applied first
        class ServiceA:
            pass

        meta = _get_metadata(ServiceA)
        assert meta is not None
        assert meta.qualifier == "x"

    def test_inline_qualifier_on_scope_plus_priority_modifier(self) -> None:
        """@Component(qualifier=...) combined with @Priority must keep both fields.

        @Priority reads and updates existing metadata, so @Component must be innermost.
        """

        @Priority(priority=4)
        @Component(qualifier="inline-q")
        class ServiceC:
            pass

        meta = _get_metadata(ServiceC)
        assert meta is not None
        assert meta.qualifier == "inline-q"
        assert meta.priority == 4

    def test_named_and_priority_can_be_stacked_together(self) -> None:
        """@Named and @Priority stacked above @Singleton both take effect."""

        @Named(name="stacked")
        @Priority(priority=6)
        @Singleton
        class StackedService:
            pass

        meta = _get_metadata(StackedService)
        assert meta is not None
        assert meta.qualifier == "stacked"
        assert meta.priority == 6
