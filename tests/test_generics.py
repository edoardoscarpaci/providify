"""Unit tests for Generic[T] injection support.

Covers the three layers that needed to change:

  1. Utilities (utils.py)
       _type_name, _is_generic_subtype, _interface_matches

  2. Container core (container.py + binding.py)
       bind(), register(), get(), get_all(), plain-annotation injection,
       error messages, multi-type disambiguation

  3. Scanner (scanner.py)
       Auto-discovery of plain Generic[T] bases and ABC+Generic[T] bases,
       multiple implementations of the same generic interface

DESIGN: domain classes live at test-function scope so each test is fully
self-contained.  The fake-module helper is borrowed from test_scanner.py to
let the scanner tests work without touching the real filesystem.
"""

from __future__ import annotations

import sys
import types
import uuid
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

import pytest

from providify.binding import ClassBinding
from providify.container import DIContainer
from providify.decorator.scope import Component, Singleton
from providify.utils import _interface_matches, _is_generic_subtype, _type_name

T = TypeVar("T")
S = TypeVar("S")
U = TypeVar("U")
V = TypeVar("V")


# ─────────────────────────────────────────────────────────────────
#  Shared fake-module fixture
# ─────────────────────────────────────────────────────────────────


def _fresh_name() -> str:
    """Return a unique module name that cannot collide with real modules."""
    return f"_gen_test_{uuid.uuid4().hex}"


def _add(mod: types.ModuleType, obj: object) -> object:
    """Stamp *obj* as defined in *mod* and set it as a module attribute.

    inspect.getmodule() resolves obj.__module__ → sys.modules lookup.
    Setting __module__ makes the scanner's 'defined here?' guard pass.

    Args:
        mod: The fake module.
        obj: A class or function to register.

    Returns:
        The same object (for chaining).
    """
    name = getattr(obj, "__name__", None) or getattr(obj, "__qualname__", "obj")
    obj.__module__ = mod.__name__  # type: ignore[union-attr]
    setattr(mod, name, obj)
    return obj


@pytest.fixture
def fake_mod() -> types.ModuleType:
    """Yield a fresh ModuleType registered in sys.modules.

    Cleaned up after each test to avoid cross-test pollution.

    Yields:
        An empty types.ModuleType ready to receive DI-decorated members.
    """
    name = _fresh_name()
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    yield mod
    sys.modules.pop(name, None)


# ─────────────────────────────────────────────────────────────────
#  Utility tests — _type_name, _is_generic_subtype, _interface_matches
# ─────────────────────────────────────────────────────────────────


