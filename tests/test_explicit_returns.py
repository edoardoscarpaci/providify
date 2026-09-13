"""Tests for `returns=` — explicit interface override for @Provider / provide().

Implements upstream gap U-20 (P2, hygiene / API-surface completeness): a
factory's binding interface can be stated explicitly, bypassing return-
annotation derivation entirely. This matters most for a per-domain-model
generic alias built inside a loop — ``Repository[model]`` for ``model`` in
``[User, Order]`` — which cannot be named by any *static* return annotation
at all. Before this feature, the only workaround was mutating
``factory.__annotations__["return"]`` before registering, which this plan
(002) makes obsolete.

Precedence, highest first:
    1. ``container.provide(fn, returns=...)`` — call-site override
    2. ``@Provider(returns=...)``              — decoration-time override
    3. ``fn``'s resolved return annotation
"""

from __future__ import annotations

import functools
from typing import Annotated, Any

import pytest

from providify.binding import ProviderBinding
from providify.container import DIContainer
from providify.decorator.scope import Provider, Scope

# ─────────────────────────────────────────────────────────────────
#  Module-level domain types
# ─────────────────────────────────────────────────────────────────


class Repository[T]:
    """Generic interface — a parameterised alias is a legal binding interface."""


class User:
    """Type argument used across generic-alias tests."""


class Order:
    """A second, distinct type argument — used to prove aliases stay distinct."""


class InMemoryRepo(Repository[Any]):
    """Concrete implementation returned by test factories."""

    def __init__(self, model: type | None = None) -> None:
        self.model = model


class Foo:
    """Plain, unrelated interface type used by the simplest happy-path tests."""


NOT_A_TYPE = 42
"""Module-level non-type — used to exercise the 'not a type' error path."""


class SomeInstance:
    """An ordinary instance (not a type, not callable-as-factory) used as a bad `returns=`."""


# ─────────────────────────────────────────────────────────────────
#  Happy paths
# ─────────────────────────────────────────────────────────────────


class TestHappyPaths:
    def test_provide_returns_kwarg_with_unannotated_factory(self, container: DIContainer) -> None:
        """returns= on provide() works even when fn has no return annotation at all."""

        def make_foo():  # no return annotation whatsoever
            return Foo()

        container.provide(make_foo, returns=Foo)

        assert isinstance(container.get(Foo), Foo)

    def test_provider_returns_kwarg_on_any_annotated_factory(self, container: DIContainer) -> None:
        """@Provider(returns=...) bypasses a useless `-> Any` annotation."""

        @Provider(returns=Repository[User])
        def make_user_repo() -> Any:
            return InMemoryRepo(User)

        container.provide(make_user_repo)

        assert isinstance(container.get(Repository[User]), InMemoryRepo)
        with pytest.raises(Exception):
            container.get(Repository[Order])

    def test_provider_returns_deferred_callable_is_lazily_evaluated(
        self, container: DIContainer
    ) -> None:
        """@Provider(returns=lambda: X) must not call the lambda at decoration time."""
        call_count = {"n": 0}

        def deferred_interface() -> type:
            call_count["n"] += 1
            return Repository[User]

        @Provider(returns=deferred_interface)
        def make_user_repo() -> Any:
            return InMemoryRepo(User)

        assert call_count["n"] == 0

        container.provide(make_user_repo)

        assert call_count["n"] == 1
        assert isinstance(container.get(Repository[User]), InMemoryRepo)

    def test_returns_annotated_unwraps_to_bare_type(self) -> None:
        """returns=Annotated[Foo, "meta"] must unwrap to Foo, not the Annotated alias."""

        @Provider
        def make_foo() -> Any:
            return Foo()

        binding = ProviderBinding(make_foo, returns=Annotated[Foo, "meta"])

        assert binding.interface is Foo

    async def test_async_factory_with_returns_kwarg(self, container: DIContainer) -> None:
        """returns= on an async factory must still set is_async and resolve via aget()."""

        @Provider(returns=Foo)
        async def make_foo() -> Any:
            return Foo()

        binding = ProviderBinding(make_foo)
        assert binding.is_async is True

        container.provide(make_foo)

        assert isinstance(await container.aget(Foo), Foo)

    def test_returns_combined_with_other_provider_kwargs(self, container: DIContainer) -> None:
        """returns= must not disturb qualifier/priority/scope/singleton handling."""

        @Provider(returns=Foo, qualifier="q", priority=3, singleton=True)
        def make_foo() -> Any:
            return Foo()

        binding = ProviderBinding(make_foo)

        assert binding.interface is Foo
        assert binding.scope == Scope.SINGLETON

        container.provide(make_foo)

        first = container.get(Foo)
        second = container.get(Foo)
        assert first is second  # singleton identity across two get() calls

    def test_motivating_case_generic_alias_per_loop_iteration(self, container: DIContainer) -> None:
        """The feature's raison d'être: one factory, distinct aliases per loop pass."""
        for model in (User, Order):

            def make_repo(model=model) -> Any:
                return InMemoryRepo(model)

            container.provide(make_repo, returns=Repository[model])

        user_repo = container.get(Repository[User])
        order_repo = container.get(Repository[Order])

        assert isinstance(user_repo, InMemoryRepo)
        assert isinstance(order_repo, InMemoryRepo)
        assert user_repo is not order_repo
        assert user_repo.model is User
        assert order_repo.model is Order


