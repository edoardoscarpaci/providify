"""Pytest-free test-override machinery: :class:`ContainerOverrides`.

Plan: `plans/007-pytest-integration.md`, step 7.

DESIGN — snapshot/restore, not an undo-log:

    Every override method (`instance()`, `bind()`, `factory()`, `remove()`,
    `profiles()`, `alternative()`) is a thin call onto ``DIContainer``'s
    existing public API. Undo works by capturing a single
    :class:`~providify.container.ContainerSnapshot` lazily on the *first*
    mutation and writing it back verbatim on `reset()`/`__exit__`, rather
    than recording and inverting each individual mutation.

    Why: `override()` and `reset_binding()` each perform several coupled
    mutations (rebuild bindings, evict singleton cache, prune lock dicts,
    reset validation state). Correctly inverting each of those, in reverse,
    for arbitrary interleavings of override calls is far more code — and a
    far larger surface for subtle bugs — than restoring a handful of
    attributes wholesale via ``DIContainer.restore()``.

    Closest prior art: FastAPI's ``app.dependency_overrides`` dict, mutated
    directly in a yield fixture and cleared in the fixture's teardown
    (research 001 §3) — the same "mutate freely, wipe the whole thing at the
    end" shape, just backed by a structured snapshot instead of a dict clear
    because providify's container has more moving parts (singleton cache,
    profiles, alternatives, interceptors) than a single override dict.

IMPORTANT: this module must **never** import ``pytest`` — that is what
keeps ``import providify`` free of a pytest dependency (see
`providify/pytest_plugin.py`, which is the only module allowed to depend on
pytest). Only `providify.container` is imported here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .container import ContainerSnapshot, DIContainer

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ContainerOverrides"]


class ContainerOverrides:
    """Context-manager scoped test overrides for a :class:`DIContainer`.

    Every override method mutates the container in place through existing
    public API (`override()`, `provide(..., returns=)`, `reset_binding()`,
    `activate_profile()`, `enable_alternative()`) and returns ``self`` for
    chaining. On `reset()` / `__exit__`, the container's mutable state is
    restored to exactly what it was before the first override — via
    :meth:`DIContainer.snapshot`/:meth:`DIContainer.restore`, not by
    inverting each call.

    Usable standalone (no pytest required):

        with ContainerOverrides(container) as ov:
            ov.instance(Clock, FrozenClock("2026-01-01"))
            ov.bind(Notifier, FakeNotifier)
            ov.remove(PaymentGateway)
            ...
        # every override undone here — no reset_binding() calls needed

    The pytest plugin's ``di_overrides`` fixture is a thin wrapper around
    this class.

    Thread safety:  ⚠️ Not safe for concurrent use on the same container —
                    inherits `DIContainer.snapshot()`/`restore()`'s
                    "not safe under concurrent resolution" caveat. Two
                    `ContainerOverrides` instances may be *nested* safely on
                    one container (LIFO exit order gives correct restoration)
                    but must not be used concurrently from multiple threads.
    Async safety:   ✅ No await points — every delegated method is sync.

    Edge cases:
        - No override method called at all → the snapshot is never taken →
          `__exit__`/`reset()` is a no-op.
        - `instance()`/`factory()` on an interface with multiple qualified
          bindings → `reset_binding(iface)` with `qualifier=None` removes
          *all* of them (see `remove()`'s docstring for the finer-grained
          alternative).
        - Nested `ContainerOverrides` on the same container → each holds its
          own snapshot; the inner `__exit__` restores the state as it was
          when the inner instance was constructed — which includes the
          outer's still-active overrides.
        - `reset()` called explicitly, then `__exit__` → the second restore
          is skipped (idempotent — no double-restore error), because
          `reset()` clears the internal snapshot after using it.

    Example:
        >>> with ContainerOverrides(container) as ov:
        ...     ov.instance(Clock, FrozenClock("2026-01-01"))
        ...     assert container.get(Clock) is FrozenClock_instance
        >>> # Clock binding restored to whatever it was before the `with`
    """

    def __init__(self, container: DIContainer) -> None:
        """Bind this override session to *container*.

        Args:
            container: The container whose state will be mutated and later
                restored. No snapshot is taken yet — see `_ensure_snapshot`.
        """
        self._container = container
        self._snapshot: ContainerSnapshot | None = None

    def _ensure_snapshot(self) -> None:
        """Take a snapshot of the container on the first mutation, only once.

        Idempotent by construction: subsequent calls are no-ops because
        `_snapshot` is already set. This is what makes "no mutation at all"
        a genuinely free no-op — a test that enters and exits a
        `ContainerOverrides` block without calling any override method never
        pays for `DIContainer.snapshot()`.
        """
        if self._snapshot is None:
            self._snapshot = self._container.snapshot()

    def instance(self, interface: Any, obj: object) -> ContainerOverrides:
        """Register *obj* itself as the resolved value for *interface*.

        Delegates to `reset_binding(interface)` (drops any existing binding
        so the double cannot lose to an existing `@Priority` winner) followed
        by `provide(lambda: obj, returns=interface)`. The provider has no
        `@Provider` decoration, so `ProviderMetadata.default()` applies:
        `singleton=False` → `Scope.DEPENDENT` — the *same* object `obj` is
        returned on every resolution (the closure just returns it), with no
        singleton-cache entanglement and nothing for `shutdown()` to tear
        down.

        Args:
            interface: The interface (or concrete type) to bind.
            obj: The exact object every resolution of *interface* returns.

        Returns:
            self, for chaining.

        Edge cases:
            - *interface* has no existing binding → `reset_binding` removes
              nothing (returns 0), the double is simply registered.
            - *obj* is never torn down by `shutdown()` — DEPENDENT-scope
              instances are only tracked with `@Component(track=True)`.
              Correct for test doubles; do not expect `@PreDestroy` on one.
        """
        self._ensure_snapshot()
        self._container.reset_binding(interface)
        self._container.provide(lambda: obj, returns=interface)
        return self

    def bind(self, interface: Any, implementation: type) -> ContainerOverrides:
        """Swap *interface*'s implementation for *implementation*.

        Thin delegate to `DIContainer.override()`. The resulting scope is
        whatever *implementation* declares via its own `@Component`/
        `@Singleton` decoration.

        Args:
            interface: The interface to override.
            implementation: The replacement implementation class — must
                already carry DI metadata (`@Component`/`@Singleton`) and be
                a subclass of *interface*, same requirements as `bind()`/
                `override()` on `DIContainer` itself.

        Returns:
            self, for chaining.
        """
        self._ensure_snapshot()
        self._container.override(interface, implementation)
        return self

    def factory(self, interface: Any, fn: Callable[..., Any]) -> ContainerOverrides:
        """Register *fn* as a per-resolution factory for *interface*.

        Like `instance()`, but *fn* is called once per `get()` rather than
        wrapping a single fixed object. Delegates to
        `reset_binding(interface)` + `provide(fn, returns=interface)`,
        which lands at DEPENDENT scope by the same default-metadata path as
        `instance()`.

        Args:
            interface: The interface to override.
            fn: A zero-argument callable invoked on every resolution.

        Returns:
            self, for chaining.
        """
        self._ensure_snapshot()
        self._container.reset_binding(interface)
        self._container.provide(fn, returns=interface)
        return self

    def remove(
        self, interface: Any, *, qualifier: str | type | None = None
    ) -> ContainerOverrides:
        """Unregister *interface*, making it unresolvable for the block's duration.

        Thin delegate to `DIContainer.reset_binding()`.

        Args:
            interface: The interface to remove.
            qualifier: If given, only bindings with this exact qualifier are
                removed; `None` (default) removes *all* bindings for
                *interface* regardless of qualifier — see
                `DIContainer.reset_binding`'s docstring for the full
                semantics.

        Returns:
            self, for chaining.
        """
        self._ensure_snapshot()
        self._container.reset_binding(interface, qualifier=qualifier)
        return self

    def profiles(self, *names: str) -> ContainerOverrides:
        """Activate one or more profiles for the block's duration.

        Thin delegate to `DIContainer.activate_profile()`, called once per
        name.

        Args:
            *names: Profile names to activate.

        Returns:
            self, for chaining.
        """
        self._ensure_snapshot()
        for name in names:
            self._container.activate_profile(name)
        return self

    def alternative(self, cls: type) -> ContainerOverrides:
        """Enable an `@Alternative`-marked class for the block's duration.

        Thin delegate to `DIContainer.enable_alternative()`.

        Args:
            cls: The alternative implementation class to enable.

        Returns:
            self, for chaining.
        """
        self._ensure_snapshot()
        self._container.enable_alternative(cls)
        return self

    def reset(self) -> None:
        """Restore the container to its pre-override state, right now.

        If no mutation was ever made (snapshot never taken), this is a
        no-op. After a successful reset, the internal snapshot reference is
        cleared, so a subsequent `__exit__` (e.g. calling `reset()`
        explicitly and then leaving the `with` block) does not attempt a
        second, redundant restore.

        Returns:
            None
        """
        if self._snapshot is not None:
            self._container.restore(self._snapshot)
            self._snapshot = None

    def __enter__(self) -> ContainerOverrides:
        """Enter the override session — returns self for use in with-statements.

        Returns:
            self
        """
        return self

    def __exit__(self, *_: object) -> None:
        """Exit the override session — restores state even if the block raised.

        Delegates to `reset()`, which is a no-op if no override was ever
        made or if `reset()` was already called explicitly inside the block.
        """
        self.reset()
