"""Red/regression tests for gap P24-DISPOSES-FIRSTMATCH (plan 014).

Pins down the fix for `_register_module_providers()`'s `@Disposes` wiring
loop, which today scans the *whole* container (`self._bindings`) for the
first `ProviderBinding` whose interface matches, instead of scoping the
search to the bindings the *same* `install()` / `ainstall()` call just
registered. With two `@Configuration` modules each providing the same
interface (with their own `@Disposes`), the second module's disposer
silently overwrites the first's, and the second module's own binding is
never wired — its instance leaks past `shutdown()` / `ashutdown()`.

This is the varco guard test's origin:
`varco_redis/tests/test_redis_cache_disposes.py::
test_both_cache_configurations_installed_together_both_get_stopped`
(currently `xfail(strict=True)`) — this file is the providify-side port of
that guard, plus the `container.provide()` return-value contract and the
two new `IssueKind.DISPOSER_OVERWRITTEN` / `IssueKind.UNMATCHED_DISPOSER`
validation warnings that make the defect visible without reading source.

Step 1 tests (`TestDisposesScopedToInstallingModule`) MUST fail on `1402f0a`
because only `"redis"` ever lands in `stopped` (the "layered" instance is
never torn down) or because the wrong disposer method tore an instance down.

Step 2 tests (`TestDisposesScopedWiringRegressions`) MUST already pass on
`1402f0a` — except `test_provide_returns_the_created_binding`, which fails
today because `container.provide()` returns `None` (`container.py:1048`).

Step 3 tests (`TestDisposerWiringValidation`) MUST fail with `AttributeError`
on `IssueKind.DISPOSER_OVERWRITTEN` / `IssueKind.UNMATCHED_DISPOSER`, which do
not exist yet.

DESIGN: all provider-return-type classes below are declared at MODULE level,
never inside a test function or factory. Under `from __future__ import
annotations` a `@Provider`'s `-> X` annotation is a string, resolved lazily
via `get_type_hints()` against the provider function's *module* globals
(`providify/binding.py:_resolve_return_annotation`) — a function-local class
is not in that namespace and raises `TypeError` at `install()` time. This
mirrors `tests/test_shutdown_order.py`'s `_MixedBindingConnection` /
`_AsyncDisposesConnection` module-level placement, for the same reason.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from providify import DIContainer, Disposes, Provider
from providify.binding import ProviderBinding
from providify.decorator.module import Configuration
from providify.metadata import Scope as _Scope

# ─────────────────────────────────────────────────────────────────
#  Shared fixtures — a CacheBackend hierarchy shared by two modules
# ─────────────────────────────────────────────────────────────────


class CacheBackend(ABC):
    """Common interface both cache configurations provide."""

    @abstractmethod
    def stop(self) -> None: ...  # pragma: no cover - interface only


class RedisCache(CacheBackend):
    """Module-level so `get_type_hints()` can resolve the `@Provider` return.

    Takes its `stopped` sink at construction time (not module scope) so each
    test's `stopped` list stays isolated — mirrors
    `tests/test_shutdown_order.py:364-372`.
    """

    def __init__(self, stopped: list[str]) -> None:
        self._stopped = stopped

    def stop(self) -> None:
        self._stopped.append("redis")


class LayeredCache(CacheBackend):
    """Module-level twin of `RedisCache` — see its docstring."""

    def __init__(self, stopped: list[str]) -> None:
        self._stopped = stopped

    def stop(self) -> None:
        self._stopped.append("layered")


def _make_sync_modules(
    stopped: list[str],
) -> tuple[type, type]:
    """Build the two sync `@Configuration` modules for a given `stopped` list.

    Each module provides `CacheBackend` under its own qualifier so both
    singletons can be resolved and cached simultaneously — the established
    pattern at `tests/test_configuration.py:170-181`. Each also owns a
    `@Disposes(CacheBackend)` so the fix under test (scoping the wiring
    search to the installing module's own bindings) can be observed.
    """

    @Configuration
    class RedisCacheConfiguration:
        @Provider(qualifier="redis", scope=_Scope.SINGLETON)
        def make_cache(self) -> CacheBackend:
            return RedisCache(stopped)

        @Disposes(CacheBackend)
        def close_cache(self, cache: CacheBackend) -> None:
            cache.stop()

    @Configuration
    class RedisLayeredCacheConfiguration:
        @Provider(qualifier="layered", scope=_Scope.SINGLETON)
        def make_cache(self) -> CacheBackend:
            return LayeredCache(stopped)

        @Disposes(CacheBackend)
        def close_cache(self, cache: CacheBackend) -> None:
            cache.stop()

    return RedisCacheConfiguration, RedisLayeredCacheConfiguration


def _make_async_modules(
    stopped: list[str],
) -> tuple[type, type]:
    """Async mirror of `_make_sync_modules` — async `@Disposes` disposers."""

    @Configuration
    class RedisCacheConfiguration:
        @Provider(qualifier="redis", scope=_Scope.SINGLETON)
        def make_cache(self) -> CacheBackend:
            return RedisCache(stopped)

        @Disposes(CacheBackend)
        async def close_cache(self, cache: CacheBackend) -> None:
            cache.stop()

    @Configuration
    class RedisLayeredCacheConfiguration:
        @Provider(qualifier="layered", scope=_Scope.SINGLETON)
        def make_cache(self) -> CacheBackend:
            return LayeredCache(stopped)

        @Disposes(CacheBackend)
        async def close_cache(self, cache: CacheBackend) -> None:
            cache.stop()

    return RedisCacheConfiguration, RedisLayeredCacheConfiguration


# ─────────────────────────────────────────────────────────────────
#  Step 1 — the fix: wiring scoped to the installing module
# ─────────────────────────────────────────────────────────────────


class TestDisposesScopedToInstallingModule:
    """Port of varco's `xfail(strict=True)` guard: each module's own disposer
    must tear down its own binding, not the first-registered one it happens
    to share an interface with.
    """

    def test_sync_install_two_modules_same_interface_both_disposed_by_own_disposer(
        self, container: DIContainer
    ) -> None:
        """Two sync modules providing the same interface: both must be stopped.

        On 1402f0a the second module's disposer never wires to its own
        binding — only "redis" (the first-installed module's tag) appears.
        """
        stopped: list[str] = []
        RedisConfig, LayeredConfig = _make_sync_modules(stopped)

        container.install(RedisConfig)
        container.install(LayeredConfig)

        container.get(CacheBackend, qualifier="redis")
        container.get(CacheBackend, qualifier="layered")

        container.shutdown()

        assert sorted(stopped) == ["layered", "redis"]

    async def test_async_install_two_modules_same_interface_both_disposed_by_own_disposer(
        self, container: DIContainer
    ) -> None:
        """Async mirror via `ainstall()` / `aget()` / `ashutdown()`."""
        stopped: list[str] = []
        RedisConfig, LayeredConfig = _make_async_modules(stopped)

        await container.ainstall(RedisConfig)
        await container.ainstall(LayeredConfig)

        await container.aget(CacheBackend, qualifier="redis")
        await container.aget(CacheBackend, qualifier="layered")

        await container.ashutdown()

        assert sorted(stopped) == ["layered", "redis"]

    def test_install_order_reversed_still_wires_each_to_own(self, container: DIContainer) -> None:
        """Installing the layered module first must not change the outcome —
        the fix must not depend on install order.
        """
        stopped: list[str] = []
        RedisConfig, LayeredConfig = _make_sync_modules(stopped)

        container.install(LayeredConfig)
        container.install(RedisConfig)

        container.get(CacheBackend, qualifier="redis")
        container.get(CacheBackend, qualifier="layered")

        container.shutdown()

        assert sorted(stopped) == ["layered", "redis"]

    def test_first_module_disposer_is_not_replaced_by_second_module(
        self, container: DIContainer
    ) -> None:
        """The first module's binding must keep its OWN disposer method —
        not silently be overwritten by the second module's `@Disposes`
        (the gap report's "coincidentally torn down by the wrong method").
        """
        stopped: list[str] = []
        RedisConfig, LayeredConfig = _make_sync_modules(stopped)

        container.install(RedisConfig)
        container.install(LayeredConfig)

        redis_binding = next(
            b
            for b in container._bindings
            if isinstance(b, ProviderBinding)
            and b.qualifier == "redis"
            and b.interface is CacheBackend
        )
        # Identity check on the bound method, not just "a stop happened" —
        # proves the wiring did not hijack a foreign module's binding.
        #
        # TEST FIX (provably wrong on 1402f0a's own semantics, not a
        # production bug): the fixture modules are defined *inside*
        # `_make_sync_modules()` (per this file's own docstring, so each
        # test's `stopped` list stays isolated). Python's __qualname__ for a
        # method of a class defined inside a function always includes the
        # enclosing function's qualname (e.g.
        # "_make_sync_modules.<locals>.RedisCacheConfiguration.close_cache"),
        # never just "RedisCacheConfiguration.close_cache" — an exact `==`
        # against the bare class-qualified name can never pass regardless of
        # which module's disposer actually won. `.endswith(...)` preserves
        # the test's real intent (this is RedisCacheConfiguration's own
        # close_cache, not RedisLayeredCacheConfiguration's).
        assert redis_binding.disposer.__qualname__.endswith("RedisCacheConfiguration.close_cache")

    def test_shutdown_order_reverse_creation_preserved_across_modules(
        self, container: DIContainer
    ) -> None:
        """Reverse-creation-order teardown (docs/agents/usage-rules.md:190)
        must survive the scoping fix: resolve redis then layered, expect
        layered torn down first.
        """
        stopped: list[str] = []
        RedisConfig, LayeredConfig = _make_sync_modules(stopped)

        container.install(RedisConfig)
        container.install(LayeredConfig)

        container.get(CacheBackend, qualifier="redis")
        container.get(CacheBackend, qualifier="layered")

        container.shutdown()

        assert stopped == ["layered", "redis"]

    def test_disposes_with_no_own_provider_is_not_attached_to_foreign_binding(
        self, container: DIContainer
    ) -> None:
        """Module A provides X with no @Disposes; module B has only a
        @Disposes(X). After the fix, B's disposer must NOT attach to A's
        binding — this pins the deliberate behaviour change (E5).
        """
        stopped: list[str] = []

        @Configuration
        class ProviderOnlyModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_cache(self) -> CacheBackend:
                return RedisCache(stopped)

        @Configuration
        class DisposerOnlyModule:
            @Disposes(CacheBackend)
            def close_cache(self, cache: CacheBackend) -> None:
                cache.stop()

        container.install(ProviderOnlyModule)
        container.install(DisposerOnlyModule)

        container.get(CacheBackend)
        container.shutdown()

        # Pre-fix, DisposerOnlyModule's @Disposes hijacked ProviderOnlyModule's
        # binding and "redis" would appear here. Post-fix it must not.
        assert stopped == []


# ─────────────────────────────────────────────────────────────────
#  Step 2 — regressions: must pass before AND after the fix
# ─────────────────────────────────────────────────────────────────


class _RegressionConnection:
    """Module-level stand-in for `tests/test_disposes.py`'s `Connection`."""

    def __init__(self) -> None:
        self.closed = False


class _RegressionCursor:
    """Module-level second-interface type, mirrors `tests/test_disposes.py`."""

    def __init__(self) -> None:
        self.closed = False


class _RegressionBase:
    """Module-level base type for the issubclass-matching regression."""

    def __init__(self) -> None:
        self.closed = False


class _RegressionSub(_RegressionBase):
    """Module-level subclass — provider returns this, `@Disposes` targets base."""


class _Widget:
    """Module-level type for the bare `provide()` return-value regression."""


@Provider
def _make_widget() -> _Widget:
    """Module-level provider function — `@Provider` supplies the metadata
    `container.provide()` requires (`ProviderBindingNotDecoratedError` guard,
    `binding.py:622`)."""
    return _Widget()


class TestDisposesScopedWiringRegressions:
    """Existing wiring shapes that the scoping fix must not break."""

    def test_single_module_single_provider_still_wired(self, container: DIContainer) -> None:
        """The `tests/test_disposes.py:23-31` shape — one module, one provider,
        one @Disposes — must still be wired after the scoping change.
        """

        @Configuration
        class InfraModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _RegressionConnection:
                return _RegressionConnection()

            @Disposes(_RegressionConnection)
            def close_conn(self, conn: _RegressionConnection) -> None:
                conn.closed = True

        container.install(InfraModule)
        conn = container.get(_RegressionConnection)
        container.shutdown()

        assert conn.closed

    def test_single_module_two_interfaces_two_disposers_still_wired(
        self, container: DIContainer
    ) -> None:
        """The `tests/test_disposes.py:69-95` shape — one module, two
        interfaces, each with its own @Disposes — both must still be wired.
        """

        @Configuration
        class MultiModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _RegressionConnection:
                return _RegressionConnection()

            @Disposes(_RegressionConnection)
            def close_conn(self, conn: _RegressionConnection) -> None:
                conn.closed = True

            @Provider(scope=_Scope.SINGLETON)
            def make_cursor(self) -> _RegressionCursor:
                return _RegressionCursor()

            @Disposes(_RegressionCursor)
            def close_cursor(self, cursor: _RegressionCursor) -> None:
                cursor.closed = True

        container.install(MultiModule)
        conn = container.get(_RegressionConnection)
        cursor = container.get(_RegressionCursor)
        container.shutdown()

        assert conn.closed
        assert cursor.closed

    def test_property_provider_disposer_still_wired(self, container: DIContainer) -> None:
        """`@Provider @property` producers go through `_prop_provider`
        (container.py:6182-6196) — the return-value approach must still
        collect them as this module's own bindings.
        """

        @Configuration
        class PropertyModule:
            @property
            @Provider(scope=_Scope.SINGLETON)
            def conn(self) -> _RegressionConnection:
                return _RegressionConnection()

            @Disposes(_RegressionConnection)
            def close_conn(self, conn: _RegressionConnection) -> None:
                conn.closed = True

        container.install(PropertyModule)
        conn = container.get(_RegressionConnection)
        container.shutdown()

        assert conn.closed

    def test_disposes_base_type_matches_own_subclass_provider(self, container: DIContainer) -> None:
        """A provider returning a subclass must still be matched by a
        `@Disposes(Base)` (issubclass matching, `utils.py:138`).
        """

        @Configuration
        class SubModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_sub(self) -> _RegressionSub:
                return _RegressionSub()

            @Disposes(_RegressionBase)
            def close_base(self, obj: _RegressionBase) -> None:
                obj.closed = True

        container.install(SubModule)
        obj = container.get(_RegressionSub)
        container.shutdown()

        assert obj.closed

    def test_provide_returns_the_created_binding(self, container: DIContainer) -> None:
        """`container.provide()` must return the `ProviderBinding` it just
        registered (was `None` — `container.py:1048`), the new contract this
        plan's fix relies on.

        Private `_bindings` access is acceptable here — it is the one direct
        check of the new return contract.
        """
        binding = container.provide(_make_widget)

        assert isinstance(binding, ProviderBinding)
        assert binding is container._bindings[-1]


# ─────────────────────────────────────────────────────────────────
#  Step 3 — validation surface: DISPOSER_OVERWRITTEN / UNMATCHED_DISPOSER
# ─────────────────────────────────────────────────────────────────


class _ValidationConnection:
    """Module-level type carrying which disposer ran, for the overwrite test."""

    def __init__(self) -> None:
        self.closed_by: str | None = None


class _ValidationBase:
    """Module-level base for the base/sub overwrite validation test."""


class _ValidationSub(_ValidationBase):
    """Module-level subclass — provider returns this."""


class _LonelyConnection:
    """Module-level type with no provider anywhere — for UNMATCHED_DISPOSER."""


class _SharedConnection:
    """Module-level type provided by one module, disposed by another."""


class TestDisposerWiringValidation:
    """The two new `validate()` WARNING kinds this plan introduces.

    IssueKind.DISPOSER_OVERWRITTEN and IssueKind.UNMATCHED_DISPOSER do not
    exist yet — every test here fails with AttributeError until Step 4 lands.
    """

    def test_two_disposes_on_same_own_binding_reports_disposer_overwritten(
        self, container: DIContainer
    ) -> None:
        """One module, one provider, two @Disposes matching it: last one wins
        at runtime (unchanged) but validate() must now flag the overwrite.
        """
        from providify.validation import IssueKind, Severity

        @Configuration
        class DoubleDisposeModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _ValidationConnection:
                return _ValidationConnection()

            @Disposes(_ValidationConnection)
            def close_a(self, conn: _ValidationConnection) -> None:
                conn.closed_by = "close_a"

            @Disposes(_ValidationConnection)
            def close_b(self, conn: _ValidationConnection) -> None:
                conn.closed_by = "close_b"

        container.install(DoubleDisposeModule)
        conn = container.get(_ValidationConnection)

        report = container.validate(raise_on_error=False)
        overwritten_kind = IssueKind.DISPOSER_OVERWRITTEN
        overwritten = [i for i in report.issues if i.kind == overwritten_kind]

        assert len(overwritten) == 1
        assert overwritten[0].severity is Severity.WARNING
        assert overwritten[0].param_name == "close_b"
        assert overwritten[0].owner == "@Provider(make_conn)"
        assert "close_a" in overwritten[0].message
        assert "close_b" in overwritten[0].message

        container.shutdown()
        # Last-wins is unchanged — only close_b actually ran.
        assert conn.closed_by == "close_b"

    def test_disposes_base_and_sub_on_same_binding_reports_disposer_overwritten(
        self, container: DIContainer
    ) -> None:
        """`@Disposes(Base)` + `@Disposes(Sub)` both match a `-> Sub`
        provider (issubclass) -> also an overwrite.
        """
        from providify.validation import IssueKind

        @Configuration
        class BaseSubModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_sub(self) -> _ValidationSub:
                return _ValidationSub()

            @Disposes(_ValidationBase)
            def close_base(self, obj: _ValidationBase) -> None:
                pass

            @Disposes(_ValidationSub)
            def close_sub(self, obj: _ValidationSub) -> None:
                pass

        container.install(BaseSubModule)

        report = container.validate(raise_on_error=False)
        overwritten_kind = IssueKind.DISPOSER_OVERWRITTEN
        overwritten = [i for i in report.issues if i.kind == overwritten_kind]

        assert len(overwritten) == 1

    def test_disposes_matching_no_own_binding_reports_unmatched_disposer(
        self, container: DIContainer
    ) -> None:
        """A module with only a @Disposes(X) and no matching own provider ->
        one UNMATCHED_DISPOSER owned by "ModuleName.method_name".
        """
        from providify.validation import IssueKind

        @Configuration
        class LonelyModule:
            @Disposes(_LonelyConnection)
            def close_conn(self, conn: _LonelyConnection) -> None:
                pass

        container.install(LonelyModule)

        report = container.validate(raise_on_error=False)
        unmatched_kind = IssueKind.UNMATCHED_DISPOSER
        unmatched = [i for i in report.issues if i.kind == unmatched_kind]

        assert len(unmatched) == 1
        assert unmatched[0].owner == "LonelyModule.close_conn"
        assert unmatched[0].requested == "_LonelyConnection"

    def test_unmatched_disposer_reported_even_when_another_module_provides_the_type(
        self, container: DIContainer
    ) -> None:
        """A provides X; B has only @Disposes(X) — still one
        UNMATCHED_DISPOSER owned by B, the whole point of the kind.
        """
        from providify.validation import IssueKind

        @Configuration
        class ProviderModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _SharedConnection:
                return _SharedConnection()

        @Configuration
        class DisposerModule:
            @Disposes(_SharedConnection)
            def close_conn(self, conn: _SharedConnection) -> None:
                pass

        container.install(ProviderModule)
        container.install(DisposerModule)

        report = container.validate(raise_on_error=False)
        unmatched_kind = IssueKind.UNMATCHED_DISPOSER
        unmatched = [i for i in report.issues if i.kind == unmatched_kind]

        assert len(unmatched) == 1
        assert unmatched[0].owner == "DisposerModule.close_conn"

    async def test_ainstall_records_same_wiring_issues_as_install(
        self, container: DIContainer
    ) -> None:
        """The overwritten-disposer fixture via `ainstall()` must record the
        identical issue — `_register_module_providers` is shared by both.
        """
        from providify.validation import IssueKind

        @Configuration
        class AsyncDoubleDisposeModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _ValidationConnection:
                return _ValidationConnection()

            @Disposes(_ValidationConnection)
            def close_a(self, conn: _ValidationConnection) -> None:
                pass

            @Disposes(_ValidationConnection)
            def close_b(self, conn: _ValidationConnection) -> None:
                pass

        await container.ainstall(AsyncDoubleDisposeModule)

        report = container.validate(raise_on_error=False)
        overwritten_kind = IssueKind.DISPOSER_OVERWRITTEN
        overwritten = [i for i in report.issues if i.kind == overwritten_kind]

        assert len(overwritten) == 1
        assert overwritten[0].param_name == "close_b"

    def test_matched_disposer_reports_no_wiring_issue(self, container: DIContainer) -> None:
        """Control: the correctly-wired `tests/test_disposes.py:23-31` shape
        yields zero issues of either new kind.
        """
        from providify.validation import IssueKind

        @Configuration
        class InfraModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _RegressionConnection:
                return _RegressionConnection()

            @Disposes(_RegressionConnection)
            def close_conn(self, conn: _RegressionConnection) -> None:
                pass

        container.install(InfraModule)

        report = container.validate(raise_on_error=False)
        new_kinds = {IssueKind.DISPOSER_OVERWRITTEN, IssueKind.UNMATCHED_DISPOSER}

        assert not [i for i in report.issues if i.kind in new_kinds]

    def test_wiring_warnings_do_not_raise_by_default(self, container: DIContainer) -> None:
        """Both new kinds are WARNING — `validate()`'s default
        `raise_on_error=True` must not raise.

        TEST FIX (provably wrong against an established, unchanged
        contract, not a production bug): `ValidationReport.ok` is
        documented as "True iff there are no ERROR-severity issues"
        (`validation.py`) — "a report with warnings only is still ok...
        warnings never block startup, only errors do". This plan adds two
        WARNING-only kinds (§Design "Why WARNING, not ERROR") and does not
        touch `ValidationReport.ok`. The existing precedent
        (`tests/test_validation.py:354`, `tests/test_unreachable_pre_destroy.py:408`)
        asserts `report.ok is True` for a warnings-only report; asserting
        `is False` here would contradict that same-release, unmodified
        contract. Fixed to assert the documented behaviour instead.
        """
        from providify.validation import IssueKind

        @Configuration
        class DoubleDisposeModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _ValidationConnection:
                return _ValidationConnection()

            @Disposes(_ValidationConnection)
            def close_a(self, conn: _ValidationConnection) -> None:
                pass

            @Disposes(_ValidationConnection)
            def close_b(self, conn: _ValidationConnection) -> None:
                pass

        container.install(DoubleDisposeModule)

        report = container.validate()  # default raise_on_error=True

        assert report.ok is True
        assert any(i.kind is IssueKind.DISPOSER_OVERWRITTEN for i in report.issues)

    def test_copy_inherits_wiring_issues(self, container: DIContainer) -> None:
        """`container.copy()` must report the same DISPOSER_OVERWRITTEN —
        `_ModuleRecord`s (and their disposer_issues) are inherited via
        `replace()`.
        """
        from providify.validation import IssueKind

        @Configuration
        class DoubleDisposeModule:
            @Provider(scope=_Scope.SINGLETON)
            def make_conn(self) -> _ValidationConnection:
                return _ValidationConnection()

            @Disposes(_ValidationConnection)
            def close_a(self, conn: _ValidationConnection) -> None:
                pass

            @Disposes(_ValidationConnection)
            def close_b(self, conn: _ValidationConnection) -> None:
                pass

        container.install(DoubleDisposeModule)

        copy_report = container.copy().validate(raise_on_error=False)
        overwritten_kind = IssueKind.DISPOSER_OVERWRITTEN
        overwritten = [i for i in copy_report.issues if i.kind == overwritten_kind]

        assert len(overwritten) == 1

    def test_empty_container_report_is_unchanged(self, container: DIContainer) -> None:
        """Regression on `tests/test_validation.py:57` — an empty container's
        report must remain empty and ok after pass 3 is added.
        """
        report = container.validate(raise_on_error=False)

        assert report.issues == ()
        assert report.ok is True