# ─────────────────────────────────────────────────────────────────
#  Precedence
# ─────────────────────────────────────────────────────────────────


class TestPrecedence:
    def test_provide_returns_wins_over_provider_returns(self, container: DIContainer) -> None:
        """provide(fn, returns=B) beats an already-decorated @Provider(returns=A)."""

        class A:
            pass

        class B:
            pass

        @Provider(returns=A)
        def make_thing() -> Any:
            return B()

        container.provide(make_thing, returns=B)

        assert isinstance(container.get(B), B)

    def test_provider_returns_wins_over_return_annotation(self) -> None:
        """@Provider(returns=A) beats the factory's own `-> C` annotation."""

        class A:
            pass

        class C:
            pass

        @Provider(returns=A)
        def make_thing() -> C:
            return A()

        binding = ProviderBinding(make_thing)

        assert binding.interface is A

    def test_returns_skips_unresolvable_return_annotation_entirely(self) -> None:
        """returns= must register cleanly even when the annotation is unresolvable.

        This is the 'don't force callers to repair an unrelated annotation'
        guarantee: the annotation is never read, so it can never raise.
        """

        @Provider(returns=Foo)
        def make_thing() -> "NotImportableAtRuntime":  # noqa: F821, UP037
            return Foo()

        binding = ProviderBinding(make_thing)

        assert binding.interface is Foo

    def test_no_returns_anywhere_preserves_existing_behaviour(self) -> None:
        """Regression guard: with no override, the annotation path is untouched.

        Both the resolved interface for a valid annotation and the exact
        TypeError message for an unresolvable one must be byte-for-byte
        identical to pre-existing (no-returns=) behaviour.
        """

        @Provider
        def make_foo() -> Foo:
            return Foo()

        binding = ProviderBinding(make_foo)
        assert binding.interface is Foo

        @Provider
        def make_bad() -> "NotImportableAtRuntime":  # noqa: F821, UP037
            return object()

        with pytest.raises(TypeError, match="unresolvable"):
            ProviderBinding(make_bad)


# ─────────────────────────────────────────────────────────────────
#  Errors
# ─────────────────────────────────────────────────────────────────


