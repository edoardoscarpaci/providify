from __future__ import annotations

import asyncio
import inspect
import threading
from types import ModuleType
from typing import (
    Annotated,
    Any,
    Callable,
    ClassVar,
    TypeVar,
    get_args,
    get_origin,
    get_type_hints,
)

from .binding import AnyBinding, ClassBinding, ProviderBinding
from .descriptor import DIContainerDescriptor
from .exceptions import CircularDependencyError, LiveInjectionRequiredError
from .decorator.lifecycle import LifecycleMarker
from .metadata import (
    LiveInjectionViolation,
    Scope,
    ScopeLeak,
    _has_own_metadata,
    _has_configuration_module,
    _is_scope_leak,
    _get_provider_metadata,
)
from .resolution import _resolution_stack, _current_stack, _format_cycle, _UNRESOLVED
from .scanner import ContainerScanner, DefaultContainerScanner
from .scope import ScopeContext
from .type import (
    InjectMeta,
    LazyMeta,
    LazyProxy,
    LiveMeta,
    LiveProxy,
    _has_providify_metadata,
    _providify,
)
from .utils import _interface_matches, _type_name

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────
#  _ScopedContainer — installs a temporary container as global
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
        self._previous: DIContainer | None = None
        self._container: DIContainer | None = None

    def _install(self) -> DIContainer:
        """Creates and installs a fresh container as the global."""
        self._previous = DIContainer._global
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


# ─────────────────────────────────────────────────────────────────
#  DIContainer — central dependency injection container
# ─────────────────────────────────────────────────────────────────