class TestTypeName:
    """Tests for _type_name() — safe __name__ accessor for any type."""

    def test_concrete_type_returns_class_name(self) -> None:
        """Concrete types have __name__ — must be returned directly."""

        class Repo:
            pass

        assert _type_name(Repo) == "Repo"

    def test_generic_alias_returns_str_representation(self) -> None:
        """Generic aliases lack __name__ — must fall back to str()."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        name = _type_name(Repo[Item])
        # str() representation varies slightly across Python versions but
        # always contains both the origin and the arg name.
        assert "Repo" in name
        assert "Item" in name

    def test_builtin_type_returns_name(self) -> None:
        """Built-in types (int, str) have __name__ — must work normally."""
        assert _type_name(int) == "int"
        assert _type_name(str) == "str"


class TestIsGenericSubtype:
    """Tests for _is_generic_subtype() — parameterised subtype check."""

    def test_concrete_interface_delegates_to_issubclass(self) -> None:
        """When interface is not generic, plain issubclass is used."""

        class Base:
            pass

        class Child(Base):
            pass

        assert _is_generic_subtype(Child, Base) is True
        assert _is_generic_subtype(Base, Child) is False

    def test_exact_generic_match(self) -> None:
        """Implementation that directly extends the exact parameterisation → True."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        assert _is_generic_subtype(ItemRepo, Repo[Item]) is True

    def test_wrong_type_arg_returns_false(self) -> None:
        """Same origin but different type arg must not match."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        class Other:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        assert _is_generic_subtype(ItemRepo, Repo[Other]) is False

    def test_unrelated_type_returns_false(self) -> None:
        """Class not in the MRO of the origin at all → False."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        class Unrelated:
            pass

        assert _is_generic_subtype(Unrelated, Repo[Item]) is False

    def test_multi_level_inheritance(self) -> None:
        """Parameterisation defined on a grandparent must still be found."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        # TypedRepo fixes the type parameter at one level of indirection
        class TypedItemRepo(Repo[Item]):
            pass

        @Singleton
        class ConcreteRepo(TypedItemRepo):
            pass

        # ConcreteRepo does not declare Repo[Item] directly in __orig_bases__,
        # but TypedItemRepo does — the MRO walk must find it.
        assert _is_generic_subtype(ConcreteRepo, Repo[Item]) is True


class TestInterfaceMatches:
    """Tests for _interface_matches() — the four structural combinations."""

    # ── Both concrete ──────────────────────────────────────────────

    def test_both_concrete_subclass_true(self) -> None:
        """issubclass(Child, Base) → True."""

        class Base:
            pass

        class Child(Base):
            pass

        assert _interface_matches(Child, Base) is True

    def test_both_concrete_unrelated_false(self) -> None:
        """Unrelated concrete types → False."""

        class A:
            pass

        class B:
            pass

        assert _interface_matches(A, B) is False

    # ── Concrete binding, generic request ─────────────────────────

    def test_concrete_binding_matches_generic_request(self) -> None:
        """Concrete impl that extends Repository[Item] must match Repository[Item]."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        assert _interface_matches(ItemRepo, Repo[Item]) is True

    def test_concrete_binding_wrong_type_arg_false(self) -> None:
        """Concrete impl extending Repo[Item] must NOT match Repo[Other]."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        class Other:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        assert _interface_matches(ItemRepo, Repo[Other]) is False

    # ── Generic binding, generic request ──────────────────────────

    def test_same_generic_binding_matches(self) -> None:
        """Binding stored as Repo[Item] must match request for Repo[Item]."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        assert _interface_matches(Repo[Item], Repo[Item]) is True

    def test_generic_binding_wrong_arg_false(self) -> None:
        """Binding Repo[Item] must NOT match request Repo[Other]."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        class Other:
            pass

        assert _interface_matches(Repo[Item], Repo[Other]) is False

    # ── Generic binding, concrete request ─────────────────────────

    def test_generic_binding_matches_origin_request(self) -> None:
        """Binding stored as Repo[Item] must match plain Repo request."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        assert _interface_matches(Repo[Item], Repo) is True

    # ── Type errors are suppressed ────────────────────────────────

    def test_non_type_arguments_return_false(self) -> None:
        """Non-type arguments must not raise — they return False."""
        assert _interface_matches("not a type", int) is False  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────
#  Container tests — bind / register / get / injection
# ─────────────────────────────────────────────────────────────────


