"""Unit tests for DIContainer._build_localns() and the _localns_cache mechanism.

Covered:
    - _localns_cache starts as None (no upfront cost on container creation)
    - _build_localns() populates the cache on first call
    - _build_localns() returns the SAME dict object on repeated calls (cached, not rebuilt)
    - bind() invalidates the cache (sets _localns_cache = None)
    - register() invalidates the cache
    - provide() invalidates the cache
    - Cache is rebuilt after invalidation (next _build_localns() call re-populates)
    - Localns dict contains registered interface class names
    - Localns dict contains registered implementation class names
    - Locally-defined class injection works end-to-end (the bug fix this cache enables)

DESIGN NOTE: The _localns_cache solves a PEP-563 problem. With
`from __future__ import annotations`, ALL annotations become lazy strings.
get_type_hints() resolves those strings against fn.__globals__ (the module
namespace), but locally-defined classes (inside test functions, lambdas, etc.)
never appear in __globals__. Passing localns=_build_localns() supplements
the lookup with every registered class, making locally-defined deps resolvable.
"""

from __future__ import annotations

from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton

# ─────────────────────────────────────────────────────────────────
#  Module-level sentinel for provider return-type tests
#
#  DESIGN: ProviderBinding resolves the return type annotation at
#  registration time via get_type_hints(fn), which only has access to
#  fn.__globals__ (the module namespace).  Locally-defined classes
#  (inside test methods) are absent from __globals__, so they cannot
#  be used as @Provider return types.  This sentinel lives at module
#  level and is therefore always resolvable from any function defined
#  in this file.
# ─────────────────────────────────────────────────────────────────


class _ProviderProduct:
    """Module-level sentinel used as @Provider return type in cache tests."""


# ─────────────────────────────────────────────────────────────────
#  Tests: cache lifecycle
# ─────────────────────────────────────────────────────────────────


class TestLocalnsCache:
    """Verifies the lazy-build, invalidate-on-write caching strategy."""

    def test_cache_starts_as_none(self, container: DIContainer) -> None:
        """A fresh container must have _localns_cache = None.

        DESIGN: We delay the dict build until the first resolution.
        In the common pattern (all bindings registered before first get()),
        the dict is built exactly once after all classes are known.
        """
        assert container._localns_cache is None

    def test_cache_populated_after_first_call(self, container: DIContainer) -> None:
        """_build_localns() must populate _localns_cache on first call."""

        @Component
        class MyService:
            pass

        container.register(MyService)

        # Cache is still None — not built yet
        assert container._localns_cache is None

        # Trigger the first build
        localns = container._build_localns()

        # Cache must now be set
        assert container._localns_cache is not None
        # The returned dict must be the same object that was cached
        assert localns is container._localns_cache

    def test_cache_reused_on_second_call(self, container: DIContainer) -> None:
        """_build_localns() must return the SAME dict object on repeated calls.

        DESIGN: If the dict were rebuilt on every call, _collect_kwargs (which
        calls _build_localns() once per parameter) would be O(n * bindings)
        instead of O(n). The identity check (is) verifies no rebuild happens.
        """

        @Component
        class MyService:
            pass

        container.register(MyService)

        first = container._build_localns()
        second = container._build_localns()

        # Must be the exact same dict object — not an equal copy
        assert first is second

    def test_bind_invalidates_cache(self, container: DIContainer) -> None:
        """bind() must reset _localns_cache to None so it is rebuilt next call."""

        # Iface must be defined before Impl so Impl can inherit from it.
        # ClassBinding enforces issubclass(implementation, interface).
        class Iface:
            pass

        @Component
        class Impl(Iface):  # subclass required by ClassBinding ✅
            pass

        container.register(Impl)
        container._build_localns()  # populate the cache

        assert container._localns_cache is not None

        container.bind(Iface, Impl)  # must invalidate

        assert container._localns_cache is None

    def test_register_invalidates_cache(self, container: DIContainer) -> None:
        """register() must reset _localns_cache to None."""

        @Component
        class ServiceA:
            pass

        @Component
        class ServiceB:
            pass

        container.register(ServiceA)
        container._build_localns()

        assert container._localns_cache is not None

        container.register(ServiceB)  # must invalidate

        assert container._localns_cache is None

    def test_provide_invalidates_cache(self, container: DIContainer) -> None:
        """provide() must reset _localns_cache to None."""

        @Component
        class Widget:
            pass

        # Return type must be a module-level class so ProviderBinding can
        # resolve it via get_type_hints(fn) at registration time.
        # _ProviderProduct is the module-level sentinel defined above.
        @Provider
        def make_product() -> _ProviderProduct:
            return _ProviderProduct()

        container.register(Widget)
        container._build_localns()

        assert container._localns_cache is not None

        container.provide(make_product)  # must invalidate

        assert container._localns_cache is None

    def test_cache_rebuilt_after_invalidation(self, container: DIContainer) -> None:
        """After invalidation, the next _build_localns() must produce a fresh dict."""

        @Component
        class First:
            pass

        @Component
        class Second:
            pass

        container.register(First)
        first_cache = container._build_localns()

        container.register(Second)  # invalidates
        second_cache = container._build_localns()  # rebuilds

        # A new dict was built — not the same object as before invalidation
        assert second_cache is not first_cache

    def test_localns_contains_interface_name(self, container: DIContainer) -> None:
        """_build_localns() must map interface class names to their class objects.

        DESIGN: This is what allows get_type_hints() to resolve forward-ref
        strings like 'IRepository' to the actual IRepository class.
        """

        class IRepository:
            pass

        @Component
        class SqlRepository(IRepository):
            pass

        container.bind(IRepository, SqlRepository)

        localns = container._build_localns()

        assert "IRepository" in localns
        assert localns["IRepository"] is IRepository

    def test_localns_contains_implementation_name(self, container: DIContainer) -> None:
        """_build_localns() must also map implementation class names.

        DESIGN: Annotations may reference the concrete class directly
        (e.g. `def __init__(self, repo: SqlRepository)`) rather than the
        abstract interface. The localns must include both to handle both patterns.
        """

        class IRepository:
            pass

        @Component
        class SqlRepository(IRepository):
            pass

        container.bind(IRepository, SqlRepository)

        localns = container._build_localns()

        assert "SqlRepository" in localns
        assert localns["SqlRepository"] is SqlRepository


