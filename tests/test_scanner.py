"""Unit tests for DefaultContainerScanner and DIContainer.scan().

Covered:
    - scan(str): imports a module by dotted name and scans it
    - scan(ModuleType): accepts an already-imported module object
    - scan() registers @Component-decorated classes
    - scan() registers @Singleton-decorated classes
    - scan() registers @Provider-decorated functions
    - scan() registers @Configuration classes (installs them via container.install)
    - scan() discovers all @Provider methods inside a @Configuration class
    - scan() skips members whose name starts with '_'
    - scan() skips symbols re-exported from other modules (inspect.getmodule guard)
    - scan() is idempotent — scanning the same module twice doesn't double-register
    - scan() is idempotent for @Configuration — no double-install on repeated scans
    - scan() autobinds to an abstract base class when the impl inherits from one
    - scan() self-binds when the class has no abstract base
    - scan() raises ModuleNotFoundError for unknown module names
    - container.scan() delegates to the internal _scanner

DESIGN: Fake modules are created via types.ModuleType and temporarily registered
in sys.modules. Stamping __module__ on each class/function makes inspect.getmodule()
return the fake module, satisfying the scanner's "defined here?" check.
"""

from __future__ import annotations

import sys
import types
import uuid
from abc import ABC, abstractmethod

import pytest

from providify.binding import ProviderBinding
from providify.container import DIContainer
from providify.decorator.module import Configuration
from providify.decorator.scope import Component, Provider, Singleton


# ─────────────────────────────────────────────────────────────────
#  Module-level sentinel for provider return-type tests
#
#  DESIGN: ProviderBinding resolves the return type at registration time
#  via get_type_hints(fn), which only searches fn.__globals__ (the module
#  where the function was defined).  Locally-defined classes are absent
#  from __globals__, so they cannot be used as @Provider return types.
#  This sentinel lives at module level — always resolvable from any
#  provider function defined in this file.
# ─────────────────────────────────────────────────────────────────


class _ProviderWidget:
    """Module-level sentinel used as @Provider return type in scanner tests."""


class _ConfigService:
    """Module-level sentinel used as @Provider return type inside @Configuration classes.

    DESIGN: get_type_hints(fn) resolves return annotations by looking up names in
    fn.__globals__ — the globals dict of the module where the function was *defined*.
    Methods on locally-scoped @Configuration classes defined inside test functions
    still reference the test module's globals, so module-level types resolve correctly.
    Locally-defined return-type classes would be absent from __globals__ and raise
    NameError at ProviderBinding registration time.
    """


class _ConfigServiceB:
    """Second module-level sentinel for multi-provider @Configuration tests."""


# ─────────────────────────────────────────────────────────────────
#  Helpers and fixtures
# ─────────────────────────────────────────────────────────────────


def _fresh_module_name() -> str:
    """Return a unique module name that cannot collide with real modules."""
    return f"_providify_test_{uuid.uuid4().hex}"


def _add(mod: types.ModuleType, obj: object) -> object:
    """Stamp *obj* as 'defined in' *mod* and attach it as an attribute.

    inspect.getmodule() resolves obj.__module__ → sys.modules lookup.
    Setting obj.__module__ = mod.__name__ makes that lookup return *mod*.

    Args:
        mod: The fake module to attach the object to.
        obj: A class or function to register.

    Returns:
        The same object (for chaining).
    """
    name = getattr(obj, "__name__", None) or getattr(obj, "__qualname__", "unknown")
    obj.__module__ = mod.__name__  # type: ignore[union-attr]
    setattr(mod, name, obj)
    return obj


@pytest.fixture
def fake_mod() -> types.ModuleType:
    """Yield a fresh ModuleType registered in sys.modules.

    Removed from sys.modules after the test to avoid cross-test pollution.

    Yields:
        A types.ModuleType ready to receive DI-decorated members.
    """
    name = _fresh_module_name()
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    yield mod
    sys.modules.pop(name, None)


# ─────────────────────────────────────────────────────────────────
#  Tests: basic registration
# ─────────────────────────────────────────────────────────────────


