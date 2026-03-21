"""Unit tests for Inject[T], InjectInstances[T], and optional injection.

Covered:
    - Inject[T]: constructor parameter gets its dependency resolved automatically
    - InjectInstances[T]: parameter receives a list of all matching bindings
    - Inject(T, qualifier=...): named qualifier forwarded to container.get()
    - Inject(T, optional=True): returns None when no binding is found
    - Inject(T, optional=False, default): raises LookupError when binding is missing
    - Annotated[T, InjectMeta] vs plain T: only annotated hints get special treatment
    - Class-level annotations (var: Inject[T]): injected after construction
    - Mixed class-level + constructor injection (user's primary use case)
    - Constructor param wins over same-named class-level annotation
    - Type alias runtime expansion: Inject[T] → Annotated[T, InjectMeta()]
    - get_type_hints() resolves Inject[T] correctly under from __future__ import annotations

DESIGN NOTE: Inject[T] and InjectInstances[T] are pure type-hint constructs —
they expand to Annotated[T, InjectMeta(...)]. The container detects the marker
in _resolve_hint_sync and acts on the metadata.
"""

from __future__ import annotations
import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component
from providify.type import Inject, InjectInstances, Lazy, Live, LazyProxy, LiveProxy


# ─────────────────────────────────────────────────────────────────
#  Domain types
# ─────────────────────────────────────────────────────────────────


class Storage:
    """Abstract-style interface for storage backends."""


@Component
class FileStorage(Storage):
    """Concrete filesystem-based storage."""


@Component(qualifier="cloud")
class CloudStorage(Storage):
    """Concrete cloud-based storage — qualifier='cloud'."""


@Component(priority=1)
class LowPriorityStorage(Storage):
    """Low-priority storage — used in get_all ordering tests."""


@Component(priority=2)
class HigherPriorityStorage(Storage):
    """Higher-priority storage — comes after LowPriorityStorage in get_all."""


# ─────────────────────────────────────────────────────────────────
#  Inject[T] tests
# ─────────────────────────────────────────────────────────────────


