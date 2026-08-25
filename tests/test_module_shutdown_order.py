"""Failing (red) tests for Plan 008 — multi-module shutdown ordering (F5).

These tests describe behaviour that does NOT exist yet:
    - Module ``@PreDestroy`` hooks running at ``shutdown()`` / ``ashutdown()``,
      in exact reverse install order, after every singleton has been torn down.
    - Failure aggregation of module teardown failures into the plan-004
      ``ShutdownError`` with ``owner == "ModuleClass.hook_name"``.
    - Idempotent double-shutdown (module hooks run once).
    - ``copy()`` producing a container whose ``shutdown()`` runs no module
      hooks (``owned=False``), leaving the original's module instances
      untouched.

See plans/008-module-startup-ordering.md, Step 10.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer
from providify.decorator.lifecycle import PreDestroy
from providify.decorator.module import Configuration
from providify.decorator.scope import Provider, Singleton
from providify.exceptions import ShutdownError


class TestModuleReverseInstallOrderTeardown:
    def test_modules_torn_down_in_exact_reverse_install_order(
        self, container: DIContainer
    ) -> None:
        """Infra -> Repo -> Service install order must tear down as
        Service -> Repo -> Infra."""
        order: list[str] = []

        @Configuration
        class InfraModule:
            @PreDestroy
            def close(self) -> None:
                order.append("Infra")

        @Configuration(depends_on=[InfraModule])
        class RepoModule:
            @PreDestroy
            def close(self) -> None:
                order.append("Repo")

        @Configuration(depends_on=[RepoModule])
        class ServiceModule:
            @PreDestroy
            def close(self) -> None:
                order.append("Service")

        container.install(ServiceModule)
        container.shutdown()

        assert order == ["Service", "Repo", "Infra"]


class TestSingletonsBeforeModules:
    def test_every_singleton_hook_runs_before_any_module_hook(
        self, container: DIContainer
    ) -> None:
        """A @Singleton component's @PreDestroy and a module's @PreDestroy
        both recording into the same list: every singleton hook must run
        before any module hook (modules teardown is phase 2)."""
        order: list[str] = []

        @Singleton
        class Component:
            @PreDestroy
            def teardown(self) -> None:
                order.append("Component")

        @Configuration
        class InfraModule:
            @PreDestroy
            def close(self) -> None:
                order.append("InfraModule")

        container.register(Component)
        container.install(InfraModule)

        container.get(Component)
        container.shutdown()

        assert order == ["Component", "InfraModule"]


class TestModulePreDestroyFailureAggregation:
    def test_failing_module_pre_destroy_is_aggregated_with_owner_name(
        self, container: DIContainer
    ) -> None:
        """A module @PreDestroy that raises must be aggregated into
        ShutdownError.failures with owner == 'InfraModule.close', and the
        remaining module hooks must still run."""
        order: list[str] = []

        @Configuration
        class InfraModule:
            @PreDestroy
            def close(self) -> None:
                raise ValueError("boom")

        @Configuration(depends_on=[InfraModule])
        class RepoModule:
            @PreDestroy
            def close(self) -> None:
                order.append("Repo")

        container.install(RepoModule)

        with pytest.raises(ShutdownError) as exc_info:
            container.shutdown()

        owners = [f.owner for f in exc_info.value.failures]
        assert "InfraModule.close" in owners
        assert order == ["Repo"]


class TestModuleShutdownIdempotency:
    def test_double_shutdown_runs_module_hooks_only_once(
        self, container: DIContainer
    ) -> None:
        calls: list[str] = []

        @Configuration
        class InfraModule:
            @PreDestroy
            def close(self) -> None:
                calls.append("Infra")

        container.install(InfraModule)

        container.shutdown()
        container.shutdown()  # second call: no-op for modules too

        assert calls == ["Infra"]


class TestModuleWithNoPreDestroySkipped:
    def test_module_with_no_pre_destroy_is_skipped_without_error(
        self, container: DIContainer
    ) -> None:
        @Configuration
        class QuietModule:
            @Provider()
            def thing(self) -> object:
                return object()

        container.install(QuietModule)

        container.shutdown()  # must not raise


class TestAsyncModuleShutdownParity:
    async def test_ashutdown_tears_down_modules_in_reverse_order_async(
        self, container: DIContainer
    ) -> None:
        order: list[str] = []

        @Configuration
        class InfraModule:
            @PreDestroy
            async def close(self) -> None:
                order.append("Infra")

        @Configuration(depends_on=[InfraModule])
        class RepoModule:
            @PreDestroy
            async def close(self) -> None:
                order.append("Repo")

        await container.ainstall(RepoModule)
        await container.ashutdown()

        assert order == ["Repo", "Infra"]


class TestSyncShutdownHittingAsyncModulePreDestroy:
    def test_sync_shutdown_reaching_async_module_pre_destroy_raises_runtime_error(
        self, container: DIContainer
    ) -> None:
        """Mirrors plan 004's un-aggregated RuntimeError guard for async hooks
        reached from sync shutdown() — the module case must behave the same,
        NOT be swallowed into ShutdownError.failures."""

        @Configuration
        class InfraModule:
            @PreDestroy
            async def close(self) -> None:
                pass

        container.install(InfraModule)

        with pytest.raises(RuntimeError, match="ashutdown"):
            container.shutdown()


class TestCopyRunsNoModuleHooks:
    def test_copy_shutdown_runs_no_module_hooks_and_original_untouched(
        self, container: DIContainer
    ) -> None:
        """container.copy() must produce a container whose shutdown() runs no
        module @PreDestroy hooks (owned=False); the original's module
        instances must remain untouched, still disposable by the original's
        own shutdown()."""
        calls: list[str] = []

        @Configuration
        class InfraModule:
            @PreDestroy
            def close(self) -> None:
                calls.append("Infra")

        container.install(InfraModule)

        copy = container.copy()
        copy.shutdown()

        assert calls == []

        container.shutdown()
        assert calls == ["Infra"]