class TestContainerGenericBind:
    """Tests for explicit container.bind(GenericAlias, Implementation)."""

    def test_bind_generic_interface_to_implementation(
        self, container: DIContainer
    ) -> None:
        """bind(Repo[Item], ItemRepo) must create a ClassBinding resolvable
        by get(Repo[Item])."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        container.bind(Repo[Item], ItemRepo)
        result = container.get(Repo[Item])

        assert isinstance(result, ItemRepo)

    def test_bind_generic_wrong_implementation_raises(
        self, container: DIContainer
    ) -> None:
        """bind(Repo[Item], OtherRepo) where OtherRepo extends Repo[Other]
        must raise TypeError at registration time."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        class Other:
            pass

        @Singleton
        class OtherRepo(Repo[Other]):
            pass

        # OtherRepo implements Repo[Other], NOT Repo[Item] — must fail
        with pytest.raises(TypeError):
            container.bind(Repo[Item], OtherRepo)

    def test_bind_multiple_generic_specialisations(
        self, container: DIContainer
    ) -> None:
        """Two different type-arg specialisations must be independently resolvable."""

        class Repo(Generic[T]):
            pass

        class User:
            pass

        class Product:
            pass

        @Singleton
        class UserRepo(Repo[User]):
            pass

        @Singleton
        class ProductRepo(Repo[Product]):
            pass

        container.bind(Repo[User], UserRepo)
        container.bind(Repo[Product], ProductRepo)

        user_repo = container.get(Repo[User])
        product_repo = container.get(Repo[Product])

        assert isinstance(user_repo, UserRepo)
        assert isinstance(product_repo, ProductRepo)
        # Must NOT cross-resolve
        assert not isinstance(user_repo, ProductRepo)
        assert not isinstance(product_repo, UserRepo)

    def test_bind_generic_alias_repr_is_readable(self, container: DIContainer) -> None:
        """ClassBinding.__repr__ must include the generic interface name."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        container.bind(Repo[Item], ItemRepo)
        binding = container._bindings[0]

        # repr must be a non-empty string containing both names
        r = repr(binding)
        assert "Repo" in r
        assert "Item" in r
        assert "ItemRepo" in r


class TestContainerGenericRegister:
    """Tests for register(Implementation) + get(GenericAlias)."""

    def test_register_resolved_by_generic_alias(self, container: DIContainer) -> None:
        """Registered concrete class must be found when querying its generic base."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        container.register(ItemRepo)
        result = container.get(Repo[Item])

        assert isinstance(result, ItemRepo)

    def test_register_multiple_specialisations_disambiguated(
        self, container: DIContainer
    ) -> None:
        """Two registered repos with different type args must each resolve correctly."""

        class Repo(Generic[T]):
            pass

        class User:
            pass

        class Order:
            pass

        @Singleton
        class UserRepo(Repo[User]):
            pass

        @Singleton
        class OrderRepo(Repo[Order]):
            pass

        container.register(UserRepo)
        container.register(OrderRepo)

        assert isinstance(container.get(Repo[User]), UserRepo)
        assert isinstance(container.get(Repo[Order]), OrderRepo)

    def test_register_lookup_error_for_unregistered_specialisation(
        self, container: DIContainer
    ) -> None:
        """get(Repo[Unregistered]) must raise LookupError, not crash."""

        class Repo(Generic[T]):
            pass

        class Known:
            pass

        class Unknown:
            pass

        @Singleton
        class KnownRepo(Repo[Known]):
            pass

        container.register(KnownRepo)

        with pytest.raises(LookupError):
            container.get(Repo[Unknown])

    def test_register_generic_still_resolvable_by_concrete_type(
        self, container: DIContainer
    ) -> None:
        """A class registered via register() can still be resolved by its own type."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        container.register(ItemRepo)
        result = container.get(ItemRepo)

        assert isinstance(result, ItemRepo)


class TestContainerGenericGetAll:
    """Tests for get_all() with generic interfaces."""

    def test_get_all_returns_all_matching_specialisations(
        self, container: DIContainer
    ) -> None:
        """get_all(Repo[Item]) must return every implementation of Repo[Item]."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Component(priority=1)
        class PrimaryRepo(Repo[Item]):
            pass

        @Component(priority=2)
        class FallbackRepo(Repo[Item]):
            pass

        container.register(PrimaryRepo)
        container.register(FallbackRepo)

        results = container.get_all(Repo[Item])

        assert len(results) == 2
        types_found = {type(r) for r in results}
        assert PrimaryRepo in types_found
        assert FallbackRepo in types_found

    def test_get_all_does_not_include_different_specialisation(
        self, container: DIContainer
    ) -> None:
        """get_all(Repo[A]) must NOT include implementations of Repo[B]."""

        class Repo(Generic[T]):
            pass

        class A:
            pass

        class B:
            pass

        @Singleton
        class RepoA(Repo[A]):
            pass

        @Singleton
        class RepoB(Repo[B]):
            pass

        container.register(RepoA)
        container.register(RepoB)

        results = container.get_all(Repo[A])

        assert len(results) == 1
        assert isinstance(results[0], RepoA)