class TestInject:
    """Tests for the Inject[T] annotation-based injection."""

    def test_inject_parameter_receives_instance(self, container: DIContainer) -> None:
        """A constructor parameter typed Inject[Storage] should be automatically injected."""

        @Component
        class Service:
            def __init__(self, store: Inject[Storage]) -> None:
                self.store = store

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, FileStorage)

    def test_inject_with_qualifier_selects_named_binding(
        self, container: DIContainer
    ) -> None:
        """Inject(T, qualifier='cloud') should resolve only the 'cloud' qualified binding."""

        @Component
        class Service:
            def __init__(
                self,
                store: Inject(Storage, qualifier="cloud"),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.bind(Storage, FileStorage)
        container.bind(Storage, CloudStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, CloudStorage)

    def test_inject_with_priority_selects_exact_priority(
        self, container: DIContainer
    ) -> None:
        """Inject(T, priority=2) should resolve only the binding with priority=2."""

        @Component
        class Service:
            def __init__(
                self,
                store: Inject(Storage, priority=2),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.bind(Storage, LowPriorityStorage)
        container.bind(Storage, HigherPriorityStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, HigherPriorityStorage)

    def test_inject_optional_returns_none_when_absent(
        self, container: DIContainer
    ) -> None:
        """Inject(T, optional=True) should inject None when no binding is registered."""

        @Component
        class Service:
            def __init__(
                self,
                store: Inject(Storage, optional=True),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.register(Service)  # Storage is NOT registered

        svc = container.get(Service)

        assert svc.store is None

    def test_inject_optional_false_raises_when_absent(
        self, container: DIContainer
    ) -> None:
        """Inject(T, optional=False) should raise LookupError when binding is missing."""

        @Component
        class Service:
            def __init__(
                self,
                # optional=False is the default — fail-fast
                store: Inject(Storage, optional=False),  # type: ignore[valid-type]
            ) -> None:
                self.store = store

        container.register(Service)  # Storage is NOT registered

        with pytest.raises(LookupError):
            container.get(Service)

    def test_plain_type_annotation_also_resolves(self, container: DIContainer) -> None:
        """Plain type annotations (without Inject[]) are also auto-injected
        when a matching binding exists — Inject[] is only needed for extra options.
        """

        @Component
        class Service:
            def __init__(self, store: Storage) -> None:
                self.store = store

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, FileStorage)


# ─────────────────────────────────────────────────────────────────
#  InjectInstances[T] tests
# ─────────────────────────────────────────────────────────────────


class TestInjectInstances:
    """Tests for the InjectInstances[T] multi-binding injection."""

    def test_receives_all_matching_bindings_as_list(
        self, container: DIContainer
    ) -> None:
        """InjectInstances[T] should inject a list containing every bound implementation."""

        @Component
        class Service:
            def __init__(self, stores: InjectInstances[Storage]) -> None:
                self.stores = stores

        container.bind(Storage, FileStorage)
        container.bind(Storage, CloudStorage)
        container.register(Service)

        svc = container.get(Service)

        assert len(svc.stores) == 2
        types = {type(s) for s in svc.stores}
        assert FileStorage in types
        assert CloudStorage in types

    def test_list_is_sorted_by_priority(self, container: DIContainer) -> None:
        """InjectInstances should return implementations ordered by ascending priority."""

        @Component
        class Service:
            def __init__(self, stores: InjectInstances[Storage]) -> None:
                self.stores = stores

        container.bind(Storage, LowPriorityStorage)  # priority=1
        container.bind(Storage, HigherPriorityStorage)  # priority=2
        container.register(Service)

        svc = container.get(Service)

        # Sorted ascending: lowest priority number first
        assert isinstance(svc.stores[0], LowPriorityStorage)
        assert isinstance(svc.stores[1], HigherPriorityStorage)

    def test_inject_instances_with_qualifier(self, container: DIContainer) -> None:
        """InjectInstances(T, qualifier=...) should filter by qualifier."""

        @Component
        class Service:
            def __init__(
                self,
                stores: InjectInstances(Storage, qualifier="cloud"),
            ) -> None:
                self.stores = stores

        container.bind(Storage, FileStorage)  # qualifier=None
        container.bind(Storage, CloudStorage)  # qualifier="cloud"
        container.register(Service)

        svc = container.get(Service)

        assert len(svc.stores) == 1
        assert isinstance(svc.stores[0], CloudStorage)


# ─────────────────────────────────────────────────────────────────
#  Class-level injection tests
# ─────────────────────────────────────────────────────────────────


class TestClassVarInjection:
    """Tests for class-level annotated attribute injection.

    Class-level annotations like ``var: Inject[T]`` are resolved and set
    on instances *after* the constructor runs but before @PostConstruct fires.
    """

    def test_inject_classvar_resolved_after_construction(
        self, container: DIContainer
    ) -> None:
        """A class-level Inject[T] annotation should be set on the instance."""

        @Component
        class Service:
            store: Inject[Storage]

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, FileStorage)

    def test_live_classvar_is_live_proxy(self, container: DIContainer) -> None:
        """A class-level Live[T] annotation should receive a LiveProxy."""

        @Component
        class Service:
            store: Live[Storage]

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        # LiveProxy — not the Storage directly
        assert isinstance(svc.store, LiveProxy)
        # Proxy resolves to the correct instance on demand
        assert isinstance(svc.store.get(), FileStorage)

    def test_lazy_classvar_is_lazy_proxy(self, container: DIContainer) -> None:
        """A class-level Lazy[T] annotation should receive a LazyProxy."""

        @Component
        class Service:
            store: Lazy[Storage]

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        # LazyProxy — not the Storage directly
        assert isinstance(svc.store, LazyProxy)
        # Proxy resolves lazily on first .get() call
        assert isinstance(svc.store.get(), FileStorage)

    def test_mixed_classvar_and_constructor_injection(
        self, container: DIContainer
    ) -> None:
        """Class-level and constructor-level Inject[T] are both resolved independently."""

        class Logger:
            """Auxiliary service resolved via class-level annotation."""

        @Component
        class ConcreteLogger(Logger):
            pass

        @Component
        class Service:
            # Class-level annotation — injected after construction
            logger: Inject[Logger]

            # Constructor parameter — injected into __init__
            def __init__(self, store: Inject[Storage]) -> None:
                self.store = store

        container.bind(Storage, FileStorage)
        container.bind(Logger, ConcreteLogger)
        container.register(Service)

        svc = container.get(Service)

        # Both injections must have happened
        assert isinstance(svc.store, FileStorage)
        assert isinstance(svc.logger, ConcreteLogger)

    def test_constructor_param_wins_over_classvar(self, container: DIContainer) -> None:
        """When a name appears in both class annotations and __init__, constructor wins."""

        @Component
        class Service:
            # Class-level annotation for 'store'
            store: Inject[Storage]

            def __init__(self, store: Storage) -> None:
                # Constructor sets store directly — should not be overwritten
                self.store = store  # type: ignore[assignment]

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        # Still resolved — constructor handled it, class-var injection skipped
        assert isinstance(svc.store, FileStorage)

    def test_classvar_with_qualifier(self, container: DIContainer) -> None:
        """Class-level Inject(T, qualifier=...) should forward the qualifier."""

        @Component
        class Service:
            store: Inject(Storage, qualifier="cloud")  # type: ignore[valid-type]

        container.bind(Storage, FileStorage)
        container.bind(Storage, CloudStorage)
        container.register(Service)

        svc = container.get(Service)

        assert isinstance(svc.store, CloudStorage)

    def test_classvar_optional_returns_none_when_absent(
        self, container: DIContainer
    ) -> None:
        """Class-level Inject(T, optional=True) injects None when no binding exists."""

        @Component
        class Service:
            store: Inject(Storage, optional=True)  # type: ignore[valid-type]

        container.register(Service)  # Storage NOT registered

        svc = container.get(Service)

        assert svc.store is None

    def test_plain_classvar_annotation_is_not_injected(
        self, container: DIContainer
    ) -> None:
        """Plain class-level annotations without Inject[T] are NOT auto-injected."""

        @Component
        class Service:
            store: Storage  # plain annotation — no providify metadata

            def __init__(self) -> None:
                self.store = None  # type: ignore[assignment]

        container.bind(Storage, FileStorage)
        container.register(Service)

        svc = container.get(Service)

        # Plain annotation is not touched — stays None as set by __init__
        assert svc.store is None


# ─────────────────────────────────────────────────────────────────
#  Type alias expansion regression tests
#
#  These tests guard the runtime half of the TYPE_CHECKING split in type.py.
#  The else-branch (Inject = _InjectedAlias(), InjectInstances = _InjectedInstancesAlias())
#  MUST produce Annotated[T, InjectMeta(...)] so the container's _has_providify_metadata()
#  and _get_providify_metadata() helpers find the InjectMeta marker.
#
#  If the expansion breaks, the container falls back to plain type resolution:
#  qualifiers/optional are silently ignored and injection may resolve wrong bindings.
#  The container-level tests above don't catch this because plain Storage hints also
#  resolve successfully — only qualifier/optional paths would fail.
# ─────────────────────────────────────────────────────────────────


class TestInjectTypeAliasExpansion:
    """Regression tests for the runtime expansion of Inject[T] and InjectInstances[T].

    These tests are NOT about type-checker behaviour (which can't be asserted
    in pytest). They verify that the _InjectedAlias / _InjectedInstancesAlias
    singletons produce the correct Annotated wrappers at runtime, and that
    get_type_hints() resolves them correctly when from __future__ import annotations
    is active (annotations are stored as strings and evaluated lazily).

    Edge cases:
        - Subscript form Inject[T]           → Annotated[T, InjectMeta()]
        - Call form Inject(T, qualifier="x") → Annotated[T, InjectMeta(qualifier="x")]
        - InjectInstances[T]                 → Annotated[list[T], InjectMeta(all=True)]
        - get_type_hints() evaluation        → resolves string annotation to Annotated form
        - No InjectMeta marker found         → container falls back to plain type, no crash
    """

    def test_inject_subscript_expands_to_annotated_with_inject_meta(self) -> None:
        """Inject[T] must expand to Annotated[T, InjectMeta()] at runtime.

        The container's _has_providify_metadata() calls get_origin() / get_args()
        on the resolved hint — if this expansion is wrong, InjectMeta is never
        found and the qualifier/optional mechanism stops working silently.

        Args:
            (none — no container fixture needed, this is a pure expansion check)

        Raises:
            AssertionError: if the runtime expansion produces the wrong type structure.
        """
        from typing import Annotated, get_args, get_origin

        from providify.type import InjectMeta

        result = Inject[Storage]

        # Must be Annotated — the container dispatches on get_origin(hint) is Annotated
        assert (
            get_origin(result) is Annotated
        ), f"Inject[T] must expand to Annotated[T, InjectMeta()]; got {result!r}"
        args = get_args(result)
        # First arg is the wrapped type
        assert args[0] is Storage
        # Second arg is the InjectMeta marker — container reads qualifier/optional from it
        assert isinstance(
            args[1], InjectMeta
        ), f"Second Annotated arg must be InjectMeta; got {type(args[1])!r}"
        # Default expansion: no qualifier, not optional, not all
        assert args[1].qualifier is None
        assert args[1].optional is False
        assert args[1].all is False

    def test_inject_call_form_forwards_qualifier_and_optional_into_inject_meta(
        self,
    ) -> None:
        """Inject(T, qualifier="x", optional=True) must embed those values in InjectMeta.

        The qualifier and optional fields are read by the container's resolution
        path; if they're lost during expansion, named injection silently resolves
        the wrong binding and optional injection raises instead of returning None.

        Args:
            (none — pure expansion check, no container needed)

        Raises:
            AssertionError: if qualifier or optional are not present in InjectMeta.
        """
        from typing import Annotated, get_args, get_origin

        from providify.type import InjectMeta

        result = Inject(Storage, qualifier="cloud", optional=True)  # type: ignore[call-arg]

        assert get_origin(result) is Annotated
        args = get_args(result)
        assert args[0] is Storage
        meta = args[1]
        assert isinstance(meta, InjectMeta)
        # Qualifier and optional must survive the expansion — container relies on them
        assert meta.qualifier == "cloud"
        assert meta.optional is True

    def test_inject_instances_subscript_expands_to_annotated_list_with_all_true(
        self,
    ) -> None:
        """InjectInstances[T] must expand to Annotated[list[T], InjectMeta(all=True)].

        The all=True flag is what tells the container to call get_all() instead
        of get(). If it's missing, the container resolves a single instance into
        a parameter that expects a list — a runtime TypeError.

        Args:
            (none — pure expansion check)

        Raises:
            AssertionError: if the expanded type is not the expected Annotated form.
        """
        from typing import Annotated, get_args, get_origin

        from providify.type import InjectMeta

        result = InjectInstances[Storage]

        assert get_origin(result) is Annotated, (
            f"InjectInstances[T] must expand to Annotated[list[T], InjectMeta(all=True)]; "
            f"got {result!r}"
        )
        args = get_args(result)
        # First arg is list[Storage] — get_all() returns a list
        assert args[0] == list[Storage]
        meta = args[1]
        assert isinstance(meta, InjectMeta)
        # all=True is the signal to call container.get_all() instead of container.get()
        assert meta.all is True

    def test_get_type_hints_resolves_inject_annotation_under_future_annotations(
        self,
    ) -> None:
        """get_type_hints() must resolve Inject[T] to Annotated[T, InjectMeta()] even
        when from __future__ import annotations is active.

        Under PEP 563 (from __future__ import annotations), ALL annotations are stored
        as plain strings at class definition time. The container calls
        get_type_hints(cls, include_extras=True) to evaluate them. If Inject is not
        importable in the annotation's namespace, or __getitem__ returns the wrong
        type, the InjectMeta marker is lost and injection falls back to plain type
        resolution without any error — a silent correctness bug.

        Args:
            (none — uses an inline class defined in this test)

        Raises:
            AssertionError: if the resolved hint is not Annotated[Storage, InjectMeta()].
        """
        from typing import Annotated, get_args, get_origin, get_type_hints

        from providify.type import InjectMeta

        # Defined here — shares this module's globals (including imported Inject).
        # Because this file has `from __future__ import annotations`, __init__'s
        # annotation dict stores 'Inject[Storage]' as a string.  get_type_hints()
        # evaluates the string against the module globals to recover the runtime value.
        @Component
        class Service:
            def __init__(self, store: Inject[Storage]) -> None:
                self.store = store

        hints = get_type_hints(Service.__init__, include_extras=True)

        assert "store" in hints, "store parameter must appear in resolved type hints"
        store_hint = hints["store"]

        # Must resolve to Annotated[Storage, InjectMeta()] — not the raw string
        assert get_origin(store_hint) is Annotated, (
            f"get_type_hints must resolve Inject[Storage] to Annotated; "
            f"got {store_hint!r}"
        )
        inner_type, meta = get_args(store_hint)[:2]
        assert inner_type is Storage
        assert isinstance(meta, InjectMeta)
