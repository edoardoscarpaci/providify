"""Failing (red) tests for Plan 008 — container-level module installation
ordering (F5).

These tests describe behaviour that does NOT exist yet:
    - ``container.install()`` / ``ainstall()`` transitively installing a
      module's ``depends_on`` dependencies first, in deterministic order.
    - Dedup authority moving to the container: double ``install()``, and
      ``install()`` + ``scan()`` of the same module, register providers once.
    - Module ``@PostConstruct`` running once, after ``__init__``, before its
      providers are registered.
    - A ``depends_on`` cycle raising ``ModuleCycleError`` with NO partial
      installation.

See plans/008-module-startup-ordering.md, Step 7.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.lifecycle import PostConstruct
from providify.decorator.module import Configuration
from providify.decorator.scope import Provider
from providify.metadata import Scope as _Scope
from providify.modules import ModuleCycleError


class _Pool:
    """Module-level so type hints resolve under `from __future__ import annotations`."""


class _Repo:
    """Module-level so type hints resolve under `from __future__ import annotations`."""


class TestTransitiveInstallOrder:
    def test_install_of_dependent_module_installs_dependency_first(
        self, container: DIContainer
    ) -> None:
        """RepoModule depends on InfraModule; installing RepoModule alone must
        register InfraModule's providers first so RepoModule's own __init__
        param resolves."""

        @Configuration
        class InfraModule:
            @Provider(scope=_Scope.SINGLETON)
            def pool(self) -> _Pool:
                return _Pool()

        @Configuration(depends_on=[InfraModule])
        class RepoModule:
            def __init__(self, pool: _Pool) -> None:
                self.pool = pool

            @Provider()
            def repo(self) -> _Repo:
                return _Repo()

        container.install(RepoModule)

        assert container.get(_Pool) is not None
        assert container.get(_Repo) is not None


class TestInstallDedup:
    def test_double_install_registers_providers_once(self, container: DIContainer) -> None:
        """Calling install() twice on the same module must not double-register
        its providers."""

        class _Widget:
            """Module-level would be nicer but locality here is fine for count assertion."""

        @Configuration
        class WidgetModule:
            @Provider()
            def widget(self) -> object:
                return object()

        container.install(WidgetModule)
        before = len(container._bindings)
        container.install(WidgetModule)
        after = len(container._bindings)

        assert after == before

    def test_install_then_scan_registers_providers_once(
        self, container: DIContainer, tmp_path, monkeypatch
    ) -> None:
        """Explicit install(M) followed by scan() covering the same package
        must not double-register M's providers — the historical bug fixed by
        moving dedup authority to the container."""
        import sys
        import types

        pkg_name = "_providify_test_pkg_install_scan"
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []  # mark as package
        sys.modules[pkg_name] = pkg

        mod_name = f"{pkg_name}.mod"
        mod = types.ModuleType(mod_name)

        @Configuration
        class InfraModule:
            @Provider()
            def thing(self) -> object:
                return object()

        mod.InfraModule = InfraModule
        sys.modules[mod_name] = mod
        pkg.mod = mod

        try:
            container.install(InfraModule)
            before = len(container._bindings)
            container.scan(pkg_name)
            after = len(container._bindings)

            assert after == before
        finally:
            del sys.modules[mod_name]
            del sys.modules[pkg_name]


class TestScanAlphabeticalOrderDefeatingCase:
    def test_scan_installs_module_depending_on_alphabetically_later_module(
        self, container: DIContainer
    ) -> None:
        """A module named AlphaModule that depends on a module named
        ZuluModule must still resolve, even though scan()'s underlying
        inspect.getmembers() walk is alphabetical — proving install order,
        not declaration/scan order, determines what's available."""
        import sys
        import types

        pkg_name = "_providify_test_pkg_alpha_zulu"
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []
        sys.modules[pkg_name] = pkg

        mod_name = f"{pkg_name}.mod"
        mod = types.ModuleType(mod_name)

        @Configuration
        class ZuluModule:
            @Provider(scope=_Scope.SINGLETON)
            def pool(self) -> _Pool:
                return _Pool()

        @Configuration(depends_on=[ZuluModule])
        class AlphaModule:
            def __init__(self, pool: _Pool) -> None:
                self.pool = pool

        # inspect.getmodule() resolves via `obj.__module__` -> sys.modules
        # lookup; without reassigning __module__ to the fake module's name,
        # the scanner's re-export guard (`inspect.getmodule(obj) is not
        # module`) would skip both classes since they're actually defined in
        # this test module. Same convention as tests/test_scanner.py's
        # `_add()` helper.
        ZuluModule.__module__ = mod_name
        AlphaModule.__module__ = mod_name
        mod.AlphaModule = AlphaModule
        mod.ZuluModule = ZuluModule
        sys.modules[mod_name] = mod
        pkg.mod = mod

        try:
            # Scan the submodule directly — container.scan()'s default
            # recursive=False only walks the members of the module handed
            # to it, and pkgutil-based recursion cannot discover an
            # in-memory-only "submodule" with no real __path__ entries;
            # this mirrors how a real `container.scan("myapp.infra")` call
            # targets the module that actually declares the classes.
            container.scan(mod_name)
            assert container.get(_Pool) is not None
        finally:
            del sys.modules[mod_name]
            del sys.modules[pkg_name]


