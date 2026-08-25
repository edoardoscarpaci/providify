"""Unit tests for warm_up() and awarm_up().

Verifies that eager singleton instantiation behaves correctly in both
sync and async contexts — all-or-nothing guard, qualifier/priority
filtering, double-construction prevention, and error propagation.

Covered:
    - warm_up(): all singletons, qualifier filter, priority filter
    - warm_up(): raises RuntimeError for async providers
    - warm_up(): does not double-construct already-cached singletons
    - awarm_up(): all singletons, async providers
    - awarm_up(): mix of sync and async providers
    - awarm_up(): does not double-construct already-cached singletons
    - Both: no-op when no matching singletons exist
"""

from __future__ import annotations

import pytest

from providify import DIContainer, Provider, Singleton

# ─────────────────────────────────────────────────────────────────
#  Domain types — simple singletons with construction counters
# ─────────────────────────────────────────────────────────────────


@Singleton
class Alpha:
    """Simple singleton — tracks how many times it was constructed."""

    instances_created: int = 0

    def __init__(self) -> None:
        Alpha.instances_created += 1

    @classmethod
    def reset(cls) -> None:
        """Reset the counter between tests."""
        cls.instances_created = 0


@Singleton(qualifier="beta-q")
class Beta:
    """Singleton with a qualifier — used to test qualifier filtering."""

    instances_created: int = 0

    def __init__(self) -> None:
        Beta.instances_created += 1

    @classmethod
    def reset(cls) -> None:
        cls.instances_created = 0


@Singleton(priority=5)
class Gamma:
    """Singleton with a priority — used to test priority filtering."""

    instances_created: int = 0

    def __init__(self) -> None:
        Gamma.instances_created += 1

    @classmethod
    def reset(cls) -> None:
        cls.instances_created = 0


# ─────────────────────────────────────────────────────────────────
#  Helpers — autouse counter reset so tests don't bleed state
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_counters() -> None:
    """Reset per-class counters before every test.

    DESIGN: class-level counters are used instead of instance attributes
    because warm_up creates the instance — we can't inspect it without
    resolving, which would defeat the purpose of the test.
    """
    Alpha.reset()
    Beta.reset()
    Gamma.reset()
    yield


# ─────────────────────────────────────────────────────────────────
#  warm_up() — synchronous
# ─────────────────────────────────────────────────────────────────


class TestWarmUp:
    """Tests for DIContainer.warm_up() — synchronous eager instantiation."""

    def test_warm_up_instantiates_all_singletons(self, container: DIContainer) -> None:
        """warm_up() with no filters must instantiate every registered singleton."""
        container.register(Alpha)
        container.register(Beta)

        container.warm_up()

        # Each class should have been constructed exactly once
        assert Alpha.instances_created == 1
        assert Beta.instances_created == 1

    def test_warm_up_qualifier_filter(self, container: DIContainer) -> None:
        """warm_up(qualifier=...) only instantiates singletons with that qualifier."""
        container.register(Alpha)  # no qualifier
        container.register(Beta)  # qualifier="beta-q"

        container.warm_up(qualifier="beta-q")

        # Only Beta should have been warmed up
        assert Alpha.instances_created == 0
        assert Beta.instances_created == 1

    def test_warm_up_priority_filter(self, container: DIContainer) -> None:
        """warm_up(priority=...) only instantiates singletons with that exact priority."""
        container.register(Alpha)  # priority=0 (default)
        container.register(Gamma)  # priority=5

        container.warm_up(priority=5)

        # Only Gamma matches priority=5
        assert Alpha.instances_created == 0
        assert Gamma.instances_created == 1

    def test_warm_up_noop_when_no_singletons(self, container: DIContainer) -> None:
        """warm_up() on an empty container (or with no singletons) must be a no-op."""
        from providify import Component

        @Component
        class DependentOnly:
            pass

        container.register(DependentOnly)

        # Should not raise and should not construct anything
        container.warm_up()

        assert Alpha.instances_created == 0

    def test_warm_up_does_not_double_construct(self, container: DIContainer) -> None:
        """warm_up() after a get() must not construct the singleton a second time.

        _instantiate_sync returns the cached instance — warm_up must not
        bypass the cache check.
        """
        container.register(Alpha)

        # First construction — via get()
        container.get(Alpha)
        assert Alpha.instances_created == 1

        # warm_up() must see the cached instance and skip construction
        container.warm_up()
        assert Alpha.instances_created == 1  # still 1 — not constructed again

    def test_warm_up_raises_for_async_provider(self, container: DIContainer) -> None:
        """warm_up() must raise RuntimeError before touching the cache when any
        singleton is backed by an async provider.

        DESIGN: all-or-nothing — the guard checks ALL bindings before
        instantiating ANY of them, so the cache is never partially warmed.
        """

        @Provider(singleton=True)
        async def make_alpha() -> Alpha:
            return Alpha()

        container.register(Alpha)  # sync singleton
        container.provide(make_alpha)  # async singleton — triggers guard

        with pytest.raises(RuntimeError):
            container.warm_up()

        # The sync singleton must NOT have been cached either (all-or-nothing)
        assert Alpha.instances_created == 0

    def test_warm_up_noop_when_qualifier_matches_nothing(
        self, container: DIContainer
    ) -> None:
        """warm_up(qualifier='nonexistent') with no matching bindings is a no-op."""
        container.register(Alpha)

        # Should not raise — just silently do nothing
        container.warm_up(qualifier="nonexistent")

        assert Alpha.instances_created == 0


