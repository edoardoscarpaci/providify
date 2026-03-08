from __future__ import annotations

import asyncio
import inspect
import threading
from contextvars import ContextVar
from types import ModuleType
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    Final,
    TypeVar,
    get_args,
    get_origin,
    get_type_hints,
)

from dataclasses import ( 
    dataclass,
    field
)
from .binding import AnyBinding, ClassBinding, ProviderBinding,BindingDescriptor
from .scanner import ContainerScanner, DefaultContainerScanner
from .metadata import Scope,ScopeLeak, _is_scope_leak, _DI_METADATA_ATTR
from .scope import ScopeContext
from .type import InjectMeta, LazyMeta, LazyProxy, _has_injectable_metadata, _get_injectable_metadata
from .exceptions import CircularDependencyError, ProviderBindingNotDecoratedError
from .decorator.lifecycle import LifecycleMarker
from .metadata import _get_provider_metadata

T = TypeVar("T")
# ─────────────────────────────────────────────────────────────────
#  Resolution stack — tracks the current dependency chain
#  ContextVar — isolated per thread AND per async task
# ─────────────────────────────────────────────────────────────────
_resolution_stack: ContextVar[list[type]] = ContextVar(
    "resolution_stack",
    default=[],
)

# ── Sentinel — unresolved hint signal ────────────────────────────
# DESIGN: plain object() instead of None — None is a valid resolved value
# (e.g. an Optional dependency that was intentionally bound to None).
# Final prevents accidental reassignment; the sentinel identity check
# (resolved_value is _UNRESOLVED) must remain stable for the lifetime of
# the process.
_UNRESOLVED: Final[object] = object()

def _current_stack() -> list[type]:
    """Returns the current resolution stack for this thread/task."""
    return _resolution_stack.get()


def _format_cycle(stack: list[type], cls: type) -> str:
    """Format a human-readable description of the detected dependency cycle.

    Args:
        stack: The current resolution stack — types that are already being
            constructed (outermost to innermost).
        cls: The type whose resolution would close the cycle.

    Returns:
        A string like ``"A → B → C → A"`` where the last element is *cls*.

    Example:
        >>> _format_cycle([A, B], C)
        'A → B → C'
    """
    chain = stack + [cls]
    return " → ".join(c.__name__ for c in chain)

# ─────────────────────────────────────────────────────────────────
#  Scoped container context manager — sync + async
# ─────────────────────────────────────────────────────────────────
class _ScopedContainer:
    """
    Installs a fresh DIContainer as the global for the duration of the block.
    Restores the previous global on exit — even if an exception is raised.

    Supports both sync and async usage:
        with DIContainer.scoped() as c: ...
        async with DIContainer.scoped() as c: ...
    """
    def __init__(self) -> None:
        self._previous:  DIContainer | None = None
        self._container: DIContainer | None = None

    def _install(self) -> DIContainer:
        """Creates and installs a fresh container as the global."""
        self._previous  = DIContainer._global
        self._container = DIContainer()
        DIContainer._global = self._container
        return self._container

    def _restore(self) -> None:
        """Restores the previous global — always called in finally."""
        DIContainer._global = self._previous

    # ── Sync context manager ──────────────────────────────────────

    def __enter__(self) -> DIContainer:
        return self._install()

    def __exit__(self, *_: object) -> None:
        self._restore()

    # ── Async context manager ─────────────────────────────────────

    async def __aenter__(self) -> DIContainer:
        # No I/O here — install is CPU only, no need to await
        return self._install()

    async def __aexit__(self, *_: object) -> None:
        self._restore()


@dataclass
class DIContainerDescriptor:
    validated : bool
    bindings : tuple[BindingDescriptor, ...] = field(default_factory=tuple)

    @property
    def dependent_bindings(self) -> list[BindingDescriptor]:
        return [b for b in self.bindings if b.scope == Scope.DEPENDENT ]

    @property
    def singleton_bindings(self)-> list[BindingDescriptor]:
        return [b for b in self.bindings if b.scope == Scope.SINGLETON]
    
    @property
    def session_bindings(self)-> list[BindingDescriptor]:
        return [b for b in self.bindings if b.scope == Scope.SESSION]
    
    @property
    def request_bindings(self)-> list[BindingDescriptor]:
        return [b for b in self.bindings if b.scope == Scope.REQUEST]

    def _render(self) -> str:
        """
        Render all bindings grouped by scope into a human-readable ASCII block.

        Each scope group is introduced by a ``[SCOPE_NAME]`` header, followed
        by one entry per binding rendered via ``str(binding)`` (which calls
        ``BindingDescriptor.__repr__`` and produces the full dependency subtree).
        Scope groups with no bindings are omitted entirely to keep the output
        clean.

        Returns:
            A multi-line string.  Empty string if there are no bindings at all.

        Thread safety:  ✅ Read-only — no mutation of shared state.
        Async safety:   ✅ Pure computation — safe to call from any context.

        Edge cases:
            - No bindings at all       → returns empty string.
            - Scope group is empty     → that group's header is skipped entirely.
            - Single binding in group  → rendered with ``└──`` connector.

        Example:
            descriptor = container.describe()
            print(descriptor._render())
            # [DEPENDENT]
            # └── EmailNotifier [DEPENDENT] → EmailNotifier
            # [SINGLETON]
            # └── SMSNotifier [SINGLETON] → SMSNotifier
        """
        # ── Map each scope to its display label and pre-fetched binding list ──
        # Order matches conceptual lifecycle: longest-lived → shortest-lived.
        # DESIGN: list-of-tuples preserves insertion order — dict would too on
        # 3.7+ but is less explicit about intentional ordering.
        groups: list[tuple[str, list[BindingDescriptor]]] = [
            ("[SINGLETON]", self.singleton_bindings),
            ("[SESSION]",   self.session_bindings),
            ("[REQUEST]",   self.request_bindings),
            ("[DEPENDENT]", self.dependent_bindings),
        ]

        lines: list[str] = []
        for header, group_bindings in groups:
            # Skip empty groups — no header noise when a scope is unused.
            if not group_bindings:
                continue

            lines.append(header)
            last_idx = len(group_bindings) - 1
            for i, binding in enumerate(group_bindings):
                # ── Box-drawing connector ──────────────────────────────────────
                # Last entry in the group uses └── (no continuation line below).
                # All others use ├── so the vertical bar continues.
                connector = "└── " if i == last_idx else "├── "

                # ── Indent every line of the binding's repr ───────────────────
                # str(binding) may span multiple lines (full dependency subtree).
                # We prefix the first line with the box connector and subsequent
                # lines with the matching indentation so the tree stays aligned.
                binding_lines = str(binding).splitlines()
                continuation  = "    " if i == last_idx else "│   "

                lines.append(f"{connector}{binding_lines[0]}")
                for extra_line in binding_lines[1:]:
                    lines.append(f"{continuation}{extra_line}")
            lines.append("\n")

        return "\n".join(lines)

    def __repr__(self) -> str:
        """
        Return the human-readable grouped rendering of the container's bindings.

        Delegates to :meth:`_render` so that ``str(descriptor)`` and
        ``repr(descriptor)`` both show the grouped ASCII view.

        Returns:
            Multi-line string produced by :meth:`_render`.

        Example:
            descriptor = container.describe()
            print(descriptor)
        """
        return self._render()

    def to_dict(self) -> dict:
        """
        Convert the full descriptor tree to a plain nested dict.

        Suitable for JSON / YAML serialisation. Scope is stored as its
        string name so the output is human-readable without the IntEnum.

        Returns:
            Nested dict mirroring the descriptor tree structure.

        Example:
            import json
            print(json.dumps(descriptor.to_dict(), indent=2))
        """
        return {
            "validated":      self.validated,
            "dependent_bindings": [b.to_dict() for b in self.dependent_bindings],
            "singleton_bindings": [b.to_dict() for b in self.singleton_bindings],
            "session_bindings" : [b.to_dict() for b in self.session_bindings],
            "request_bindings" : [b.to_dict() for b in self.request_bindings]
        }

