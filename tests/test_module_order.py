"""Failing (red) tests for Plan 008 — multi-module startup/shutdown ordering (F5).

These tests describe behaviour that does NOT exist yet:
    - A new, pure ``providify/modules.py`` module exposing
      ``resolve_install_order()`` / ``module_dependencies()`` / ``ModuleCycleError``.
    - The ``@Configuration`` decorator's dual form (bare / call-with-kwargs)
      and its new ``depends_on=`` parameter.

See plans/008-module-startup-ordering.md, Steps 1 and 5.
"""

from __future__ import annotations

import pytest

from providify.decorator.module import Configuration
from providify.modules import (
    ModuleCycleError,
    module_dependencies,
    resolve_install_order,
)

# ─────────────────────────────────────────────────────────────────
#  Step 1 — pure ordering function (no container)
# ─────────────────────────────────────────────────────────────────


class TestResolveInstallOrder:
    def test_linear_chain_orders_deps_first(self) -> None:
        """C -> B -> A must install A, then B, then C."""

        @Configuration
        class A: ...

        @Configuration(depends_on=[A])
        class B: ...

        @Configuration(depends_on=[B])
        class C: ...

        assert resolve_install_order([C]) == [A, B, C]

    def test_diamond_installs_shared_dep_once_A_first_D_last(self) -> None:
        """D depends on (B, C), both depend on A: A first, D last, B before C
        (declaration order preserved, deterministic)."""

        @Configuration
        class A: ...

        @Configuration(depends_on=[A])
        class B: ...

        @Configuration(depends_on=[A])
        class C: ...

        @Configuration(depends_on=[B, C])
        class D: ...

        order = resolve_install_order([D])

        assert order[0] is A
        assert order[-1] is D
        assert order.index(B) < order.index(C)
        assert order.count(A) == 1

    def test_module_with_no_depends_on_returns_itself_only(self) -> None:
        """A bare @Configuration module with no dependencies is a singleton list."""

        @Configuration
        class Solo: ...

        assert resolve_install_order([Solo]) == [Solo]

    def test_two_roots_sharing_a_dependency_install_it_once(self) -> None:
        """Two roots that both depend on the same module must install it once,
        not once per root."""

        @Configuration
        class Shared: ...

        @Configuration(depends_on=[Shared])
        class RootA: ...

        @Configuration(depends_on=[Shared])
        class RootB: ...

        order = resolve_install_order([RootA, RootB])

        assert order.count(Shared) == 1
        assert order.index(Shared) < order.index(RootA)
        assert order.index(Shared) < order.index(RootB)

    def test_self_dependency_raises_module_cycle_error(self) -> None:
        """A module depending on itself is a length-2 cycle."""
        from providify.metadata import _DI_CONFIGURATION_ATTR, ConfigurationMetadata

        @Configuration
        class Self_: ...

        # Patch the marker after the fact so it can name the class itself —
        # the class object does not exist yet inside its own class body.
        setattr(
            Self_, _DI_CONFIGURATION_ATTR, ConfigurationMetadata(depends_on=(Self_,))
        )

        with pytest.raises(ModuleCycleError):
            resolve_install_order([Self_])

    def test_three_cycle_raises_module_cycle_error_naming_all_three(self) -> None:
        """A -> B -> C -> A must raise ModuleCycleError whose .cycle names all
        three modules in order."""

        # Forward-reference dance: declare bare classes first, then patch
        # depends_on via re-decoration once every class object exists.
        @Configuration
        class A: ...

        @Configuration
        class B: ...

        @Configuration
        class C: ...

        A = Configuration(depends_on=[C])(A)
        B = Configuration(depends_on=[A])(B)
        C = Configuration(depends_on=[B])(C)

        with pytest.raises(ModuleCycleError) as exc_info:
            resolve_install_order([A])

        cycle = exc_info.value.cycle
        assert set(cycle) == {A, B, C}
        assert len(cycle) >= 3

    def test_depends_on_naming_non_configuration_class_raises_type_error(self) -> None:
        """depends_on referencing a class without @Configuration must raise
        TypeError naming it."""

        class NotAModule: ...

        @Configuration(depends_on=[NotAModule])
        class M: ...

        with pytest.raises(TypeError, match="NotAModule"):
            resolve_install_order([M])


class TestModuleDependencies:
    def test_reads_depends_on_from_configuration_metadata(self) -> None:
        @Configuration
        class A: ...

        @Configuration(depends_on=[A])
        class B: ...

        assert module_dependencies(B) == (A,)

    def test_module_with_no_depends_on_returns_empty_tuple(self) -> None:
        @Configuration
        class Solo: ...

        assert module_dependencies(Solo) == ()


# ─────────────────────────────────────────────────────────────────
#  Step 5 — @Configuration decorator dual form
# ─────────────────────────────────────────────────────────────────


class TestConfigurationDecoratorDualForm:
    def test_bare_usage_returns_class_with_empty_depends_on(self) -> None:
        @Configuration
        class Bare: ...

        assert module_dependencies(Bare) == ()

    def test_empty_parens_usage_works_identically_to_bare(self) -> None:
        @Configuration()
        class EmptyParens: ...

        assert module_dependencies(EmptyParens) == ()

    def test_depends_on_kwarg_stores_tuple(self) -> None:
        @Configuration
        class A: ...

        @Configuration(depends_on=[A])
        class B: ...

        assert module_dependencies(B) == (A,)

    def test_depends_on_single_class_normalizes_to_tuple(self) -> None:
        @Configuration
        class A: ...

        @Configuration(depends_on=A)
        class B: ...

        assert module_dependencies(B) == (A,)

    def test_depends_on_list_normalizes_to_tuple(self) -> None:
        @Configuration
        class A: ...

        @Configuration
        class B: ...

        @Configuration(depends_on=[A, B])
        class C: ...

        assert module_dependencies(C) == (A, B)

    def test_depends_on_tuple_normalizes_to_tuple(self) -> None:
        @Configuration
        class A: ...

        @Configuration
        class B: ...

        @Configuration(depends_on=(A, B))
        class C: ...

        assert module_dependencies(C) == (A, B)

    def test_marker_is_not_inherited_by_subclass(self) -> None:
        """Configuration metadata is looked up via __dict__, not inherited
        attribute resolution — a plain subclass of a @Configuration class is
        not itself a module."""

        @Configuration
        class Parent: ...

        class Child(Parent): ...

        assert module_dependencies(Child) == ()