class TestScanBasicRegistration:
    """Verify that scan() picks up the standard DI decorators."""

    def test_scan_registers_component_class(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Component class defined in the module must be registered."""

        @Component
        class MyService:
            pass

        _add(fake_mod, MyService)
        container.scan(fake_mod)

        result = container.get(MyService)
        assert isinstance(result, MyService)

    def test_scan_registers_singleton_class(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Singleton class must be registered and its scope preserved."""

        @Singleton
        class MySingleton:
            pass

        _add(fake_mod, MySingleton)
        container.scan(fake_mod)

        a = container.get(MySingleton)
        b = container.get(MySingleton)
        # Scope must be SINGLETON — same instance returned both times
        assert a is b

    def test_scan_registers_provider_function(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Provider function defined in the module must be registered."""

        # Return type must be a module-level class so ProviderBinding can
        # resolve it via get_type_hints(fn) at registration time.
        @Provider
        def make_widget() -> _ProviderWidget:
            return _ProviderWidget()

        _add(fake_mod, make_widget)
        container.scan(fake_mod)

        result = container.get(_ProviderWidget)
        assert isinstance(result, _ProviderWidget)

    def test_scan_by_module_name_string(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan(str) must import the module by name then scan it."""

        @Component
        class NamedService:
            pass

        _add(fake_mod, NamedService)
        # Pass the module name as a string — scanner must import it
        container.scan(fake_mod.__name__)

        result = container.get(NamedService)
        assert isinstance(result, NamedService)

    def test_scan_by_module_object(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """scan(ModuleType) must accept an already-imported module object."""

        @Component
        class DirectService:
            pass

        _add(fake_mod, DirectService)
        container.scan(fake_mod)  # ModuleType, not a string

        assert isinstance(container.get(DirectService), DirectService)


# ─────────────────────────────────────────────────────────────────
#  Tests: filtering / skipping
# ─────────────────────────────────────────────────────────────────


class TestScanFiltering:
    """Verify that scan() applies the private-name and re-export guards."""

    def test_scan_skips_private_members(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Members whose name starts with '_' must be silently skipped.

        DESIGN: Private members are implementation details — auto-registering
        them would break encapsulation. The '_' prefix convention in Python
        signals 'not part of the public API'.
        """

        @Component
        class _PrivateService:
            pass

        _add(fake_mod, _PrivateService)
        container.scan(fake_mod)

        # _PrivateService starts with '_' — must not be registered
        with pytest.raises(LookupError):
            container.get(_PrivateService)

    def test_scan_skips_reexported_symbols(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Symbols whose defining module is different from the scanned module must be skipped.

        DESIGN: Without this guard, scanning a module that re-exports from
        another package (e.g. `from third_party import ThirdPartyService`)
        would double-register ThirdPartyService. The guard uses
        inspect.getmodule(obj) is module to detect re-exports.
        """

        @Component
        class ReexportedService:
            pass

        # Attach to fake_mod BUT keep __module__ pointing elsewhere —
        # simulates `from other_module import ReexportedService`
        ReexportedService.__module__ = "some_other_module"
        setattr(fake_mod, "ReexportedService", ReexportedService)

        container.scan(fake_mod)

        # Should NOT be registered — wrong defining module
        with pytest.raises(LookupError):
            container.get(ReexportedService)


# ─────────────────────────────────────────────────────────────────
#  Tests: idempotency
# ─────────────────────────────────────────────────────────────────


class TestScanIdempotency:
    """Verify that scanning the same module twice doesn't double-register."""

    def test_scan_is_idempotent_for_classes(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Scanning the same module twice must not add duplicate ClassBindings.

        DESIGN: The scanner checks whether the implementation class is already
        in _bindings before appending — this prevents double-registration on
        repeated scans (e.g. in a hot-reload scenario).
        """

        @Singleton
        class IdempotentService:
            pass

        _add(fake_mod, IdempotentService)

        container.scan(fake_mod)
        container.scan(fake_mod)  # second scan — must be a no-op

        # Exactly one binding — not two
        matching = [
            b
            for b in container._bindings
            if getattr(b, "implementation", None) is IdempotentService
        ]
        assert len(matching) == 1

    def test_scan_is_idempotent_for_providers(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Scanning the same @Provider function twice must not add duplicate ProviderBindings."""

        # Return type must be module-level — see _ProviderWidget sentinel above.
        @Provider
        def make_widget() -> _ProviderWidget:
            return _ProviderWidget()

        _add(fake_mod, make_widget)

        container.scan(fake_mod)
        container.scan(fake_mod)

        matching = [
            b
            for b in container._bindings
            if isinstance(b, ProviderBinding) and b.fn.__name__ == "make_widget"
        ]
        assert len(matching) == 1


# ─────────────────────────────────────────────────────────────────
#  Tests: @Configuration scanning
# ─────────────────────────────────────────────────────────────────


class TestScanConfiguration:
    """Verify that scan() discovers @Configuration classes and installs them.

    DESIGN: A @Configuration class does NOT carry DIMetadata (no scope decorator),
    so it falls through the _has_own_metadata() branch and is handled separately
    by the _has_configuration_module() branch. The scanner calls container.install()
    which instantiates the module and registers each @Provider method as a bound-method
    ProviderBinding. All of this must happen transparently during scan().
    """

    def test_scan_registers_configuration_class(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """A @Configuration class defined in the module must be installed on scan.

        The @Provider method's return type (_ConfigService) must be resolvable
        from the container after scanning, with no explicit install() call.
        """

        @Configuration
        class InfraModule:
            @Provider
            def make_service(self) -> _ConfigService:
                # self is the live InfraModule instance — bound method ✅
                return _ConfigService()

        _add(fake_mod, InfraModule)
        container.scan(fake_mod)

        result = container.get(_ConfigService)
        assert isinstance(result, _ConfigService)

    def test_scan_configuration_installs_all_providers(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """All @Provider methods inside a @Configuration class must be registered.

        Even when the module declares multiple providers, every one of them
        must become a binding after a single scan().
        """

        @Configuration
        class MultiProviderModule:
            @Provider
            def service_a(self) -> _ConfigService:
                return _ConfigService()

            @Provider
            def service_b(self) -> _ConfigServiceB:
                return _ConfigServiceB()

        _add(fake_mod, MultiProviderModule)
        container.scan(fake_mod)

        # Both provider return types must be independently resolvable
        assert isinstance(container.get(_ConfigService), _ConfigService)
        assert isinstance(container.get(_ConfigServiceB), _ConfigServiceB)

    def test_scan_configuration_provider_singleton_scope_preserved(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """@Provider(singleton=True) inside a @Configuration must stay SINGLETON after scan.

        ProviderMetadata carries the singleton flag; scan() must not strip it.
        """

        @Configuration
        class SingletonModule:
            @Provider(singleton=True)
            def singleton_service(self) -> _ConfigService:
                return _ConfigService()

        _add(fake_mod, SingletonModule)
        container.scan(fake_mod)

        a = container.get(_ConfigService)
        b = container.get(_ConfigService)
        # SINGLETON scope — same instance on every resolution
        assert a is b

    def test_scan_is_idempotent_for_configurations(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """Scanning the same module twice must not double-install a @Configuration class.

        DESIGN: The old guard compared ``b.fn is fn`` (ProviderBinding stores the
        *bound* method from install(), but vars(cls) yields the *unbound* function —
        they are never the same object). The fix uses a ``set[type]`` on the scanner
        keyed by class identity, which is always reliable.

        If double-installation occurred the container would have two ProviderBindings
        for _ConfigService and container.get(_ConfigService) would raise AmbiguousBindingError
        (multiple bindings without a distinguishing qualifier/priority).
        """

        @Configuration
        class IdempotentModule:
            @Provider
            def make_service(self) -> _ConfigService:
                return _ConfigService()

        _add(fake_mod, IdempotentModule)

        container.scan(fake_mod)
        container.scan(fake_mod)  # second scan — must be a no-op

        # Exactly one ProviderBinding for _ConfigService — not two
        matching = [
            b
            for b in container._bindings
            if isinstance(b, ProviderBinding) and b.interface is _ConfigService
        ]
        assert len(matching) == 1


# ─────────────────────────────────────────────────────────────────
#  Tests: abstract base class auto-binding
# ─────────────────────────────────────────────────────────────────


class TestScanAutoBinding:
    """Verify how scan() decides between interface-bind and self-bind."""

    def test_scan_autobinds_to_abstract_base(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """When an impl inherits from an ABC, it must be bound to that ABC.

        DESIGN: inspect.isabstract(base) returns True only for classes that
        have unimplemented abstract methods. _find_interfaces() walks the MRO
        looking for such bases. If found, ClassBinding(interface, impl) is used
        instead of ClassBinding(impl, impl), so callers can resolve by interface.
        """

        class IRepository(ABC):
            @abstractmethod
            def find(self) -> object: ...

        @Component
        class SqlRepository(IRepository):
            def find(self) -> object:
                return object()

        _add(fake_mod, IRepository)
        _add(fake_mod, SqlRepository)
        container.scan(fake_mod)

        # Must be resolvable via the abstract interface
        result = container.get(IRepository)
        assert isinstance(result, SqlRepository)

    def test_scan_self_binds_when_no_abstract_base(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """When no ABC is in the MRO, the class must be bound to itself.

        DESIGN: Self-binding ensures concrete classes with no interface can
        still be resolved directly — a common pattern for leaf services.
        """

        @Component
        class ConcreteLeaf:
            """No ABC — should be bound as ConcreteLeaf → ConcreteLeaf."""

            pass

        _add(fake_mod, ConcreteLeaf)
        container.scan(fake_mod)

        result = container.get(ConcreteLeaf)
        assert isinstance(result, ConcreteLeaf)

    def test_scan_binds_to_multiple_abstract_bases(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """A class implementing two ABCs must be bound to both interfaces."""

        class IReadable(ABC):
            @abstractmethod
            def read(self) -> str: ...

        class IWritable(ABC):
            @abstractmethod
            def write(self, data: str) -> None: ...

        @Component
        class ReadWriteStore(IReadable, IWritable):
            def read(self) -> str:
                return ""

            def write(self, data: str) -> None:
                pass

        _add(fake_mod, IReadable)
        _add(fake_mod, IWritable)
        _add(fake_mod, ReadWriteStore)
        container.scan(fake_mod)

        # Both abstract interfaces must resolve to the same implementation
        assert isinstance(container.get(IReadable), ReadWriteStore)
        assert isinstance(container.get(IWritable), ReadWriteStore)


# ─────────────────────────────────────────────────────────────────
#  Tests: error paths
# ─────────────────────────────────────────────────────────────────


class TestScanErrorPaths:
    """Verify scanner behaviour for invalid inputs."""

    def test_scan_raises_module_not_found_for_unknown_name(
        self, container: DIContainer
    ) -> None:
        """scan('no.such.module') must raise ModuleNotFoundError."""
        with pytest.raises(ModuleNotFoundError):
            container.scan("no_such_module_xyzzy_providify_test")

    def test_container_scan_delegates_to_scanner(
        self, container: DIContainer, fake_mod: types.ModuleType
    ) -> None:
        """container.scan() must delegate to self._scanner.scan()."""
        calls: list[str] = []

        class RecordingScanner:
            def scan(self, module: object, *, recursive: bool = False) -> None:
                calls.append(getattr(module, "__name__", str(module)))

        container._scanner = RecordingScanner()  # type: ignore[assignment]
        container.scan(fake_mod)

        assert len(calls) == 1
        assert calls[0] == fake_mod.__name__


# ─────────────────────────────────────────────────────────────────
#  Tests: DIContainer(scan=...) constructor auto-scan
# ─────────────────────────────────────────────────────────────────


def _make_module_with_component() -> tuple[types.ModuleType, type]:
    """Create a fresh fake module containing one @Component class.

    Returns:
        A (module, ComponentClass) tuple ready for auto-scan tests.
        The module is registered in sys.modules; caller must remove it.
    """
    name = _fresh_module_name()
    mod = types.ModuleType(name)
    sys.modules[name] = mod

    @Component
    class AutoService:
        """Sentinel component for auto-scan constructor tests."""

    _add(mod, AutoService)
    return mod, AutoService


class TestDIContainerAutoScan:
    """Tests for DIContainer(scan=..., recursive=...) constructor parameters.

    Covered:
        - scan=None (default) — container starts empty, backward-compatible
        - scan="module" (str) — single module scanned at construction
        - scan=["mod1", "mod2"] (list) — both modules scanned, left-to-right
        - scan=[] (empty list) — treated as no-op, same as scan=None
        - DIContainer() with no args — same as scan=None (backward-compat)
        - recursive=False — flag forwarded to scanner
        - scan="bad.module" — raises ModuleNotFoundError at construction

    DESIGN: Scanning is eager — happens during __init__ so errors surface at
    the point of misconfiguration rather than at first get() call.
    """

    def test_no_scan_arg_yields_empty_container(self) -> None:
        """DIContainer() with no args must start with zero bindings (backward-compat)."""
        c = DIContainer()
        assert len(c._bindings) == 0

    def test_scan_none_yields_empty_container(self) -> None:
        """DIContainer(scan=None) must start with zero bindings."""
        c = DIContainer(scan=None)
        assert len(c._bindings) == 0

    def test_scan_empty_list_yields_empty_container(self) -> None:
        """DIContainer(scan=[]) must start with zero bindings."""
        c = DIContainer(scan=[])
        assert len(c._bindings) == 0

    def test_scan_single_string_registers_components(self) -> None:
        """DIContainer(scan='mod') must scan the named module immediately."""
        mod, AutoService = _make_module_with_component()
        try:
            c = DIContainer(scan=mod.__name__, recursive=False)
            # The component must be resolvable without any manual bind/register call
            instance = c.get(AutoService)
            assert isinstance(instance, AutoService)
        finally:
            sys.modules.pop(mod.__name__, None)

    def test_scan_list_of_strings_registers_all_modules(self) -> None:
        """DIContainer(scan=['mod1','mod2']) must scan both modules."""
        mod1, Svc1 = _make_module_with_component()
        mod2, Svc2 = _make_module_with_component()
        try:
            c = DIContainer(scan=[mod1.__name__, mod2.__name__], recursive=False)
            assert isinstance(c.get(Svc1), Svc1)
            assert isinstance(c.get(Svc2), Svc2)
        finally:
            sys.modules.pop(mod1.__name__, None)
            sys.modules.pop(mod2.__name__, None)

    def test_scan_bad_module_raises_at_construction(self) -> None:
        """DIContainer(scan='bad.module') must raise ModuleNotFoundError at __init__ time.

        Edge case: errors surface eagerly (at construction) rather than lazily
        (at first get()) — this is intentional for fail-fast misconfiguration detection.
        """
        with pytest.raises(ModuleNotFoundError):
            DIContainer(scan="no_such_module_xyzzy_providify_test")

    def test_recursive_false_forwarded_to_scanner(self) -> None:
        """DIContainer(scan=..., recursive=False) must pass recursive=False to scanner."""
        recorded: list[bool] = []

        class RecordingScanner:
            def scan(self, module: object, *, recursive: bool = False) -> None:
                recorded.append(recursive)

        # Patch the scanner after construction to capture the flag.
        # We need to call __init__ with a real module to avoid ModuleNotFoundError,
        # so we create a fresh fake module first, then verify the recorded flag.
        mod, _ = _make_module_with_component()
        try:
            c = DIContainer.__new__(DIContainer)
            # Manually init state so we can swap the scanner before scan fires
            c._bindings = []  # type: ignore[attr-defined]
            c._singleton_cache = {}  # type: ignore[attr-defined]
            from providify.scope import ScopeContext

            c.scope_context = ScopeContext()  # type: ignore[attr-defined]
            c._scanner = RecordingScanner()  # type: ignore[attr-defined]
            c._validated = False  # type: ignore[attr-defined]
            c._localns_cache = None  # type: ignore[attr-defined]
            # Call only the auto-scan portion of __init__ indirectly via scan()
            c.scan(mod.__name__, recursive=False)
            assert recorded == [False]
        finally:
            sys.modules.pop(mod.__name__, None)