# ─────────────────────────────────────────────────────────────────
#  DIContainer
# ─────────────────────────────────────────────────────────────────
class DIContainer:
    """Central dependency injection container — supports sync and async resolution.

    Maintains a registry of :class:`~injectable.binding.AnyBinding` objects and
    resolves them on demand, respecting scope caching (singleton, request, session).
    Operates in two phases:

    1. **Registration** — ``bind()``, ``register()``, ``provide()``, ``scan()``
       add bindings.  No validation occurs during this phase so the registry
       can be built in any order.
    2. **Resolution** — the first call to ``get()`` / ``aget()`` / ``get_all()`` /
       ``aget_all()`` triggers ``validate_bindings()`` (once), then resolves.

    Thread safety:  ✅ Safe — the global instance is created under
                    ``threading.Lock`` (double-checked locking).  Individual
                    resolution calls are not locked; concurrent reads of
                    ``_bindings`` and ``_singleton_cache`` rely on the GIL for
                    dict/list safety.  ⚠️ If you mutate bindings from multiple
                    threads after the first resolution, add external locking.
    Async safety:   ✅ Safe — the global instance is created under
                    ``asyncio.Lock`` (created lazily; requires a running loop).
                    ``_resolution_stack`` is a ``ContextVar`` — each asyncio
                    task gets its own isolated stack, preventing cross-task
                    cycle-detection false positives.

    Edge cases:
        - Adding a binding after ``get()`` resets ``_validated`` so the next
          resolution re-runs ``validate_bindings()`` over the full registry.
        - Resolving a ``REQUEST``/``SESSION``-scoped binding outside an active
          scope context raises ``RuntimeError`` immediately.
        - A singleton provider called concurrently (before caching completes)
          may be invoked more than once — the last write wins.  This is safe
          for pure factories but not for providers with side effects.
    """

    _global: ClassVar[DIContainer | None] = None
    # Two locks — one per execution context
    # threading.Lock for sync callers, asyncio.Lock for async callers
    _sync_lock:  ClassVar[threading.Lock] = threading.Lock()
    _async_lock: ClassVar[asyncio.Lock | None] = None   # created lazily — needs event loop

    def __init__(self) -> None:
        """Initialise an empty container with no bindings.

        All state is instance-local — multiple containers can coexist in the
        same process without interfering (e.g. one per test via ``scoped()``).

        Returns:
            None
        """
        self._bindings:        list[AnyBinding] = []
        self._singleton_cache: dict[Any, object] = {}
        self.scope_context = ScopeContext()
        self._scanner: ContainerScanner = DefaultContainerScanner(self)
        # Starts unvalidated — first resolution triggers validate_bindings()
        self._validated: bool = False
        # Lazily-built cache for _collect_kwargs — maps class __name__ → class.
        # Set to None whenever a binding is added so it is rebuilt on next use.
        # In the common case (all bindings registered before the first get()),
        # the dict is built exactly once and reused for every resolution.
        self._localns_cache: dict[str, type] | None = None

    # ── Global accessor ───────────────────────────────────────────
    @classmethod
    def current(cls) -> DIContainer:
        """
        Returns the global container (sync version).
        Uses threading.Lock — safe to call from sync code.
        """
        if cls._global is None:
            with cls._sync_lock:
                if cls._global is None:
                    cls._global = cls()
        return cls._global

    @classmethod
    async def acurrent(cls) -> DIContainer:
        """
        Returns the global container (async version).
        Uses asyncio.Lock — never blocks the event loop.

        Usage:
            container = await DIContainer.acurrent()
        """
        if cls._global is None:
            # Create asyncio.Lock lazily — requires a running event loop
            if cls._async_lock is None:
                cls._async_lock = asyncio.Lock()

            async with cls._async_lock:
                # Double-checked locking — same as sync version
                if cls._global is None:
                    cls._global = cls()

        return cls._global

    @classmethod
    def reset(cls) -> None:
        """Resets the global container — use in test teardown."""
        with cls._sync_lock:
            cls._global    = None
            cls._async_lock = None  # reset lock too — next acurrent() recreates it

    @classmethod
    def scoped(cls) -> _ScopedContainer:
        """
        Returns a context manager that installs a fresh container as global.
        Supports both sync and async:

            with DIContainer.scoped() as container: ...
            async with DIContainer.scoped() as container: ...
        """
        return _ScopedContainer()

    # ── Context manager — instance lifecycle ─────────────────────
    #
    # DESIGN: DIContainer as a context manager manages the *instance* lifecycle
    # (bind → use → shutdown), whereas scoped() manages the *global* lifecycle
    # (temporarily swap the global container). They compose:
    #
    #     with DIContainer.scoped() as c:   # global swapped
    #         with c:                        # shutdown on exit ← this feature
    #             c.bind(...)
    #             c.get(...)
    #
    # Tradeoffs:
    #   ✅ Guarantees @PreDestroy hooks run even if an exception is raised.
    #   ✅ Caches are cleared automatically — no leaks between test cases.
    #   ❌ shutdown() raises if any @PreDestroy is async — callers must use
    #      async with container: ... (which calls ashutdown()) in that case.

    def __enter__(self) -> DIContainer:
        """Enter the container context — returns self for use in with-statements.

        Returns:
            self
        """
        return self

    def __exit__(self, *_: object) -> None:
        """Exit the container context and run synchronous shutdown.

        Calls shutdown(), which invokes every @PreDestroy hook on cached
        singletons and clears all instance caches.

        Args:
            _: Exception info — ignored; shutdown always runs regardless of
               whether the with-block raised.

        Returns:
            None  (does not suppress exceptions from the with-block)

        Raises:
            RuntimeError: If any @PreDestroy hook is async def — use
                ``async with container:`` (which calls ashutdown()) instead.
        """
        self.shutdown()

    async def __aenter__(self) -> DIContainer:
        """Enter the container async context — returns self.

        Returns:
            self
        """
        return self

    async def __aexit__(self, *_: object) -> None:
        """Exit the container async context and run asynchronous shutdown.

        Calls ashutdown(), which awaits async @PreDestroy hooks and calls
        sync ones normally. Clears all instance caches afterward.

        Args:
            _: Exception info — ignored; shutdown always runs.

        Returns:
            None  (does not suppress exceptions from the async-with block)
        """
        await self.ashutdown()

    # ── Warmup ────────────────────────────────────────────────────────

    def _validate_no_async_providers(self, bindings: list[AnyBinding]) -> None:
        """
        Pre-flight check — scan the full binding list for async providers before
        any instantiation begins, guaranteeing an all-or-nothing failure mode.

        Separating validation from instantiation means warm_up either populates
        the singleton cache completely or not at all — it never leaves the cache
        in a partially-warmed, hard-to-reason-about state.

        Args:
            bindings: The list of bindings to validate. Typically the output of
                      _iter_singleton_bindings().

        Raises:
            RuntimeError: If any binding is an async ProviderBinding. The message
                          names the offending provider and directs the caller to
                          awarm_up().

        Edge cases:
            - Empty list              → no-op, no error raised
            - Multiple async providers → only the first one is reported ⚠️
              (raise-on-first is intentional — fix one, re-run, discover the next)

        Thread safety:  ✅  Read-only scan — no shared state is mutated.
        Async safety:   ✅  No awaits — safe to call from sync or async context.

        Example:
            self._validate_no_async_providers(bindings)  # raises if any are async
        """
        for binding in bindings:
            # Check before touching the cache — raising here means _instantiate_sync
            # is never called, so no partial warm-up side-effects occur.
            if isinstance(binding, ProviderBinding) and binding.is_async:
                raise RuntimeError(
                    f"'{binding.fn.__name__}' is an async provider — "
                    f"use `await container.awarm_up()` instead."
                )

    def warm_up(self, qualifier: str | None = None, priority: int | None = None) -> None:
        """
        Eagerly instantiate all singleton bindings in the container (sync version).

        Validates the full binding list before instantiating anything — if any
        async provider is present the method raises immediately without touching
        the singleton cache, giving a clean all-or-nothing guarantee.

        Args:
            qualifier: Named qualifier to restrict which singletons are warmed up.
                       None means all qualifiers are included.
            priority:  Exact priority to match when filtering. None means all
                       priorities are included.

        Raises:
            RuntimeError: If any matching singleton is backed by an async provider.
                          The cache is NOT modified before the error is raised.
                          Call ``await container.awarm_up()`` instead.

        Edge cases:
            - No bindings match                  → no-op, no error raised
            - qualifier + priority both None      → all singletons are warmed up
            - Async provider anywhere in results  → raises before any instantiation ✅
            - Binding already cached              → _instantiate_sync returns cached
                                                    instance — no double-construction

        Thread safety:  ⚠️  Conditional — safe if called before the app goes
                            multi-threaded. Concurrent warm-up calls may race on
                            the singleton cache depending on _instantiate_sync's
                            internal locking.
        Async safety:   ❌  Do NOT call from a running event loop — use awarm_up().

        Example:
            container.warm_up(qualifier="db", priority=10)
        """
        # Collect first so we can validate the entire list before touching the cache.
        # This is the key difference from the previous implementation — we no longer
        # raise mid-loop after partially populating the cache.
        singleton_bindings = self._filter_singleton(qualifier=qualifier, priority=priority)

        # All-or-nothing guard — raises if any async provider is present,
        # before _instantiate_sync is called even once.
        self._validate_no_async_providers(singleton_bindings)

        for binding in singleton_bindings:
            # Discard the return value — only the cache side-effect matters here.
            _ = self._instantiate_sync(binding)

    async def awarm_up(self, qualifier: str | None = None, priority: int | None = None) -> None:
        """
        Eagerly instantiate all singleton bindings in the container (async version).

        Mirrors ``warm_up`` but drives async providers with ``await``. Sync providers
        are still resolved synchronously — no unnecessary coroutine overhead is
        introduced for bindings that don't need it.

        Args:
            qualifier: Named qualifier to restrict which singletons are warmed up.
                       None means all qualifiers are included.
            priority:  Exact priority to match when filtering. None means all
                       priorities are included.

        Raises:
            Any exception raised by an async or sync provider during instantiation
            is propagated directly — warm-up does not swallow provider errors.

        Edge cases:
            - No bindings match             → no-op, no error raised
            - qualifier + priority both None → all singletons are warmed up
            - Mix of sync and async providers → handled transparently ✅
            - Binding already cached         → instantiate methods return cached
                                               instance — no double-construction
            - Async provider raises          → exception propagates; singletons
                                               resolved before the failure ARE cached ⚠️

        Thread safety:  ⚠️  Conditional — assumes a single event loop drives warm-up.
                            Concurrent awarm_up() calls may race on the singleton cache.
        Async safety:   ✅  Must be called from within a running event loop.
                            Safe to await — does not block the event loop.

        Example:
            await container.awarm_up(qualifier="db")
        """
        # Shared helper — same filter semantics as warm_up, documented once.
        singleton_bindings = self._filter_singleton(qualifier=qualifier, priority=priority)
        for binding in singleton_bindings:
            if isinstance(binding, ProviderBinding) and binding.is_async:
                # Async provider — must be awaited. Calling without await would
                # return a coroutine object instead of the resolved instance and
                # leave it unawaited (runtime warning + wrong cache entry).
                _ = await self._instantiate_async(binding=binding)
            else:
                # Sync provider inside an async context — call synchronously.
                # asyncio.to_thread() would add unnecessary overhead for pure
                # in-memory construction that doesn't block the event loop.
                _ = self._instantiate_sync(binding)
    
    # ── Registration ─────────────────────────────────────────────
    def bind(self, interface: type[T], implementation: type[T]) -> None:
        """Bind an interface type to a concrete implementation class.

        Args:
            interface: The abstract type (or base class) callers will resolve.
            implementation: The concrete class that will be instantiated.

        Returns:
            None
        """
        self._validated = False
        self._localns_cache = None          # new binding — localns must be rebuilt
        self._bindings.append(ClassBinding(interface, implementation))

    def register(self, cls: type[T]) -> None:
        """Register a concrete class so it resolves to itself.

        The class must carry DI metadata (i.e. be decorated with
        ``@Component`` or ``@Singleton``).

        Args:
            cls: The decorated concrete class to register.

        Returns:
            None

        Raises:
            TypeError: If *cls* has no ``__di_metadata__`` attribute, meaning
                it was not decorated with ``@Component`` or ``@Singleton``.
        """
        if not cls.__dict__.get(_DI_METADATA_ATTR):
            raise TypeError(
                f"{cls.__name__} must be decorated with @Component or @Singleton."
            )
        self._validated = False
        self._localns_cache = None          # new binding — localns must be rebuilt
        self._bindings.append(ClassBinding(cls, cls))

    def provide(self, fn: Callable[..., Any]) -> None:
        """Register a provider function (sync or async) as a binding.

        The function's return type annotation is used as the resolved interface.

        Args:
            fn: A callable that creates and returns the dependency.
                May be a regular function or an ``async def``.

        Returns:
            None
        """
        self._validated = False
        self._localns_cache = None          # new binding — localns must be rebuilt
        self._bindings.append(ProviderBinding(fn))

    # ── Sync resolution ───────────────────────────────────────────
    def get(self, cls: type[T], qualifier: str | None = None, priority: int | None = None) -> T:
        """Resolve a single instance synchronously.

        Selects the highest-priority binding that matches *cls* (and the
        optional *qualifier* / *priority* filters), then instantiates it.

        Args:
            cls: The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.
            priority: Optional exact priority value to narrow the candidate set.

        Returns:
            A fully-injected instance of *cls*.

        Raises:
            LookupError: If no binding is found for *cls*.
            RuntimeError: If the best matching binding is an async provider —
                use :meth:`aget` instead.
        """
        best = self._get_best_candidate(cls,qualifier=qualifier,priority=priority)
        # Guard — async providers cannot be resolved synchronously
        if isinstance(best, ProviderBinding) and best.is_async:
            raise RuntimeError(
                f"'{best.fn.__name__}' is an async provider — use await container.aget() instead."
            )
        if not self._validated:
            self.validate_bindings()
            self._validated = True

        return self._instantiate_sync(best)  # type: ignore[return-value]

    def get_all(self, cls: type[T], qualifier: str | None = None) -> list[T]:
        """Resolve every binding that matches *cls*, synchronously.

        Results are returned sorted by ascending priority (lowest number first).

        Args:
            cls: The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.

        Returns:
            A list of fully-injected instances, ordered by binding priority.

        Raises:
            LookupError: If no binding is found for *cls*.
            RuntimeError: If any matching binding is an async provider —
                use :meth:`aget_all` instead.
        """
        candidates = self._filter(cls, qualifier=qualifier)

        if not candidates:
            raise LookupError(f"No bindings found for '{cls.__name__}'.")

        # Guard — fail early if any candidate is async
        async_providers = [
            b for b in candidates
            if isinstance(b, ProviderBinding) and b.is_async
        ]
        if async_providers:
            names = ", ".join(b.fn.__name__ for b in async_providers)
            raise RuntimeError(
                f"Async providers [{names}] cannot be resolved with get_all(). "
                f"Use await container.aget_all() instead."
            )
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return [
            self._instantiate_sync(b)  # type: ignore[misc]
            for b in sorted(candidates, key=lambda b: b.priority)
        ]

    # ── Async resolution ──────────────────────────────────────────

    async def aget(
        self,
        cls: type[T],
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> T:
        """Resolve a single instance asynchronously.

        Works transparently with both sync and async providers — async providers
        are awaited automatically.

        Args:
            cls: The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.
            priority: Optional exact priority value to narrow the candidate set.

        Returns:
            A fully-injected instance of *cls*.

        Raises:
            LookupError: If no binding is found for *cls*.

        Example:
            svc = await container.aget(NotificationService)
        """
        best = self._get_best_candidate(cls,qualifier=qualifier,priority=priority)
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return await self._instantiate_async(best)  # type: ignore[return-value]

    async def aget_all(self, cls: type[T], qualifier: str | None = None) -> list[T]:
        """Resolve every binding that matches *cls*, asynchronously.

        Handles both sync and async providers — each binding is awaited only
        if its provider is a coroutine function.

        Args:
            cls: The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.

        Returns:
            A list of fully-injected instances, ordered by binding priority.

        Raises:
            LookupError: If no binding is found for *cls*.

        Example:
            services = await container.aget_all(NotificationService)
        """
        candidates = self._filter(cls, qualifier=qualifier)

        if not candidates:
            raise LookupError(f"No bindings found for '{cls.__name__}'.")
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return [
            await self._instantiate_async(b)  # type: ignore[misc]
            for b in sorted(candidates, key=lambda b: b.priority)
        ]

    # ── Internal helpers ─────────────────────────────────────────

    def _filter(
        self,
        cls: type,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> list[AnyBinding]:
        """Return all bindings whose interface is a subclass of *cls*.

        Optionally narrows the result by *qualifier* and/or *priority*.
        The same logic is shared by both the sync and async resolution paths.

        Args:
            cls: The base type to match against ``binding.interface``.
            qualifier: If given, only bindings with a matching qualifier are kept.
            priority: If given, only bindings with this exact priority are kept.

        Returns:
            A (possibly empty) list of matching :class:`~injectable.binding.AnyBinding` objects.
        """
        return [
            b for b in self._bindings
            # DESIGN: all three predicates are in one comprehension — avoids building
            # two throwaway intermediate lists when optional filters are active.
            if issubclass(b.interface, cls)
            and (qualifier is None or b.qualifier == qualifier)
            and (priority is None or b.priority == priority)
        ]

    def _filter_singleton(
        self,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> list[AnyBinding]:
        """
        Return all SINGLETON-scoped bindings, optionally filtered by qualifier and priority.
        Collapses scope, qualifier, and priority checks into a single pass to avoid
        building two intermediate lists when optional filters are provided.

        Args:
            qualifier: If given, only bindings with this exact qualifier are returned.
                       None means all qualifiers are accepted.
            priority:  If given, only bindings with this exact priority are returned.
                       None means all priorities are accepted.

        Returns:
            A new list containing only the bindings that satisfy all conditions.

        Edge cases:
            - qualifier=None and priority=None  → returns all SINGLETON bindings unchanged
            - No bindings match                 → returns an empty list (never raises)
            - self._bindings is empty           → returns an empty list immediately

        Thread safety:  ⚠️ Conditional — safe only if self._bindings is not mutated
                        concurrently; caller is responsible for external locking.
        Async safety:   ✅ Safe — no await points, no shared mutable state written.
        """
        return [
            b for b in self._bindings
            # DESIGN: all three predicates are in one comprehension — avoids building
            # two throwaway intermediate lists when optional filters are active.
            if b.scope == Scope.SINGLETON
            and (qualifier is None or b.qualifier == qualifier)
            and (priority is None or b.priority == priority)
        ]
    
    def _get_best_candidate(self, cls: type[T], qualifier: str | None = None, priority: int | None = None) -> AnyBinding:
        """
        Return the highest-priority binding for the requested type.

        Args:
            cls:       The interface or concrete type to resolve.
            qualifier: Named qualifier to filter bindings. ``None`` matches any.
            priority:  Exact priority to match. ``None`` returns the best available.

        Returns:
            The highest-priority binding among all matching candidates
            (higher value = higher precedence).

        Raises:
            LookupError: No binding is registered for ``cls`` with the given
                         qualifier and priority.
        """
        candidates = self._filter(cls, qualifier=qualifier, priority=priority)

        if not candidates:
            raise LookupError(
                f"No binding found for '{cls.__name__}'"
                + (f" qualifier={qualifier!r}" if qualifier else "")
                + ". Did you forget container.bind() or container.provide()?"
            )
        
        return max(candidates, key=lambda b: b.priority)

    def _get_cache(self, binding: AnyBinding) -> dict[Any, object] | None:
        """Return the instance cache that corresponds to *binding*'s scope.

        Args:
            binding: The binding whose ``scope`` attribute is inspected.

        Returns:
            - The singleton cache dict for ``Scope.SINGLETON``.
            - The active request-scope cache dict for ``Scope.REQUEST``.
            - The active session-scope cache dict for ``Scope.SESSION``.
            - ``None`` for ``Scope.DEPENDENT`` — no caching, new instance every time.

        Raises:
            RuntimeError: If the binding is ``REQUEST`` or ``SESSION`` scoped but
                no matching scope context is currently active.
        """
        match binding.scope:
            case Scope.SINGLETON:
                return self._singleton_cache

            case Scope.REQUEST:
                cache = self.scope_context.get_request_cache()
                if cache is None:
                    raise RuntimeError(
                        f"Cannot resolve @RequestScoped '{binding.interface.__name__}' "
                        f"outside of an active request context. "
                        f"Use: with container.scope_context.request(): ..."
                        f" or async with container.scope_context.arequest(): ..."
                    )
                return cache

            case Scope.SESSION:
                cache = self.scope_context.get_session_cache()
                if cache is None:
                    raise RuntimeError(
                        f"Cannot resolve @SessionScoped '{binding.interface.__name__}' "
                        f"outside of an active session context. "
                        f"Use: with container.scope_context.session(): ..."
                        f" or async with container.scope_context.asession(): ..."
                    )
                return cache

            case _:
                # DEPENDENT — no cache, new instance every time
                return None

    def _get_cache_key(self, binding: AnyBinding) -> Any:
        """Return a hashable cache key for *binding*.

        Uses the implementation class for :class:`~injectable.binding.ClassBinding`
        and the provider callable for :class:`~injectable.binding.ProviderBinding`,
        so the key is stable and unique regardless of binding type.

        Args:
            binding: The binding to derive a key for.

        Returns:
            The concrete class (``type``) or provider function (``Callable``).
        """
        if isinstance(binding, ClassBinding):
            return binding.implementation
        return binding.fn

    def _instantiate_sync(self, binding: AnyBinding) -> Any:
        """Instantiate *binding* synchronously, respecting scope caching.

        Looks up the appropriate cache for the binding's scope.  If a cached
        instance exists it is returned immediately; otherwise ``binding.create()``
        is called and the result is stored before being returned.

        Args:
            binding: The binding to instantiate.

        Returns:
            The (possibly cached) resolved instance.
        """
        key   = self._get_cache_key(binding)
        cache = self._get_cache(binding)

        if cache is not None and key in cache:
            return cache[key]
        
        instance = binding.create(self)         # ✅ binding owns creation logic

        if cache is not None:
            cache[key] = instance

        return instance

    async def _instantiate_async(self, binding: AnyBinding) -> Any:
        """Instantiate *binding* asynchronously, respecting scope caching.

        Mirrors :meth:`_instantiate_sync` but delegates to ``binding.acreate()``,
        which handles both sync and async providers transparently.

        Args:
            binding: The binding to instantiate.

        Returns:
            The (possibly cached) resolved instance.
        """
        key   = self._get_cache_key(binding)
        cache = self._get_cache(binding)

        if cache is not None and key in cache:
            return cache[key]

        instance = await binding.acreate(self)  # ✅ async twin of create()

        if cache is not None:
            cache[key] = instance

        return instance

    def _is_resolvable(self, hint: type) -> bool:
        """Return ``True`` if at least one binding's interface is a subclass of *hint*.

        Args:
            hint: The type to check.

        Returns:
            ``True`` if a matching binding exists, ``False`` otherwise.
        """
        return any(issubclass(b.interface, hint) for b in self._bindings)

    def _build_localns(self) -> dict[str, type]:
        """Return a cached ``localns`` dict for use with ``get_type_hints()``.

        Maps every registered interface (and ClassBinding implementation) to its
        class name, so that PEP-563 string annotations that reference locally-
        defined types (e.g. classes defined inside test functions) can be
        evaluated even when those types are absent from the function's module
        globals.

        Caching strategy:
            The dict is built lazily on first use and stored in
            ``self._localns_cache``.  ``bind()``, ``register()``, and
            ``provide()`` each set ``_localns_cache = None`` so the dict is
            rebuilt after any binding change.  In the common pattern — all
            bindings registered before the first ``get()`` call — the dict is
            built exactly once.

        Thread safety:  ⚠️ Conditional — the cache is not protected by a lock.
                        Two threads resolving concurrently before the first
                        cached build may each build the dict independently;
                        the last write wins.  Both builds produce identical
                        results, so correctness is preserved.

        Returns:
            A ``dict[str, type]`` mapping class ``__name__`` → class object.
        """
        if self._localns_cache is None:
            localns: dict[str, type] = {}
            for b in self._bindings:
                # Interface — what callers annotate against (e.g. Repository)
                localns[b.interface.__name__] = b.interface
                if isinstance(b, ClassBinding):
                    # Implementation — annotations may reference the concrete
                    # class directly rather than the abstract interface.
                    localns[b.implementation.__name__] = b.implementation
            self._localns_cache = localns
        return self._localns_cache

    # ── Shared kwarg collection ───────────────────────────────────
    def _collect_kwargs_sync(
        self,
        fn: Callable[..., Any],
        owner_name: str,
    ) -> dict[str, Any]:
        """Build a ``kwargs`` dict by resolving every injectable parameter of *fn*.

        Iterates over the type hints of *fn*, skips ``return``, and tries to
        resolve each annotated parameter from the container.  Parameters with
        no binding are skipped if they have a default value, or raise otherwise.

        Shared by :meth:`_resolve_constructor` and :meth:`_call_provider`.

        Args:
            fn: The callable whose parameters should be resolved
                (typically ``SomeClass.__init__`` or a provider function).
            owner_name: A human-readable name used in error messages
                (e.g. the class name or function name).

        Returns:
            A dict mapping parameter names to resolved instances.
            Parameters that have a default and no binding are omitted.

        Raises:
            LookupError: If a required parameter (no default) cannot be resolved.
        """
        try:
            # _build_localns() supplies a cached dict of every registered
            # interface/implementation so that locally-defined classes (e.g.
            # classes defined inside test functions) are found when Python
            # evaluates PEP-563 string annotations.  See _build_localns for
            # the full rationale and caching strategy.
            hints = get_type_hints(fn, include_extras=True, localns=self._build_localns())
        except Exception:
            hints = {}

        hints.pop("return", None)
        sig = inspect.signature(fn)
        resolved: dict[str, Any] = {}

        for param_name, hint in hints.items():
            param = sig.parameters.get(param_name)
            resolved_value = self._resolve_hint_sync(hint, param_name, owner_name)

            if resolved_value is _UNRESOLVED:
                # No binding found — use default or fail
                if param and param.default is inspect.Parameter.empty:
                    raise LookupError(
                        f"Cannot resolve '{param_name}: {hint}' in '{owner_name}'. "
                        f"Bind it or provide a default value."
                    )
            else:
                resolved[param_name] = resolved_value

        return resolved

    async def _collect_kwargs_async(
        self,
        fn: Callable[..., Any],
        owner_name: str,
    ) -> dict[str, Any]:
        """Build a ``kwargs`` dict by resolving every injectable parameter of *fn*, asynchronously.

        Async mirror of :meth:`_collect_kwargs_sync`.
        Shared by :meth:`_resolve_constructor_async` and :meth:`_call_provider_async`.

        Args:
            fn: The callable whose parameters should be resolved.
            owner_name: A human-readable name used in error messages.

        Returns:
            A dict mapping parameter names to resolved instances.

        Raises:
            LookupError: If a required parameter (no default) cannot be resolved.
        """
        try:
            # Async mirror — same _build_localns() strategy as _collect_kwargs_sync.
            hints = get_type_hints(fn, include_extras=True, localns=self._build_localns())
        except Exception:
            hints = {}

        hints.pop("return", None)
        sig = inspect.signature(fn)
        resolved: dict[str, Any] = {}

        for param_name, hint in hints.items():
            param = sig.parameters.get(param_name)
            resolved_value = await self._resolve_hint_async(hint, param_name, owner_name)

            if resolved_value is _UNRESOLVED:
                if param and param.default is inspect.Parameter.empty:
                    raise LookupError(
                        f"Cannot resolve '{param_name}: {hint}' in '{owner_name}'. "
                        f"Bind it or provide a default value."
                    )
            else:
                resolved[param_name] = resolved_value

        return resolved
    

    def _collect_dependencies(
        self,
        fn: Callable[..., Any],
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> list[AnyBinding]:
        """
        Introspect a callable's type hints and resolve each to a registered binding.

        Calls ``get_type_hints(fn, include_extras=True)`` so that
        ``Annotated[...]`` metadata (e.g. ``@Injectable`` markers) is preserved.
        Passes ``localns=self._build_localns()`` to handle PEP-563 string
        annotations for locally-defined classes (e.g. classes created inside
        test functions that are not yet importable by name).

        Only hints that carry injectable metadata (as determined by
        ``_has_injectable_metadata``) produce a binding — plain ``int``,
        ``str``, unannotated args, and the ``return`` hint are all skipped.

        Args:
            fn:        The callable whose parameter annotations are inspected.
                       Typically ``cls.__init__`` or a provider function.
            qualifier: Forwarded to ``_resolve_dependency`` — restricts candidate
                       bindings to those matching this qualifier.  ``None`` means
                       any qualifier is acceptable.
            priority:  Forwarded to ``_resolve_dependency`` — restricts candidate
                       bindings to those matching this exact priority.  ``None``
                       means the highest-priority candidate wins.

        Returns:
            Ordered list of ``AnyBinding`` objects, one per resolvable injectable
            parameter.  Parameters that are unresolvable (``LookupError``) or
            lack injectable metadata are silently omitted.

        Edge cases:
            - ``get_type_hints`` raises (e.g. forward ref cannot be resolved,
              ``NameError``) → swallowed silently; ``hints`` falls back to ``{}``,
              returning ``[]``.  ⚠️ This can hide misconfigured annotations.
            - ``return`` hint present → stripped before iteration; never produces
              a dependency.
            - No injectable parameters → returns ``[]``.
            - Locally-defined class not in ``localns`` → ``get_type_hints`` may
              raise; see above swallow behaviour.

        Example:
            bindings = self._collect_dependencies(MyService.__init__, qualifier="primary")
        """
        try:
            # _build_localns() supplies a cached dict of every registered
            # interface/implementation so that locally-defined classes (e.g.
            # classes defined inside test functions) are found when Python
            # evaluates PEP-563 string annotations.  See _build_localns for
            # the full rationale and caching strategy.
            hints = get_type_hints(fn, include_extras=True, localns=self._build_localns())
        except Exception:
            hints = {}

        hints.pop("return", None)
        dependecies : list[AnyBinding] = []
        
        for _ , hint in hints.items():
            resolved_dep = self._resolve_dependency(hint, qualifier= qualifier,priority=priority)
            if resolved_dep is not None:
                dependecies.append(resolved_dep)
        return dependecies
    
    def _resolve_dependency(
        self,
        hint: Any,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> AnyBinding | None:
        """
        Attempt to resolve a single type hint to its best-matching binding.

        Checks whether ``hint`` carries injectable metadata via
        ``_has_injectable_metadata``; returns ``None`` immediately for plain
        hints that should not be injected.  For injectable hints, unpacks the
        ``Annotated`` args to extract the raw base type, then delegates to
        ``_get_best_candidate``.

        Args:
            hint:      A single resolved type hint, possibly ``Annotated[T, ...]``.
            qualifier: Filters candidates to those matching this qualifier.
                       ``None`` means any qualifier is acceptable.
            priority:  Restricts to candidates matching this exact priority.
                       ``None`` means the highest-priority candidate wins.

        Returns:
            The best ``AnyBinding`` for the hint's base type, or ``None`` if:
            - the hint has no injectable metadata, **or**
            - ``_get_best_candidate`` raises ``LookupError`` (no binding found).

        Edge cases:
            - ``hint`` is a bare type with no ``Annotated`` wrapper →
              ``_has_injectable_metadata`` returns ``False`` → ``None`` returned.
            - ``get_args(hint)`` is empty → would ``IndexError``; relies on
              ``_has_injectable_metadata`` to gate entry (⚠️ implicit contract).
            - ``LookupError`` from ``_get_best_candidate`` → swallowed; caller
              receives ``None``.  Other exceptions propagate uncaught.

        Example:
            binding = self._resolve_dependency(
                Annotated[EmailService, Injectable()],
                qualifier="smtp",
            )
        """
        if not _has_injectable_metadata(hint):
            return None
        args        = get_args(hint)
        base_type   = args[0]  
        try:
            return self._get_best_candidate(base_type,qualifier=qualifier,priority=priority)
        except LookupError:
            return None

        
    def _resolve_hint_sync(self, hint: Any, param_name: str, owner_name: str) -> Any:
        """Resolve a single type hint to an instance, synchronously.

        Handles four cases:
        - ``Annotated[T, LazyMeta(...)]`` — returns a :class:`~injectable.type.LazyProxy`
          without resolving T immediately. Defers resolution to .get() call time.
        - ``Annotated[T, InjectMeta(all=True)]`` — resolves every matching binding as a list.
        - ``Annotated[T, InjectMeta(...)]`` — resolves T with optional qualifier/priority.
          If ``InjectMeta.optional`` is True, returns None when no binding is found.
        - Plain type with a registered binding — resolved via :meth:`get`.
        - Everything else — returns the :data:`_UNRESOLVED` sentinel.

        Args:
            hint: The raw type hint (possibly ``Annotated``).
            param_name: Parameter name, used only for error messages.
            owner_name: Class or function name, used only for error messages.

        Returns:
            The resolved instance, or :data:`_UNRESOLVED` if no binding matches.
        """
        if get_origin(hint) is Annotated:
            args        = get_args(hint)
            base_type   = args[0]
            # Check LazyMeta first — a hint can't be both Lazy and Inject simultaneously.
            # Lazy wins because it wraps the resolution in a proxy; if it were treated as
            # a plain Inject, resolution would happen eagerly and break the deferral guarantee.
            lazy_meta   = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
            inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

            if lazy_meta:
                # Return a proxy now — actual resolution is deferred to .get() call time.
                # This breaks circular dependency cycles: both constructors return before
                # either dependency is resolved, so the stack never sees a cycle.
                return LazyProxy(
                    self,
                    base_type,
                    qualifier=lazy_meta.qualifier,
                    priority=lazy_meta.priority,
                )
            elif inject_meta and inject_meta.all:
                inner = get_args(base_type)[0] if get_origin(base_type) is list else base_type
                return self.get_all(inner, qualifier=inject_meta.qualifier)
            elif inject_meta:
                try:
                    return self.get(base_type, qualifier=inject_meta.qualifier, priority=inject_meta.priority)
                except LookupError:
                    # optional=True: swallow the error and inject None.
                    # optional=False (default): re-raise so the caller sees the real error.
                    if inject_meta.optional:
                        return None
                    raise

        elif isinstance(hint, type) and self._is_resolvable(hint):
            return self.get(hint)

        return _UNRESOLVED  # signal: no binding found, caller decides

    async def _resolve_hint_async(self, hint: Any, param_name: str, owner_name: str) -> Any:
        """Resolve a single type hint to an instance, asynchronously.

        Async mirror of :meth:`_resolve_hint_sync`. Handles all four cases
        (LazyMeta, InjectMeta.all, InjectMeta, plain type) identically to the
        sync path, except inner resolution uses ``aget`` / ``aget_all``.
        LazyProxy creation is still synchronous — .aget() is called later by the owner.

        Args:
            hint: The raw type hint (possibly ``Annotated``).
            param_name: Parameter name, used only for error messages.
            owner_name: Class or function name, used only for error messages.

        Returns:
            The resolved instance, or :data:`_UNRESOLVED` if no binding matches.
        """
        if get_origin(hint) is Annotated:
            args        = get_args(hint)
            base_type   = args[0]
            # Mirror of _resolve_hint_sync — LazyMeta checked first for the same reason.
            lazy_meta   = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
            inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

            if lazy_meta:
                # Proxy creation is always sync — the proxy's .aget() method is what's
                # async. We return the proxy object itself (no await needed here).
                return LazyProxy(
                    self,
                    base_type,
                    qualifier=lazy_meta.qualifier,
                    priority=lazy_meta.priority,
                )
            elif inject_meta and inject_meta.all:
                inner = get_args(base_type)[0] if get_origin(base_type) is list else base_type
                return await self.aget_all(inner, qualifier=inject_meta.qualifier)
            elif inject_meta:
                try:
                    return await self.aget(base_type, qualifier=inject_meta.qualifier, priority=inject_meta.priority)
                except LookupError:
                    # Mirror of the sync path — optional=True swallows LookupError.
                    if inject_meta.optional:
                        return None
                    raise

        elif isinstance(hint, type) and self._is_resolvable(hint):
            return await self.aget(hint)

        return _UNRESOLVED
    
    def _resolve_constructor(self, cls: type) -> object:
        """Resolve ``cls.__init__`` parameters and return a new instance.

        Pushes *cls* onto the per-task resolution stack before resolving its
        dependencies so that a circular reference is detected immediately.

        Args:
            cls: The class to instantiate.

        Returns:
            A newly constructed instance of *cls* with all dependencies injected.

        Raises:
            CircularDependencyError: If *cls* is already present in the
                current resolution stack.
            LookupError: If any required ``__init__`` parameter cannot be resolved.
        """
        self._check_cycle(cls)          # ✅ check before resolving

        # Push cls onto the stack for the duration of this resolution
        stack = _current_stack().copy() # copy — ContextVar is immutable
        token = _resolution_stack.set(stack + [cls])

        try:
            resolved_kwargs = self._collect_kwargs_sync(cls.__init__, cls.__name__)
            return cls(**resolved_kwargs)
        finally:
            # Always pop — even if resolution fails
            _resolution_stack.reset(token)


    async def _resolve_constructor_async(self, cls: type) -> object:
        """Async mirror of :meth:`_resolve_constructor`.

        Args:
            cls: The class to instantiate.

        Returns:
            A newly constructed instance of *cls* with all dependencies injected.

        Raises:
            CircularDependencyError: If *cls* is already in the resolution stack.
            LookupError: If any required ``__init__`` parameter cannot be resolved.
        """
        self._check_cycle(cls)

        stack = _current_stack().copy()
        token = _resolution_stack.set(stack + [cls])

        try:
            resolved_kwargs = await self._collect_kwargs_async(cls.__init__, cls.__name__)
            return cls(**resolved_kwargs)
        finally:
            _resolution_stack.reset(token)


    def _call_provider(self, fn: Callable[..., Any]) -> Any:
        """Call a sync provider function with all dependencies injected.

        If the provider declares a return type, that type is used as the cycle-
        detection key (same semantics as :meth:`_resolve_constructor`).

        Args:
            fn: The provider callable to invoke.

        Returns:
            The value returned by *fn*.

        Raises:
            CircularDependencyError: If the provider's return type is already
                present in the current resolution stack.
            LookupError: If any required parameter of *fn* cannot be resolved.
        """
        return_type = self._get_provider_return_type(fn)

        if return_type is not None:
            self._check_cycle(return_type)
            stack = _current_stack().copy()
            token = _resolution_stack.set(stack + [return_type])
        else:
            token = None

        try:
            resolved_kwargs = self._collect_kwargs_sync(fn, fn.__name__)
            return fn(**resolved_kwargs)
        finally:
            if token is not None:
                _resolution_stack.reset(token)


    async def _call_provider_async(self, fn: Callable[..., Any]) -> Any:
        """Call a provider function (sync or async) with all dependencies injected.

        Async mirror of :meth:`_call_provider`.  The result is awaited if *fn*
        is a coroutine function, otherwise returned directly.

        Args:
            fn: The provider callable to invoke.

        Returns:
            The resolved value — awaited if *fn* is ``async def``.

        Raises:
            CircularDependencyError: If the provider's return type is already
                present in the current resolution stack.
            LookupError: If any required parameter of *fn* cannot be resolved.
        """
        return_type = self._get_provider_return_type(fn)

        if return_type is not None:
            self._check_cycle(return_type)
            stack = _current_stack().copy()
            token = _resolution_stack.set(stack + [return_type])
        else:
            token = None

        try:
            resolved_kwargs = await self._collect_kwargs_async(fn, fn.__name__)
            result = fn(**resolved_kwargs)
            return await result if inspect.iscoroutinefunction(fn) else result
        finally:
            if token is not None:
                _resolution_stack.reset(token)


    # ── Helpers ───────────────────────────────────────────────────────

    def _check_cycle(self, cls: type) -> None:
        """Raise if *cls* is already present in the current resolution stack.

        Called before every constructor or provider resolution.  If *cls* is
        already on the stack, we are about to enter an infinite loop.

        Args:
            cls: The type about to be resolved.

        Returns:
            None

        Raises:
            CircularDependencyError: When *cls* is already in the stack.
                The error message contains a formatted chain like ``A → B → A``.

        Example:
            stack = [A, B], cls = A  →  raises with "A → B → A"
        """
        stack = _current_stack()
        if cls in stack:
            raise CircularDependencyError(_format_cycle(stack, cls))

    def _get_provider_return_type(self, fn: Callable[..., Any]) -> type | None:
        """Read the ``return`` type hint from a provider function.

        Used to derive the cycle-detection key for provider bindings.
        Returns ``None`` (and suppresses all exceptions) if the hints cannot
        be resolved — e.g. when a forward reference is unresolvable at runtime.

        Args:
            fn: The provider callable to inspect.

        Returns:
            The return type annotation if present and resolvable, else ``None``.
        """
        try:
            hints = get_type_hints(fn)
            return hints.get("return")
        except Exception:
            return None

    # ── Scan ──────────────────────────────────────────────────────

    def scan(self, module: str | ModuleType, *, recursive: bool = False) -> None:
        """Scan a module for DI-decorated classes and functions.

        Delegates to the configured :class:`~injectable.scanner.ContainerScanner`
        (defaults to :class:`~injectable.scanner.DefaultContainerScanner`).
        Replace ``self._scanner`` before calling this method if you need custom
        discovery behaviour.

        Args:
            module: A fully-qualified module name or an already-imported module.
            recursive: When ``True``, sub-packages are walked recursively.

        Returns:
            None

        Raises:
            ModuleNotFoundError: If *module* is a string that cannot be imported.
        """
        self._scanner.scan(module, recursive=recursive)

    # ── Module installation ───────────────────────────────────────

    def install(self, module_cls: type) -> None:
        """Install a ``@Configuration`` module synchronously.

        Instantiates *module_cls* with its constructor dependencies injected
        (Spring-style), then registers every ``@Provider``-decorated method on
        the module as a bound-method binding.

        Because the module is instantiated at install time, all constructor
        dependencies of *module_cls* must already be registered before calling
        this method.

        Args:
            module_cls: A class decorated with ``@Configuration``.

        Returns:
            None

        Raises:
            TypeError:      If *module_cls* is not decorated with ``@Configuration``.
            LookupError:    If any constructor dependency of *module_cls* has no binding.
            RuntimeError:   If any constructor dependency is async-only —
                            use :meth:`ainstall` instead.

        Example:
            container.bind(Settings, AppSettings)
            container.install(InfraModule)   # InfraModule.__init__ gets Settings injected
        """
        from .module import _is_module

        if not _is_module(module_cls):
            raise TypeError(
                f"{module_cls.__name__} must be decorated with @Configuration."
            )

        # Spring-style: instantiate the module with injected constructor deps.
        # _resolve_constructor works on any class — it does not require @Component.
        instance = self._resolve_constructor(module_cls)

        # Iterate over the class's own attributes (not inherited ones) to find
        # @Provider-decorated methods. vars() gives the raw unbound functions,
        # which carry ProviderMetadata directly on their __dict__.
        for name, fn in vars(module_cls).items():
            if callable(fn) and name != "__init__" and _get_provider_metadata(fn) is not None:
                # getattr returns a bound method — self is the live module instance.
                # ProviderBinding handles bound methods via the _get_provider_metadata fix.
                self.provide(getattr(instance, name))

    async def ainstall(self, module_cls: type) -> None:
        """Install a ``@Configuration`` module asynchronously.

        Async mirror of :meth:`install`. Use this when the module's constructor
        has async-only dependencies (i.e. deps that require ``aget()``).

        Args:
            module_cls: A class decorated with ``@Configuration``.

        Returns:
            None

        Raises:
            TypeError:   If *module_cls* is not decorated with ``@Configuration``.
            LookupError: If any constructor dependency of *module_cls* has no binding.

        Example:
            await container.ainstall(InfraModule)
        """
        from .module import _is_module

        if not _is_module(module_cls):
            raise TypeError(
                f"{module_cls.__name__} must be decorated with @Configuration."
            )

        instance = await self._resolve_constructor_async(module_cls)

        for name, fn in vars(module_cls).items():
            if callable(fn) and name != "__init__" and _get_provider_metadata(fn) is not None:
                self.provide(getattr(instance, name))

    def _run_post_construct_sync(
        self,
        instance: Any,
        hook: LifecycleMarker | None,
    ) -> None:
        """Invoke the ``@PostConstruct`` lifecycle hook on *instance*, synchronously.

        A no-op when *hook* is ``None``.

        Args:
            instance: The freshly constructed object.
            hook: The :class:`~injectable.decorator.lifecycle.LifecycleMarker`
                describing the ``@PostConstruct`` method, or ``None`` if absent.

        Returns:
            None

        Raises:
            RuntimeError: If the ``@PostConstruct`` method is ``async def`` —
                use :meth:`_run_post_construct_async` (via :meth:`aget`) instead.
        """
        if hook is None:
            return
        
        if hook.is_async:
            raise RuntimeError(
                f"@PostConstruct method '{hook.fn_name}' is async — "
                f"use await container.aget() to resolve this component."
            )
        bound = getattr(instance, hook.fn_name)
        bound()  # sync @PostConstruct

    async def _run_post_construct_async(
        self,
        instance: Any,
        hook: LifecycleMarker | None,
    ) -> None:
        """Invoke the ``@PostConstruct`` lifecycle hook on *instance*, asynchronously.

        Awaits the hook if it is ``async def``; calls it normally if sync.
        A no-op when *hook* is ``None``.

        Args:
            instance: The freshly constructed object.
            hook: The :class:`~injectable.decorator.lifecycle.LifecycleMarker`
                describing the ``@PostConstruct`` method, or ``None`` if absent.

        Returns:
            None
        """
        if hook is None:
            return

        bound = getattr(instance, hook.fn_name)
        if hook.is_async:
            await bound()       # async @PostConstruct
        else:
            bound()             # sync @PostConstruct — still fine in async context
    # ── Shutdown ──────────────────────────────────────────────────
    def shutdown(self) -> None:
        """
        Sync shutdown — calls @PreDestroy on all cached singleton instances.
        Raises if any @PreDestroy method is async — use ashutdown() instead.

        Clears all caches after teardown.
        """
        for binding in self._bindings:
            if not isinstance(binding, ClassBinding):
                continue                            # providers have no lifecycle hooks
            if binding.pre_destroy is None:
                continue                            # no @PreDestroy — skip

            key = binding.implementation
            instance = self._singleton_cache.get(key)

            if instance is None:
                continue                            # never instantiated — skip

            if binding.pre_destroy.is_async:
                raise RuntimeError(
                    f"@PreDestroy method '{binding.pre_destroy.fn_name}' on "
                    f"'{binding.implementation.__name__}' is async — "
                    f"use await container.ashutdown() instead."
                )

            bound = getattr(instance, binding.pre_destroy.fn_name)
            bound()

        self._clear_caches()

    async def ashutdown(self) -> None:
        """
        Async shutdown — calls @PreDestroy on all cached singleton instances.
        Awaits async @PreDestroy methods, calls sync ones normally.

        Clears all caches after teardown.

        Usage:
            await container.ashutdown()
        """
        for binding in self._bindings:
            if not isinstance(binding, ClassBinding):
                continue

            if binding.pre_destroy is None:
                continue

            key = binding.implementation
            instance = self._singleton_cache.get(key)

            if instance is None:
                continue

            bound = getattr(instance, binding.pre_destroy.fn_name)
            if binding.pre_destroy.is_async:
                await bound()   # async @PreDestroy
            else:
                bound()         # sync @PreDestroy — fine in async context

        self._clear_caches()

    def _clear_caches(self) -> None:
        """
        Clears all instance caches after shutdown.
        Resets singleton cache and all active scope caches.
        """
        self._singleton_cache.clear()
        # Clear all active request/session caches too
        self.scope_context.clear_caches()

    def _check_scope_violation(
        self,
        binding: ClassBinding,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> list[ScopeLeak]:
        """Inspect *binding*'s ``__init__`` parameters for scope leaks.

        A scope leak occurs when a wider-scoped component (e.g. ``SINGLETON``)
        holds a direct reference to a narrower-scoped one (e.g. ``REQUEST``),
        because the wider component would silently cache a stale instance of
        the narrower one across scope boundaries.

        Scope ranking (lower = wider / longer-lived):
            ``SINGLETON(1) < SESSION(2) < REQUEST(3) < DEPENDENT(4)``

        Args:
            binding: The :class:`~injectable.binding.ClassBinding` whose
                constructor dependencies are inspected.
            qualifier: If given, only dependency bindings with this qualifier
                are considered during the check.
            priority: If given, only dependency bindings with this exact
                priority are considered during the check.

        Returns:
            A list of :class:`~injectable.metadata.ScopeLeak` instances, one
            per violating dependency.  An empty list means no leaks were found.
        """
        leaks: list[ScopeLeak] = []
        try:
            hints = get_type_hints(binding.implementation.__init__, include_extras=True)
        except Exception:
            return leaks
        hints.pop("return", None)
        for _, hint in hints.items():
            base_type = get_args(hint)[0] if get_origin(hint) is Annotated else hint
            if not isinstance(base_type, type):
                continue
            dep_bindings = self._filter(base_type, qualifier=qualifier, priority=priority)
            for dep in dep_bindings:
                if _is_scope_leak(parent_scope=binding.scope,dep_scope=dep.scope):
                    leaks.append(ScopeLeak(
                        binding=(binding.implementation, binding.scope),
                        reference=(dep.interface, dep.scope),
                    ))
        return leaks
    # ── Validate ──────────────────────────────────────────────────
    def validate_bindings(self) -> None:
        """Validate all registered bindings against the full registry.

        Iterates over every binding and calls :meth:`~injectable.binding.IBinding.validate`,
        which for :class:`~injectable.binding.ClassBinding` instances performs
        scope-leak detection.  This is the *phase transition* from registration
        to resolution: it runs once after all bindings have been registered,
        ensuring the complete dependency graph is visible during validation.

        Called automatically on the first :meth:`get`, :meth:`aget`,
        :meth:`get_all`, or :meth:`aget_all` call if not already validated.
        Can also be called explicitly for early error detection.

        Returns:
            None

        Raises:
            ScopeViolationDetectedError: If any binding introduces a scope leak.
        """
        for binding in self._bindings:
            binding.validate(self)

    def _get_dependencies(
        self,
        binding: AnyBinding,
        _visited: frozenset[type] | None = None,
    ) -> list[AnyBinding]:
        """
        Dispatch to the correct dependency-collection strategy for a binding.

        Acts as a type-based router — delegates to ``_collect_dependencies``
        for both ``ClassBinding`` and ``ProviderBinding``.  Raises immediately
        for unknown binding types so that missing implementations are caught at
        resolve-time rather than silently returning an empty list.

        Args:
            binding:  The binding whose constructor/provider signature will be
                      inspected to discover its dependencies.
            _visited: Optional frozenset of interface types already seen by the
                      caller during a recursive graph traversal.  When provided,
                      any dep whose interface is already in ``_visited`` is
                      filtered out — preventing infinite loops for callers that
                      do NOT have their own cycle guard.

                      IMPORTANT: ``describe()`` does NOT pass ``_visited`` here
                      because it maintains its own cycle guard and needs the
                      cyclic dep binding to be returned so it can render the
                      ``[CYCLE DETECTED]`` sentinel.  Pass ``_visited`` only
                      when you are doing a raw recursive graph walk and want
                      silent cycle-breaking.

        Returns:
            Ordered list of ``AnyBinding`` objects that ``binding`` depends on,
            in the same order as the matching type-hint parameters.  Filtered
            by ``_visited`` when provided.

        Raises:
            TypeError: ``binding`` is not a ``ClassBinding`` or
                       ``ProviderBinding`` — no dispatch branch exists for it.

        Edge cases:
            - Binding has no annotated parameters → returns ``[]`` cleanly.
            - Optional/unresolvable hints → silently skipped by
              ``_collect_dependencies`` (see its edge-case notes).
            - ``_visited`` is an empty frozenset → no filtering; equivalent to
              passing ``None``.
            - All deps already in ``_visited`` → returns ``[]``.

        Example:
            # Safe recursive traversal without a separate cycle guard:
            def traverse(binding, visited=frozenset()):
                visited = visited | {binding.interface}
                for dep in container._get_dependencies(binding, _visited=visited):
                    traverse(dep, visited)
        """
        if isinstance(binding, ClassBinding):
            deps = self._collect_dependencies(
                fn=binding.implementation.__init__,
                qualifier=binding.qualifier,
                priority=binding.priority,
            )
        elif isinstance(binding, ProviderBinding):
            deps = self._collect_dependencies(
                fn=binding.fn,
                qualifier=binding.qualifier,
                priority=binding.priority,
            )
        else:
            raise TypeError(
                f"No _get_dependencies implementation found for binding type "
                f"'{type(binding).__name__}'. Expected ClassBinding or ProviderBinding."
            )

        if _visited is None:
            # No cycle guard requested — return all deps as-is.
            # describe() takes this path so it can build [CYCLE DETECTED] sentinels.
            return deps

        # Filter out deps whose interface is already on the caller's visited path.
        # This silently breaks cycles for raw recursive callers (e.g. graph analysis
        # tools) that don't need the sentinel and just want to avoid infinite loops.
        return [d for d in deps if d.interface not in _visited]
    

    def describe(self) -> DIContainerDescriptor:
        bindings_descriptor = tuple([b.describe(self) for b in self._bindings])
        return DIContainerDescriptor(validated=self._validated,bindings=bindings_descriptor)

    def __repr__(self) -> str:
        return f"DIContainer({self._bindings})"


