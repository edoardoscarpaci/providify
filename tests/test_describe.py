"""Unit tests for ClassBinding.describe(), ProviderBinding.describe(),
and the BindingDescriptor data-class.

WHY MODULE-LEVEL FIXTURES:
  All providify classes are defined at module level (not inside test functions)
  because ``from __future__ import annotations`` turns every annotation into a
  lazy string.  ``get_type_hints()`` resolves those strings from the function's
  ``__globals__``, which is the *module* namespace — not the local scope of the
  enclosing test.  Classes defined inside a test function are invisible to
  ``get_type_hints()``, so the container would silently skip their constructor
  parameters.

WHY Inject[T] SYNTAX:
  ``_collect_dependencies`` (called by ``_get_dependencies``) only recognises
  ``Annotated[T, InjectMeta(...)]`` hints — produced by the ``Inject[T]`` alias.
  Plain type-hints (e.g. ``def __init__(self, dep: MyClass)``) are invisible to
  this layer, so all constructor parameters in these fixtures use ``Inject[T]``.

Covered:
    - ClassBinding.describe() with no dependencies
    - ClassBinding.describe() with a single Inject[T] dependency
    - ClassBinding.describe() with a three-level chain (nested descriptor tree)
    - ClassBinding.describe() with a two-class cycle → [CYCLE DETECTED] sentinel
    - ClassBinding.describe() qualifier propagated into descriptor
    - Scope-leak flag: SINGLETON parent → DEPENDENT dep → scope_leak=True
    - ProviderBinding.describe() with no dependencies
    - ProviderBinding.describe() with an injected parameter
    - BindingDescriptor.__repr__ renders an ASCII tree with correct connectors
    - BindingDescriptor.to_dict() serialises the full tree to a plain dict
    - BindingDescriptor.scope_leak property logic (True/False/no-deps cases)
    - _get_dependencies with _visited filters out already-seen interfaces
"""

from __future__ import annotations


from providify.binding import BindingDescriptor, ClassBinding, ProviderBinding
from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton
from providify.metadata import Scope
from providify.type import Inject


# ─────────────────────────────────────────────────────────────────
#  Leaf — no constructor parameters; acts as the bottom of any dep chain.
# ─────────────────────────────────────────────────────────────────


@Component
class _DescLeaf:
    """No constructor params — produces an empty dependencies tuple."""


# ─────────────────────────────────────────────────────────────────
#  Middle — one Inject[T] dep (→ _DescLeaf).
# ─────────────────────────────────────────────────────────────────


@Component
class _DescMiddle:
    """One-level dep: uses Inject[_DescLeaf] so _collect_dependencies sees it."""

    def __init__(self, leaf: Inject[_DescLeaf]) -> None:
        self.leaf = leaf


# ─────────────────────────────────────────────────────────────────
#  Root → Middle → Leaf  (three-level chain)
# ─────────────────────────────────────────────────────────────────


@Component
class _DescRoot:
    """Root of a three-level chain; depends on _DescMiddle → _DescLeaf."""

    def __init__(self, middle: Inject[_DescMiddle]) -> None:
        self.middle = middle


# ─────────────────────────────────────────────────────────────────
#  Scope-leak fixtures:
#    _DescSingletonParent (SINGLETON) → _DescLeaf (DEPENDENT)
#  SINGLETON outlives DEPENDENT, so the dep has a narrower scope.
# ─────────────────────────────────────────────────────────────────


@Singleton
class _DescSingletonParent:
    """SINGLETON that directly injects a DEPENDENT dep — scope leak scenario."""

    def __init__(self, leaf: Inject[_DescLeaf]) -> None:
        self.leaf = leaf


# ─────────────────────────────────────────────────────────────────
#  Qualifier fixture
# ─────────────────────────────────────────────────────────────────


@Component(qualifier="primary")
class _DescQualified(_DescLeaf):
    """@Component with qualifier='primary' — tests qualifier propagation."""