class TestContainerGenericAnnotationInjection:
    """Tests for plain annotation injection without Inject[]."""

    def test_plain_annotation_injects_generic_dep(self, container: DIContainer) -> None:
        """repo: Repo[Item] in __init__ must be auto-resolved without Inject[]."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemRepo(Repo[Item]):
            pass

        @Singleton
        class ItemService:
            def __init__(self, repo: Repo[Item]) -> None:
                self.repo = repo

        container.register(ItemRepo)
        container.register(ItemService)

        svc = container.get(ItemService)

        assert isinstance(svc.repo, ItemRepo)

    def test_multiple_generic_deps_injected_correctly(
        self, container: DIContainer
    ) -> None:
        """Two different generic deps in __init__ must each resolve to the right impl."""

        class Repo(Generic[T]):
            pass

        class User:
            pass

        class Order:
            pass

        @Singleton
        class UserRepo(Repo[User]):
            pass

        @Singleton
        class OrderRepo(Repo[Order]):
            pass

        @Singleton
        class AppService:
            def __init__(
                self,
                users: Repo[User],
                orders: Repo[Order],
            ) -> None:
                self.users = users
                self.orders = orders

        container.register(UserRepo)
        container.register(OrderRepo)
        container.register(AppService)

        svc = container.get(AppService)

        assert isinstance(svc.users, UserRepo)
        assert isinstance(svc.orders, OrderRepo)

    def test_get_by_generic_alias_returns_correct_type(
        self, container: DIContainer
    ) -> None:
        """container.get(Repo[User]) must return an instance of UserRepo,
        even when there is also a Repo[Order] in the registry."""

        class Repo(Generic[T]):
            pass

        class User:
            pass

        class Order:
            pass

        @Singleton
        class UserRepo(Repo[User]):
            pass

        @Singleton
        class OrderRepo(Repo[Order]):
            pass

        container.register(UserRepo)
        container.register(OrderRepo)

        user_result = container.get(Repo[User])
        order_result = container.get(Repo[Order])

        assert isinstance(user_result, UserRepo)
        assert isinstance(order_result, OrderRepo)


class TestContainerGenericWithABC:
    """Tests for the combination of ABC and Generic[T]."""

    def test_abc_generic_bind_and_resolve(self, container: DIContainer) -> None:
        """ABC + Generic[T] interface must bind and resolve via generic alias."""

        class IRepo(ABC, Generic[T]):
            @abstractmethod
            def find(self, id: int) -> T: ...

        class Item:
            pass

        @Singleton
        class ItemRepo(IRepo[Item]):
            def find(self, id: int) -> Item:
                return Item()

        container.bind(IRepo[Item], ItemRepo)
        result = container.get(IRepo[Item])

        assert isinstance(result, ItemRepo)

    def test_abc_generic_scan_auto_binds(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan() must auto-bind a class to its ABC+Generic base."""

        class IRepo(ABC, Generic[T]):
            @abstractmethod
            def find(self) -> T: ...

        class Item:
            pass

        @Singleton
        class ItemRepo(IRepo[Item]):
            def find(self) -> Item:
                return Item()

        _add(fake_mod, IRepo)
        _add(fake_mod, Item)
        _add(fake_mod, ItemRepo)
        container.scan(fake_mod)

        result = container.get(IRepo[Item])
        assert isinstance(result, ItemRepo)


class TestContainerGenericMultiLevel:
    """Tests for multi-level inheritance with generic parameterisation."""

    def test_parameterisation_on_grandparent_is_found(
        self, container: DIContainer
    ) -> None:
        """If Repo[Item] is in a grandparent's __orig_bases__, the MRO walk
        must find it and the binding must be resolvable."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        # TypedItemRepo fixes the type arg one level up from the concrete class
        class TypedItemRepo(Repo[Item]):
            pass

        @Singleton
        class ConcreteItemRepo(TypedItemRepo):
            pass

        container.bind(Repo[Item], ConcreteItemRepo)
        result = container.get(Repo[Item])

        assert isinstance(result, ConcreteItemRepo)

    def test_multi_level_register_and_get(self, container: DIContainer) -> None:
        """register() + get() must work even when the generic base is indirect."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        class TypedRepo(Repo[Item]):
            pass

        @Singleton
        class ConcreteRepo(TypedRepo):
            pass

        container.register(ConcreteRepo)
        result = container.get(Repo[Item])

        assert isinstance(result, ConcreteRepo)


# ─────────────────────────────────────────────────────────────────
#  Scanner tests — generic auto-discovery
# ─────────────────────────────────────────────────────────────────


