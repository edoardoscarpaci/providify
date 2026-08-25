"""Failing tests for F7: multibinding collection injection (plan 010, Part A).

Covers: container.multibind(), @Multibound, bare list[T] injection at
constructor / class-var sites, InjectMeta interplay, precedence rules, and
validate_bindings() ambiguity reporting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pytest

from providify import Component, DIContainer, Inject, InjectInstances, Priority

# ─────────────────────────────────────────────────────────────────
#  Fixtures / test doubles
# ─────────────────────────────────────────────────────────────────


class Handler(ABC):
    @abstractmethod
    def handle(self) -> str: ...


@Component
class HandlerA(Handler):
    def handle(self) -> str:
        return "a"


@Component
class HandlerB(Handler):
    def handle(self) -> str:
        return "b"


@Priority(priority=1)
@Component
class LowPriorityHandler(Handler):
    def handle(self) -> str:
        return "low"


@Priority(priority=2)
@Component
class HighPriorityHandler(Handler):
    def handle(self) -> str:
        return "high"


class Repository:
    pass


class UserRepositoryImpl(Repository):
    pass


class GenericHandler(ABC):
    """Used for the generic T (list[Repository[User]]-style) edge case."""


# ─────────────────────────────────────────────────────────────────
#  container.multibind() API
# ─────────────────────────────────────────────────────────────────


def test_multibind_is_idempotent(container: DIContainer) -> None:
    # Calling multibind twice for the same type must not raise or duplicate state.
    container.multibind(Handler)
    container.multibind(Handler)  # should not raise


def test_multibind_declares_collection_point_for_list_injection(
    container: DIContainer,
) -> None:
    # After multibind(), container.get(list[Handler]) must resolve, not _UNRESOLVED.
    container.multibind(Handler)
    container.bind(Handler, HandlerA)
    container.bind(Handler, HandlerB)

    handlers = container.get(list[Handler])

    assert {type(h) for h in handlers} == {HandlerA, HandlerB}


def test_multibind_returns_empty_list_when_no_contributions(
    container: DIContainer,
) -> None:
    # §Design F7.4 — declaring the collection point makes zero a legal count.
    container.multibind(Handler)

    handlers = container.get(list[Handler])

    assert handlers == []


def test_multibind_result_is_priority_ascending(container: DIContainer) -> None:
    container.multibind(Handler)
    container.bind(Handler, HighPriorityHandler)
    container.bind(Handler, LowPriorityHandler)

    handlers = container.get(list[Handler])

    assert isinstance(handlers[0], LowPriorityHandler)
    assert isinstance(handlers[1], HighPriorityHandler)


def test_list_injection_without_collection_point_declared_is_unresolved(
    container: DIContainer,
) -> None:
    # Edge case: T not a collection point, no literal binding → falls back to
    # parameter default (unchanged v1 behaviour), not a full collection.
    @Component
    class Dispatcher:
        def __init__(self, handlers: list[Handler] | None = None) -> None:
            self.handlers = handlers

    container.bind(Handler, HandlerA)
    container.register(Dispatcher)

    dispatcher = container.get(Dispatcher)

    assert dispatcher.handlers is None


# ─────────────────────────────────────────────────────────────────
#  Injection-site syntax
# ─────────────────────────────────────────────────────────────────


def test_constructor_param_list_t_receives_all_implementations(
    container: DIContainer,
) -> None:
    container.multibind(Handler)

    @Component
    class Dispatcher:
        def __init__(self, handlers: list[Handler]) -> None:
            self.handlers = handlers

    container.bind(Handler, HandlerA)
    container.bind(Handler, HandlerB)
    container.register(Dispatcher)

    dispatcher = container.get(Dispatcher)

    assert len(dispatcher.handlers) == 2


def test_class_var_list_t_receives_all_implementations(
    container: DIContainer,
) -> None:
    # Verifies the _inject_class_vars_sync path inherits collection resolution.
    container.multibind(Handler)

    @Component
    class Dispatcher:
        handlers: Inject[list[Handler]]

    container.bind(Handler, HandlerA)
    container.bind(Handler, HandlerB)
    container.register(Dispatcher)

    dispatcher = container.get(Dispatcher)

    assert len(dispatcher.handlers) == 2


@pytest.mark.asyncio
async def test_async_resolution_of_collection_point(container: DIContainer) -> None:
    container.multibind(Handler)
    container.bind(Handler, HandlerA)
    container.bind(Handler, HandlerB)

    handlers = await container.aget(list[Handler])

    assert len(handlers) == 2


def test_qualifier_filtered_collection(container: DIContainer) -> None:
    # container.get() already accepts qualifier= for single-value resolution
    # (container.py's get()); collection injection reuses the exact same
    # parameter — a qualifier narrows _filter()'s candidate set the same way
    # for both. A binding's qualifier is set via @Component(qualifier=...)
    # (see tests/test_qualifier.py), never as a bind()-call kwarg — bind()
    # itself has no qualifier parameter.
    @Component(qualifier="special")
    class SpecialHandler(Handler):
        def handle(self) -> str:
            return "special"

    container.multibind(Handler)
    container.bind(Handler, HandlerA)  # default qualifier
    container.bind(Handler, SpecialHandler)

    handlers = container.get(list[Handler], qualifier="special")

    assert len(handlers) == 1
    assert isinstance(handlers[0], SpecialHandler)


def test_profile_inactive_contribution_excluded_from_collection(
    container: DIContainer,
) -> None:
    from providify import Alternative

    container.multibind(Handler)
    container.bind(Handler, HandlerA)

    @Alternative
    @Component
    class DisabledHandler(Handler):
        def handle(self) -> str:
            return "disabled"

    container.bind(Handler, DisabledHandler)

    handlers = container.get(list[Handler])

    assert all(not isinstance(h, DisabledHandler) for h in handlers)


def test_inject_marker_wrapping_bare_list_behaves_same_as_bare_list(
    container: DIContainer,
) -> None:
    # Edge case: Inject[list[T]] (marker + bare collection): InjectMeta.all is
    # False, so it must route through get(list[T]) and hit the new rule.
    container.multibind(Handler)
    container.bind(Handler, HandlerA)
    container.bind(Handler, HandlerB)

    @Component
    class Dispatcher:
        def __init__(self, handlers: Inject[list[Handler]]) -> None:
            self.handlers = handlers

    container.register(Dispatcher)
    dispatcher = container.get(Dispatcher)

    assert len(dispatcher.handlers) == 2


def test_inject_instances_still_raises_on_zero_matches_even_if_collection_point(
    container: DIContainer,
) -> None:
    # Edge case: InjectInstances[T] differs from list[T] only on the empty case.
    container.multibind(Handler)

    @Component
    class Dispatcher:
        def __init__(self, handlers: InjectInstances[Handler]) -> None:
            self.handlers = handlers

    container.register(Dispatcher)

    with pytest.raises(LookupError):
        container.get(Dispatcher)


def test_generic_inner_type_collection_point(container: DIContainer) -> None:
    # Edge case: list[Repository[User]]-style parameterised interface.
    from typing import Generic, TypeVar

    T = TypeVar("T")

    class GenericRepo(Generic[T], ABC):
        @abstractmethod
        def all(self) -> list[T]: ...

    @Component
    class ConcreteRepo(GenericRepo):
        def all(self) -> list:
            return []

    container.multibind(GenericRepo)
    container.bind(GenericRepo, ConcreteRepo)

    repos = container.get(list[GenericRepo])

    assert len(repos) == 1
    assert isinstance(repos[0], ConcreteRepo)


# ─────────────────────────────────────────────────────────────────
#  Async provider guard (mirrors get_all's guard)
# ─────────────────────────────────────────────────────────────────


def test_collection_with_async_provider_raises_on_sync_get(
    container: DIContainer,
) -> None:
    container.multibind(Handler)
    container.bind(Handler, HandlerA)

    async def make_async_handler() -> Handler:
        return HandlerB()

    container.provide(make_async_handler, returns=Handler)

    with pytest.raises(RuntimeError):
        container.get(list[Handler])


# ─────────────────────────────────────────────────────────────────
#  @Multibound decorator
# ─────────────────────────────────────────────────────────────────


def test_multibound_decorator_declares_collection_point_without_multibind_call(
    container: DIContainer,
) -> None:
    from providify import Multibound

    @Multibound
    class DecoratedHandler(ABC):
        @abstractmethod
        def handle(self) -> str: ...

    @Component
    class Impl(DecoratedHandler):
        def handle(self) -> str:
            return "impl"

    container.bind(DecoratedHandler, Impl)

    handlers = container.get(list[DecoratedHandler])

    assert len(handlers) == 1
    assert isinstance(handlers[0], Impl)


def test_multibound_on_class_is_discovered_without_explicit_multibind(
    container: DIContainer,
) -> None:
    from providify import Multibound

    @Multibound
    class ScanHandler(ABC):
        @abstractmethod
        def handle(self) -> str: ...

    @Component
    class ScanImpl(ScanHandler):
        def handle(self) -> str:
            return "scan"

    container.bind(ScanHandler, ScanImpl)

    # No container.multibind(ScanHandler) call anywhere.
    result = container.get(list[ScanHandler])

    assert isinstance(result[0], ScanImpl)


# ─────────────────────────────────────────────────────────────────
#  Precedence rule: literal list[T] binding wins over collection point
# ─────────────────────────────────────────────────────────────────


def test_literal_list_binding_wins_over_collection_point(
    container: DIContainer,
) -> None:
    # §Design F7.3 rule 1: a binding that literally matches list[T] wins.
    container.multibind(Handler)
    container.bind(Handler, HandlerA)

    literal_list = [HandlerB()]

    def make_literal_list() -> list:
        return literal_list

    container.provide(make_literal_list, returns=list[Handler])

    result = container.get(list[Handler])

    assert result is literal_list


def test_validate_bindings_reports_collection_point_and_literal_binding_ambiguity(
    container: DIContainer,
) -> None:
    # §Design F7.3 / step 12 — validate_bindings() must surface the ambiguity
    # (a warning, not a raise: validate_bindings() only raises on scope
    # leaks) so users notice the literal binding silently wins.
    container.multibind(Handler)
    container.bind(Handler, HandlerA)

    def make_literal_list() -> list:
        return []

    container.provide(make_literal_list, returns=list[Handler])

    with pytest.warns(Warning, match="Handler"):
        container.validate_bindings()