# ─────────────────────────────────────────────────────────────────
#  Cycle fixtures: _DescCycleA → _DescCycleB → _DescCycleA
#
#  DESIGN: forward-reference to _DescCycleB in _DescCycleA is safe because
#  ``from __future__ import annotations`` makes all annotations lazy strings,
#  and both classes are in module globals by the time get_type_hints() runs.
# ─────────────────────────────────────────────────────────────────


@Component
class _DescCycleA:
    """First leg of a two-class cycle: A → B → A."""

    def __init__(self, b: Inject[_DescCycleB]) -> None:
        self.b = b


@Component
class _DescCycleB:
    """Second leg of a two-class cycle: A → B → A."""

    def __init__(self, a: Inject[_DescCycleA]) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  ClassBinding.describe() tests
# ─────────────────────────────────────────────────────────────────


class TestClassBindingDescribe:
    """Tests for ClassBinding.describe() and the BindingDescriptor it produces."""

    def test_describe_no_deps_returns_empty_dependencies(
        self, container: DIContainer
    ) -> None:
        """A binding with no constructor params must produce an empty dependencies tuple.

        Edge cases: _DescLeaf has no __init__ parameters — dependencies must
        be an empty tuple, not None.
        """
        container.register(_DescLeaf)
        binding = ClassBinding(_DescLeaf, _DescLeaf)

        descriptor = binding.describe(container)

        # No deps at all — tuple is empty, not None or []
        assert descriptor.dependencies == ()

    def test_describe_interface_and_implementation_names(
        self, container: DIContainer
    ) -> None:
        """Descriptor must carry the correct interface and implementation names."""
        container.register(_DescLeaf)
        binding = ClassBinding(_DescLeaf, _DescLeaf)

        descriptor = binding.describe(container)

        assert descriptor.interface == "_DescLeaf"
        assert descriptor.implementation == "_DescLeaf"

    def test_describe_scope_matches_binding(self, container: DIContainer) -> None:
        """Scope on the descriptor must equal the binding's scope."""
        container.register(_DescLeaf)
        binding = ClassBinding(_DescLeaf, _DescLeaf)

        descriptor = binding.describe(container)

        # _DescLeaf is @Component → DEPENDENT scope
        assert descriptor.scope == Scope.DEPENDENT

    def test_describe_single_dep_included(self, container: DIContainer) -> None:
        """Binding with one Inject[T] dep must include exactly one child descriptor."""
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        binding = ClassBinding(_DescMiddle, _DescMiddle)

        descriptor = binding.describe(container)

        # One direct dep: _DescLeaf
        assert len(descriptor.dependencies) == 1
        assert descriptor.dependencies[0].interface == "_DescLeaf"

    def test_describe_three_level_chain_nested(self, container: DIContainer) -> None:
        """Three-level chain Root → Middle → Leaf must produce a fully nested tree.

        DESIGN: describe() recurses into each dep's own describe() call, so
        the full subtree is built eagerly at describe-time.
        """
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        container.register(_DescRoot)
        binding = ClassBinding(_DescRoot, _DescRoot)

        descriptor = binding.describe(container)

        # Root → Middle
        assert len(descriptor.dependencies) == 1
        middle = descriptor.dependencies[0]
        assert middle.interface == "_DescMiddle"

        # Middle → Leaf
        assert len(middle.dependencies) == 1
        leaf = middle.dependencies[0]
        assert leaf.interface == "_DescLeaf"
        assert leaf.dependencies == ()

    def test_describe_cycle_returns_cycle_detected_sentinel(
        self, container: DIContainer
    ) -> None:
        """A → B → A cycle must produce a [CYCLE DETECTED] sentinel instead of recursing.

        DESIGN: describe() maintains a frozenset of visited interfaces.  When
        it encounters an interface already in the set, it returns a sentinel
        BindingDescriptor rather than recursing infinitely.
        """
        container.register(_DescCycleA)
        container.register(_DescCycleB)
        binding = ClassBinding(_DescCycleA, _DescCycleA)

        # Must not raise RecursionError
        descriptor = binding.describe(container)

        # A → B is the first dep
        assert len(descriptor.dependencies) == 1
        b_desc = descriptor.dependencies[0]
        assert b_desc.interface == "_DescCycleB"

        # B → A is the second dep — but A is already visited; sentinel is returned
        assert len(b_desc.dependencies) == 1
        cycle_sentinel = b_desc.dependencies[0]
        assert "CYCLE DETECTED" in cycle_sentinel.interface

    def test_describe_qualifier_stored_on_descriptor(
        self, container: DIContainer
    ) -> None:
        """Qualifier from @Component(qualifier=...) must appear on the descriptor."""
        container.register(_DescQualified)
        binding = ClassBinding(_DescQualified, _DescQualified)

        descriptor = binding.describe(container)

        assert descriptor.qualifier == "primary"

    def test_describe_scope_leak_singleton_over_dependent(
        self, container: DIContainer
    ) -> None:
        """SINGLETON depending on DEPENDENT dep must report scope_leak=True.

        DESIGN: scope_leak is a computed property on BindingDescriptor — it
        compares each direct dep's scope_rank() to the parent's scope_rank().
        SINGLETON has a higher rank than DEPENDENT, so injecting a DEPENDENT
        dep into a SINGLETON is a leak.
        """
        container.register(_DescLeaf)
        container.register(_DescSingletonParent)
        binding = ClassBinding(_DescSingletonParent, _DescSingletonParent)

        descriptor = binding.describe(container)

        # _DescSingletonParent is SINGLETON, _DescLeaf is DEPENDENT — leak
        assert descriptor.scope_leak is True

    def test_describe_no_scope_leak_for_same_scope(
        self, container: DIContainer
    ) -> None:
        """Same scope parent and dep must produce scope_leak=False."""
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        binding = ClassBinding(_DescMiddle, _DescMiddle)

        descriptor = binding.describe(container)

        # Both are DEPENDENT — no leak
        assert descriptor.scope_leak is False

    def test_describe_no_scope_leak_when_no_deps(self, container: DIContainer) -> None:
        """Binding with no deps must report scope_leak=False."""
        container.register(_DescLeaf)
        binding = ClassBinding(_DescLeaf, _DescLeaf)

        descriptor = binding.describe(container)

        assert descriptor.scope_leak is False