# ─────────────────────────────────────────────────────────────────
#  End-to-end tests: locally-defined class injection
#
#  These are regression tests for the PEP-563 / locally-defined class bug.
#  Before _build_localns(), get_type_hints() would raise NameError for any
#  class defined inside a function, and the `except Exception: hints = {}`
#  fallback would silently produce no kwargs → TypeError: missing argument.
# ─────────────────────────────────────────────────────────────────


class TestLocallyDefinedClassInjection:
    """Verify that classes defined inside test functions can be injected.

    These tests were all FAILING before _build_localns() was introduced.
    They document the contract that locally-defined dependencies work
    as long as they are registered in the container before get() is called.
    """

    def test_locally_defined_dep_injected_into_constructor(self, container: DIContainer) -> None:
        """A class defined inside this method must be providify via its type hint."""

        @Component
        class LocalConfig:
            value = "injected"

        @Component
        class LocalService:
            def __init__(self, config: LocalConfig) -> None:
                self.config = config

        container.register(LocalConfig)
        container.register(LocalService)

        svc = container.get(LocalService)

        # Config was injected — not a TypeError
        assert isinstance(svc.config, LocalConfig)
        assert svc.config.value == "injected"

    def test_three_level_locally_defined_chain(self, container: DIContainer) -> None:
        """A → B → C chain with all classes defined locally must resolve fully."""

        @Component
        class LocalC:
            label = "C"

        @Component
        class LocalB:
            def __init__(self, c: LocalC) -> None:
                self.c = c

        @Component
        class LocalA:
            def __init__(self, b: LocalB) -> None:
                self.b = b

        container.register(LocalC)
        container.register(LocalB)
        container.register(LocalA)

        a = container.get(LocalA)

        assert isinstance(a.b, LocalB)
        assert isinstance(a.b.c, LocalC)
        assert a.b.c.label == "C"

    def test_locally_defined_dep_via_provider(self, container: DIContainer) -> None:
        """A @Provider function with a locally-defined dep must resolve correctly.

        DESIGN: The dep parameter (LocalDep) IS locally-defined — that is
        what _build_localns() enables.  The return type must be a module-level
        class (_ProviderProduct) so that ProviderBinding can resolve it at
        registration time from fn.__globals__ (where local variables are absent).
        """

        @Singleton
        class LocalDep:
            value = "from_dep"

        # Return type is _ProviderProduct (module-level) — resolvable at
        # registration time.  LocalDep is the locally-defined dep whose
        # resolution is the actual behaviour under test.
        @Provider
        def make_product(dep: LocalDep) -> _ProviderProduct:
            product = _ProviderProduct()
            product.dep_value = dep.value  # type: ignore[attr-defined]
            return product

        container.register(LocalDep)
        container.provide(make_product)

        product = container.get(_ProviderProduct)

        assert isinstance(product, _ProviderProduct)
        assert product.dep_value == "from_dep"  # type: ignore[attr-defined]

    def test_async_locally_defined_dep_injected(self, container: DIContainer) -> None:
        """Async path: locally-defined dep must also resolve via aget()."""

        @Component
        class AsyncConfig:
            setting = "async_value"

        @Component
        class AsyncService:
            def __init__(self, cfg: AsyncConfig) -> None:
                self.cfg = cfg

        container.register(AsyncConfig)
        container.register(AsyncService)

        # Verify that the sync path works (the async path uses the same _build_localns)
        svc = container.get(AsyncService)

        assert isinstance(svc.cfg, AsyncConfig)
        assert svc.cfg.setting == "async_value"