class DIContainer:
    """Central dependency injection container — supports sync and async resolution.

    Maintains a registry of :class:`~providify.binding.AnyBinding` objects and
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
    # Two locks — one per execution context.
    # threading.Lock for sync callers, asyncio.Lock for async callers.
    _sync_lock: ClassVar[threading.Lock] = threading.Lock()
    _async_lock: ClassVar[asyncio.Lock | None] = (
        None  # created lazily — needs event loop
    )

    # ── Initialisation ────────────────────────────────────────────

    def __init__(self) -> None:
        """Initialise an empty container with no bindings.

        All state is instance-local — multiple containers can coexist in the
        same process without interfering (e.g. one per test via ``scoped()``).

        Returns:
            None
        """
        self._bindings: list[AnyBinding] = []
        self._singleton_cache: dict[Any, object] = {}
        self.scope_context: ScopeContext = ScopeContext()
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
        """Return the global container (sync version).

        Uses ``threading.Lock`` — safe to call from sync code.

        Returns:
            The global singleton ``DIContainer``, creating it if needed.
        """
        if cls._global is None:
            with cls._sync_lock:
                if cls._global is None:
                    cls._global = cls()
        return cls._global

    @classmethod
    async def acurrent(cls) -> DIContainer:
        """Return the global container (async version).

        Uses ``asyncio.Lock`` — never blocks the event loop.

        Returns:
            The global singleton ``DIContainer``, creating it if needed.

        Example:
            container = await DIContainer.acurrent()
        """
        if cls._global is None:
            # Create asyncio.Lock lazily — requires a running event loop
            if cls._async_lock is None:
                cls._async_lock = asyncio.Lock()

            async with cls._async_lock:
                # Double-checked locking — same pattern as the sync version
                if cls._global is None:
                    cls._global = cls()

        return cls._global

    @classmethod
    def reset(cls) -> None:
        """Reset the global container — use in test teardown.

        Returns:
            None
        """
        with cls._sync_lock:
            cls._global = None
            cls._async_lock = None  # reset lock too — next acurrent() recreates it

    @classmethod
    def scoped(cls) -> _ScopedContainer:
        """Return a context manager that installs a fresh container as global.

        Supports both sync and async:

            with DIContainer.scoped() as container: ...
            async with DIContainer.scoped() as container: ...

        Returns:
            A :class:`_ScopedContainer` context manager.
        """
        return _ScopedContainer()

    # ── Instance context manager ──────────────────────────────────
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

    # ── Registration ──────────────────────────────────────────────

    def bind(self, interface: Any, implementation: type) -> None:
        """Bind an interface type to a concrete implementation class.

        *interface* may be a concrete type (``Repository``) or a parameterised
        generic alias (``Repository[User]``).  The container will match any
        ``get(Repository[User])`` call — or a plain ``repo: Repository[User]``
        annotation — to this binding.

        Args:
            interface:      The abstract type (or base class) callers will resolve.
                            Accepts both concrete types and generic aliases.
            implementation: The concrete class that will be instantiated.
                            Must be a subclass of *interface*'s origin type and
                            must implement the exact type parameterisation.

        Returns:
            None
        """
        self._validated = False
        self._localns_cache = None  # new binding — localns must be rebuilt
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
            TypeError: If *cls* has no DI metadata, meaning it was not
                decorated with ``@Component`` or ``@Singleton``.
        """
        if not _has_own_metadata(cls):
            raise TypeError(
                f"{cls.__name__} must be decorated with @Component or @Singleton."
            )
        self._validated = False
        self._localns_cache = None  # new binding — localns must be rebuilt
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
        self._localns_cache = None  # new binding — localns must be rebuilt
        self._bindings.append(ProviderBinding(fn))

    # ── Warm-up ───────────────────────────────────────────────────

    def _validate_no_async_providers(self, bindings: list[AnyBinding]) -> None:
        """Pre-flight check — raise if any binding is an async provider.

        Separating validation from instantiation means warm_up either populates
        the singleton cache completely or not at all — it never leaves the cache
        in a partially-warmed state.

        Args:
            bindings: The list of bindings to validate. Typically the output of
                      _filter_singleton().

        Raises:
            RuntimeError: If any binding is an async ProviderBinding. Only the
                          first one is reported — fix one, re-run, discover the next.

        Edge cases:
            - Empty list → no-op, no error raised.

        Thread safety:  ✅ Read-only scan — no shared state is mutated.
        Async safety:   ✅ No awaits — safe to call from sync or async context.
        """
        for binding in bindings:
            if isinstance(binding, ProviderBinding) and binding.is_async:
                raise RuntimeError(
                    f"'{binding.fn.__name__}' is an async provider — "
                    f"use `await container.awarm_up()` instead."
                )

    def warm_up(
        self,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> None:
        """Eagerly instantiate all singleton bindings in the container (sync version).

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
            - No bindings match                  → no-op, no error raised.
            - qualifier + priority both None      → all singletons are warmed up.
            - Async provider anywhere in results  → raises before any instantiation ✅.
            - Binding already cached              → _instantiate_sync returns cached
                                                    instance — no double-construction.

        Thread safety:  ⚠️ Conditional — safe if called before the app goes
                            multi-threaded.
        Async safety:   ❌ Do NOT call from a running event loop — use awarm_up().

        Example:
            container.warm_up(qualifier="db", priority=10)
        """
        singleton_bindings = self._filter_singleton(
            qualifier=qualifier, priority=priority
        )
        # All-or-nothing guard — raises if any async provider is present
        self._validate_no_async_providers(singleton_bindings)
        for binding in singleton_bindings:
            self._instantiate_sync(binding)

    async def awarm_up(
        self,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> None:
        """Eagerly instantiate all singleton bindings in the container (async version).

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
            - No bindings match              → no-op, no error raised.
            - Mix of sync and async providers → handled transparently ✅.
            - Binding already cached         → returns cached — no double-construction.
            - Async provider raises          → exception propagates; singletons
                                               resolved before the failure ARE cached ⚠️.

        Thread safety:  ⚠️ Conditional — assumes a single event loop drives warm-up.
        Async safety:   ✅ Must be called from within a running event loop.

        Example:
            await container.awarm_up(qualifier="db")
        """
        singleton_bindings = self._filter_singleton(
            qualifier=qualifier, priority=priority
        )
        for binding in singleton_bindings:
            if isinstance(binding, ProviderBinding) and binding.is_async:
                await self._instantiate_async(binding=binding)
            else:
                self._instantiate_sync(binding)

    # ── Sync resolution ───────────────────────────────────────────

    def get(
        self,
        cls: type[T] | Any,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> T:
        """Resolve a single instance synchronously.

        Selects the highest-priority binding that matches *cls* (and the
        optional *qualifier* / *priority* filters), then instantiates it.

        Args:
            cls:       The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.
            priority:  Optional exact priority value to narrow the candidate set.

        Returns:
            A fully-injected instance of *cls*.

        Raises:
            LookupError:   If no binding is found for *cls*.
            RuntimeError:  If the best matching binding is an async provider —
                           use :meth:`aget` instead.
        """
        best = self._get_best_candidate(cls, qualifier=qualifier, priority=priority)
        # Guard — async providers cannot be resolved synchronously
        if isinstance(best, ProviderBinding) and best.is_async:
            raise RuntimeError(
                f"'{best.fn.__name__}' is an async provider — "
                f"use await container.aget() instead."
            )
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return self._instantiate_sync(best)  # type: ignore[return-value]

    def get_all(
        self,
        cls: type[T] | Any,
        qualifier: str | None = None,
    ) -> list[T]:
        """Resolve every binding that matches *cls*, synchronously.

        Results are returned sorted by ascending priority (lowest number first).

        Args:
            cls:       The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.

        Returns:
            A list of fully-injected instances, ordered by binding priority.

        Raises:
            LookupError:  If no binding is found for *cls*.
            RuntimeError: If any matching binding is an async provider —
                          use :meth:`aget_all` instead.
        """
        candidates = self._filter(cls, qualifier=qualifier)
        if not candidates:
            raise LookupError(f"No bindings found for '{_type_name(cls)}'.")

        # Guard — fail early if any candidate is async
        async_providers = [
            b for b in candidates if isinstance(b, ProviderBinding) and b.is_async
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
        cls: type[T] | Any,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> T:
        """Resolve a single instance asynchronously.

        Works transparently with both sync and async providers — async providers
        are awaited automatically.

        Args:
            cls:       The type to resolve.
            qualifier: Optional named qualifier to narrow the candidate set.
            priority:  Optional exact priority value to narrow the candidate set.

        Returns:
            A fully-injected instance of *cls*.

        Raises:
            LookupError: If no binding is found for *cls*.

        Example:
            svc = await container.aget(NotificationService)
        """
        best = self._get_best_candidate(cls, qualifier=qualifier, priority=priority)
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return await self._instantiate_async(best)  # type: ignore[return-value]

    async def aget_all(
        self,
        cls: type[T] | Any,
        qualifier: str | None = None,
    ) -> list[T]:
        """Resolve every binding that matches *cls*, asynchronously.

        Handles both sync and async providers — each binding is awaited only
        if its provider is a coroutine function.

        Args:
            cls:       The type to resolve.
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
            raise LookupError(f"No bindings found for '{_type_name(cls)}'.")
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return [
            await self._instantiate_async(b)  # type: ignore[misc]
            for b in sorted(candidates, key=lambda b: b.priority)
        ]

    # ── Filtering helpers ─────────────────────────────────────────

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
            cls:       The base type to match against ``binding.interface``.
            qualifier: If given, only bindings with a matching qualifier are kept.
            priority:  If given, only bindings with this exact priority are kept.

        Returns:
            A (possibly empty) list of matching bindings.
        """
        return [
            b
            for b in self._bindings
            # DESIGN: _interface_matches replaces plain issubclass so that generic
            # aliases like Repository[User] are matched correctly — issubclass does
            # not accept parameterised types as its second argument.
            if _interface_matches(b.interface, cls)
            and (qualifier is None or b.qualifier == qualifier)
            and (priority is None or b.priority == priority)
        ]

    def _filter_singleton(
        self,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> list[AnyBinding]:
        """Return all SINGLETON-scoped bindings, optionally filtered.

        Args:
            qualifier: If given, only bindings with this exact qualifier are returned.
            priority:  If given, only bindings with this exact priority are returned.

        Returns:
            A new list containing only the bindings that satisfy all conditions.

        Edge cases:
            - qualifier=None and priority=None → returns all SINGLETON bindings.
            - No bindings match               → returns an empty list.

        Thread safety:  ⚠️ Conditional — safe only if self._bindings is not mutated
                        concurrently.
        Async safety:   ✅ No await points, no shared mutable state written.
        """
        return [
            b
            for b in self._bindings
            if b.scope == Scope.SINGLETON
            and (qualifier is None or b.qualifier == qualifier)
            and (priority is None or b.priority == priority)
        ]

    def _get_best_candidate(
        self,
        cls: type[T],
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> AnyBinding:
        """Return the highest-priority binding for the requested type.

        Args:
            cls:       The interface or concrete type to resolve.
            qualifier: Named qualifier to filter bindings. ``None`` matches any.
            priority:  Exact priority to match. ``None`` returns the best available.

        Returns:
            The lowest-priority-value binding among all matching candidates
            (lower value = higher precedence).

        Raises:
            LookupError: No binding is registered for ``cls`` with the given
                         qualifier and priority.
        """
        candidates = self._filter(cls, qualifier=qualifier, priority=priority)
        if not candidates:
            raise LookupError(
                f"No binding found for '{_type_name(cls)}'"
                + (f" qualifier={qualifier!r}" if qualifier else "")
                + ". Did you forget container.bind() or container.provide()?"
            )
        return max(candidates, key=lambda b: b.priority)

    # ── Cache helpers ─────────────────────────────────────────────

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
                        f"Cannot resolve @RequestScoped '{_type_name(binding.interface)}' "
                        f"outside of an active request context. "
                        f"Use: with container.request(): ..."
                        f" or async with container.arequest(): ..."
                    )
                return cache

            case Scope.SESSION:
                cache = self.scope_context.get_session_cache()
                if cache is None:
                    raise RuntimeError(
                        f"Cannot resolve @SessionScoped '{_type_name(binding.interface)}' "
                        f"outside of an active session context. "
                        f"Use: with container.session(...): ..."
                        f" or async with container.asession(...): ..."
                    )
                return cache

            case _:
                # DEPENDENT — no cache, new instance every time
                return None

    def _get_cache_key(self, binding: AnyBinding) -> Any:
        """Return a hashable cache key for *binding*.

        Uses the implementation class for :class:`~providify.binding.ClassBinding`
        and the provider callable for :class:`~providify.binding.ProviderBinding`,
        so the key is stable and unique regardless of binding type.

        Args:
            binding: The binding to derive a key for.

        Returns:
            The concrete class (``type``) or provider function (``Callable``).
        """
        if isinstance(binding, ClassBinding):
            return binding.implementation
        return binding.fn

    # ── Instantiation ─────────────────────────────────────────────

    def _instantiate_sync(self, binding: AnyBinding) -> Any:
        """Instantiate *binding* synchronously, respecting scope caching.

        Looks up the appropriate cache for the binding's scope. If a cached
        instance exists it is returned immediately; otherwise ``binding.create()``
        is called and the result is stored before being returned.

        Args:
            binding: The binding to instantiate.

        Returns:
            The (possibly cached) resolved instance.
        """
        key = self._get_cache_key(binding)
        cache = self._get_cache(binding)

        if cache is not None and key in cache:
            return cache[key]

        instance = binding.create(self)

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
        key = self._get_cache_key(binding)
        cache = self._get_cache(binding)

        if cache is not None and key in cache:
            return cache[key]

        instance = await binding.acreate(self)

        if cache is not None:
            cache[key] = instance

        return instance

    # ── Type-hint resolution ──────────────────────────────────────

    def _is_resolvable(self, hint: Any) -> bool:
        """Return ``True`` if at least one binding's interface satisfies *hint*.

        Accepts both concrete types and parameterised generic aliases.

        Args:
            hint: The type or generic alias to check.

        Returns:
            ``True`` if a matching binding exists, ``False`` otherwise.
        """
        # _interface_matches replaces issubclass — handles generic aliases safely
        return any(_interface_matches(b.interface, hint) for b in self._bindings)

    def _build_localns(self) -> dict[str, type]:
        """Return a cached ``localns`` dict for use with ``get_type_hints()``.

        Maps every registered interface (and ClassBinding implementation) to its
        class name, so that PEP-563 string annotations that reference locally-
        defined types (e.g. classes defined inside test functions) can be
        evaluated even when those types are absent from the function's module
        globals.

        Caching strategy:
            The dict is built lazily on first use and stored in
            ``self._localns_cache``. ``bind()``, ``register()``, and
            ``provide()`` each set ``_localns_cache = None`` so the dict is
            rebuilt after any binding change. In the common pattern — all
            bindings registered before the first ``get()`` call — the dict is
            built exactly once.

        Thread safety:  ⚠️ Conditional — the cache is not protected by a lock.
                        Two threads resolving concurrently before the first
                        cached build may each build the dict independently;
                        the last write wins. Both builds produce identical
                        results, so correctness is preserved.

        Returns:
            A ``dict[str, type]`` mapping class ``__name__`` → class object.
        """
        if self._localns_cache is None:
            localns: dict[str, type] = {}
            for b in self._bindings:
                # Interface — what callers annotate against (e.g. Repository).
                # For generic aliases (Repository[User]), __name__ does not exist;
                # map the origin type (Repository) instead so string annotations
                # like "Repository" in PEP-563 deferred mode still resolve.
                iface_origin = get_origin(b.interface)
                if iface_origin is not None:
                    # Generic alias: map "Repository" → Repository (origin type)
                    localns[iface_origin.__name__] = iface_origin
                else:
                    localns[b.interface.__name__] = b.interface  # type: ignore[union-attr]
                if isinstance(b, ClassBinding):
                    # Implementation — annotations may reference the concrete
                    # class directly rather than the abstract interface.
                    localns[b.implementation.__name__] = b.implementation

                    # Also add any generic origin types and their type arguments
                    # from the implementation's __orig_bases__.
                    #
                    # DESIGN: PEP-563 (from __future__ import annotations) makes
                    # ALL annotations lazy strings.  When a caller annotates a
                    # parameter as `repo: Repository[User]`, the string
                    # `"Repository[User]"` must be eval'd by get_type_hints().
                    # That eval needs both `Repository` (the generic class) and
                    # `User` (the type argument) in the namespace.
                    #
                    # These are often locally-defined types that are absent from
                    # fn.__globals__, so we harvest them here from the MRO of
                    # each registered implementation — the only place where the
                    # full parameterised form is preserved.
                    for base in getattr(b.implementation, "__orig_bases__", ()):
                        origin = get_origin(base)
                        if origin is not None and isinstance(origin, type):
                            localns[origin.__name__] = origin
                        for arg in get_args(base):
                            if isinstance(arg, type):
                                localns[arg.__name__] = arg
            self._localns_cache = localns
        return self._localns_cache

    def _collect_kwargs_sync(
        self,
        fn: Callable[..., Any],
        owner_name: str,
    ) -> dict[str, Any]:
        """Build a ``kwargs`` dict by resolving every providify parameter of *fn*.

        Iterates over the type hints of *fn*, skips ``return``, and tries to
        resolve each annotated parameter from the container. Parameters with
        no binding are skipped if they have a default value, or raise otherwise.

        Shared by :meth:`_resolve_constructor` and :meth:`_call_provider`.

        Args:
            fn:         The callable whose parameters should be resolved.
            owner_name: A human-readable name used in error messages.

        Returns:
            A dict mapping parameter names to resolved instances.
            Parameters that have a default and no binding are omitted.

        Raises:
            LookupError: If a required parameter (no default) cannot be resolved.
        """
        try:
            hints = get_type_hints(
                fn, include_extras=True, localns=self._build_localns()
            )
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
        """Build a ``kwargs`` dict by resolving every providify parameter, asynchronously.

        Async mirror of :meth:`_collect_kwargs_sync`.
        Shared by :meth:`_resolve_constructor_async` and :meth:`_call_provider_async`.

        Args:
            fn:         The callable whose parameters should be resolved.
            owner_name: A human-readable name used in error messages.

        Returns:
            A dict mapping parameter names to resolved instances.

        Raises:
            LookupError: If a required parameter (no default) cannot be resolved.
        """
        try:
            hints = get_type_hints(
                fn, include_extras=True, localns=self._build_localns()
            )
        except Exception:
            hints = {}

        hints.pop("return", None)
        sig = inspect.signature(fn)
        resolved: dict[str, Any] = {}

        for param_name, hint in hints.items():
            param = sig.parameters.get(param_name)
            resolved_value = await self._resolve_hint_async(
                hint, param_name, owner_name
            )

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
        """Introspect a callable's type hints and resolve each to a registered binding.

        Only hints that carry providify metadata produce a binding — plain
        ``int``, ``str``, unannotated args, and the ``return`` hint are skipped.

        Args:
            fn:        The callable whose parameter annotations are inspected.
                       Typically ``cls.__init__`` or a provider function.
            qualifier: Forwarded to ``_resolve_dependency``.
            priority:  Forwarded to ``_resolve_dependency``.

        Returns:
            Ordered list of ``AnyBinding`` objects, one per resolvable providify
            parameter. Parameters that are unresolvable or lack providify metadata
            are silently omitted.

        Edge cases:
            - ``get_type_hints`` raises → swallowed; returns ``[]``.
            - ``return`` hint present  → stripped before iteration.
            - No providify parameters → returns ``[]``.
        """
        try:
            hints = get_type_hints(
                fn, include_extras=True, localns=self._build_localns()
            )
        except Exception:
            hints = {}

        hints.pop("return", None)
        dependencies: list[AnyBinding] = []

        for _, hint in hints.items():
            resolved_dep = self._resolve_dependency(
                hint, qualifier=qualifier, priority=priority
            )
            if resolved_dep is not None:
                dependencies.append(resolved_dep)
        return dependencies

    def _resolve_dependency(
        self,
        hint: Any,
        qualifier: str | None = None,
        priority: int | None = None,
    ) -> AnyBinding | None:
        """Attempt to resolve a single type hint to its best-matching binding.

        Args:
            hint:      A single resolved type hint, possibly ``Annotated[T, ...]``.
            qualifier: Filters candidates to those matching this qualifier.
            priority:  Restricts to candidates matching this exact priority.

        Returns:
            The best ``AnyBinding`` for the hint's base type, or ``None`` if:
            - the hint has no providify metadata, **or**
            - ``_get_best_candidate`` raises ``LookupError``.

        Edge cases:
            - Bare type with no ``Annotated`` wrapper → ``None`` returned.
            - ``LookupError`` from ``_get_best_candidate`` → swallowed, returns ``None``.
        """
        if not _has_providify_metadata(hint):
            return None
        args = get_args(hint)
        base_type = args[0]
        try:
            return self._get_best_candidate(
                base_type, qualifier=qualifier, priority=priority
            )
        except LookupError:
            return None

    def _resolve_hint_sync(self, hint: Any, param_name: str, owner_name: str) -> Any:
        """Resolve a single type hint to an instance, synchronously.

        Handles four cases:
        - ``Annotated[T, LazyMeta(...)]``        — returns a :class:`LazyProxy`.
        - ``Annotated[T, InjectMeta(all=True)]`` — resolves every matching binding as a list.
        - ``Annotated[T, InjectMeta(...)]``       — resolves T with optional qualifier/priority.
        - Plain type with a registered binding    — resolved via :meth:`get`.
        - Everything else                         — returns :data:`_UNRESOLVED`.

        Args:
            hint:       The raw type hint (possibly ``Annotated``).
            param_name: Parameter name, used only for error messages.
            owner_name: Class or function name, used only for error messages.

        Returns:
            The resolved instance, or :data:`_UNRESOLVED` if no binding matches.
        """
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            base_type = args[0]
            # Priority order: LiveMeta → LazyMeta → InjectMeta.
            # A hint can only carry one _providify marker at a time, but we
            # check in this order so the most specific proxy type wins.
            live_meta = next((a for a in args[1:] if isinstance(a, LiveMeta)), None)
            lazy_meta = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
            inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

            if live_meta:
                # Return a LiveProxy — re-resolves on every .get() call.
                # Correct for REQUEST/SESSION scoped deps held by longer-lived components.
                return LiveProxy(
                    self,
                    base_type,
                    qualifier=live_meta.qualifier,
                    priority=live_meta.priority,
                )
            elif lazy_meta:
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
                inner = (
                    get_args(base_type)[0]
                    if get_origin(base_type) is list
                    else base_type
                )
                return self.get_all(inner, qualifier=inject_meta.qualifier)
            elif inject_meta:
                try:
                    return self.get(
                        base_type,
                        qualifier=inject_meta.qualifier,
                        priority=inject_meta.priority,
                    )
                except LookupError:
                    # optional=True: swallow the error and inject None.
                    # optional=False (default): re-raise so the caller sees the real error.
                    if inject_meta.optional:
                        return None
                    raise

        elif (
            isinstance(hint, type) or get_origin(hint) is not None
        ) and self._is_resolvable(hint):
            # DESIGN: also accept generic aliases (e.g. Repository[User]) which are
            # not `type` instances but do have a get_origin().  Plain annotations
            # like `repo: Repository[User]` land here when no Inject[] wrapper is used.
            return self.get(hint)

        return _UNRESOLVED  # signal: no binding found, caller decides

    async def _resolve_hint_async(
        self, hint: Any, param_name: str, owner_name: str
    ) -> Any:
        """Resolve a single type hint to an instance, asynchronously.

        Async mirror of :meth:`_resolve_hint_sync`. Handles all four cases
        identically to the sync path, except inner resolution uses ``aget`` / ``aget_all``.
        LazyProxy creation is still synchronous — .aget() is called later by the owner.

        Args:
            hint:       The raw type hint (possibly ``Annotated``).
            param_name: Parameter name, used only for error messages.
            owner_name: Class or function name, used only for error messages.

        Returns:
            The resolved instance, or :data:`_UNRESOLVED` if no binding matches.
        """
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            base_type = args[0]
            # Mirror of _resolve_hint_sync — same priority order: Live → Lazy → Inject.
            live_meta = next((a for a in args[1:] if isinstance(a, LiveMeta)), None)
            lazy_meta = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
            inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

            if live_meta:
                # Proxy creation is always sync — the proxy's .aget() method is async.
                return LiveProxy(
                    self,
                    base_type,
                    qualifier=live_meta.qualifier,
                    priority=live_meta.priority,
                )
            elif lazy_meta:
                # Proxy creation is always sync — the proxy's .aget() method is async.
                return LazyProxy(
                    self,
                    base_type,
                    qualifier=lazy_meta.qualifier,
                    priority=lazy_meta.priority,
                )
            elif inject_meta and inject_meta.all:
                inner = (
                    get_args(base_type)[0]
                    if get_origin(base_type) is list
                    else base_type
                )
                return await self.aget_all(inner, qualifier=inject_meta.qualifier)
            elif inject_meta:
                try:
                    return await self.aget(
                        base_type,
                        qualifier=inject_meta.qualifier,
                        priority=inject_meta.priority,
                    )
                except LookupError:
                    if inject_meta.optional:
                        return None
                    raise

        elif (
            isinstance(hint, type) or get_origin(hint) is not None
        ) and self._is_resolvable(hint):
            # Mirror of _resolve_hint_sync — accept generic aliases here too
            return await self.aget(hint)

        return _UNRESOLVED

    # ── Constructor & provider resolution ─────────────────────────

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
        self._check_cycle(cls)  # ✅ check before resolving

        # Push cls onto the stack for the duration of this resolution.
        # copy() — ContextVar is isolated per task, we build a new list.
        stack = _current_stack().copy()
        token = _resolution_stack.set(stack + [cls])

        try:
            resolved_kwargs = self._collect_kwargs_sync(cls.__init__, cls.__name__)
            return cls(**resolved_kwargs)
        finally:
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
            resolved_kwargs = await self._collect_kwargs_async(
                cls.__init__, cls.__name__
            )
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

        Async mirror of :meth:`_call_provider`. The result is awaited if *fn*
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

    # ── Cycle detection ───────────────────────────────────────────

    def _check_cycle(self, cls: type) -> None:
        """Raise if *cls* is already present in the current resolution stack.

        Called before every constructor or provider resolution. If *cls* is
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

    # ── Lifecycle hooks ───────────────────────────────────────────

    def _run_post_construct_sync(
        self,
        instance: Any,
        hook: LifecycleMarker | None,
    ) -> None:
        """Invoke the ``@PostConstruct`` lifecycle hook on *instance*, synchronously.

        A no-op when *hook* is ``None``.

        Args:
            instance: The freshly constructed object.
            hook:     The ``@PostConstruct`` marker, or ``None`` if absent.

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
        getattr(instance, hook.fn_name)()

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
            hook:     The ``@PostConstruct`` marker, or ``None`` if absent.

        Returns:
            None
        """
        if hook is None:
            return
        bound = getattr(instance, hook.fn_name)
        if hook.is_async:
            await bound()
        else:
            bound()

    # ── Scope context — convenience façade ───────────────────────
    #
    # DESIGN: these methods delegate to self.scope_context so callers
    # never need to access the attribute directly.  The container is
    # the single public entry point; scope_context is an implementation
    # detail.
    #
    #   Before:  with container.scope_context.request(): ...
    #   After:   with container.request(): ...

    def request(self) -> Any:
        """Activate a sync request scope context.

        Shorthand for ``container.scope_context.request()``.
        All @RequestScoped components resolved inside this block share one
        instance; a fresh instance is created for each new block.

        Returns:
            A sync context manager that yields the request ID string.

        Example:
            with container.request():
                svc = container.get(MyRequestScopedService)
        """
        return self.scope_context.request()

    def arequest(self) -> Any:
        """Activate an async request scope context.

        Shorthand for ``container.scope_context.arequest()``.

        Returns:
            An async context manager that yields the request ID string.

        Example:
            async with container.arequest():
                svc = await container.aget(MyRequestScopedService)
        """
        return self.scope_context.arequest()

    def session(self, session_id: str | None = None) -> Any:
        """Activate a sync session scope context.

        Shorthand for ``container.scope_context.session(session_id)``.
        Reuses an existing session cache when the same session_id is
        provided, creating a new one on first use.

        Args:
            session_id: Explicit session identifier (e.g. a user ID or
                        cookie value). A random UUID is used when omitted.

        Returns:
            A sync context manager that yields the session ID string.

        Example:
            with container.session("user-abc"):
                profile = container.get(UserProfile)
        """
        return self.scope_context.session(session_id)

    def asession(self, session_id: str | None = None) -> Any:
        """Activate an async session scope context.

        Shorthand for ``container.scope_context.asession(session_id)``.

        Args:
            session_id: Explicit session identifier. A random UUID is
                        used when omitted.

        Returns:
            An async context manager that yields the session ID string.

        Example:
            async with container.asession("user-abc"):
                async with container.arequest():
                    profile = await container.aget(UserProfile)
        """
        return self.scope_context.asession(session_id)

    def invalidate_session(self, session_id: str) -> None:
        """Destroy a session cache — call on logout or session expiry.

        Shorthand for ``container.scope_context.invalidate_session(session_id)``.

        Args:
            session_id: The session ID to invalidate. No-op if unknown.
        """
        self.scope_context.invalidate_session(session_id)

    def set_scoped(self, tp: type, instance: object) -> None:
        """Register a pre-built instance into the currently active scope cache.

        This lets middleware (or any code that runs inside a ``request()`` /
        ``session()`` block) push an already-constructed value into the DI
        container so that later ``get(tp)`` calls return it directly — without
        invoking any provider or constructor.

        The request cache is preferred when both are active (request scope is
        more specific than session scope).

        Args:
            tp:       The type to register the instance under — must match
                      the type used in ``container.get(tp)`` at resolution time.
            instance: The pre-built instance to store.

        Returns:
            None

        Raises:
            RuntimeError: If neither a request nor a session scope context
                is currently active.

        Edge cases:
            - Calling set_scoped() twice with the same type overwrites the
              first value — last write wins within a scope.
            - The instance is only visible for the lifetime of the current
              scope block; it is discarded when the context manager exits.
            - set_scoped() uses the class itself as the cache key, matching
              the key produced by ClassBinding._get_cache_key().  Registering
              under a base class / interface requires a separate call.

        Example — FastAPI JWT middleware::

            @app.middleware("http")
            async def jwt_middleware(request: Request, call_next):
                raw = request.headers.get("Authorization", "")
                if raw.startswith("Bearer "):
                    token = decode_jwt(raw.removeprefix("Bearer "))
                    container.set_scoped(JWTToken, token)
                return await call_next(request)

        Thread safety:  ✅ Safe — writes to the per-request dict which is
                        isolated to the current ContextVar scope.
        Async safety:   ✅ Safe — each asyncio Task has its own request cache
                        via ContextVar; concurrent requests never interfere.
        """
        # Prefer the request cache — it is more specific and shorter-lived.
        # Fall back to session cache so set_scoped() also works inside
        # session-only blocks (e.g. session setup middleware without an
        # inner request block).
        cache = self.scope_context.get_request_cache()
        if cache is None:
            cache = self.scope_context.get_session_cache()
        if cache is None:
            raise RuntimeError(
                f"set_scoped({tp.__name__!r}) called outside any active scope context. "
                f"Wrap the call inside `with container.request():` or "
                f"`with container.session(...):` first."
            )
        # Cache key matches _get_cache_key() for ClassBinding — the concrete class.
        cache[tp] = instance

    # ── Shutdown ──────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Sync shutdown — call ``@PreDestroy`` on all cached singleton instances.

        Raises if any ``@PreDestroy`` method is ``async def`` — use
        ``ashutdown()`` in that case. Clears all caches after teardown.

        Raises:
            RuntimeError: If any @PreDestroy hook is async def.
        """
        for binding in self._bindings:
            if not isinstance(binding, ClassBinding):
                continue  # providers have no lifecycle hooks
            if binding.pre_destroy is None:
                continue  # no @PreDestroy — skip

            key = binding.implementation
            instance = self._singleton_cache.get(key)
            if instance is None:
                continue  # never instantiated — skip

            if binding.pre_destroy.is_async:
                raise RuntimeError(
                    f"@PreDestroy method '{binding.pre_destroy.fn_name}' on "
                    f"'{binding.implementation.__name__}' is async — "
                    f"use await container.ashutdown() instead."
                )
            getattr(instance, binding.pre_destroy.fn_name)()

        self._clear_caches()

    async def ashutdown(self) -> None:
        """Async shutdown — call ``@PreDestroy`` on all cached singleton instances.

        Awaits async ``@PreDestroy`` methods, calls sync ones normally.
        Clears all caches after teardown.

        Example:
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
                await bound()
            else:
                bound()

        self._clear_caches()

    def _clear_caches(self) -> None:
        """Clear all instance caches — called at the end of shutdown."""
        self._singleton_cache.clear()
        self.scope_context.clear_caches()

    # ── Scope-leak validation ─────────────────────────────────────

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
            binding:   The ClassBinding whose constructor dependencies are inspected.
            qualifier: If given, only dependency bindings with this qualifier
                       are considered during the check.
            priority:  If given, only dependency bindings with this exact
                       priority are considered during the check.

        Returns:
            A list of :class:`~providify.metadata.ScopeLeak` instances, one
            per violating dependency. An empty list means no leaks were found.
        """
        leaks: list[ScopeLeak] = []
        # Accumulated Live[T] violations — raised as a group so the developer
        # sees all affected parameters at once, not just the first one.
        live_violations: list[LiveInjectionViolation] = []
        try:
            # include_extras=True preserves Annotated wrappers — we need the
            # InjectMeta / LazyMeta / LiveMeta inside them to distinguish HOW
            # each dep is wired, not just what type it resolves to.
            hints = get_type_hints(binding.implementation.__init__, include_extras=True)
        except Exception:
            return leaks

        hints.pop("return", None)
        for param_name, hint in hints.items():
            # Extract the injection marker BEFORE stripping Annotated — we need
            # to know whether the caller used Inject[T], Lazy[T], Live[T], or a
            # bare type.  Bare type and Inject[T] are wrong for scoped deps;
            # Lazy[T] is also wrong (it caches after the first call); Live[T] is correct.
            inject_marker: _providify | None = None
            if get_origin(hint) is Annotated:
                args = get_args(hint)
                base_type = args[0]
                inject_marker = next(
                    (a for a in args[1:] if isinstance(a, _providify)), None
                )
            else:
                base_type = hint

            if not isinstance(base_type, type):
                continue

            dep_bindings = self._filter(
                base_type, qualifier=qualifier, priority=priority
            )
            for dep in dep_bindings:
                if not _is_scope_leak(parent_scope=binding.scope, dep_scope=dep.scope):
                    continue

                if dep.scope in (Scope.REQUEST, Scope.SESSION):
                    # REQUEST and SESSION scoped deps must always be wrapped in Live[T]
                    # when held by a longer-lived component.  Inject[T] and Lazy[T]
                    # both capture one instance at construction time — that instance
                    # becomes stale the moment the scope boundary rotates.
                    if not isinstance(inject_marker, LiveMeta):
                        live_violations.append(
                            LiveInjectionViolation(
                                binding=(binding.implementation, binding.scope),
                                dep=(base_type, dep.scope),
                                param_name=param_name,
                            )
                        )
                else:
                    # Non-scoped leak (e.g. SINGLETON holding a DEPENDENT dep) —
                    # kept as a regular ScopeLeak, not a Live injection error.
                    leaks.append(
                        ScopeLeak(
                            binding=(binding.implementation, binding.scope),
                            reference=(dep.interface, dep.scope),
                        )
                    )

        if live_violations:
            raise LiveInjectionRequiredError(violations=live_violations)

        return leaks

    def validate_bindings(self) -> None:
        """Validate all registered bindings against the full registry.

        Iterates over every binding and calls
        :meth:`~providify.binding.Binding.validate`, which for
        :class:`~providify.binding.ClassBinding` instances performs
        scope-leak detection. This is the *phase transition* from registration
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

    # ── Dependency graph ──────────────────────────────────────────

    def _get_dependencies(
        self,
        binding: AnyBinding,
        _visited: frozenset[type] | None = None,
    ) -> list[AnyBinding]:
        """Dispatch to the correct dependency-collection strategy for a binding.

        Acts as a type-based router — delegates to ``_collect_dependencies``
        for both ``ClassBinding`` and ``ProviderBinding``. Raises immediately
        for unknown binding types so that missing implementations are caught at
        resolve-time rather than silently returning an empty list.

        Args:
            binding:  The binding whose constructor/provider signature will be
                      inspected to discover its dependencies.
            _visited: Optional frozenset of interface types already seen by the
                      caller during a recursive graph traversal. When provided,
                      any dep whose interface is already in ``_visited`` is
                      filtered out — preventing infinite loops for callers that
                      do NOT have their own cycle guard.

                      IMPORTANT: ``describe()`` does NOT pass ``_visited`` here
                      because it maintains its own cycle guard and needs the
                      cyclic dep binding to be returned so it can render the
                      ``[CYCLE DETECTED]`` sentinel.

        Returns:
            Ordered list of ``AnyBinding`` objects that *binding* depends on.

        Raises:
            TypeError: *binding* is not a ``ClassBinding`` or ``ProviderBinding``.
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
            return deps

        return [d for d in deps if d.interface not in _visited]

    # ── Scanning & module installation ────────────────────────────

    def scan(self, module: str | ModuleType, *, recursive: bool = False) -> None:
        """Scan a module for DI-decorated classes and functions.

        Delegates to the configured :class:`~providify.scanner.ContainerScanner`
        (defaults to :class:`~providify.scanner.DefaultContainerScanner`).

        Args:
            module:    A fully-qualified module name or an already-imported module.
            recursive: When ``True``, sub-packages are walked recursively.

        Returns:
            None

        Raises:
            ModuleNotFoundError: If *module* is a string that cannot be imported.
        """
        self._scanner.scan(module, recursive=recursive)

    def install(self, module_cls: type) -> None:
        """Install a ``@Configuration`` module synchronously.

        Instantiates *module_cls* with its constructor dependencies injected
        (Spring-style), then registers every ``@Provider``-decorated method on
        the module as a bound-method binding.

        Args:
            module_cls: A class decorated with ``@Configuration``.

        Returns:
            None

        Raises:
            TypeError:    If *module_cls* is not decorated with ``@Configuration``.
            LookupError:  If any constructor dependency of *module_cls* has no binding.
            RuntimeError: If any constructor dependency is async-only —
                          use :meth:`ainstall` instead.

        Example:
            container.bind(Settings, AppSettings)
            container.install(InfraModule)
        """
        if not _has_configuration_module(module_cls):
            raise TypeError(
                f"{module_cls.__name__} must be decorated with @Configuration."
            )
        instance = self._resolve_constructor(module_cls)
        self._register_module_providers(module_cls, instance)

    async def ainstall(self, module_cls: type) -> None:
        """Install a ``@Configuration`` module asynchronously.

        Async mirror of :meth:`install`. Use when the module's constructor
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
        if not _has_configuration_module(module_cls):
            raise TypeError(
                f"{module_cls.__name__} must be decorated with @Configuration."
            )
        instance = await self._resolve_constructor_async(module_cls)
        self._register_module_providers(module_cls, instance)

    def _register_module_providers(self, module_cls: type, instance: object) -> None:
        """Register every ``@Provider``-decorated method from a module instance.

        Iterates over the class's own attributes (not inherited ones) to find
        ``@Provider``-decorated methods. ``vars()`` gives the raw unbound functions,
        which carry ``ProviderMetadata`` directly on their ``__dict__``.

        Args:
            module_cls: The ``@Configuration`` class to inspect.
            instance:   The live module instance — getattr returns bound methods.

        Returns:
            None
        """
        for name, fn in vars(module_cls).items():
            if (
                callable(fn)
                and name != "__init__"
                and _get_provider_metadata(fn) is not None
            ):
                # getattr returns a bound method — self is the live module instance.
                self.provide(getattr(instance, name))

    # ── Describe ──────────────────────────────────────────────────

    def describe(self) -> DIContainerDescriptor:
        """Build a full ``DIContainerDescriptor`` snapshot of this container.

        Recursively describes every registered binding and its dependency tree.
        The result is a plain, serialisable object — safe to render, log, or
        convert to JSON via :meth:`~providify.descriptor.DIContainerDescriptor.to_dict`.

        Returns:
            A :class:`~providify.descriptor.DIContainerDescriptor` containing
            all binding descriptors grouped by scope.

        Example:
            descriptor = container.describe()
            print(descriptor)           # renders grouped ASCII tree
            data = descriptor.to_dict() # JSON-serialisable dict
        """
        return DIContainerDescriptor(
            validated=self._validated,
            bindings=tuple(b.describe(self) for b in self._bindings),
        )

    def __repr__(self) -> str:
        return (
            f"DIContainer(bindings={len(self._bindings)}, validated={self._validated})"
        )