# ─────────────────────────────────────────────────────────────────
#  ProviderBinding.describe() tests
# ─────────────────────────────────────────────────────────────────


class TestProviderBindingDescribe:
    """Tests for ProviderBinding.describe() and the BindingDescriptor it produces."""

    def test_provider_describe_no_deps(self, container: DIContainer) -> None:
        """@Provider with no parameters must produce an empty dependencies tuple."""

        @Provider
        def make_leaf() -> _DescLeaf:
            return _DescLeaf()

        container.provide(make_leaf)
        binding = ProviderBinding(make_leaf)

        descriptor = binding.describe(container)

        assert descriptor.interface == "_DescLeaf"
        assert descriptor.implementation == "make_leaf"
        assert descriptor.dependencies == ()

    def test_provider_describe_with_injected_dep(self, container: DIContainer) -> None:
        """@Provider with one Inject[T] parameter must list that dep in its descriptor.

        DESIGN: ProviderBinding.describe() calls _get_dependencies() which inspects
        the provider function's parameter annotations — same path as ClassBinding.
        """
        container.register(_DescLeaf)

        @Provider
        def make_middle(leaf: Inject[_DescLeaf]) -> _DescMiddle:
            return _DescMiddle.__new__(_DescMiddle)

        container.provide(make_middle)
        binding = ProviderBinding(make_middle)

        descriptor = binding.describe(container)

        # Provider function name used as "implementation"
        assert descriptor.implementation == "make_middle"
        assert len(descriptor.dependencies) == 1
        assert descriptor.dependencies[0].interface == "_DescLeaf"

    def test_provider_describe_scope_singleton(self, container: DIContainer) -> None:
        """@Provider(singleton=True) must produce a SINGLETON descriptor."""

        @Provider(singleton=True)
        def make_singleton_leaf() -> _DescLeaf:
            return _DescLeaf()

        container.provide(make_singleton_leaf)
        binding = ProviderBinding(make_singleton_leaf)

        descriptor = binding.describe(container)

        assert descriptor.scope == Scope.SINGLETON