class TestErrors:
    def test_returns_string_is_rejected(self) -> None:
        """A string `returns=` must be rejected — points at the deferred-callable form."""

        @Provider
        def make_foo() -> Any:
            return Foo()

        with pytest.raises(TypeError) as excinfo:
            ProviderBinding(make_foo, returns="Repository[User]")

        message = str(excinfo.value)
        assert "make_foo" in message

    @pytest.mark.parametrize("bad_value", [42, SomeInstance()])
    def test_returns_non_type_non_callable_shape_raises(self, bad_value: Any) -> None:
        """An int or a plain instance can never be a legal `returns=` value."""

        @Provider
        def make_thing() -> Any:
            return Foo()

        with pytest.raises(TypeError) as excinfo:
            ProviderBinding(make_thing, returns=bad_value)

        assert "make_thing" in str(excinfo.value)

    def test_returns_deferred_callable_producing_non_type_raises(self) -> None:
        """A deferred callable's *result* must still pass the type/alias check."""

        @Provider
        def make_thing() -> Any:
            return Foo()

        with pytest.raises(TypeError) as excinfo:
            ProviderBinding(make_thing, returns=lambda: 42)

        assert "42" in str(excinfo.value)

    def test_returns_deferred_callable_that_raises_chains_cause(self) -> None:
        """A deferred callable that raises must chain the original exception."""

        @Provider
        def make_thing() -> Any:
            return Foo()

        def boom():
            raise RuntimeError("boom")

        with pytest.raises(TypeError) as excinfo:
            ProviderBinding(make_thing, returns=boom)

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == "boom"


# ─────────────────────────────────────────────────────────────────
#  Edge cases — additional coverage for the shape table
# ─────────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_returns_none_falls_back_to_provider_returns(self) -> None:
        """returns=None on provide() means 'not given' — falls back to @Provider(returns=)."""

        class A:
            pass

        @Provider(returns=A)
        def make_thing() -> Any:
            return A()

        binding = ProviderBinding(make_thing, returns=None)

        assert binding.interface is A

    def test_returns_bare_type_not_invoked(self) -> None:
        """returns=SomeClass binds the class itself — it must never be called."""
        calls: list[int] = []

        class Trackable:
            def __init__(self) -> None:
                calls.append(1)

        @Provider
        def make_thing() -> Any:
            return Trackable()

        binding = ProviderBinding(make_thing, returns=Trackable)

        assert binding.interface is Trackable
        assert calls == []  # never invoked as a factory

    def test_returns_bare_unparameterised_generic_class(self) -> None:
        """returns=Repository (unparameterised) is legal — isinstance(_, type) is True."""

        @Provider
        def make_thing() -> Any:
            return InMemoryRepo()

        binding = ProviderBinding(make_thing, returns=Repository)

        assert binding.interface is Repository

    def test_returns_deferred_callable_producing_annotated_unwraps(self) -> None:
        """Deferred callable returning Annotated[X, ...] unwraps to X."""

        @Provider
        def make_thing() -> Any:
            return Foo()

        binding = ProviderBinding(make_thing, returns=lambda: Annotated[Foo, "meta"])

        assert binding.interface is Foo

    def test_returns_deferred_callable_returning_callable_raises(self) -> None:
        """No recursion: a deferred callable returning another callable is an error."""

        @Provider
        def make_thing() -> Any:
            return Foo()

        with pytest.raises(TypeError) as excinfo:
            ProviderBinding(make_thing, returns=lambda: lambda: Foo)

        # Guards against a false pass from the unrelated "unexpected keyword
        # argument 'returns'" TypeError that fires before the feature exists.
        assert "make_thing" in str(excinfo.value)

    def test_returns_functools_partial_is_invoked(self) -> None:
        """functools.partial is not a type, has no get_origin — it gets called."""

        def interface_factory(iface: type) -> type:
            return iface

        partial_returns = functools.partial(interface_factory, Foo)

        @Provider
        def make_thing() -> Any:
            return Foo()

        binding = ProviderBinding(make_thing, returns=partial_returns)

        assert binding.interface is Foo

    def test_stacked_provider_returns_last_decorator_wins(self) -> None:
        """@Provider(returns=X) applied twice — innermost-outward, last wins."""

        class A:
            pass

        class B:
            pass

        @Provider(returns=B)
        @Provider(returns=A)
        def make_thing() -> Any:
            return B()

        binding = ProviderBinding(make_thing)

        assert binding.interface is B