class TestModulePostConstruct:
    def test_post_construct_runs_once_before_providers_registered(
        self, container: DIContainer
    ) -> None:
        """A module's @PostConstruct must run exactly once, after __init__,
        before its @Provider methods are registered as bindings."""
        calls: list[str] = []

        @Configuration
        class ObservedModule:
            def __init__(self) -> None:
                calls.append("init")

            @PostConstruct
            def setup(self) -> None:
                calls.append("post_construct")

            @Provider()
            def thing(self) -> object:
                calls.append("provider_called")
                return object()

        container.install(ObservedModule)

        assert calls == ["init", "post_construct"]


class TestInstallTypeErrorGuardUnchanged:
    def test_install_of_non_configuration_class_raises_type_error(
        self, container: DIContainer
    ) -> None:
        class NotAModule: ...

        with pytest.raises(TypeError):
            container.install(NotAModule)


class TestInstallCycleLeavesNoPartialInstallation:
    def test_depends_on_cycle_raises_and_installs_nothing(self, container: DIContainer) -> None:
        """A cycle must be detected before any instantiation — binding count
        must be unchanged after the raise."""

        @Configuration
        class A: ...

        @Configuration
        class B: ...

        A = Configuration(depends_on=[B])(A)
        B = Configuration(depends_on=[A])(B)

        before = len(container._bindings)

        with pytest.raises(ModuleCycleError):
            container.install(A)

        assert len(container._bindings) == before


class TestAinstallParity:
    async def test_ainstall_installs_dependency_first_with_async_deps_and_post_construct(
        self, container: DIContainer
    ) -> None:
        """await ainstall(RepoModule) must mirror the sync ordering, support
        an async-resolved constructor dependency, and run an async
        @PostConstruct hook."""
        calls: list[str] = []

        @Configuration
        class InfraModule:
            @Provider(scope=_Scope.SINGLETON)
            async def pool(self) -> _Pool:
                return _Pool()

        @Configuration(depends_on=[InfraModule])
        class RepoModule:
            def __init__(self, pool: _Pool) -> None:
                self.pool = pool

            @PostConstruct
            async def setup(self) -> None:
                calls.append("post_construct")

            @Provider()
            def repo(self) -> _Repo:
                return _Repo()

        await container.ainstall(RepoModule)

        assert calls == ["post_construct"]
        assert await container.aget(_Pool) is not None
        assert await container.aget(_Repo) is not None