class TestScannerGenericAutoBinding:
    """Tests for scan() auto-binding of plain Generic[T] bases."""

    def test_scan_autobinds_plain_generic_base(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """A class inheriting from a plain Generic[T] (non-abstract) base must
        be bound to the parameterised alias, e.g. Repository[User]."""

        class Repository(Generic[T]):
            pass

        class User:
            pass

        @Singleton
        class UserRepository(Repository[User]):
            pass

        _add(fake_mod, Repository)
        _add(fake_mod, User)
        _add(fake_mod, UserRepository)
        container.scan(fake_mod)

        result = container.get(Repository[User])
        assert isinstance(result, UserRepository)

    def test_scan_multiple_specialisations_are_disambiguated(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Two scanned classes with different type args must resolve independently."""

        class Repository(Generic[T]):
            pass

        class User:
            pass

        class Product:
            pass

        @Singleton
        class UserRepository(Repository[User]):
            pass

        @Singleton
        class ProductRepository(Repository[Product]):
            pass

        for obj in (Repository, User, Product, UserRepository, ProductRepository):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        user_repo = container.get(Repository[User])
        product_repo = container.get(Repository[Product])

        assert isinstance(user_repo, UserRepository)
        assert isinstance(product_repo, ProductRepository)
        assert not isinstance(user_repo, ProductRepository)
        assert not isinstance(product_repo, UserRepository)

    def test_scan_three_specialisations_all_resolved(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Three different type-arg specialisations of the same generic must each
        resolve to their own implementation after a single scan() call."""

        class Handler(Generic[T]):
            pass

        class TypeA:
            pass

        class TypeB:
            pass

        class TypeC:
            pass

        @Singleton
        class HandlerA(Handler[TypeA]):
            pass

        @Singleton
        class HandlerB(Handler[TypeB]):
            pass

        @Singleton
        class HandlerC(Handler[TypeC]):
            pass

        for obj in (Handler, TypeA, TypeB, TypeC, HandlerA, HandlerB, HandlerC):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        assert isinstance(container.get(Handler[TypeA]), HandlerA)
        assert isinstance(container.get(Handler[TypeB]), HandlerB)
        assert isinstance(container.get(Handler[TypeC]), HandlerC)

    def test_scan_generic_class_is_idempotent(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Scanning the same module twice must not create duplicate generic bindings."""

        class Service(Generic[T]):
            pass

        class Item:
            pass

        @Singleton
        class ItemService(Service[Item]):
            pass

        _add(fake_mod, Service)
        _add(fake_mod, Item)
        _add(fake_mod, ItemService)

        container.scan(fake_mod)
        container.scan(fake_mod)  # second scan — must be a no-op

        matching = [
            b
            for b in container._bindings
            if isinstance(b, ClassBinding) and b.implementation is ItemService
        ]
        # Two bindings per implementation: one interface binding and one
        # exact_only self-binding.  Scanning twice must not create four.
        assert len(matching) == 2

    def test_scan_generic_base_not_abstract_still_bound(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """A plain Generic[T] base (no @abstractmethod) must be treated as a
        valid interface — inspect.isabstract() returns False for it, so the
        scanner must not fall back to self-binding in this case."""

        class Queue(Generic[T]):
            """Not abstract — no @abstractmethod."""

            pass

        class Event:
            pass

        @Component
        class EventQueue(Queue[Event]):
            pass

        _add(fake_mod, Queue)
        _add(fake_mod, Event)
        _add(fake_mod, EventQueue)
        container.scan(fake_mod)

        # Must be resolvable via the generic interface, NOT just via EventQueue itself
        result = container.get(Queue[Event])
        assert isinstance(result, EventQueue)

    def test_scan_abc_and_generic_both_bound(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """When a class inherits from both an ABC and a parameterised generic,
        the scanner must register it against the ABC AND the generic alias."""

        class IStore(ABC):
            @abstractmethod
            def save(self) -> None: ...

        class Cache(Generic[T]):
            pass

        class Record:
            pass

        @Component
        class RecordCache(IStore, Cache[Record]):
            def save(self) -> None:
                pass

        for obj in (IStore, Cache, Record, RecordCache):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        # Must be resolvable via both interfaces
        via_abc = container.get(IStore)
        via_generic = container.get(Cache[Record])

        assert isinstance(via_abc, RecordCache)
        assert isinstance(via_generic, RecordCache)

    def test_scan_get_all_generic_returns_all_impls(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """get_all(Repo[Item]) after scan must return every scanned implementation."""

        class Repo(Generic[T]):
            pass

        class Item:
            pass

        @Component(priority=1)
        class PrimaryRepo(Repo[Item]):
            pass

        @Component(priority=2)
        class SecondaryRepo(Repo[Item]):
            pass

        for obj in (Repo, Item, PrimaryRepo, SecondaryRepo):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        results = container.get_all(Repo[Item])
        assert len(results) == 2
        result_types = {type(r) for r in results}
        assert PrimaryRepo in result_types
        assert SecondaryRepo in result_types


# ─────────────────────────────────────────────────────────────────
#  Multi-parameter generic tests — Generic[T, U], Generic[T, U, V, ...]
# ─────────────────────────────────────────────────────────────────


class TestIsGenericSubtypeMultiParam:
    """Unit tests for _is_generic_subtype() with multiple type parameters."""

    def test_two_param_exact_match(self) -> None:
        """Both type args must match for a two-param generic."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        @Singleton
        class EntityModelRepo(Repo[Entity, Model]):
            pass

        assert _is_generic_subtype(EntityModelRepo, Repo[Entity, Model]) is True

    def test_two_param_first_arg_mismatch(self) -> None:
        """Mismatch in the FIRST arg must return False even if second matches."""

        class Repo(Generic[T, S]):
            pass

        class A:
            pass

        class B:
            pass

        class C:
            pass

        @Singleton
        class ABRepo(Repo[A, B]):
            pass

        # C ≠ A in position 0
        assert _is_generic_subtype(ABRepo, Repo[C, B]) is False

    def test_two_param_second_arg_mismatch(self) -> None:
        """Mismatch in the SECOND arg must return False even if first matches."""

        class Repo(Generic[T, S]):
            pass

        class A:
            pass

        class B:
            pass

        class C:
            pass

        @Singleton
        class ABRepo(Repo[A, B]):
            pass

        # C ≠ B in position 1
        assert _is_generic_subtype(ABRepo, Repo[A, C]) is False

    def test_three_param_generic_exact_match(self) -> None:
        """Three-parameter generic must match only the exact (T, U, V) combination."""

        class Mapper(Generic[T, S, U]):
            pass

        class Input:
            pass

        class Output:
            pass

        class Context:
            pass

        @Singleton
        class SpecificMapper(Mapper[Input, Output, Context]):
            pass

        assert (
            _is_generic_subtype(SpecificMapper, Mapper[Input, Output, Context]) is True
        )
        assert (
            _is_generic_subtype(SpecificMapper, Mapper[Output, Input, Context]) is False
        )

    def test_four_param_generic_exact_match(self) -> None:
        """Four-parameter generic: all four args must match exactly."""

        class Pipeline(Generic[T, S, U, V]):
            pass

        class A:
            pass

        class B:
            pass

        class C:
            pass

        class D:
            pass

        class E:
            pass

        @Singleton
        class ABCDPipeline(Pipeline[A, B, C, D]):
            pass

        assert _is_generic_subtype(ABCDPipeline, Pipeline[A, B, C, D]) is True
        # Swapping last arg must NOT match
        assert _is_generic_subtype(ABCDPipeline, Pipeline[A, B, C, E]) is False


class TestInterfaceMatchesMultiParam:
    """Unit tests for _interface_matches() with multiple type parameters."""

    def test_two_param_binding_matches_request(self) -> None:
        """Concrete impl of Repo[Entity, Model] must match request for Repo[Entity, Model]."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        @Singleton
        class EntityRepo(Repo[Entity, Model]):
            pass

        assert _interface_matches(EntityRepo, Repo[Entity, Model]) is True

    def test_two_param_partial_arg_mismatch_returns_false(self) -> None:
        """Swapping one arg out of two must not match."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        class OtherModel:
            pass

        @Singleton
        class EntityRepo(Repo[Entity, Model]):
            pass

        assert _interface_matches(EntityRepo, Repo[Entity, OtherModel]) is False

    def test_two_param_generic_binding_vs_generic_request(self) -> None:
        """Generic alias stored as binding interface must match same generic request."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        assert _interface_matches(Repo[Entity, Model], Repo[Entity, Model]) is True
        assert _interface_matches(Repo[Entity, Model], Repo[Model, Entity]) is False


class TestContainerMultiParamGeneric:
    """Container integration tests for Generic[T, U], Generic[T, U, V], etc."""

    # ── Two type parameters ────────────────────────────────────────

    def test_bind_two_param_generic(self, container: DIContainer) -> None:
        """bind(Repo[Entity, Model], EntityRepo) must resolve via get(Repo[Entity, Model])."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        @Singleton
        class EntityRepo(Repo[Entity, Model]):
            pass

        container.bind(Repo[Entity, Model], EntityRepo)
        result = container.get(Repo[Entity, Model])

        assert isinstance(result, EntityRepo)

    def test_two_param_multiple_specialisations_disambiguated(
        self, container: DIContainer
    ) -> None:
        """Two repos that share one type arg but differ in the other must each
        resolve to their own implementation — (Entity, UserModel) ≠ (Entity, AdminModel).
        """

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class UserModel:
            pass

        class AdminModel:
            pass

        @Singleton
        class UserRepo(Repo[Entity, UserModel]):
            pass

        @Singleton
        class AdminRepo(Repo[Entity, AdminModel]):
            pass

        container.register(UserRepo)
        container.register(AdminRepo)

        user_result = container.get(Repo[Entity, UserModel])
        admin_result = container.get(Repo[Entity, AdminModel])

        assert isinstance(user_result, UserRepo)
        assert isinstance(admin_result, AdminRepo)
        # Cross-access must raise
        with pytest.raises(LookupError):
            container.get(Repo[AdminModel, Entity])  # wrong arg order

    def test_wrong_arg_combination_raises_lookup_error(
        self, container: DIContainer
    ) -> None:
        """get(Repo[Entity, WrongModel]) must raise LookupError when only
        Repo[Entity, RightModel] is registered."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class RightModel:
            pass

        class WrongModel:
            pass

        @Singleton
        class RightRepo(Repo[Entity, RightModel]):
            pass

        container.register(RightRepo)

        with pytest.raises(LookupError):
            container.get(Repo[Entity, WrongModel])

    def test_two_param_plain_annotation_injection(self, container: DIContainer) -> None:
        """repo: Repo[Entity, Model] in __init__ must be auto-resolved."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        @Singleton
        class EntityRepo(Repo[Entity, Model]):
            pass

        @Singleton
        class EntityService:
            def __init__(self, repo: Repo[Entity, Model]) -> None:
                self.repo = repo

        container.register(EntityRepo)
        container.register(EntityService)

        svc = container.get(EntityService)

        assert isinstance(svc.repo, EntityRepo)

    # ── Three type parameters ──────────────────────────────────────

    def test_three_param_generic_bind_and_resolve(self, container: DIContainer) -> None:
        """Three-type-param generic must bind and resolve independently of
        two-param and single-param variants of the same origin."""

        class Mapper(Generic[T, S, U]):
            pass

        class Input:
            pass

        class Output:
            pass

        class Context:
            pass

        @Singleton
        class SpecificMapper(Mapper[Input, Output, Context]):
            pass

        container.bind(Mapper[Input, Output, Context], SpecificMapper)
        result = container.get(Mapper[Input, Output, Context])

        assert isinstance(result, SpecificMapper)

    def test_three_param_wrong_order_raises(self, container: DIContainer) -> None:
        """Correct types in wrong positions must NOT match."""

        class Mapper(Generic[T, S, U]):
            pass

        class Input:
            pass

        class Output:
            pass

        class Context:
            pass

        @Singleton
        class SpecificMapper(Mapper[Input, Output, Context]):
            pass

        container.register(SpecificMapper)

        # All three correct types but in a different order
        with pytest.raises(LookupError):
            container.get(Mapper[Output, Input, Context])

    def test_three_param_plain_annotation_injection(
        self, container: DIContainer
    ) -> None:
        """mapper: Mapper[Input, Output, Context] in __init__ must be injected."""

        class Mapper(Generic[T, S, U]):
            pass

        class Input:
            pass

        class Output:
            pass

        class Context:
            pass

        @Singleton
        class SpecificMapper(Mapper[Input, Output, Context]):
            pass

        @Singleton
        class Pipeline:
            def __init__(self, mapper: Mapper[Input, Output, Context]) -> None:
                self.mapper = mapper

        container.register(SpecificMapper)
        container.register(Pipeline)

        pipeline = container.get(Pipeline)

        assert isinstance(pipeline.mapper, SpecificMapper)

    # ── Four type parameters ───────────────────────────────────────

    def test_four_param_generic_bind_and_resolve(self, container: DIContainer) -> None:
        """Four-type-param generic must bind and resolve to the exact implementation."""

        class Stage(Generic[T, S, U, V]):
            pass

        class A:
            pass

        class B:
            pass

        class C:
            pass

        class D:
            pass

        @Singleton
        class ConcreteStage(Stage[A, B, C, D]):
            pass

        container.bind(Stage[A, B, C, D], ConcreteStage)
        result = container.get(Stage[A, B, C, D])

        assert isinstance(result, ConcreteStage)

    def test_four_param_multiple_specialisations(self, container: DIContainer) -> None:
        """Two implementations of a four-param generic must be independently
        resolvable — differing only in the last type arg is enough to distinguish them.
        """

        class Stage(Generic[T, S, U, V]):
            pass

        class A:
            pass

        class B:
            pass

        class C:
            pass

        class D1:
            pass

        class D2:
            pass

        @Singleton
        class StageD1(Stage[A, B, C, D1]):
            pass

        @Singleton
        class StageD2(Stage[A, B, C, D2]):
            pass

        container.register(StageD1)
        container.register(StageD2)

        assert isinstance(container.get(Stage[A, B, C, D1]), StageD1)
        assert isinstance(container.get(Stage[A, B, C, D2]), StageD2)


class TestScannerMultiParamGeneric:
    """Scanner tests for multi-parameter generics."""

    def test_scan_autobinds_two_param_generic(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan() must auto-bind an impl of Generic[T, S] to its parameterised base."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        @Singleton
        class EntityRepo(Repo[Entity, Model]):
            pass

        for obj in (Repo, Entity, Model, EntityRepo):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        result = container.get(Repo[Entity, Model])
        assert isinstance(result, EntityRepo)

    def test_scan_two_param_multiple_specialisations_disambiguated(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """After scanning, two impls of Repo[T, S] with different arg pairs must
        each resolve to their own class."""

        class Repo(Generic[T, S]):
            pass

        class EntityA:
            pass

        class ModelA:
            pass

        class EntityB:
            pass

        class ModelB:
            pass

        @Singleton
        class RepoAA(Repo[EntityA, ModelA]):
            pass

        @Singleton
        class RepoBB(Repo[EntityB, ModelB]):
            pass

        for obj in (Repo, EntityA, ModelA, EntityB, ModelB, RepoAA, RepoBB):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        assert isinstance(container.get(Repo[EntityA, ModelA]), RepoAA)
        assert isinstance(container.get(Repo[EntityB, ModelB]), RepoBB)

    def test_scan_three_param_generic_resolves(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan() must auto-bind a three-param generic class to its full specialisation."""

        class Handler(Generic[T, S, U]):
            pass

        class Req:
            pass

        class Res:
            pass

        class Ctx:
            pass

        @Singleton
        class ConcreteHandler(Handler[Req, Res, Ctx]):
            pass

        for obj in (Handler, Req, Res, Ctx, ConcreteHandler):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        result = container.get(Handler[Req, Res, Ctx])
        assert isinstance(result, ConcreteHandler)

    def test_scan_two_param_wrong_combo_raises(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """After scanning Repo[Entity, Model], requesting Repo[Model, Entity]
        (args swapped) must raise LookupError — order matters."""

        class Repo(Generic[T, S]):
            pass

        class Entity:
            pass

        class Model:
            pass

        @Singleton
        class EntityModelRepo(Repo[Entity, Model]):
            pass

        for obj in (Repo, Entity, Model, EntityModelRepo):
            _add(fake_mod, obj)
        container.scan(fake_mod)

        with pytest.raises(LookupError):
            container.get(Repo[Model, Entity])  # swapped args — no match
