"""Unit tests for circular dependency detection.

Covered:
    - A → B → A: two-class cycle raises CircularDependencyError
    - A → B → C → A: three-class cycle raises CircularDependencyError
    - Error message contains the human-readable cycle chain (e.g. "A → B → A")
    - Lazy[T] breaks a cycle that would otherwise raise
    - Non-circular dep graph resolves successfully (no false positives)
    - Diamond pattern (shared dep, not a cycle) resolves correctly

DESIGN NOTE: Cycle detection uses a ContextVar[list[type]] — the resolution
stack is isolated per asyncio Task and per thread. This means a concurrent
aget() for a different type doesn't interfere with the current resolution.

WHY MODULE-LEVEL CLASSES:
All test classes are defined at module level (not inside test methods) because
`from __future__ import annotations` turns every annotation into a lazy string.
`get_type_hints()` resolves those strings from the function's __globals__, which
is the *module* namespace — not the local scope of the enclosing test function.
Classes defined locally inside a test function are invisible to get_type_hints(),
so the container would silently skip their constructor parameters.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from providify.container import DIContainer
from providify.decorator.scope import Component, Provider, Singleton
from providify.exceptions import CircularDependencyError
from providify.type import Lazy

# ─────────────────────────────────────────────────────────────────
#  Two-class cycle: _TwoA → _TwoB → _TwoA
#  _TwoA is defined before _TwoB, so the annotation is a forward
#  reference. With PEP 563, it becomes a lazy string resolved later
#  by get_type_hints() — both classes are in module globals by then.
# ─────────────────────────────────────────────────────────────────


@Component
class _TwoA:
    def __init__(self, b: _TwoB) -> None:
        self.b = b


@Component
class _TwoB:
    def __init__(self, a: _TwoA) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  Three-class cycle: _ThreeA → _ThreeB → _ThreeC → _ThreeA
# ─────────────────────────────────────────────────────────────────


@Component
class _ThreeA:
    def __init__(self, b: _ThreeB) -> None:
        self.b = b


@Component
class _ThreeB:
    def __init__(self, c: _ThreeC) -> None:
        self.c = c


@Component
class _ThreeC:
    def __init__(self, a: _ThreeA) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  Error-message cycle — named distinctly to check name in output
# ─────────────────────────────────────────────────────────────────


@Component
class _CycleAlpha:
    def __init__(self, beta: _CycleBeta) -> None:
        self.beta = beta


@Component
class _CycleBeta:
    def __init__(self, alpha: _CycleAlpha) -> None:
        self.alpha = alpha


# ─────────────────────────────────────────────────────────────────
#  Lazy cycle-break: _LazyA holds Lazy[_LazyB], _LazyB holds _LazyA
#  Lazy defers _LazyB's resolution past _LazyA's constructor return,
#  so the cycle-detection stack never sees both at the same time.
# ─────────────────────────────────────────────────────────────────


@Singleton
class _LazyA:
    def __init__(self, b: Lazy[_LazyB]) -> None:  # type: ignore[valid-type]
        self.b = b


@Singleton
class _LazyB:
    def __init__(self, a: _LazyA) -> None:
        self.a = a


# ─────────────────────────────────────────────────────────────────
#  Non-circular linear chain: _LinearA → _LinearB → _LinearC
# ─────────────────────────────────────────────────────────────────


@Component
class _LinearC:
    pass


@Component
class _LinearB:
    def __init__(self, c: _LinearC) -> None:
        self.c = c


@Component
class _LinearA:
    def __init__(self, b: _LinearB) -> None:
        self.b = b


# ─────────────────────────────────────────────────────────────────
#  Diamond: _DiamondA → {_DiamondB, _DiamondC} → _DiamondD
#  _DiamondD is a shared dep, not a cycle — it appears twice in the
#  resolution tree but never simultaneously in the same stack path.
# ─────────────────────────────────────────────────────────────────


@Component
class _DiamondD:
    pass


@Component
class _DiamondB:
    def __init__(self, d: _DiamondD) -> None:
        self.d = d


@Component
class _DiamondC:
    def __init__(self, d: _DiamondD) -> None:
        self.d = d


@Component
class _DiamondA:
    def __init__(self, b: _DiamondB, c: _DiamondC) -> None:
        self.b = b
        self.c = c


# ─────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────


class TestCircularDependencyDetection:
    """Tests for CircularDependencyError detection and reporting."""

    def test_two_class_cycle_raises(self, container: DIContainer) -> None:
        """_TwoA → _TwoB → _TwoA must raise CircularDependencyError."""
        container.register(_TwoA)
        container.register(_TwoB)

        with pytest.raises(CircularDependencyError):
            container.get(_TwoA)

    def test_three_class_cycle_raises(self, container: DIContainer) -> None:
        """_ThreeA → _ThreeB → _ThreeC → _ThreeA must raise CircularDependencyError."""
        container.register(_ThreeA)
        container.register(_ThreeB)
        container.register(_ThreeC)

        with pytest.raises(CircularDependencyError):
            container.get(_ThreeA)

    def test_error_message_contains_cycle_chain(self, container: DIContainer) -> None:
        """CircularDependencyError message must contain both class names in the chain."""
        container.register(_CycleAlpha)
        container.register(_CycleBeta)

        with pytest.raises(CircularDependencyError) as exc_info:
            container.get(_CycleAlpha)

        error_text = str(exc_info.value)
        assert "_CycleAlpha" in error_text
        assert "_CycleBeta" in error_text

    def test_lazy_breaks_two_class_cycle(self, container: DIContainer) -> None:
        """Lazy[T] must allow _LazyA → _LazyB → _LazyA to resolve without error.

        Lazy[_LazyB] in _LazyA's constructor creates a proxy without resolving
        _LazyB. _LazyB's constructor then resolves _LazyA (already constructed),
        so no cycle is detected.
        """
        container.register(_LazyA)
        container.register(_LazyB)

        # Must NOT raise — Lazy[_LazyB] defers _LazyB's resolution
        a = container.get(_LazyA)
        assert isinstance(a, _LazyA)

    def test_non_circular_graph_resolves_correctly(
        self, container: DIContainer
    ) -> None:
        """A linear _LinearA → _LinearB → _LinearC (no cycle) must resolve cleanly."""
        container.register(_LinearC)
        container.register(_LinearB)
        container.register(_LinearA)

        a = container.get(_LinearA)

        assert isinstance(a, _LinearA)
        assert isinstance(a.b, _LinearB)
        assert isinstance(a.b.c, _LinearC)

    def test_diamond_dependency_resolves_correctly(
        self, container: DIContainer
    ) -> None:
        """Diamond pattern must not trigger a false CircularDependencyError.

        _DiamondD is a shared dependency — it appears twice in the resolution
        tree but is never on the same stack path simultaneously, so it is not
        a cycle.
        """
        container.register(_DiamondD)
        container.register(_DiamondB)
        container.register(_DiamondC)
        container.register(_DiamondA)

        a = container.get(_DiamondA)

        assert isinstance(a.b.d, _DiamondD)
        assert isinstance(a.c.d, _DiamondD)


# ─────────────────────────────────────────────────────────────────
#  Self-referential SINGLETON — the deadlock regression
#
#  A bare `object` annotation matches EVERY registered binding
#  (`issubclass(anything, object)` is always True), so a singleton whose
#  own constructor/provider takes an `object` parameter can resolve back
#  to itself. On the DEPENDENT path that is caught cleanly by
#  `_check_cycle`; on the SINGLETON path the per-key lock used for
#  double-check locking is acquired BEFORE `create()` runs, so the
#  re-entrant `get()` blocked on a non-reentrant lock held by its own
#  thread — a permanent hang rather than an error.
#
#  `object` is merely the easiest trigger: ANY self-referential singleton
#  reaches the same lock. These tests assert the failure is reported, not
#  slept on.
# ─────────────────────────────────────────────────────────────────


@Singleton
class _SelfRefSingleton:
    """Singleton whose parameter type matches its own binding via `object`."""

    def __init__(self, unbound: object | None = None) -> None:
        self.unbound = unbound


class _SelfRefProduct:
    """Plain product type returned by the self-referential provider below."""


@Singleton
class _ConcurrentSingleton:
    """Well-behaved singleton used as the concurrency control case.

    Deliberately has no injected parameters — the control test is about the
    per-key lock still serialising creation, not about dependency resolution.
    """


@Provider(singleton=True)
def _self_ref_provider(unbound: object | None = None) -> _SelfRefProduct:
    """Singleton provider whose own parameter resolves back to this binding."""
    return _SelfRefProduct()


def _run_with_timeout(fn: Callable[[], object], timeout: float = 5.0) -> BaseException:
    """Run *fn* in a daemon thread and return the exception it raised.

    A deadlock cannot be expressed as a normal assertion — the call never
    returns — so the call under test is pushed onto a throwaway thread and
    the test fails if it has not finished within *timeout*.

    Args:
        fn:      Zero-argument callable to execute.
        timeout: Seconds to wait before declaring a deadlock.

    Returns:
        The exception raised by *fn*.

    Raises:
        Failed: (via ``pytest.fail``) if *fn* did not finish within *timeout*,
            or if it returned successfully instead of raising.

    Edge cases:
        - The worker is a daemon thread, so a genuinely deadlocked run does
          not prevent the interpreter from exiting; it simply leaks one
          blocked thread for the remainder of the session.
    """
    outcome: dict[str, BaseException | None] = {}
    finished = threading.Event()

    def target() -> None:
        try:
            fn()
            outcome["exc"] = None
        except BaseException as exc:  # noqa: BLE001 — re-raised to the test below
            outcome["exc"] = exc
        finally:
            finished.set()

    threading.Thread(target=target, daemon=True).start()

    if not finished.wait(timeout):
        pytest.fail(
            f"Resolution deadlocked: no result after {timeout}s. "
            f"A self-referential singleton must raise CircularDependencyError, "
            f"not block on its own per-key creation lock."
        )

    exc = outcome["exc"]
    if exc is None:
        pytest.fail("Expected CircularDependencyError, but resolution succeeded.")
    return exc


class TestSelfReferentialSingletonDoesNotDeadlock:
    """A singleton that depends on itself must raise, never hang.

    Regression tests for the per-key singleton lock being non-reentrant:
    `_instantiate_sync` / `_instantiate_async` held it across `create()`,
    so a re-entrant resolution of the same key from the same thread/task
    waited on a lock it already owned.
    """

    def test_self_referential_singleton_class_sync(
        self, container: DIContainer
    ) -> None:
        """`container.get()` on a self-referential singleton class must raise."""
        container.bind(_SelfRefSingleton, _SelfRefSingleton)

        exc = _run_with_timeout(lambda: container.get(_SelfRefSingleton))

        assert isinstance(exc, CircularDependencyError)
        assert "_SelfRefSingleton" in str(exc)

    def test_self_referential_singleton_class_async(
        self, container: DIContainer
    ) -> None:
        """`container.aget()` must fail the same way as the sync path."""
        container.bind(_SelfRefSingleton, _SelfRefSingleton)

        exc = _run_with_timeout(lambda: asyncio.run(container.aget(_SelfRefSingleton)))

        assert isinstance(exc, CircularDependencyError)
        assert "_SelfRefSingleton" in str(exc)

    def test_self_referential_singleton_provider_sync(
        self, container: DIContainer
    ) -> None:
        """A `@Provider(singleton=True)` resolving back to itself must raise."""
        container.provide(_self_ref_provider)

        exc = _run_with_timeout(lambda: container.get(_SelfRefProduct))

        assert isinstance(exc, CircularDependencyError)
        assert "_SelfRefProduct" in str(exc)

    def test_self_referential_singleton_provider_async(
        self, container: DIContainer
    ) -> None:
        """Async mirror of the provider case."""
        container.provide(_self_ref_provider)

        exc = _run_with_timeout(lambda: asyncio.run(container.aget(_SelfRefProduct)))

        assert isinstance(exc, CircularDependencyError)
        assert "_SelfRefProduct" in str(exc)

    def test_concurrent_singleton_creation_still_yields_one_instance(
        self, container: DIContainer
    ) -> None:
        """The re-entrancy guard must not weaken double-check locking.

        Eight threads race to create the same cold singleton. The per-key
        lock must still serialise creation so exactly one instance exists —
        proving the guard rejects only SAME-thread re-entry, never a
        different thread legitimately waiting its turn.
        """
        container.bind(_ConcurrentSingleton, _ConcurrentSingleton)

        with ThreadPoolExecutor(max_workers=8) as pool:
            instances = list(
                pool.map(lambda _: container.get(_ConcurrentSingleton), range(8))
            )

        assert len({id(i) for i in instances}) == 1