# ─────────────────────────────────────────────────────────────────
#  BindingDescriptor unit tests (no container needed — direct construction)
# ─────────────────────────────────────────────────────────────────


class TestBindingDescriptorRepr:
    """Tests for BindingDescriptor.__repr__ ASCII tree rendering."""

    def test_repr_contains_interface_name(self) -> None:
        """Root node must include the interface name."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.DEPENDENT,
        )

        assert "MyService" in repr(d)

    def test_repr_contains_implementation_name(self) -> None:
        """Root node must include the implementation name (right of →)."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.DEPENDENT,
        )

        assert "MyServiceImpl" in repr(d)

    def test_repr_contains_scope_name(self) -> None:
        """Scope must appear in bracket notation e.g. [SINGLETON]."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.SINGLETON,
        )

        assert "SINGLETON" in repr(d)

    def test_repr_single_dep_uses_last_child_connector(self) -> None:
        """A single child dep must use the └── connector (last child)."""
        child = BindingDescriptor(
            interface="DepA",
            implementation="DepAImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="Parent",
            implementation="ParentImpl",
            scope=Scope.DEPENDENT,
            dependencies=(child,),
        )

        tree = repr(parent)

        # Last (and only) child uses └──
        assert "└──" in tree

    def test_repr_multiple_deps_uses_fork_connector_for_non_last(self) -> None:
        """Non-last children must use the ├── connector."""
        child_a = BindingDescriptor(
            interface="DepA",
            implementation="DepAImpl",
            scope=Scope.DEPENDENT,
        )
        child_b = BindingDescriptor(
            interface="DepB",
            implementation="DepBImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="Parent",
            implementation="ParentImpl",
            scope=Scope.DEPENDENT,
            dependencies=(child_a, child_b),
        )

        tree = repr(parent)

        # First dep (non-last) → ├──; last dep → └──
        assert "├──" in tree
        assert "└──" in tree

    def test_repr_scope_leak_flag_present(self) -> None:
        """⚠️ SCOPE LEAK must appear in the repr when the dep is shorter-lived."""
        # SINGLETON parent, DEPENDENT dep → dep is shorter-lived → leak
        dep = BindingDescriptor(
            interface="ShortLived",
            implementation="ShortLivedImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="LongLived",
            implementation="LongLivedImpl",
            scope=Scope.SINGLETON,
            dependencies=(dep,),
        )

        tree = repr(parent)

        assert "SCOPE LEAK" in tree

    def test_repr_no_scope_leak_flag_for_same_scope(self) -> None:
        """⚠️ SCOPE LEAK must NOT appear when parent and dep have the same scope."""
        dep = BindingDescriptor(
            interface="DepA",
            implementation="DepAImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="Parent",
            implementation="ParentImpl",
            scope=Scope.DEPENDENT,
            dependencies=(dep,),
        )

        tree = repr(parent)

        assert "SCOPE LEAK" not in tree


class TestBindingDescriptorToDict:
    """Tests for BindingDescriptor.to_dict() JSON-serialisable output."""

    def test_to_dict_flat_contains_required_keys(self) -> None:
        """to_dict() must include all required top-level keys."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.DEPENDENT,
        )

        result = d.to_dict()

        required_keys = {
            "interface",
            "implementation",
            "scope",
            "qualifier",
            "scope_leak",
            "dependencies",
        }
        assert required_keys.issubset(result.keys())

    def test_to_dict_scope_stored_as_string_name(self) -> None:
        """Scope must be serialised as the string name, not an IntEnum value.

        DESIGN: storing the name makes JSON output human-readable without needing
        to know the IntEnum mapping.
        """
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.SINGLETON,
        )

        result = d.to_dict()

        assert result["scope"] == "SINGLETON"

    def test_to_dict_empty_deps_is_empty_list(self) -> None:
        """No-dep binding must produce an empty 'dependencies' list, not None."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.DEPENDENT,
        )

        result = d.to_dict()

        assert result["dependencies"] == []

    def test_to_dict_nested_deps_recursively_serialised(self) -> None:
        """Nested deps must appear as nested dicts inside 'dependencies' list."""
        child = BindingDescriptor(
            interface="DepA",
            implementation="DepAImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="Parent",
            implementation="ParentImpl",
            scope=Scope.DEPENDENT,
            dependencies=(child,),
        )

        result = parent.to_dict()

        assert len(result["dependencies"]) == 1
        dep_dict = result["dependencies"][0]
        assert dep_dict["interface"] == "DepA"
        assert dep_dict["scope"] == "DEPENDENT"

    def test_to_dict_qualifier_included_when_set(self) -> None:
        """qualifier field must reflect the value passed at construction time."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.DEPENDENT,
            qualifier="smtp",
        )

        result = d.to_dict()

        assert result["qualifier"] == "smtp"

    def test_to_dict_qualifier_none_when_not_set(self) -> None:
        """qualifier must be None in the dict when not provided."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.DEPENDENT,
        )

        result = d.to_dict()

        assert result["qualifier"] is None

    def test_to_dict_scope_leak_true_when_dep_shorter_lived(self) -> None:
        """scope_leak key must be True when the binding has a shorter-lived dep."""
        dep = BindingDescriptor(
            interface="ShortLived",
            implementation="ShortLivedImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="LongLived",
            implementation="LongLivedImpl",
            scope=Scope.SINGLETON,
            dependencies=(dep,),
        )

        result = parent.to_dict()

        assert result["scope_leak"] is True

    def test_to_dict_scope_leak_false_no_deps(self) -> None:
        """scope_leak must be False when there are no dependencies."""
        d = BindingDescriptor(
            interface="MyService",
            implementation="MyServiceImpl",
            scope=Scope.SINGLETON,
        )

        result = d.to_dict()

        assert result["scope_leak"] is False


class TestBindingDescriptorScopeLeak:
    """Tests for the scope_leak computed property."""

    def test_scope_leak_false_when_no_deps(self) -> None:
        """scope_leak is always False with an empty dependency tuple."""
        d = BindingDescriptor(
            interface="S",
            implementation="SImpl",
            scope=Scope.SINGLETON,
        )

        assert d.scope_leak is False

    def test_scope_leak_false_when_dep_same_scope(self) -> None:
        """Equal scope ranks must NOT trigger a leak — only strictly narrower."""
        dep = BindingDescriptor(
            interface="D",
            implementation="DImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="P",
            implementation="PImpl",
            scope=Scope.DEPENDENT,
            dependencies=(dep,),
        )

        assert parent.scope_leak is False

    def test_scope_leak_true_singleton_over_dependent(self) -> None:
        """SINGLETON parent with DEPENDENT dep — classic scope leak."""
        dep = BindingDescriptor(
            interface="D",
            implementation="DImpl",
            scope=Scope.DEPENDENT,
        )
        parent = BindingDescriptor(
            interface="P",
            implementation="PImpl",
            scope=Scope.SINGLETON,
            dependencies=(dep,),
        )

        assert parent.scope_leak is True

    def test_scope_leak_false_when_dep_outlives_parent(self) -> None:
        """dep_scope > parent_scope (dep outlives parent) must NOT be a leak.

        e.g. a DEPENDENT service injecting a SINGLETON helper is safe —
        the singleton outlives every request, so there is no premature eviction.
        """
        dep = BindingDescriptor(
            interface="Singleton",
            implementation="SingletonImpl",
            scope=Scope.SINGLETON,
        )
        parent = BindingDescriptor(
            interface="ShortLived",
            implementation="ShortLivedImpl",
            scope=Scope.DEPENDENT,
            dependencies=(dep,),
        )

        # dep outlives parent → safe, not a leak
        assert parent.scope_leak is False


# ─────────────────────────────────────────────────────────────────
#  _get_dependencies cycle safety (_visited parameter)
# ─────────────────────────────────────────────────────────────────


class TestGetDependenciesCycleSafety:
    """Tests for the _visited parameter on DIContainer._get_dependencies().

    Verifies that passing _visited filters out already-seen interfaces so that
    raw recursive callers can traverse the graph without infinite loops.

    NOTE: describe() does NOT pass _visited to _get_dependencies — it relies on
    its own cycle guard to build [CYCLE DETECTED] sentinels.  These tests cover
    the standalone _visited behaviour for external callers.
    """

    def test_no_visited_returns_all_deps(self, container: DIContainer) -> None:
        """Without _visited, _get_dependencies returns all deps (baseline)."""
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        binding = ClassBinding(_DescMiddle, _DescMiddle)

        deps = container._get_dependencies(binding)

        # _DescMiddle depends on _DescLeaf via Inject[_DescLeaf]
        assert len(deps) == 1
        assert deps[0].interface is _DescLeaf

    def test_visited_filters_out_cyclic_dep(self, container: DIContainer) -> None:
        """_visited containing the dep's interface must exclude that dep.

        Simulates calling _get_dependencies mid-traversal when the caller has
        already visited _DescLeaf.  The dep should be filtered out so the
        caller does not loop back.
        """
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        binding = ClassBinding(_DescMiddle, _DescMiddle)

        # _DescLeaf is already "visited" — its binding should be excluded
        deps = container._get_dependencies(binding, _visited=frozenset({_DescLeaf}))

        assert deps == []

    def test_visited_empty_frozenset_returns_all_deps(
        self, container: DIContainer
    ) -> None:
        """An empty frozenset is equivalent to _visited=None — no filtering."""
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        binding = ClassBinding(_DescMiddle, _DescMiddle)

        deps = container._get_dependencies(binding, _visited=frozenset())

        assert len(deps) == 1

    def test_visited_does_not_filter_unrelated_types(
        self, container: DIContainer
    ) -> None:
        """Types in _visited that are NOT deps of this binding are irrelevant."""
        container.register(_DescLeaf)
        container.register(_DescMiddle)
        binding = ClassBinding(_DescMiddle, _DescMiddle)

        # _DescRoot is in visited, but _DescMiddle depends on _DescLeaf, not _DescRoot
        deps = container._get_dependencies(binding, _visited=frozenset({_DescRoot}))

        assert len(deps) == 1
        assert deps[0].interface is _DescLeaf

    def test_visited_enables_safe_recursive_graph_traversal(
        self, container: DIContainer
    ) -> None:
        """_visited allows a recursive graph walk to terminate on cycles.

        Simulates what an external tool might do to collect ALL unique dep
        interfaces without a stack overflow on A → B → A.
        """
        container.register(_DescCycleA)
        container.register(_DescCycleB)

        collected: list[type] = []

        def traverse(binding: ClassBinding, visited: frozenset[type]) -> None:
            # Mark this binding's interface as visited before recursing.
            new_visited = visited | {binding.interface}
            for dep in container._get_dependencies(binding, _visited=new_visited):
                collected.append(dep.interface)
                if isinstance(dep, ClassBinding):
                    traverse(dep, new_visited)  # type: ignore[arg-type]

        a_binding = ClassBinding(_DescCycleA, _DescCycleA)
        traverse(a_binding, frozenset({_DescCycleA}))

        # Only _DescCycleB is collected — _DescCycleA is filtered as cyclic
        assert _DescCycleB in collected
        assert collected.count(_DescCycleB) == 1  # visited only once, no loop