# ─────────────────────────────────────────────────────────────────
#  awarm_up() — asynchronous
# ─────────────────────────────────────────────────────────────────


class TestAWarmUp:
    """Tests for DIContainer.awarm_up() — async eager instantiation."""

    @pytest.mark.asyncio
    async def test_awarm_up_instantiates_sync_singletons(
        self, container: DIContainer
    ) -> None:
        """awarm_up() handles plain sync singletons without needing async providers."""
        container.register(Alpha)
        container.register(Beta)

        await container.awarm_up()

        assert Alpha.instances_created == 1
        assert Beta.instances_created == 1

    @pytest.mark.asyncio
    async def test_awarm_up_handles_async_provider(
        self, container: DIContainer
    ) -> None:
        """awarm_up() must await async singleton providers and cache the result."""
        constructed: list[int] = []  # track order of construction

        @Provider(singleton=True)
        async def async_alpha() -> Alpha:
            # Simulates async work (no actual IO needed in tests)
            constructed.append(1)
            return Alpha()

        container.provide(async_alpha)

        await container.awarm_up()

        # Provider was called exactly once
        assert len(constructed) == 1

    @pytest.mark.asyncio
    async def test_awarm_up_handles_mixed_providers(
        self, container: DIContainer
    ) -> None:
        """awarm_up() handles a mix of sync and async singleton providers."""
        async_calls: list[str] = []

        @Provider(singleton=True)
        async def async_beta() -> Beta:
            async_calls.append("beta")
            return Beta()

        container.register(Alpha)  # sync singleton
        container.provide(async_beta)  # async singleton

        await container.awarm_up()

        assert Alpha.instances_created == 1
        assert len(async_calls) == 1

    @pytest.mark.asyncio
    async def test_awarm_up_does_not_double_construct(
        self, container: DIContainer
    ) -> None:
        """awarm_up() after aget() must not construct the singleton a second time."""
        container.register(Alpha)

        # First construction — via aget()
        await container.aget(Alpha)
        assert Alpha.instances_created == 1

        # awarm_up() must hit the cache and skip construction
        await container.awarm_up()
        assert Alpha.instances_created == 1

    @pytest.mark.asyncio
    async def test_awarm_up_noop_when_no_singletons(
        self, container: DIContainer
    ) -> None:
        """awarm_up() on a container with only DEPENDENT bindings is a no-op."""
        from providify import Component

        @Component
        class Transient:
            pass

        container.register(Transient)

        # Should not raise and should not construct anything
        await container.awarm_up()

    @pytest.mark.asyncio
    async def test_awarm_up_qualifier_filter(self, container: DIContainer) -> None:
        """awarm_up(qualifier=...) only instantiates singletons with that qualifier."""
        container.register(Alpha)  # no qualifier
        container.register(Beta)  # qualifier="beta-q"

        await container.awarm_up(qualifier="beta-q")

        assert Alpha.instances_created == 0
        assert Beta.instances_created == 1
