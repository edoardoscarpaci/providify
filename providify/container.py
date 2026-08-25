from __future__ import annotations

import asyncio
import collections
import functools
import inspect
import logging
import threading
import types
import warnings
import weakref
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from time import perf_counter_ns
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Literal,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from ._annotations import (
    _annotation_namespaces,
    _eval_annotation,
    resolve_class_annotations,
    resolve_params,
)
from .binding import AnyBinding, ClassBinding, ProviderBinding
from .config import bind_config_object
from .decorator.interceptor import (
    _get_around_invoke_method,
    _is_interceptor,
    _is_interceptor_binding,
)
from .decorator.lifecycle import (
    LifecycleMarker,
    _find_post_construct,
    _find_pre_destroy,
    _get_disposes_marker,
    _get_observes_marker,
)
from .decorator.scope import Provider
from .descriptor import DIContainerDescriptor
from .exceptions import (
    AnnotationResolutionError,
    CircularDependencyError,
    LiveInjectionRequiredError,
    ScopeViolationDetectedError,
    ShutdownError,
    ShutdownFailure,
)
from .metadata import (
    LiveInjectionViolation,
    Scope,
    ScopeLeak,
    _get_config_properties,
    _get_metadata,
    _get_provider_metadata,
    _has_configuration_module,
    _has_own_metadata,
    _is_scope_leak,
)
from .modules import resolve_install_order
from .observability import InstanceCreated, InstanceDisposed, ScopeEntered, ScopeExited
from .profiles import _normalise as _normalise_profile
from .profiles import resolve_active_profiles
from .resolution import (
    _UNRESOLVED,
    _current_injection_point,
    _current_stack,
    _format_cycle,
    _resolution_stack,
    _singleton_in_progress,
)
from .scanner import ContainerScanner, DefaultContainerScanner
from .scope import ScopeContext
from .type import (
    DelegateMeta,
    EventMeta,
    EventProxy,
    InjectionPoint,
    InjectMeta,
    InstanceMeta,
    InstanceProxy,
    InvocationContext,
    LazyMeta,
    LazyProxy,
    LiveMeta,
    LiveProxy,
    NamedMeta,
    _has_providify_metadata,
    _providify,
    _unwrap_classvar,
)
from .utils import _interface_matches, _type_name

if TYPE_CHECKING:
    # Only needed for validate()'s -> ValidationReport return annotation and
    # _iter_injection_points()'s _HintSpec yield type — `from __future__
    # import annotations` (top of file) means neither executes at runtime,
    # so this cannot create the import cycle a top-level `from .validation
    # import ...` would (validation.py's docstring notes it must never
    # import container.py; container.py importing IT is fine, just deferred
    # to inside each method itself for the runtime symbols).
    from .validation import ValidationReport, _HintSpec

T = TypeVar("T")

logger = logging.getLogger(__name__)


def _unwrap_union(hint: Any) -> tuple[list[Any], bool] | None:
    """Decompose a Union type hint into its candidate types and optionality.

    Handles both union representations Python provides:
    - ``typing.Union[T1, T2]`` and ``Optional[T]`` (== ``Union[T, None]``) —
      detected via ``get_origin(hint) is Union``.
    - Python 3.10+ pipe syntax ``T1 | T2`` — produces ``types.UnionType``,
      detected via ``isinstance(hint, types.UnionType)``.

    Returns ``None`` for any hint that is not a union, so callers can use a
    simple ``if _unwrap_union(hint) is not None:`` guard without worrying about
    false positives from plain concrete types or generics.

    Args:
        hint: Any type annotation, already evaluated (not a string).

    Returns:
        A ``(candidates, is_optional)`` tuple where:
        - ``candidates`` — non-``NoneType`` args, preserving declaration order.
        - ``is_optional`` — ``True`` iff ``NoneType`` appeared in the union args
          (i.e. the whole union is nullable / optional).
        Or ``None`` if *hint* is not a union type at all.

    Edge cases:
        - ``Union[None]``          → candidates=[], is_optional=True (degenerate)
        - ``Union[T]``             → not produced by Python; ``Union[T]`` collapses
          to bare ``T`` at parse time, so this helper never sees it.
        - Nested unions (Python flattens them) → already handled by ``get_args``.

    Example:
        >>> _unwrap_union(Optional[int])
        ([<class 'int'>], True)
        >>> _unwrap_union(int | str | None)
        ([<class 'int'>, <class 'str'>], True)
        >>> _unwrap_union(int)
        None
    """
    # DESIGN: Two separate isinstance/get_origin checks are needed because
    # Python uses different runtime representations for the two union syntaxes:
    #   - typing.Union produces a _GenericAlias whose get_origin() returns Union
    #   - X | Y produces a types.UnionType which has no get_origin() result
    # Unifying them here avoids duplicating this detection in every call site.
    if isinstance(hint, types.UnionType):
        # Python 3.10+ pipe syntax: int | str | None
        args = get_args(hint)
    elif get_origin(hint) is Union:
        # typing.Union[...] and Optional[T] (which is Union[T, None])
        args = get_args(hint)
    else:
        return None

    none_type = type(None)
    is_optional = none_type in args
    # Preserve declaration order — important for Union[T1, T2] where T1 is tried first
    candidates = [a for a in args if a is not none_type]
    return candidates, is_optional


# ─────────────────────────────────────────────────────────────────
#  ContainerSnapshot — opaque, restorable capture of mutable state
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ContainerSnapshot:
    """Opaque, restorable capture of a :class:`DIContainer`'s mutable state.

    Produced by :meth:`DIContainer.snapshot` and consumed by
    :meth:`DIContainer.restore`. The **only** supported use is
    ``container.restore(snap)`` on the *same* container instance that
    produced it — this is a value object for round-tripping state, not a
    general-purpose container description. Field values are opaque to
    callers other than ``DIContainer`` itself; do not read or mutate them.

    Frozen because a snapshot is a fact about a point in time — mutating one
    after capture would silently desync it from what ``restore()`` claims to
    reproduce (same reasoning as ``ValidationIssue``/``ShutdownFailure``/
    ``ConfigIssue``).

    Attributes:
        bindings: Shallow copy of ``_bindings`` at capture time.
        singleton_cache: Shallow copy of ``_singleton_cache`` at capture time.
        singleton_order: Shallow copy of ``_singleton_order`` at capture time.
        enabled_alternatives: Copy of ``_enabled_alternatives`` at capture time.
        active_profiles: The ``_active_profiles`` frozenset at capture time
            (already immutable, so no copy is needed).
        interceptor_classes: Shallow copy of ``_interceptor_classes`` at
            capture time.

    Edge cases:
        - Restoring into a *different* container than the one that produced
          the snapshot is not guarded against — it is a private-ish escape
          hatch, deliberately undocumented as a supported feature.
    """

    bindings: tuple[AnyBinding, ...]
    singleton_cache: dict[Any, object]
    singleton_order: tuple[tuple[Any, AnyBinding], ...]
    enabled_alternatives: frozenset[type]
    active_profiles: frozenset[str]
    interceptor_classes: tuple[type, ...]


# ─────────────────────────────────────────────────────────────────
#  _ModuleRecord — one entry per installed @Configuration module (Plan 008)
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ModuleRecord:
    """One entry in ``DIContainer._installed_modules`` — tracks a single
    installed ``@Configuration`` module instance.

    Three roles, one dict entry (see ``_installed_modules``'s DESIGN comment
    in ``__init__`` for the full rationale):
        1. **Dedup** — key (the module class) presence in the dict IS the
           "already installed" check; ``install()``/``ainstall()`` skip any
           class already present instead of re-instantiating it.
        2. **Install order** — dict insertion order (Python 3.7+ dicts are
           ordered) records the order modules were actually installed in.
        3. **Teardown order** — ``shutdown()``/``ashutdown()`` walk
           ``reversed(_installed_modules.items())`` to run ``@PreDestroy``
           hooks in exact reverse install order.

    Frozen — a record is a fact about one install event; ``owned``/
    ``disposed`` are updated via ``dataclasses.replace()`` (never mutated
    in place), mirroring ``ShutdownFailure``/``ConfigIssue``'s immutability
    rationale elsewhere in this codebase.

    Attributes:
        instance: The live module instance — ``getattr(instance, hook_name)``
            is how its ``@PreDestroy`` hook (if any) is invoked at shutdown.
        owned: ``True`` if *this* container created the instance (via
            ``install()``/``ainstall()``); ``False`` on a ``copy()`` — the
            copy inherits the dedup/install-order history (so it never
            re-registers providers it already holds bindings for) but did
            not create the instance, so it must never dispose it. Mirrors
            ``_singleton_order``'s ownership reasoning for ``copy()``.
        disposed: ``True`` after this record's ``@PreDestroy`` hook has run
            at shutdown — makes ``shutdown()``/``ashutdown()`` idempotent:
            a second ``shutdown()`` call skips every already-disposed
            record instead of re-running its hook.
    """

    instance: object
    owned: bool
    disposed: bool


# ─────────────────────────────────────────────────────────────────
#  _ScopedContainer — installs a temporary container as global
# ─────────────────────────────────────────────────────────────────


class _ScopedContainer:
    """
    Installs a fresh (or adopted) DIContainer as the global for the duration
    of the block. Restores the previous global on exit — even if an
    exception is raised.

    Supports both sync and async usage:
        with DIContainer.scoped() as c: ...
        async with DIContainer.scoped() as c: ...

    Adopting an existing container (``DIContainer.scoped(existing)``) does
    **not** shut it down on exit — only the global reference is restored.
    The caller remains responsible for the adopted container's lifecycle
    (e.g. via ``with existing: ...`` or an explicit ``shutdown()``).
    """

    def __init__(self, container: DIContainer | None = None) -> None:
        self._previous: DIContainer | None = None
        self._container: DIContainer | None = None
        # The container to adopt, if any — kept separate from ``_container``
        # (which only holds the *installed* container once _install() runs)
        # so repeated __enter__/__exit__ cycles on one _ScopedContainer keep
        # adopting the same instance rather than falling back to a fresh one
        # after the first cycle.
        self._container_arg = container

    def _install(self) -> DIContainer:
        """Creates (or adopts) and installs a container as the global."""
        self._previous = DIContainer._global
        self._container = self._container_arg or DIContainer()
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
#  _InterceptorProxy — transparent AOP wrapper for @Interceptor
# ─────────────────────────────────────────────────────────────────


class _InterceptorProxy:
    """Wraps a bean instance and intercepts method calls via the interceptor chain.

    All attribute accesses are delegated to the underlying ``_target``. For
    callable attributes the proxy wraps the call in an ``InvocationContext``
    so each ``@AroundInvoke`` method in *chain* is invoked before the real
    method body runs.

    Note:
        ``isinstance(proxy, TargetClass)`` returns ``False``. Use
        ``type(proxy._target)`` when class identity is required.
    """

    __slots__ = ("_target", "_chain")

    def __init__(self, target: object, chain: list[tuple[object, str]]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_chain", chain)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_target"), name)
        chain = object.__getattribute__(self, "_chain")
        if not callable(attr):
            return attr

        def _intercepted(*args: Any, **kwargs: Any) -> Any:
            ctx = InvocationContext(
                target=object.__getattribute__(self, "_target"),
                method=attr,
                parameters=args,
                kwargs=kwargs,
                _chain=list(chain),
            )
            return ctx.proceed()

        return _intercepted

    def __repr__(self) -> str:
        target = object.__getattribute__(self, "_target")
        return f"_InterceptorProxy({target!r})"


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
        - Singleton resolution is protected by per-key double-check locking
          (Feature 4) — exactly one instance is created even under concurrent
          access from multiple threads or tasks.

    Inheritance and MRO:
        The two injection paths differ deliberately in how they handle MRO:

        * **``__init__`` parameters** — the container reads *only* the
          declared signature of the class being constructed.  If a subclass
          overrides ``__init__``, only the subclass's parameters are injected.
          When ``__init__`` is *not* overridden, Python's own MRO already
          resolves ``cls.__init__`` to the parent's method, so parent params
          are injected automatically with no special handling.  Walking parent
          ``__init__`` signatures on top of that would conflict with any
          ``super().__init__(arg)`` call the developer writes — the same dep
          could be resolved twice, producing two separate instances.

        * **Class-level annotations** — :meth:`_resolve_class_annotations`
          (Phase 7's per-attribute replacement for a single
          ``get_type_hints(cls)`` call) walks the full MRO, so
          ``var: Inject[T]`` annotations declared on a parent class are
          inherited and injected on every subclass instance automatically.  This
          is the right extension point for shared deps that all subclasses need.
    """

    _global: ClassVar[DIContainer | None] = None
    # Two locks — one per execution context.
    # threading.Lock for sync callers, asyncio.Lock for async callers.
    _sync_lock: ClassVar[threading.Lock] = threading.Lock()
    _async_lock: ClassVar[asyncio.Lock | None] = (
        None  # created lazily — needs event loop
    )

    # Exposed as a class attribute so callers (and tests) that already have
    # a resolved `Annotated[...]` hint in hand can classify its union shape
    # without needing a container instance — e.g. asserting that
    # `Live[Foo | None]`'s underlying union carries `optional=True`.
    # `optional` is a property of the *evaluated hint object*, never stamped
    # onto the marker itself (`LiveMeta`/`InjectMeta` have no `optional`
    # field to stamp for this case) — this module-level helper is the single
    # source of truth for that derivation; the container's instance methods
    # (`_resolve_hint_sync`/`_async`) call the free function directly.
    _unwrap_union = staticmethod(_unwrap_union)

    # ── Initialisation ────────────────────────────────────────────

    def __init__(
        self,
        *,
        scan: str | list[str] | None = None,
        recursive: bool = True,
        profiles: Iterable[str] | None = None,
    ) -> None:
        """Initialise an empty container with no bindings.

        All state is instance-local — multiple containers can coexist in the
        same process without interfering (e.g. one per test via ``scoped()``).

        Args:
            scan:      A fully-qualified module name (``str``), a list of module
                       names, or ``None`` (default).  When provided, each module
                       is scanned immediately at construction time — equivalent
                       to calling ``container.scan(name, recursive=recursive)``
                       for each name after the container is created.
                       Useful for applications that want a single, declarative
                       registration step:

                       .. code-block:: python

                           container = DIContainer(scan="myapp", recursive=True)
                           # all @Component/@Singleton/@Provider in myapp are
                           # registered before the first get() call.

            recursive: When ``True`` (default), sub-packages under each *scan*
                       entry are walked recursively.  Has no effect when *scan*
                       is ``None``.

            profiles:  The container's initial active profile set for
                       ``@Profile``-gated bindings (plan 005). Precedence,
                       deliberately explicit:

                       .. list-table::
                          :header-rows: 1

                          * - ``profiles=``
                            - ``PROVIDIFY_PROFILES``
                            - active set
                          * - ``None`` (default)
                            - unset
                            - ``frozenset()``
                          * - ``None``
                            - ``"prod,eu"``
                            - ``{"prod", "eu"}``
                          * - ``("prod",)``
                            - ignored
                            - ``{"prod"}``
                          * - ``()``
                            - ignored
                            - ``frozenset()`` — *explicitly* no profiles

                       An explicit argument (even an empty one) always wins
                       over the environment variable. See
                       ``providify.profiles.resolve_active_profiles`` for the
                       resolution logic and ``providify.profiles.ENV_VAR``
                       for the environment variable name.

        Returns:
            None

        Raises:
            ModuleNotFoundError: If a module name in *scan* cannot be imported.

        Edge cases:
            - ``scan=None`` (default) — no scanning, backward-compatible.
            - ``scan=[]`` — empty list is treated the same as ``None``.
            - ``scan="myapp"`` — single string, scanned once with *recursive*.
            - ``scan=["a", "b"]`` — each module scanned left-to-right; later
              modules may add bindings that complement earlier ones.
            - ``profiles=()`` with ``PROVIDIFY_PROFILES`` set in the
              environment — the explicit empty set wins; ``active_profiles``
              is ``frozenset()``.
        """
        self._bindings: list[AnyBinding] = []
        self._singleton_cache: dict[Any, object] = {}
        # DESIGN: append-only creation log for singletons; reversed at
        # shutdown to obtain reverse-dependency order — valid because
        # ``_instantiate_sync``/``_instantiate_async`` cache a dependency
        # before its dependent (see line ~1611-1615): a binding's own
        # dependencies are resolved *inside* ``binding.create(self)`` and
        # only written to ``_singleton_cache`` afterwards, so this list is
        # always a valid topological order of the singleton dependency DAG.
        #
        # Tradeoffs:
        #   ✅ O(1) append on creation, O(n) reverse-scan on shutdown —
        #      no graph re-derivation, no cycle handling needed
        #   ✅ Captures the *actual* runtime graph, including edges only
        #      visible at runtime (Lazy[T]/Live[T]/Provider[T])
        #   ❌ A late Lazy[T]/Provider[T] pull can invert an edge relative
        #      to "true" dependency order (documented on shutdown()'s
        #      docstring) — inherent to reverse-creation-order tracking
        # Alternative considered: static topological sort over
        # _get_dependencies() — rejected (see plans/004 §Alternatives):
        # that method is deliberately lossy for its real caller, describe().
        self._singleton_order: list[tuple[Any, AnyBinding]] = []
        # DESIGN: ScopeContext receives @PreDestroy callbacks so it can run
        # lifecycle hooks exactly when a request/session scope frame exits —
        # before the cache is popped.  Both sync and async callbacks are wired
        # here; ScopeContext calls whichever is appropriate for its context manager.
        #
        # Tradeoffs:
        #   ✅ Lifecycle hooks fire at the right moment (scope exit, not shutdown)
        #   ✅ Container logic stays in the container; ScopeContext stays generic
        #   ❌ Two extra bound-method references stored on every ScopeContext
        # DESIGN: all four callbacks use the same underlying methods —
        # invalidate_session / ainvalidate_session are semantically identical
        # to scope exit (run @PreDestroy, discard cache).  Separate callbacks
        # keep ScopeContext's control flow explicit rather than special-casing
        # the invalidation path inside the shared on_scope_exit callback.
        self.scope_context: ScopeContext = ScopeContext(
            on_scope_exit=self._run_pre_destroy_for_scope,
            on_scope_exit_async=self._arun_pre_destroy_for_scope,
            on_invalidate_session=self._run_pre_destroy_for_scope,
            on_invalidate_session_async=self._arun_pre_destroy_for_scope,
        )
        self._scanner: ContainerScanner = DefaultContainerScanner(self)
        # Starts unvalidated — first resolution triggers validate_bindings()
        self._validated: bool = False
        # Lazily-built cache for _collect_kwargs — maps class __name__ → class.
        # Set to None whenever a binding is added so it is rebuilt on next use.
        # In the common case (all bindings registered before the first get()),
        # the dict is built exactly once and reused for every resolution.
        self._localns_cache: dict[str, type] | None = None
        # Phase 7 (Plan 001) — per-callable/per-class resolved-hints cache.
        # Key: the callable/class object itself (fn, cls, or cls.__init__) —
        # see `_invalidate_type_caches` and `_resolve_params` /
        # `_resolve_class_annotations` for the full caching rationale
        # (plan §7.4). Dies together with `_localns_cache` because a
        # resolved hint's correctness depends on `_build_localns()`.
        self._hints_cache: dict[Any, dict[str, Any]] = {}

        # ── Feature 4: per-key singleton locks (double-check locking) ──────
        # DESIGN: _singleton_locks maps a cache key to a per-key threading.Lock.
        # _singleton_lock_guard protects the creation of per-key locks so that
        # two threads that simultaneously reach a cold cache entry do not both
        # create new lock objects — only the first one's lock is used.
        #
        # Tradeoffs:
        #   ✅ True double-check locking: one instance created per singleton key
        #   ✅ Only SINGLETON scope is affected — REQUEST/SESSION use ContextVar
        #      isolation which already prevents races at the scope level
        #   ❌ Small overhead for the guard lock on first access per key
        #   ❌ Lock dict grows with the number of distinct singleton keys (bounded)
        self._singleton_locks: dict[Any, threading.Lock] = {}
        self._singleton_lock_guard: threading.Lock = threading.Lock()

        # ── Feature 4: per-key asyncio locks ────────────────────────────────
        # Created lazily inside _instantiate_async — asyncio.Lock() requires a
        # running event loop at creation time, so we cannot create them here.
        # _async_singleton_locks is the dict; the guard is the same
        # _singleton_lock_guard (creating asyncio.Lock also needs protection).
        # DESIGN: asyncio.Lock instances must NEVER be created at module level
        # or in __init__ — they require an active event loop.
        self._async_singleton_locks: dict[Any, asyncio.Lock] = {}

        # ── Jakarta CDI parity state ───────────────────────────────
        # @Alternative — activated per-container; excluded by default from _filter()
        self._enabled_alternatives: set[type] = set()
        # @Profile — active profile set for this container; consulted by
        # _binding_is_active() inside _filter() on every resolution.
        # resolve_active_profiles() handles the explicit-arg-vs-env-var
        # precedence documented on __init__'s docstring above.
        self._active_profiles: frozenset[str] = resolve_active_profiles(profiles)
        # @Interceptor — registered interceptor classes applied to all resolved instances
        self._interceptor_classes: list[type] = []
        # Event[T] / @Observes — maps event type → [(weakref, method_name)]
        self._observers: dict[type, list] = {}
        # @Component(track=True) — tracked DEPENDENT instances for flush_dependents()
        self._tracked_dependents: list[object] = []

        # ── Observability hooks (Plan 009/F6) ───────────────────────
        # DESIGN: keyed by exact event TYPE (InstanceCreated, InstanceDisposed,
        # ScopeEntered, ScopeExited), never a base class — `self._hooks.get(type(event))`
        # gives exact-type dispatch with no Protocol/base-class machinery, and
        # makes the zero-hook fast path a single truthiness test (`if self._hooks:`)
        # BEFORE any timer or event-object work, all the way down in
        # `_instantiate_sync`/`_instantiate_async`/`shutdown`/the scope façades.
        # This is deliberately NOT the `Event[T]` / `@Observes` dispatcher above
        # (`self._observers`) — that system resolves *observer beans* through the
        # container for *application* events; this dispatches *container
        # telemetry* to *plain callables* and never re-enters resolution. See
        # `observability.py`'s module docstring for the full isolation rationale.
        self._hooks: dict[type, list[Callable[[Any], None]]] = {}

        # ── @Configuration module install/teardown tracking (Plan 008/F5) ──
        # DESIGN: one dict, three roles — see _ModuleRecord's docstring above
        # for the full breakdown (dedup / install order / teardown order).
        # Moves dedup authority from the scanner (scanner.py's
        # _installed_configurations, a same-session shortcut only) to the
        # container, which is the only thing that can see BOTH an explicit
        # install(M) call and a later scan() covering the same M — fixing
        # the historical double-registration bug (plan 008 §Design, item 4).
        #
        # Tradeoffs:
        #   ✅ O(1) "already installed?" check via key presence
        #   ✅ dict insertion order gives install order for free (no separate list)
        #   ✅ owned=False on copy() prevents a copy from disposing instances
        #      it never created (mirrors _singleton_order's copy() handling)
        #   ❌ Survives shutdown() (deliberately — see _clear_caches, which
        #      does NOT touch this dict): re-installing after shutdown() into
        #      the SAME container is a no-op, not a re-registration. A fully
        #      reusable container after shutdown() requires copy() or a new
        #      DIContainer() — documented on install()/ainstall().
        self._installed_modules: dict[type, _ModuleRecord] = {}

        # ── Auto-scan at construction time ────────────────────────
        # DESIGN: Eager scan (at __init__) rather than lazy scan (deferred to
        # first get()) was chosen because it makes errors surface at the point
        # of misconfiguration (container creation) rather than later at first
        # resolution, which is harder to trace.
        #
        # Tradeoffs:
        #   ✅ Fail-fast — ModuleNotFoundError pinpoints the bad module name
        #   ✅ All bindings present before any manual bind() / register() call
        #   ❌ If scan modules have side-effects on import, they run at __init__
        #      rather than deferred (acceptable; import side-effects are rare)
        #
        # Alternative considered: lazy scan triggered by first get() — rejected
        # because it would surface import errors far from the registration site.
        if scan is not None:
            # Normalise to a list so the loop below is uniform
            modules = [scan] if isinstance(scan, str) else list(scan)
            for module_name in modules:
                self.scan(module_name, recursive=recursive)

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
    def scoped(cls, container: DIContainer | None = None) -> _ScopedContainer:
        """Return a context manager that installs a container as global.

        Supports both sync and async:

            with DIContainer.scoped() as container: ...
            async with DIContainer.scoped() as container: ...

        Args:
            container: An existing container to adopt as the global for the
                duration of the block. When ``None`` (default), a fresh
                ``DIContainer()`` is created instead — the original
                behaviour. Adopting an existing container does **not**
                shut it down on exit; only the previous global reference is
                restored. This is the mechanism behind pytest's ``di_global``
                fixture, which needs ``DIContainer.current()`` to resolve to
                a specific, already-configured test container.

        Returns:
            A :class:`_ScopedContainer` context manager.

        Example:
            test_container = DIContainer()
            test_container.bind(Clock, FrozenClock)
            with DIContainer.scoped(test_container):
                assert DIContainer.current() is test_container
        """
        return _ScopedContainer(container)

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
        self.flush_dependents()
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
        await self.aflush_dependents()
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
        self._invalidate_type_caches()  # new binding — localns/hints must be rebuilt
        self._bindings.append(ClassBinding(interface, implementation))

        # When the caller binds an interface to a *different* implementation,
        # also self-bind the implementation so it is resolvable directly via
        # container.get(ConcreteClass) — not just via the interface key.
        # Without this, any injection point that declares the concrete type
        # (rather than the interface) gets a LookupError.
        # Guard: skip when interface IS implementation to avoid a duplicate
        # self-binding that would create ambiguous resolution.
        # exact_only=True ensures this synthetic entry is invisible to
        # supertype sweeps like get_all(BaseClass) — it only answers to
        # container.get(ConcreteClass) directly.
        if interface is not implementation:
            self._bindings.append(
                ClassBinding(implementation, implementation, exact_only=True)
            )

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
        self._invalidate_type_caches()  # new binding — localns/hints must be rebuilt
        self._bindings.append(ClassBinding(cls, cls))

    def provide(self, fn: Callable[..., Any], *, returns: Any = None) -> None:
        """Register a provider function (sync or async) as a binding.

        The function's return type annotation is used as the resolved
        interface, unless overridden — see ``returns`` below.

        Interface resolution priority (highest wins):
            1. ``returns`` — this call's override.
            2. ``@Provider(returns=...)`` — decoration-time override.
            3. ``fn``'s resolved return annotation.

        When an override is in effect, the return annotation is not read,
        not evaluated, and not validated.

        Example — an interface only nameable as a generic alias built inside
        a loop, which no static return annotation could ever express::

            for model in (User, Order):
                def repo_factory(model=model) -> Any:
                    return InMemoryRepo(model)
                container.provide(repo_factory, returns=Repository[model])

        Args:
            fn: A callable that creates and returns the dependency.
                May be a regular function or an ``async def``.
            returns: Explicit interface override for this registration. A
                type, a parameterised generic alias (e.g. ``Repository[User]``),
                an ``Annotated[...]`` wrapper (unwrapped automatically), or a
                zero-arg callable evaluated once, right now, to produce one of
                the above. See ``binding._normalize_explicit_interface`` for
                the full accepted-shapes table.

        Returns:
            None

        Raises:
            TypeError: If no override is in effect and *fn* has no return
                type annotation (or it is unresolvable); or if a given
                ``returns`` value cannot be resolved to a type or generic
                alias.
        """
        self._validated = False
        self._invalidate_type_caches()  # new binding — localns/hints must be rebuilt
        self._bindings.append(ProviderBinding(fn, returns=returns))

    def bind_config(self, cls: type, *, sources: Sequence[Any] | None = None) -> None:
        """Register a ``@ConfigProperties`` class as a singleton binding.

        Config binding is *exactly* a provider registration — no new
        ``Binding`` subclass (plan 006 §Design/"Container integration").
        The class's merged configuration (env/YAML/JSON/TOML, per its
        ``@ConfigProperties`` sources) is loaded and bound into an instance
        the first time it is resolved, via ``provide(fn, returns=cls)``:
        it is visible to ``describe()``, ``validate()``, ``override()`` and
        ``reset_binding()`` with no special-casing, and torn down/ordered by
        ``_singleton_order`` like any other provider.

        ``sources`` precedence (highest wins):
            1. ``sources`` — this call's override.
            2. ``@ConfigProperties(sources=...)`` — decoration-time sources.

        The override-at-call-site precedence mirrors ``provide(fn,
        returns=...)``'s own (``:692-698`` above) — it is also the test
        seam: ``bind_config(DbSettings, sources=[DictSource({...})])`` needs
        no env or files.

        Binding is **lazy** and **singleton**: the factory runs once, on the
        first ``get(cls)`` — not at registration time. A misconfigured
        deployment (missing required field, unparsable value) therefore
        raises ``ConfigBindingError`` at first resolution, not at
        ``bind_config()`` call time. See ``warm_up()`` / ``validate()`` for
        fail-at-startup — ``validate()`` does not instantiate anything, so it
        will not catch a bad config *value*; only ``warm_up()`` will.

        Args:
            cls: A class decorated with ``@ConfigProperties``.
            sources: Optional override for the sources declared on the
                decorator — loaded in order, later sources win on key
                conflicts (deep merge, plan 006 §Design).

        Returns:
            None

        Raises:
            TypeError: If *cls* is not decorated with ``@ConfigProperties``.

        Example:
            @ConfigProperties(prefix="db", sources=(EnvSource(),))
            @dataclass(frozen=True)
            class DbSettings:
                url: str

            container.bind_config(DbSettings)
            container.get(DbSettings)   # raises ConfigBindingError here, not above,
                                         # if DB__URL is unset
        """
        meta = _get_config_properties(cls)  # raises TypeError if not decorated
        effective = tuple(sources) if sources is not None else meta.sources

        @Provider(singleton=True)
        def _config_factory() -> Any:
            return bind_config_object(cls, effective, prefix=meta.prefix)

        self.provide(_config_factory, returns=cls)

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
        qualifier: str | type | None = None,
        priority: int | None = None,
    ) -> None:
        """Eagerly instantiate all singleton bindings in the container (sync version).

        Validates the full binding list before instantiating anything — if any
        async provider is present the method raises immediately without touching
        the singleton cache, giving a clean all-or-nothing guarantee.

        Note: only instantiates SINGLETONs — use :meth:`validate` for
        whole-graph checks (missing/ambiguous bindings, circular
        dependencies) that cover every scope without instantiating anything.

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
        qualifier: str | type | None = None,
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
        qualifier: str | type | None = None,
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
                f"'{best.fn.__name__}' is an async provider — use await container.aget() instead."
            )
        if not self._validated:
            self.validate_bindings()
            self._validated = True
        return self._instantiate_sync(best)  # type: ignore[return-value]

    def get_all(
        self,
        cls: type[T] | Any,
        qualifier: str | type | None = None,
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
        qualifier: str | type | None = None,
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
        qualifier: str | type | None = None,
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

    def _binding_is_active(self, b: AnyBinding) -> bool:
        """Return True if *b* is eligible for resolution under current activation state.

        Uniform activation predicate consulted by ``_filter()`` on every
        resolution — the single place that decides whether a binding exists
        "right now", combining ``@Profile`` and ``@Alternative`` activation
        (plan 005 §Design):

        ::

            _binding_is_active(b) <=> profile_ok(b) AND alternative_ok(b)

            profile_ok(b)      : b.profiles == ()      -> True   (unprofiled)
                                  any expr in b.profiles matches self._active_profiles

            alternative_ok(b)  : b.source not @Alternative -> True
                                  b.profiles != ()          -> True  (@Profile is the activator)
                                  b.source in self._enabled_alternatives

        ``b.source`` is ``ClassBinding.implementation`` or
        ``ProviderBinding.fn`` — both ``@Alternative`` and ``@Profile``
        markers are read from ``__dict__``, matching ``_is_alternative``'s
        non-inherited lookup. This closes a pre-existing asymmetry: before
        this plan, the ``@Alternative`` guard only inspected
        ``ClassBinding``, so the marker on a ``@Provider`` function was
        silently ignored (always active) — this predicate now reads it off
        ``ProviderBinding.fn`` too (plan 005 §Design, "behaviour change").

        Args:
            b: The binding to test — a ``ClassBinding`` or ``ProviderBinding``.

        Returns:
            True if *b* should be visible to ``get()``/``get_all()``/
            ``is_resolvable()``/``validate()`` right now.

        Thread safety: ⚠️ Conditional — safe only if ``_active_profiles`` /
            ``_enabled_alternatives`` are not mutated concurrently with a
            resolution (same caveat as ``_filter()``).
        Async safety:  ✅ No await points; no shared mutable state written.

        Edge cases:
            - No ``@Profile`` and no ``@Alternative`` anywhere -> always True
              (short-circuits on ``not b.profiles``, falls through to the
              unchanged ``@Alternative`` rule for the rare marked case).
            - ``@Alternative`` + matching ``@Profile`` + inactive in
              ``_enabled_alternatives`` -> still active; the profile is the
              activator and ``_enabled_alternatives`` membership is not
              consulted once ``b.profiles`` is non-empty.
            - ``@Alternative`` + ``@Profile`` that does NOT match -> inactive,
              even after ``enable_alternative()`` — the profile is a hard
              gate (AND), not overridable imperatively.
        """
        from .binding import ClassBinding as _ClassBinding
        from .metadata import _is_alternative
        from .profiles import matches as _profile_matches

        # Short-circuit order: `not b.profiles` first — the overwhelmingly
        # common case is an unprofiled binding, and this avoids the frozenset
        # scan in `matches()` entirely for it.
        profile_ok = not b.profiles or _profile_matches(
            b.profiles, self._active_profiles
        )
        if not profile_ok:
            return False

        source = b.implementation if isinstance(b, _ClassBinding) else b.fn
        if not _is_alternative(source):
            return True
        # @Profile is the declarative activator for @Alternative — once a
        # profile expression is present, it alone governs eligibility.
        if b.profiles:
            return True
        return source in self._enabled_alternatives

    def _filter(
        self,
        cls: type,
        qualifier: str | type | None = None,
        priority: int | None = None,
    ) -> list[AnyBinding]:
        """Return all bindings whose interface is a subclass of *cls*.

        Optionally narrows the result by *qualifier* and/or *priority*.
        The same logic is shared by both the sync and async resolution paths.

        Activation rules (plan 005) applied via ``_binding_is_active()``:

        | condition                                   | included? |
        |----------------------------------------------|-----------|
        | unprofiled, not @Alternative                  | always    |
        | unprofiled @Alternative, not enabled           | no        |
        | unprofiled @Alternative, enabled               | yes       |
        | @Profile matches active set                    | yes       |
        | @Profile does not match active set              | no        |
        | @Profile matches + @Alternative (any enable state) | yes   |

        Args:
            cls:       The base type to match against ``binding.interface``.
            qualifier: If given, only bindings with a matching qualifier are kept.
            priority:  If given, only bindings with this exact priority are kept.

        Returns:
            A (possibly empty) list of matching bindings.
        """
        from .decorator.scope import Default as _Default

        # @Default is semantically equivalent to no qualifier
        if qualifier is _Default:
            qualifier = None

        return [
            b
            for b in self._bindings
            # DESIGN: _interface_matches replaces plain issubclass so that generic
            # aliases like Repository[User] are matched correctly — issubclass does
            # not accept parameterised types as its second argument.
            if _interface_matches(b.interface, cls)
            # DESIGN: exact_only bindings are self-bindings auto-generated to make
            # a concrete class resolvable by its own type.  They must NOT participate
            # in supertype sweeps (e.g. get_all(BaseClass)) because they would surface
            # every concrete subclass registered under its parent interface — creating
            # duplicates.  We allow them only when the requested type is exactly the
            # binding's interface (identity check is safe here: self-bindings are
            # always concrete classes, never generic aliases).
            and (not getattr(b, "exact_only", False) or b.interface is cls)
            and (qualifier is None or b.qualifier == qualifier)
            and (priority is None or b.priority == priority)
            # @Profile / @Alternative activation — see _binding_is_active().
            and self._binding_is_active(b)
        ]

    def is_resolvable(
        self,
        cls: type,
        *,
        qualifier: str | type | None = None,
        priority: int | None = None,
    ) -> bool:
        """Return True if at least one registered binding matches *cls*.

        A side-effect-free predicate — no instances are created or cached.
        Intended as the public counterpart to the private ``_filter()`` helper,
        so that :class:`~providify.type.InstanceProxy` and user code can guard
        optional dependencies without coupling to internal APIs.

        Args:
            cls:       The interface or concrete type to check.
            qualifier: If given, only bindings registered with this qualifier
                       are considered. ``None`` matches any qualifier.
            priority:  If given, only bindings with this exact priority are
                       considered. ``None`` accepts any priority.

        Returns:
            True  — at least one binding satisfies all conditions; calling
                    ``get(cls, qualifier=qualifier, priority=priority)``
                    will not raise ``LookupError``.
            False — no binding matches; ``get()`` would raise.

        Thread safety:  ⚠️ Conditional — safe only if ``_bindings`` is not
                        mutated concurrently.  See class-level safety note.
        Async safety:   ✅ No await points; no shared mutable state written.

        Edge cases:
            - No bindings registered at all → False.
            - qualifier narrows to zero     → False.
            - Called before first get()     → does NOT trigger validate_bindings().
            - Results are not cached — re-evaluated on every call, so a new
              binding added between two calls will be reflected immediately. ✅

        Example:
            if container.is_resolvable(Notifier, qualifier="sms"):
                svc = container.get(Notifier, qualifier="sms")
        """
        # DESIGN: delegate to _filter() which is the single source of truth for
        # binding matching logic.  _filter() is a pure list comprehension with no
        # side effects — calling it here does not trigger validation or instantiation.
        return bool(self._filter(cls, qualifier=qualifier, priority=priority))

    # ── @Alternative / @Profile — deployment-time bean activation ──

    def enable_alternative(self, cls: type) -> None:
        """Activate an @Alternative-marked class for this container.

        Once enabled, the alternative participates in _filter() results
        and will be returned by get() / get_all() like any other binding.

        Args:
            cls: A class decorated with @Alternative.
        """
        self._enabled_alternatives.add(cls)
        self._validated = False

    def disable_alternative(self, cls: type) -> None:
        """Deactivate a previously enabled @Alternative class.

        After disabling, the alternative is excluded from resolution again —
        UNLESS *cls* also carries ``@Profile`` and that profile currently
        matches ``active_profiles``. In that case the profile is the
        activator (plan 005 §Design) and this call has no visible effect on
        resolution: ``_binding_is_active()`` never consults
        ``_enabled_alternatives`` once a binding's ``.profiles`` is
        non-empty. ``_enabled_alternatives`` is still updated (so a later
        profile deactivation and re-enable-by-name compose predictably), but
        the class remains resolvable until its profile itself stops matching.

        Args:
            cls: A class previously passed to enable_alternative().
        """
        self._enabled_alternatives.discard(cls)
        self._validated = False

    @property
    def active_profiles(self) -> frozenset[str]:
        """Return a read-only snapshot of this container's active profile set.

        Returns:
            The current ``frozenset[str]`` of normalised, active profile
            names. Frozensets are immutable, so the returned object cannot
            be mutated by the caller to affect container state — mirroring
            the read-only intent of other snapshot-style properties in this
            class.

        Thread safety: ✅ Safe — reads a single reference; the frozenset
            itself is immutable once built.
        Async safety:  ✅ No await points.

        Example:
            container.activate_profile("prod")
            assert "prod" in container.active_profiles
        """
        return self._active_profiles

    def activate_profile(self, name: str) -> None:
        """Add *name* to this container's active profile set.

        Mirrors ``enable_alternative()``'s shape exactly, including the
        ``self._validated = False`` reset — the reachable graph may have
        changed (a previously-filtered ``@Profile``d binding can now be
        selected), so the next resolution should re-run scope-leak /
        graph checks. ``_invalidate_type_caches()`` is deliberately **not**
        called: no binding was added or removed, so ``_localns_cache`` and
        ``_hints_cache`` (which key off bindings, not activation state)
        remain valid.

        Args:
            name: A profile name — normalised (stripped, lower-cased) the
                same way ``@Profile(...)`` and ``resolve_active_profiles``
                normalise their inputs, so ``"Dev"`` and ``"dev"`` are the
                same profile.

        Returns:
            None

        Thread safety: ⚠️ Not synchronized — mutating ``_active_profiles``
            concurrently with an in-flight resolution is a plain race, the
            same caveat as ``enable_alternative()``.
        Async safety:  Same caveat as thread safety.

        Edge cases:
            - Activating an already-active profile is a no-op (frozenset
              union is idempotent).
            - A ``@Profile``d singleton already resolved under the OLD
              profile set is **not** evicted from ``_singleton_cache`` —
              profiles are a startup-time concern; ``override()`` /
              ``reset_binding()`` remain the eviction tools.

        Example:
            container.activate_profile("prod")
            container.get(Mailer)  # now sees @Profile("prod") bindings
        """
        self._active_profiles = self._active_profiles | {_normalise_profile(name)}
        self._validated = False

    def deactivate_profile(self, name: str) -> None:
        """Remove *name* from this container's active profile set.

        Mirrors ``disable_alternative()``'s shape exactly, including the
        ``self._validated = False`` reset. A silent no-op when *name* is not
        currently active — mirroring ``set.discard()``'s tolerance, which
        ``disable_alternative()`` already relies on.

        Args:
            name: A profile name — normalised the same way
                ``activate_profile`` normalises its input, so
                ``deactivate_profile("dev")`` removes a profile that was
                activated as ``"Dev"``.

        Returns:
            None

        Thread safety: ⚠️ Not synchronized — same caveat as
            ``activate_profile()``.
        Async safety:  Same caveat as thread safety.

        Edge cases:
            - *name* not currently active -> silent no-op, no exception.
            - Does not evict any cached singleton — see
              ``activate_profile()``'s note; profiles select bindings, never
              cached instances.

        Example:
            container.deactivate_profile("dev")  # safe even if never activated
        """
        self._active_profiles = self._active_profiles - {_normalise_profile(name)}
        self._validated = False

    # ── Interceptor registration (F5) ────────────────────────────

    def add_interceptor(self, cls: type) -> None:
        """Register an ``@Interceptor``-decorated class to intercept matching beans.

        Interceptors are applied in registration order. The interceptor class
        must be decorated with ``@Interceptor`` and must have exactly one
        ``@AroundInvoke`` method.

        Args:
            cls: An ``@Interceptor``-decorated class.
        """
        if not _is_interceptor(cls):
            raise TypeError(f"{cls.__name__} must be decorated with @Interceptor.")
        if cls not in self._interceptor_classes:
            self._interceptor_classes.append(cls)

    def _apply_interceptors(self, instance: object, cls: type) -> object:
        """Wrap *instance* with an interceptor proxy if any interceptors apply.

        Finds interceptors whose ``@InterceptorBinding`` annotations overlap with
        those on *cls*, resolves interceptor instances, and returns an
        ``_InterceptorProxy`` wrapping the original. If no interceptors apply,
        returns *instance* unchanged.

        Args:
            instance: The fully constructed bean instance.
            cls:      The bean's implementation class.

        Returns:
            The original instance, or an ``_InterceptorProxy`` wrapping it.
        """
        if not self._interceptor_classes:
            return instance

        # Collect interceptor binding annotation types on the target class
        target_bindings: set[type] = set()
        for name in dir(cls):
            val = getattr(cls, name, None)
            if isinstance(val, type) and _is_interceptor_binding(val):
                target_bindings.add(val)

        if not target_bindings:
            return instance

        # Find interceptors that share at least one binding annotation
        chain: list[tuple[object, str]] = []
        for interceptor_cls in self._interceptor_classes:
            for name in dir(interceptor_cls):
                val = getattr(interceptor_cls, name, None)
                if isinstance(val, type) and val in target_bindings:
                    around_invoke = _get_around_invoke_method(interceptor_cls)
                    if around_invoke is not None:
                        interceptor_instance = self._resolve_constructor(
                            interceptor_cls
                        )
                        chain.append((interceptor_instance, around_invoke))
                    break

        if not chain:
            return instance

        return _InterceptorProxy(instance, chain)

    # ── Event dispatch (F7) ──────────────────────────────────────

    def _register_observers(self, instance: object, cls: type) -> None:
        """Scan *cls* for ``@Observes`` methods and register *instance* as a subscriber.

        Walks the MRO to find all ``@Observes``-decorated methods. Stores weak
        references so observer registrations don't prevent garbage collection.

        Args:
            instance: The newly created bean instance.
            cls:      The bean's implementation class.
        """
        for base in cls.__mro__:
            for name, fn in vars(base).items():
                if not callable(fn):
                    continue
                marker = _get_observes_marker(fn)
                if marker is None:
                    continue
                event_type = marker.event_type
                if event_type not in self._observers:
                    self._observers[event_type] = []
                self._observers[event_type].append(
                    (weakref.ref(instance), name, marker.is_async)
                )

    def _dispatch_event_sync(self, event: object) -> None:
        """Dispatch *event* synchronously to all registered ``@Observes`` observers.

        Delivers to observers whose registered event type is a supertype of
        ``type(event)``. Dead weak references are purged during dispatch.

        Args:
            event: The event object to dispatch.
        """
        event_type = type(event)

        for registered_type, observers in self._observers.items():
            if not issubclass(event_type, registered_type):
                continue
            live: list = []
            for ref, method_name, is_async in observers:
                inst = ref()
                if inst is None:
                    continue
                live.append((ref, method_name, is_async))
                if is_async:
                    warnings.warn(
                        f"@Observes method '{method_name}' is async — "
                        f"use EventProxy.afire() for async dispatch.",
                        stacklevel=2,
                    )
                    continue
                getattr(inst, method_name)(event)
            self._observers[registered_type] = live

    async def _dispatch_event_async(self, event: object) -> None:
        """Dispatch *event* asynchronously to all registered ``@Observes`` observers.

        Awaits async observer methods; calls sync ones normally. Dead weak
        references are purged during dispatch.

        Args:
            event: The event object to dispatch.
        """
        event_type = type(event)

        for registered_type, observers in self._observers.items():
            if not issubclass(event_type, registered_type):
                continue
            live: list = []
            for ref, method_name, is_async in observers:
                inst = ref()
                if inst is None:
                    continue
                live.append((ref, method_name, is_async))
                bound = getattr(inst, method_name)
                if is_async:
                    await bound(event)
                else:
                    bound(event)
            self._observers[registered_type] = live

    # ── DEPENDENT scope flush (F10) ──────────────────────────────

    def flush_dependents(self) -> None:
        """Call ``@PreDestroy`` on all tracked DEPENDENT-scoped instances and clear them.

        Only instances created by classes decorated with ``@Component(track=True)``
        (or any scoping decorator with ``track=True``) are tracked and flushed.

        Raises:
            RuntimeError: If any @PreDestroy is ``async def`` — use ``aflush_dependents()``.
        """
        for instance in self._tracked_dependents:
            cls = type(instance)
            binding = next(
                (
                    b
                    for b in self._bindings
                    if isinstance(b, ClassBinding) and b.implementation is cls
                ),
                None,
            )
            if binding is None or binding.pre_destroy is None:
                continue
            if binding.pre_destroy.is_async:
                raise RuntimeError(
                    f"@PreDestroy on '{cls.__name__}' is async — "
                    f"use await container.aflush_dependents() instead."
                )
            getattr(instance, binding.pre_destroy.fn_name)()
        self._tracked_dependents.clear()

    async def aflush_dependents(self) -> None:
        """Async variant of :meth:`flush_dependents`.

        Awaits async ``@PreDestroy`` hooks; calls sync ones normally.
        """
        for instance in self._tracked_dependents:
            cls = type(instance)
            binding = next(
                (
                    b
                    for b in self._bindings
                    if isinstance(b, ClassBinding) and b.implementation is cls
                ),
                None,
            )
            if binding is None or binding.pre_destroy is None:
                continue
            bound = getattr(instance, binding.pre_destroy.fn_name)
            if binding.pre_destroy.is_async:
                await bound()
            else:
                bound()
        self._tracked_dependents.clear()

    # ── Scope utilities (F14) ────────────────────────────────────

    def run_in_request(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute *fn* inside a request scope, returning its result.

        Args:
            fn:     A sync callable to execute.
            *args:  Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            The return value of *fn*.
        """
        with self.request():
            return fn(*args, **kwargs)

    async def arun_in_request(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Execute *fn* inside an async request scope, returning its result.

        Args:
            fn:     An async callable to execute.
            *args:  Positional arguments forwarded to *fn*.
            **kwargs: Keyword arguments forwarded to *fn*.

        Returns:
            The awaited return value of *fn*.
        """
        async with self.arequest():
            return await fn(*args, **kwargs)

    def run_in_session(
        self, session_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Execute *fn* inside a session scope identified by *session_id*.

        Args:
            session_id: The session identifier.
            fn:         A sync callable to execute.
            *args:      Positional arguments forwarded to *fn*.
            **kwargs:   Keyword arguments forwarded to *fn*.

        Returns:
            The return value of *fn*.
        """
        with self.session(session_id):
            return fn(*args, **kwargs)

    async def arun_in_session(
        self, session_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Execute *fn* inside an async session scope identified by *session_id*.

        Args:
            session_id: The session identifier.
            fn:         An async callable to execute.
            *args:      Positional arguments forwarded to *fn*.
            **kwargs:   Keyword arguments forwarded to *fn*.

        Returns:
            The awaited return value of *fn*.
        """
        async with self.asession(session_id):
            return await fn(*args, **kwargs)

    def _filter_singleton(
        self,
        qualifier: str | type | None = None,
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
        qualifier: str | type | None = None,
        priority: int | None = None,
    ) -> AnyBinding:
        """Return the highest-priority binding for the requested type.

        Args:
            cls:       The interface or concrete type to resolve.
            qualifier: Named qualifier to filter bindings. ``None`` matches any.
            priority:  Exact priority to match. ``None`` returns the best available.

        Returns:
            The highest-priority-value binding among all matching candidates
            (higher value = higher precedence).

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

    def _record_singleton_creation(self, key: Any, binding: AnyBinding) -> None:
        """Append ``(key, binding)`` to the singleton creation log.

        Called exactly once per key: both call sites (``_instantiate_sync``
        and ``_instantiate_async``) are inside the per-key double-check lock
        (see ``_check_singleton_reentry`` / the ``with self._locks[key]:``
        blocks around lines ~1618 and ~1707), so a given key can never reach
        ``cache[key] = instance`` twice. The log therefore stays free of
        duplicate entries without any dedup logic here — ``_teardown_plan``
        still defends against duplicates defensively (see its docstring).

        Args:
            key:     The singleton cache key, from :meth:`_get_cache_key`.
            binding: The binding that produced the instance now at *key* —
                     stored so teardown can find its ``pre_destroy``/disposer
                     without re-deriving it from ``_bindings``.

        Returns:
            None.

        Thread safety: ✅ Safe — appends under ``self._singleton_lock_guard``,
            the same cheap, no-I/O guard already held on this code path
            (the per-key lock creation guard), so recording the creation adds
            no new lock contention and the list stays consistent without
            relying on GIL atomicity for ``list.append``.
        """
        with self._singleton_lock_guard:
            self._singleton_order.append((key, binding))

    def _check_singleton_reentry(self, binding: AnyBinding, key: Any) -> None:
        """Raise if this thread/task is already creating the singleton *key*.

        Both singleton paths hold a non-reentrant per-key lock across
        ``create()`` / ``acreate()``. If that call resolves back to the same
        binding, re-acquiring the lock would block forever on a lock the
        caller already owns — and the cycle detection that runs inside
        ``create()`` would never be reached. This converts that hang into the
        same :class:`CircularDependencyError` the DEPENDENT path already
        raises, at the point where the cycle is actually detectable.

        Args:
            binding: The binding about to be instantiated — used only to name
                     the offending component in the error message.
            key:     Its singleton cache key (from :meth:`_get_cache_key`).

        Returns:
            None — returns silently when no re-entry is in progress.

        Raises:
            CircularDependencyError: When *key* is already being created in
                the current context, with the resolution chain in the message.

        Thread safety:  ✅ Reads a ``ContextVar`` private to this thread; a
                        different thread waiting on the per-key lock has its
                        own empty set and is never affected.
        Async safety:   ✅ Each ``asyncio.Task`` inherits its own copy, so
                        concurrent tasks cannot see each other's in-progress
                        keys.

        Edge cases:
            - Same key, different container → different ``id(self)``, so no
              false positive.
            - ``create()`` raised on a previous attempt → the key was reset in
              a ``finally``, so a retry is not mistaken for a cycle.
        """
        if (id(self), key) not in _singleton_in_progress.get():
            return

        # The resolution stack was pushed by _resolve_constructor / _call_provider
        # inside create(), so it already names the component we re-entered.
        owner = getattr(binding, "implementation", None) or binding.interface
        name = _type_name(owner)
        raise CircularDependencyError(
            f"{_format_cycle(_current_stack(), owner)}\n"
            f"The singleton '{name}' requested itself while it was still being "
            f"created — its own constructor or provider resolved back to its own "
            f"binding. A common cause is a parameter annotated with a type that "
            f"matches every binding: a bare 'object' (or 'object | None') is a "
            f"supertype of all of them, including this one."
        )

    # ── Instantiation ─────────────────────────────────────────────

    @staticmethod
    def _instance_created_implementation(
        binding: AnyBinding,
    ) -> type | Callable[..., Any] | None:
        """Return the ``implementation`` field for an ``InstanceCreated``/``InstanceDisposed`` event.

        A ``ClassBinding`` carries the concrete class directly
        (``binding.implementation``); a ``ProviderBinding`` has no such
        attribute — its "implementation" is the factory function
        (``binding.fn``). Centralised here so both instrumentation sites
        (`_instantiate_sync`, `_instantiate_async`) and both disposal sites
        agree on the mapping.

        Args:
            binding: The binding being created or disposed.

        Returns:
            The implementation class for a ``ClassBinding``, the factory
            callable for a ``ProviderBinding``, or ``None`` for any other
            binding kind.
        """
        if isinstance(binding, ClassBinding):
            return binding.implementation
        if isinstance(binding, ProviderBinding):
            return binding.fn
        return None

    def _instantiate_sync(self, binding: AnyBinding) -> Any:
        """Instantiate *binding* synchronously, respecting scope caching.

        For SINGLETON scope, uses per-key double-check locking to guarantee
        exactly one instance is created under concurrent access from multiple
        threads.

        Pattern (singleton path only):
            1. Check cache without lock — fast path for already-cached singletons.
            2. Lazy-create a per-key threading.Lock (under guard lock).
            3. Check cache again inside the per-key lock — another thread may
               have won the race between steps 1 and 2.
            4. Create instance and store under the per-key lock.

        For REQUEST/SESSION scope, the ContextVar isolation guarantees that each
        task sees its own cache — no additional locking is needed.
        For DEPENDENT scope, no cache exists — a new instance is always created.

        Args:
            binding: The binding to instantiate.

        Returns:
            The (possibly cached) resolved instance.

        Thread safety:  ✅ SINGLETON: double-check lock ensures one creation.
                        REQUEST/SESSION: ContextVar-isolated — no shared state.
                        DEPENDENT: no cache — concurrent calls each get a new instance.
        Async safety:   ✅ No await points — safe to call from sync code.

        Edge cases:
            - binding.create() raises → no cache entry is stored (exception propagates).
            - Two threads race on a cold singleton key → guard lock serialises
              lock creation; per-key lock serialises instance creation.
            - The singleton's own create() resolves back to this same key on this
              same thread → ``CircularDependencyError`` from
              :meth:`_check_singleton_reentry`. The per-key lock is not
              reentrant, so without that guard the call would block forever on a
              lock it already holds.
        """
        key = self._get_cache_key(binding)
        cache = self._get_cache(binding)

        # ── Fast path: already in cache (no lock needed) ─────────────────────
        if cache is not None and key in cache:
            return cache[key]

        # ── SINGLETON double-check locking ────────────────────────────────────
        # Only apply per-key locking for the singleton cache.  REQUEST/SESSION
        # caches are per-context (ContextVar) so concurrent tasks cannot share
        # them; DEPENDENT has no cache at all.
        if cache is self._singleton_cache:
            # Re-entrancy guard — MUST run before acquiring the per-key lock.
            # If this thread is already inside create() for this key, taking
            # the lock again would block forever on a lock we ourselves hold
            # (threading.Lock is not reentrant), and the cycle detection that
            # lives inside create() would never get to run.
            self._check_singleton_reentry(binding, key)

            # Lazy-create the per-key lock under the guard lock to prevent
            # two threads from each creating a different lock object for the
            # same key — only the first one must be used.
            with self._singleton_lock_guard:
                if key not in self._singleton_locks:
                    self._singleton_locks[key] = threading.Lock()
            per_key_lock = self._singleton_locks[key]

            with per_key_lock:
                # Second check inside the lock: another thread may have already
                # created and cached the instance between our first check and
                # acquiring this lock.
                if key in cache:
                    return cache[key]
                # Zero-cost rule (plan 009 §Design): the timer is only started
                # when a hook is registered — `perf_counter_ns` is imported by
                # name so a test can patch `providify.container.perf_counter_ns`
                # and assert it is never called with zero hooks.
                started = perf_counter_ns() if self._hooks else 0
                token = _singleton_in_progress.set(
                    _singleton_in_progress.get() | {(id(self), key)}
                )
                try:
                    instance = binding.create(self)
                    if isinstance(binding, ClassBinding):
                        self._register_observers(instance, binding.implementation)
                        instance = self._apply_interceptors(
                            instance, binding.implementation
                        )
                    cache[key] = instance
                    # Record AFTER the cache write so a concurrent shutdown()
                    # racing this creation never sees the key in _singleton_order
                    # before it is actually retrievable from _singleton_cache.
                    self._record_singleton_creation(key, binding)
                finally:
                    # Reset even when create() raises: the failed key must not
                    # stay marked in-progress, or a later retry in the same
                    # context would report a phantom cycle.
                    _singleton_in_progress.reset(token)
            # Emission happens OUTSIDE the per-key lock (plan 009 §Design) — a
            # hook is arbitrary user code; running it while holding the
            # creation lock would deadlock if it re-resolves another singleton.
            if self._hooks:
                self._emit(
                    InstanceCreated(
                        interface=binding.interface,
                        implementation=self._instance_created_implementation(binding),
                        scope=binding.scope,
                        qualifier=binding.qualifier,
                        duration_ns=perf_counter_ns() - started,
                        is_async=False,
                    )
                )
            return instance

        # ── Non-singleton path (REQUEST, SESSION, DEPENDENT) ─────────────────
        started = perf_counter_ns() if self._hooks else 0
        instance = binding.create(self)
        if isinstance(binding, ClassBinding):
            self._register_observers(instance, binding.implementation)
            instance = self._apply_interceptors(instance, binding.implementation)
        # REQUEST/SESSION: emit only when this call actually populates the
        # scope cache for the first time — a second get() within the same
        # scope frame is a cache hit (checked above) and must emit nothing.
        if cache is not None:
            cache[key] = instance
        elif isinstance(binding, ClassBinding):
            # DEPENDENT scope: track if requested
            meta = _get_metadata(binding.implementation)
            if meta is not None and meta.track:
                self._tracked_dependents.append(instance)
        if self._hooks:
            self._emit(
                InstanceCreated(
                    interface=binding.interface,
                    implementation=self._instance_created_implementation(binding),
                    scope=binding.scope,
                    qualifier=binding.qualifier,
                    duration_ns=perf_counter_ns() - started,
                    is_async=False,
                )
            )
        return instance

    async def _instantiate_async(self, binding: AnyBinding) -> Any:
        """Instantiate *binding* asynchronously, respecting scope caching.

        Mirrors :meth:`_instantiate_sync` but delegates to ``binding.acreate()``.
        For SINGLETON scope, uses per-key asyncio.Lock double-check locking —
        the same pattern as the sync path but with asyncio primitives so the
        event loop is never blocked.

        asyncio.Lock objects are created lazily inside this method (never at
        module level or ``__init__``) because they require a running event loop.

        Args:
            binding: The binding to instantiate.

        Returns:
            The (possibly cached) resolved instance.

        Async safety:   ✅ SINGLETON: asyncio.Lock ensures one creation per key
                        per event loop.  REQUEST/SESSION: ContextVar-isolated.
                        DEPENDENT: no cache — each awaiter gets a fresh instance.

        Edge cases:
            - binding.acreate() raises → no cache entry is stored.
            - Two concurrent tasks race on a cold singleton key → the asyncio.Lock
              serialises them; only one runs acreate().
            - The singleton's own acreate() awaits this same key from this same
              task → ``CircularDependencyError`` from
              :meth:`_check_singleton_reentry`; ``asyncio.Lock`` is not reentrant
              either, so the await would otherwise never complete.
        """
        key = self._get_cache_key(binding)
        cache = self._get_cache(binding)

        # ── Fast path ─────────────────────────────────────────────────────────
        if cache is not None and key in cache:
            return cache[key]

        # ── SINGLETON double-check locking (async) ────────────────────────────
        if cache is self._singleton_cache:
            # Re-entrancy guard — see _instantiate_sync. asyncio.Lock is no more
            # reentrant than threading.Lock: a singleton whose acreate() awaits
            # its own resolution would await a lock its own task already holds,
            # which never completes.
            self._check_singleton_reentry(binding, key)

            # Lazy-create asyncio.Lock under the threading guard lock.
            # DESIGN: the guard lock is a threading.Lock here (not asyncio.Lock)
            # because asyncio.Lock cannot be created outside an event loop and
            # we only need it for a very brief dict-check + insert — no I/O.
            # The window is tiny; the risk of blocking the loop is negligible.
            with self._singleton_lock_guard:
                if key not in self._async_singleton_locks:
                    # Created lazily — requires a running loop ✅
                    self._async_singleton_locks[key] = asyncio.Lock()
            async_lock = self._async_singleton_locks[key]

            async with async_lock:
                if key in cache:
                    return cache[key]
                # Zero-cost rule — see _instantiate_sync's matching comment.
                started = perf_counter_ns() if self._hooks else 0
                token = _singleton_in_progress.set(
                    _singleton_in_progress.get() | {(id(self), key)}
                )
                try:
                    instance = await binding.acreate(self)
                    if isinstance(binding, ClassBinding):
                        self._register_observers(instance, binding.implementation)
                        instance = self._apply_interceptors(
                            instance, binding.implementation
                        )
                    cache[key] = instance  # type: ignore[index]
                    # Record AFTER the cache write — see _instantiate_sync.
                    self._record_singleton_creation(key, binding)
                finally:
                    # Reset even when acreate() raises — see _instantiate_sync.
                    _singleton_in_progress.reset(token)
            # Emission outside the asyncio.Lock — see _instantiate_sync's
            # matching comment; the invariant is identical on the async path.
            if self._hooks:
                self._emit(
                    InstanceCreated(
                        interface=binding.interface,
                        implementation=self._instance_created_implementation(binding),
                        scope=binding.scope,
                        qualifier=binding.qualifier,
                        duration_ns=perf_counter_ns() - started,
                        is_async=True,
                    )
                )
            return instance

        # ── Non-singleton path ────────────────────────────────────────────────
        started = perf_counter_ns() if self._hooks else 0
        instance = await binding.acreate(self)
        if isinstance(binding, ClassBinding):
            self._register_observers(instance, binding.implementation)
            instance = self._apply_interceptors(instance, binding.implementation)
        if cache is not None:
            cache[key] = instance
        elif isinstance(binding, ClassBinding):
            meta = _get_metadata(binding.implementation)
            if meta is not None and meta.track:
                self._tracked_dependents.append(instance)
        if self._hooks:
            self._emit(
                InstanceCreated(
                    interface=binding.interface,
                    implementation=self._instance_created_implementation(binding),
                    scope=binding.scope,
                    qualifier=binding.qualifier,
                    duration_ns=perf_counter_ns() - started,
                    is_async=True,
                )
            )
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

    def _invalidate_type_caches(self) -> None:
        """Discard every cache whose contents depend on the current binding set.

        `_localns_cache` and `_hints_cache` (plan §7.4) must die TOGETHER:
        every value in `_hints_cache` was resolved using the localns
        `_build_localns()` produced at the time, so a stale `_hints_cache`
        entry surviving a `_localns_cache` rebuild would silently serve
        pre-mutation hints (e.g. a parameter that was unresolvable-and-
        skipped before a new `bind()` call, wrongly staying skipped after).

        This is the ONE place both caches are cleared — every call site that
        used to write ``self._localns_cache = None`` directly (``bind()``,
        ``register()``, ``provide()``, ``reset_binding()``, ``copy()``, and
        ``scanner.py``'s external poke into the container's private state)
        now calls this method instead, so a future third cache has exactly
        one place to be added — no "forgot the seventh site" bug.

        Thread safety:  ⚠️ Same caveat as `_build_localns` — no lock. Two
                        threads invalidating concurrently both end up with
                        empty caches; the next resolution on either thread
                        rebuilds from the (now-consistent) binding list. The
                        window this closes is "stale hints survive a binding
                        change", not "no data race" — the container's binding
                        mutation itself is not documented as thread-safe post
                        first resolution (see class docstring).

        Returns:
            None
        """
        self._localns_cache = None
        self._hints_cache.clear()

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

        Edge cases:
            - A binding whose ``interface`` is not a type or generic alias
              (e.g. corrupted by external mutation) is skipped with a
              ``logger.warning`` rather than raising — this namespace is
              shared by every resolution in the container, so one malformed
              binding must never abort construction for all the others.

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
                iface_key = iface_origin if iface_origin is not None else b.interface
                # WHY the getattr guard: this namespace is CONTAINER-WIDE, so an
                # unhandled AttributeError here silently zeroes the type hints of
                # every other binding (the original forward-reference bug).  One
                # malformed interface must cost only itself.
                iface_name = getattr(iface_key, "__name__", None)
                if iface_name is None:
                    logger.warning(
                        "Skipping binding with non-type interface %r while building "
                        "the type-hint namespace; its dependents may fail to resolve.",
                        b.interface,
                    )
                else:
                    localns[iface_name] = iface_key  # type: ignore[assignment]
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

    def _resolve_params(
        self,
        target: Callable[..., Any],
        owner_name: str,
        *,
        owner: type | None = None,
    ) -> dict[str, Any]:
        """Resolve *target*'s parameters one at a time — the Phase-7 replacement for both Tier-1/2 whole-signature helpers.

        Delegates to :func:`providify._annotations.resolve_params`, which
        evaluates each parameter's annotation in isolation so that one
        unresolvable, non-injected parameter can no longer wipe out every
        other parameter's hints (the false positive both
        ``_resolve_hints_or_warn`` and ``_resolve_hints_or_raise`` used to
        paper over from opposite ends). Replaces both of those release-A
        helpers — one resolver, two callers (the injection path and the
        validation path), because the "raise vs. skip" decision is now made
        PER PARAMETER by :func:`resolve_params` itself, not by the caller's
        choice of helper.

        Caching:
            Keyed by *target* itself (the callable object — see
            :attr:`_hints_cache`'s definition in ``__init__`` for why NOT
            ``id(target)`` or a ``WeakKeyDictionary``). A cache hit returns a
            SHALLOW COPY so a caller mutating the returned dict (several do,
            e.g. ``hints.pop(...)`` historically) can never corrupt the
            cached entry for the next resolution.  Only successes are
            cached — a failure re-raises fresh every time rather than
            replaying a exception with a stale traceback.

        Thread safety:  ✅ Safe without a lock. Every cache value is a pure
                        function of ``(target, self._bindings)`` — a race
                        between two threads recomputes IDENTICAL data, and
                        plain ``dict`` item assignment is atomic under the
                        GIL, so no torn read/write is possible. The one
                        mutable hazard (a caller editing its returned dict)
                        is eliminated by the copy-on-read above, not by a
                        lock — see plan 001 §7.4.
        Async safety:   ✅ Safe — no ``await`` inside resolution; sync and
                        async callers share one cache with no special
                        casing needed.

        Args:
            target:     The callable whose parameters are resolved —
                        typically ``cls.__init__`` or a provider function.
            owner_name: Human-readable name embedded in any raised error.
            owner:      The class that declares *target* as a method, for
                        PEP-695 ``__type_params__`` seeding — forwarded to
                        :func:`providify._annotations._annotation_namespaces`.

        Returns:
            ``dict[param_name -> resolved hint]`` — a fresh copy on every
            call, safe for the caller to mutate.

        Raises:
            AnnotationResolutionError: A parameter's annotation IS (or
                plausibly is) an injection point but cannot be evaluated —
                naming both *owner_name* and the specific parameter.
        """
        cached = self._hints_cache.get(target)
        if cached is not None:
            return dict(cached)

        globalns, localns = _annotation_namespaces(
            target, self._build_localns(), owner=owner
        )
        hints = resolve_params(target, owner_name, globalns, localns)
        self._hints_cache[target] = hints
        return dict(hints)

    def _resolve_class_annotations(self, cls: type) -> dict[str, Any]:
        """Resolve *cls*'s class-level annotations one at a time (Phase-7 replacement).

        Delegates to :func:`providify._annotations.resolve_class_annotations`,
        which walks the full MRO and evaluates each attribute's annotation in
        isolation. Replaces the release-A pairing of
        ``_resolve_hints_or_warn`` (Tier-1, ``_inject_class_vars_sync/async``)
        and ``_resolve_hints_or_raise`` (Tier-2, ``_collect_class_var_hints``)
        for this target kind — per-attribute resolution makes both policies
        the SAME function, because an unresolvable annotation is now either
        an injection point (raise, naming the attribute) or not (silently
        omitted) regardless of which caller asked.

        Caching:            Keyed by *cls* itself; see :meth:`_resolve_params`
                             — identical strategy (copy-on-read, success-only
                             caching, shared ``_hints_cache``).
        Thread/async safety: Identical rationale to :meth:`_resolve_params`.

        Args:
            cls: The class whose (and whose ancestors') class-level
                 annotations are resolved.

        Returns:
            ``dict[attr_name -> resolved hint]`` — a fresh copy on every call.

        Raises:
            AnnotationResolutionError: A class attribute's annotation IS (or
                plausibly is) an injection point but cannot be evaluated —
                naming the declaring class and the attribute.
        """
        cached = self._hints_cache.get(cls)
        if cached is not None:
            return dict(cached)

        hints = resolve_class_annotations(cls, self._build_localns())
        self._hints_cache[cls] = hints
        return dict(hints)

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
            AnnotationResolutionError: A parameter's annotation IS (or
                plausibly is) an injection point but cannot be evaluated —
                see :meth:`_resolve_params`. Unresolvable annotations on
                parameters that are NOT injection points no longer affect
                this call at all (Phase 7 — per-parameter resolution).
        """
        hints = self._resolve_params(fn, owner_name)

        sig = inspect.signature(fn)
        resolved: dict[str, Any] = {}

        # Declaring class for InjectionPoint — outermost class on the resolution stack.
        declaring_class = _current_stack()[-1] if _current_stack() else None

        for param_name, hint in hints.items():
            param = sig.parameters.get(param_name)
            # Build InjectionPoint context so InjectionPoint-typed params resolve correctly.
            ip = InjectionPoint(
                declaring_class=declaring_class,
                param_name=param_name,
                qualifier=None,
                annotation=hint,
            )
            ip_token = _current_injection_point.set(ip)
            try:
                resolved_value = self._resolve_hint_sync(hint, param_name, owner_name)
            finally:
                _current_injection_point.reset(ip_token)

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
            AnnotationResolutionError: See :meth:`_collect_kwargs_sync` —
                identical policy, async mirror.
        """
        hints = self._resolve_params(fn, owner_name)

        sig = inspect.signature(fn)
        resolved: dict[str, Any] = {}

        declaring_class = _current_stack()[-1] if _current_stack() else None

        for param_name, hint in hints.items():
            param = sig.parameters.get(param_name)
            ip = InjectionPoint(
                declaring_class=declaring_class,
                param_name=param_name,
                qualifier=None,
                annotation=hint,
            )
            ip_token = _current_injection_point.set(ip)
            try:
                resolved_value = await self._resolve_hint_async(
                    hint, param_name, owner_name
                )
            finally:
                _current_injection_point.reset(ip_token)

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
        qualifier: str | type | None = None,
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
            - A parameter that IS an injection point but cannot be resolved
              (``AnnotationResolutionError`` from :meth:`_resolve_params`) →
              swallowed **and logged**; every OTHER parameter that DID
              resolve is still included — Phase 7 yields a partial graph
              here instead of the whole-signature ``[]`` release A produced.
            - No providify parameters → returns ``[]``.
        """
        try:
            hints = self._resolve_params(fn, getattr(fn, "__qualname__", str(fn)))
        except AnnotationResolutionError as exc:
            # Tier 3 — advisory/reporting path (dependency-graph construction).
            # A partial or missing graph node is a worse debugging experience,
            # not a wiring bug, so the failure is swallowed — but it must never
            # be silent, or the gap in the graph looks like "no dependencies"
            # rather than "couldn't tell". Per-parameter resolution means this
            # only fires for a parameter that IS an injection point — every
            # OTHER parameter on *fn* already resolved fine independently, so
            # in practice this now only ever loses ONE dependency, not all of them.
            logger.warning(
                "Dependency graph for '%s' is incomplete — cannot resolve type hints (%s).",
                getattr(fn, "__qualname__", fn),
                exc,
            )
            hints = {}

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
        qualifier: str | type | None = None,
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

            # ── Detect Optional[T] / T | None inside the Inject/Live/Lazy wrapper ──
            # Handles: Inject[Optional[T]], Inject[T | None],
            #          Live[Optional[T]],   Live[T | None],
            #          Lazy[Optional[T]],   Lazy[T | None].
            #
            # When the user writes Inject[T | None], the runtime expansion is
            # Annotated[T | None, InjectMeta()].  base_type is then the union
            # T | None, NOT a registered concrete type — so self.get(T | None)
            # would always raise LookupError.  _unwrap_union unwraps it to T
            # and signals that None should be returned when the binding is absent.
            #
            # DESIGN: only simplify for exactly ONE non-None candidate, i.e. a
            # true Optional[T].  Multi-type unions (Inject[T1 | T2 | None]) are
            # passed through unchanged — the caller can spell that as a plain
            # T1 | T2 | None annotation outside of Inject/Live/Lazy, which the
            # existing Union-branch below already handles correctly.
            _base_union = _unwrap_union(base_type)
            if (
                _base_union is not None
                and len(_base_union[0]) == 1
                and _base_union[1]  # NoneType was present → truly optional
            ):
                # Simple Optional[T]: replace the union with its sole inner type
                # and mark the whole annotation as optional.
                effective_base_type: Any = _base_union[0][0]
                effective_optional: bool = True
            else:
                # Not an Optional inside a wrapper — resolve as-is.
                effective_base_type = base_type
                effective_optional = False

            # Priority order: LiveMeta → LazyMeta → InstanceMeta → InjectMeta.
            # A hint can only carry one _providify marker at a time, but we
            # check in this order so the most specific proxy type wins.
            live_meta = next((a for a in args[1:] if isinstance(a, LiveMeta)), None)
            lazy_meta = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
            instance_meta = next(
                (a for a in args[1:] if isinstance(a, InstanceMeta)), None
            )
            inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

            if live_meta:
                # Return a LiveProxy — re-resolves on every .get() call.
                # Correct for REQUEST/SESSION scoped deps held by longer-lived components.
                # Merge optionality from the type annotation (T | None) with the
                # explicit LiveMeta.optional field so both spell-forms work.
                return LiveProxy(
                    self,
                    effective_base_type,
                    qualifier=live_meta.qualifier,
                    priority=live_meta.priority,
                    optional=effective_optional or live_meta.optional,
                )
            elif lazy_meta:
                # Return a proxy now — actual resolution is deferred to .get() call time.
                # This breaks circular dependency cycles: both constructors return before
                # either dependency is resolved, so the stack never sees a cycle.
                # Merge optionality from the type annotation with LazyMeta.optional.
                return LazyProxy(
                    self,
                    effective_base_type,
                    qualifier=lazy_meta.qualifier,
                    priority=lazy_meta.priority,
                    optional=effective_optional or lazy_meta.optional,
                )
            elif instance_meta:
                # Return an InstanceProxy — gives the owner full control: .get() for
                # a single best-priority instance, .get_all() for all matches,
                # .resolvable() for an optional guard.  No resolution happens here.
                # Qualifier/priority are NOT baked in at construction — the caller
                # passes them at call time on get() / get_all() / resolvable().
                return InstanceProxy(self, effective_base_type)
            elif inject_meta and inject_meta.all:
                inner = (
                    get_args(effective_base_type)[0]
                    if get_origin(effective_base_type) is list
                    else effective_base_type
                )
                return self.get_all(inner, qualifier=inject_meta.qualifier)
            elif inject_meta:
                # Merge optionality: type-annotation form (T | None) OR explicit
                # InjectMeta(optional=True) — both should inject None when absent.
                is_optional = inject_meta.optional or effective_optional
                try:
                    return self.get(
                        effective_base_type,
                        qualifier=inject_meta.qualifier,
                        priority=inject_meta.priority,
                    )
                except LookupError:
                    # optional=True: swallow the error and inject None.
                    # optional=False (default): re-raise so the caller sees the real error.
                    if is_optional:
                        return None
                    raise

            # ── NamedMeta / DelegateMeta / EventMeta (still inside Annotated) ──
            named_meta = next((a for a in args[1:] if isinstance(a, NamedMeta)), None)
            delegate_meta = next(
                (a for a in args[1:] if isinstance(a, DelegateMeta)), None
            )
            event_meta = next((a for a in args[1:] if isinstance(a, EventMeta)), None)

            if named_meta:
                try:
                    return self.get(effective_base_type, qualifier=named_meta.name)
                except LookupError:
                    return _UNRESOLVED
            elif delegate_meta:
                # Resolve the delegate: the inner bean that the @Decorator wraps.
                # Exclude the class currently being constructed to avoid self-injection.
                current_cls = _current_stack()[-1] if _current_stack() else None
                candidates = [
                    b
                    for b in self._filter(effective_base_type)
                    if not (
                        isinstance(b, ClassBinding) and b.implementation is current_cls
                    )
                ]
                if not candidates:
                    return _UNRESOLVED
                best = max(candidates, key=lambda b: b.priority or 0)
                return self._instantiate_sync(best)
            elif event_meta:
                return EventProxy(self, effective_base_type)

        # ── Union / Optional resolution ───────────────────────────
        # Handles: Optional[T], T | None, Union[T1, T2], Union[T1, T2, None]
        # Must come BEFORE the plain-type check below because Union types have
        # get_origin() != None (for typing.Union) or are types.UnionType instances,
        # neither of which is a registered binding — the plain-type branch would
        # call _is_resolvable(Union[T, None]) which always returns False.
        union_result = _unwrap_union(hint)
        if union_result is not None:
            candidates, is_optional = union_result
            # Try each non-None candidate in declaration order; return the first
            # that resolves. This mirrors how Python's runtime picks the first
            # match in isinstance() checks — predictable and declaration-order stable.
            for candidate in candidates:
                try:
                    return self.get(candidate)
                except LookupError:
                    # Binding not found for this candidate; try the next one.
                    continue
            # No candidate resolved. If NoneType was in the union, inject None
            # (same semantics as InjectMeta(optional=True)). Otherwise signal
            # _collect_kwargs that no binding was found — it will raise or fall
            # back to the parameter's default value.
            return None if is_optional else _UNRESOLVED

        elif (
            isinstance(hint, type) or get_origin(hint) is not None
        ) and self._is_resolvable(hint):
            # DESIGN: also accept generic aliases (e.g. Repository[User]) which are
            # not `type` instances but do have a get_origin().  Plain annotations
            # like `repo: Repository[User]` land here when no Inject[] wrapper is used.
            return self.get(hint)

        # ── InjectionPoint — plain type hint, not Annotated ────────────────────
        if hint is InjectionPoint:
            ip = _current_injection_point.get()
            return ip if ip is not None else _UNRESOLVED

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

            # ── Detect Optional[T] / T | None inside the wrapper (async mirror) ──
            # Identical logic to _resolve_hint_sync — see that method for the full
            # design rationale.  Duplicated here rather than extracted to a shared
            # helper so the sync and async paths remain independently readable.
            _base_union = _unwrap_union(base_type)
            if _base_union is not None and len(_base_union[0]) == 1 and _base_union[1]:
                effective_base_type: Any = _base_union[0][0]
                effective_optional: bool = True
            else:
                effective_base_type = base_type
                effective_optional = False

            # Mirror of _resolve_hint_sync — same priority order: Live → Lazy → Instance → Inject.
            live_meta = next((a for a in args[1:] if isinstance(a, LiveMeta)), None)
            lazy_meta = next((a for a in args[1:] if isinstance(a, LazyMeta)), None)
            instance_meta = next(
                (a for a in args[1:] if isinstance(a, InstanceMeta)), None
            )
            inject_meta = next((a for a in args[1:] if isinstance(a, InjectMeta)), None)

            if live_meta:
                # Proxy creation is always sync — the proxy's .aget() method is async.
                return LiveProxy(
                    self,
                    effective_base_type,
                    qualifier=live_meta.qualifier,
                    priority=live_meta.priority,
                    optional=effective_optional or live_meta.optional,
                )
            elif lazy_meta:
                # Proxy creation is always sync — the proxy's .aget() method is async.
                return LazyProxy(
                    self,
                    effective_base_type,
                    qualifier=lazy_meta.qualifier,
                    priority=lazy_meta.priority,
                    optional=effective_optional or lazy_meta.optional,
                )
            elif instance_meta:
                # Proxy creation is always sync — .aget() / .aget_all() are async.
                # No qualifier/priority baked in — caller supplies them at call time.
                return InstanceProxy(self, effective_base_type)
            elif inject_meta and inject_meta.all:
                inner = (
                    get_args(effective_base_type)[0]
                    if get_origin(effective_base_type) is list
                    else effective_base_type
                )
                return await self.aget_all(inner, qualifier=inject_meta.qualifier)
            elif inject_meta:
                is_optional = inject_meta.optional or effective_optional
                try:
                    return await self.aget(
                        effective_base_type,
                        qualifier=inject_meta.qualifier,
                        priority=inject_meta.priority,
                    )
                except LookupError:
                    if is_optional:
                        return None
                    raise

            # ── NamedMeta / DelegateMeta / EventMeta (async mirror) ────────────
            named_meta = next((a for a in args[1:] if isinstance(a, NamedMeta)), None)
            delegate_meta = next(
                (a for a in args[1:] if isinstance(a, DelegateMeta)), None
            )
            event_meta = next((a for a in args[1:] if isinstance(a, EventMeta)), None)

            if named_meta:
                try:
                    return await self.aget(
                        effective_base_type, qualifier=named_meta.name
                    )
                except LookupError:
                    return _UNRESOLVED
            elif delegate_meta:
                current_cls = _current_stack()[-1] if _current_stack() else None
                candidates = [
                    b
                    for b in self._filter(effective_base_type)
                    if not (
                        isinstance(b, ClassBinding) and b.implementation is current_cls
                    )
                ]
                if not candidates:
                    return _UNRESOLVED
                best = max(candidates, key=lambda b: b.priority or 0)
                return await self._instantiate_async(best)
            elif event_meta:
                return EventProxy(self, effective_base_type)

        # ── Union / Optional resolution (async mirror) ───────────────
        # Mirrors _resolve_hint_sync Union branch exactly — see that method
        # for the full design rationale.
        union_result = _unwrap_union(hint)
        if union_result is not None:
            candidates, is_optional = union_result
            for candidate in candidates:
                try:
                    return await self.aget(candidate)
                except LookupError:
                    continue
            return None if is_optional else _UNRESOLVED

        elif (
            isinstance(hint, type) or get_origin(hint) is not None
        ) and self._is_resolvable(hint):
            # Mirror of _resolve_hint_sync — accept generic aliases here too
            return await self.aget(hint)

        # ── InjectionPoint (async mirror) ───────────────────────────────────
        if hint is InjectionPoint:
            ip = _current_injection_point.get()
            return ip if ip is not None else _UNRESOLVED

        return _UNRESOLVED

    # ── Class-variable injection ───────────────────────────────────

    def _inject_class_vars_sync(self, instance: object, cls: type) -> None:
        """Resolve and set class-level annotated attributes on a freshly constructed instance.

        Class-level annotations like ``var: Inject[Something]`` are not part of
        ``__init__`` — they live in ``cls.__annotations__`` and are invisible to
        :meth:`_collect_kwargs_sync`. This method reads the full MRO-resolved hints
        for *cls* via :meth:`_resolve_class_annotations` (Phase 7 — evaluates each
        attribute's annotation in isolation rather than one whole-class
        ``get_type_hints(cls, include_extras=True)`` call), filters to those
        carrying providify metadata (``Inject[T]``, ``Live[T]``, ``Lazy[T]``), and
        sets each resolved value on the instance via ``setattr``.

        Called after ``cls(**kwargs)`` returns but before ``@PostConstruct`` fires,
        so injected class vars are visible to lifecycle hooks.

        Thread safety:  ✅ Safe — operates on a freshly constructed instance not yet
                        shared with other threads or tasks.
        Async safety:   ✅ Safe — no awaits, no shared state.

        Args:
            instance: The freshly constructed instance to inject into.
            cls:      The class whose type hints are inspected. Full MRO traversal
                      via :meth:`_resolve_class_annotations` — includes annotations
                      from parent classes.

        Returns:
            None

        Raises:
            LookupError: If a required class-var annotation (non-optional) refers to
                         a type that has no registered binding.
            AnnotationResolutionError: A class attribute's annotation IS (or
                plausibly is) an injection point but cannot be evaluated —
                see :meth:`_resolve_class_annotations`. Unresolvable
                annotations on attributes that are NOT injection points no
                longer affect construction at all (Phase 7).

        Edge cases:
            - cls has no annotations at all       → no-op (hints is empty)
            - annotation has no providify marker  → silently skipped
            - name also appears in __init__ sig   → skipped; constructor kwargs win
            - unresolvable name, not a marker      → silently skipped, no warning
            - unresolvable name, IS a marker       → raises, naming the attribute
        """
        hints = self._resolve_class_annotations(cls)

        if not hints:
            return

        # Constructor params already injected via _collect_kwargs_sync take priority.
        # Skip matching names to avoid overwriting values set by __init__.
        try:
            init_params = set(inspect.signature(cls.__init__).parameters.keys()) - {
                "self"
            }
        except (ValueError, TypeError):
            # __init__ may not be inspectable (e.g. C-extension types). Safe default.
            init_params = set()

        for name, hint in hints.items():
            if name in init_params:
                # Constructor already handled this — do not overwrite.
                continue
            if not _has_providify_metadata(hint):
                # Plain type annotation or bare ClassVar — not a DI injection target.
                continue
            # ClassVar[Instance[T]] expands to ClassVar[Annotated[T, InstanceMeta()]].
            # _resolve_hint_sync expects the Annotated form as its top-level type,
            # so strip the ClassVar wrapper before resolving.
            resolved = self._resolve_hint_sync(
                _unwrap_classvar(hint), name, cls.__name__
            )
            if resolved is not _UNRESOLVED:
                setattr(instance, name, resolved)

    async def _inject_class_vars_async(self, instance: object, cls: type) -> None:
        """Async mirror of :meth:`_inject_class_vars_sync`.

        Resolves class-level providify-annotated attributes asynchronously.
        ``Live[T]`` and ``Lazy[T]`` proxy objects are still created synchronously
        here — their ``.aget()`` methods are called later by the caller.

        Args:
            instance: The freshly constructed instance to inject into.
            cls:      The class whose type hints are inspected.

        Returns:
            None

        Raises:
            LookupError: If a required class-var annotation refers to an unregistered type.
            AnnotationResolutionError: See :meth:`_inject_class_vars_sync`.

        Edge cases: same as :meth:`_inject_class_vars_sync`.
        """
        hints = self._resolve_class_annotations(cls)

        if not hints:
            return

        try:
            init_params = set(inspect.signature(cls.__init__).parameters.keys()) - {
                "self"
            }
        except (ValueError, TypeError):
            init_params = set()

        for name, hint in hints.items():
            if name in init_params:
                continue
            if not _has_providify_metadata(hint):
                continue
            # Mirror of _inject_class_vars_sync — strip ClassVar[...] wrapper so
            # _resolve_hint_async receives a plain Annotated[T, Meta(...)] type.
            resolved = await self._resolve_hint_async(
                _unwrap_classvar(hint), name, cls.__name__
            )
            if resolved is not _UNRESOLVED:
                setattr(instance, name, resolved)

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
            instance = cls(**resolved_kwargs)
            # Inject class-level annotations (var: Inject[T], var: Live[T], etc.)
            # after construction — these are invisible to _collect_kwargs_sync which
            # only reads __init__ parameters.
            self._inject_class_vars_sync(instance, cls)
            return instance
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
            instance = cls(**resolved_kwargs)
            # Async mirror — same class-var injection after construction.
            await self._inject_class_vars_async(instance, cls)
            return instance
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

        Returns ``None`` (and logs a warning) if the hint cannot be resolved
        — e.g. when a forward reference is unresolvable at runtime. Tier 3 —
        advisory/reporting: a missing return type is a worse debugging
        experience, not a wiring bug, so the failure is swallowed but never
        silent.

        Phase 7: resolved via :func:`providify._annotations._eval_annotation`
        directly (not the whole-signature ``get_type_hints(fn)``) — an
        unresolvable PARAMETER annotation must never block resolving the
        return annotation, and vice versa; they are now fully independent
        (see :meth:`_collect_kwargs_sync` / :meth:`_resolve_params` for the
        parameter side, which no longer shares a single failure point with
        this method the way both did under the old whole-signature call).

        Args:
            fn: The provider callable to inspect.

        Returns:
            The return type annotation if present and resolvable, else ``None``.
        """
        sig = inspect.signature(inspect.unwrap(fn))
        raw_return = sig.return_annotation
        if raw_return is inspect.Signature.empty:
            return None
        globalns, localns = _annotation_namespaces(fn, self._build_localns())
        try:
            return _eval_annotation(raw_return, globalns, localns)
        except Exception as exc:
            logger.warning(
                "Cannot resolve return type hint for '%s' (%s: %s).",
                getattr(fn, "__qualname__", fn),
                type(exc).__name__,
                exc,
            )
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

    @contextmanager
    def _emit_scope_events(
        self, kind: Literal["request", "session"], inner: Any
    ) -> Iterator[Any]:
        """Wrap a sync ``ScopeContext`` context manager with ``ScopeEntered``/``ScopeExited`` emission.

        Only constructed by the façade methods below, and only when
        ``self._hooks`` is non-empty — see each façade's "zero-cost" branch.
        Yields the SAME scope id the wrapped context manager yields, so the
        wrapper is transparent to callers that only use the ``with ... as
        scope_id:`` protocol.

        Args:
            kind: ``"request"`` or ``"session"`` — which façade this wraps.
            inner: The raw ``ScopeContext`` context manager to wrap
                (``self.scope_context.request()`` / ``.session(...)``).

        Yields:
            The scope id string yielded by *inner*.

        Thread safety / Async safety: same as the wrapped ``ScopeContext``
            context manager — this adds no new shared state.
        """
        with inner as scope_id:
            self._emit(ScopeEntered(kind=kind, scope_id=scope_id))
            started = perf_counter_ns()
            try:
                yield scope_id
            finally:
                self._emit(
                    ScopeExited(
                        kind=kind,
                        scope_id=scope_id,
                        duration_ns=perf_counter_ns() - started,
                    )
                )

    @asynccontextmanager
    async def _aemit_scope_events(
        self, kind: Literal["request", "session"], inner: Any
    ) -> Any:
        """Async mirror of :meth:`_emit_scope_events` — wraps an async ``ScopeContext`` CM.

        Args:
            kind: ``"request"`` or ``"session"``.
            inner: The raw async ``ScopeContext`` context manager to wrap.

        Yields:
            The scope id string yielded by *inner*.
        """
        async with inner as scope_id:
            self._emit(ScopeEntered(kind=kind, scope_id=scope_id))
            started = perf_counter_ns()
            try:
                yield scope_id
            finally:
                self._emit(
                    ScopeExited(
                        kind=kind,
                        scope_id=scope_id,
                        duration_ns=perf_counter_ns() - started,
                    )
                )

    def request(self) -> Any:
        """Activate a sync request scope context.

        Shorthand for ``container.scope_context.request()``.
        All @RequestScoped components resolved inside this block share one
        instance; a fresh instance is created for each new block.

        With no hooks registered, returns the raw ``ScopeContext`` context
        manager UNCHANGED (plan 009 §Design — zero-cost rule extends to
        scope façades: no wrapper object is built when nobody is listening).
        Calling ``container.scope_context.request()`` directly always
        bypasses ``ScopeEntered``/``ScopeExited`` instrumentation, even when
        hooks are registered — see plan 009 §Design.

        Returns:
            A sync context manager that yields the request ID string.

        Example:
            with container.request():
                svc = container.get(MyRequestScopedService)
        """
        if not self._hooks:
            return self.scope_context.request()
        return self._emit_scope_events("request", self.scope_context.request())

    def arequest(self) -> Any:
        """Activate an async request scope context.

        Shorthand for ``container.scope_context.arequest()``. See
        :meth:`request`'s docstring for the zero-cost / direct-access caveats.

        Returns:
            An async context manager that yields the request ID string.

        Example:
            async with container.arequest():
                svc = await container.aget(MyRequestScopedService)
        """
        if not self._hooks:
            return self.scope_context.arequest()
        return self._aemit_scope_events("request", self.scope_context.arequest())

    def session(self, session_id: str | None = None) -> Any:
        """Activate a sync session scope context.

        Shorthand for ``container.scope_context.session(session_id)``.
        Reuses an existing session cache when the same session_id is
        provided, creating a new one on first use. See :meth:`request`'s
        docstring for the zero-cost / direct-access caveats.

        Args:
            session_id: Explicit session identifier (e.g. a user ID or
                        cookie value). A random UUID is used when omitted.

        Returns:
            A sync context manager that yields the session ID string.

        Example:
            with container.session("user-abc"):
                profile = container.get(UserProfile)
        """
        if not self._hooks:
            return self.scope_context.session(session_id)
        return self._emit_scope_events(
            "session", self.scope_context.session(session_id)
        )

    def asession(self, session_id: str | None = None) -> Any:
        """Activate an async session scope context.

        Shorthand for ``container.scope_context.asession(session_id)``. See
        :meth:`request`'s docstring for the zero-cost / direct-access caveats.

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
        if not self._hooks:
            return self.scope_context.asession(session_id)
        return self._aemit_scope_events(
            "session", self.scope_context.asession(session_id)
        )

    def invalidate_session(self, session_id: str) -> None:
        """Destroy a session cache and run sync @PreDestroy hooks — call on logout or expiry.

        Runs @PreDestroy for all session-scoped instances in the cache before
        discarding it.  Async @PreDestroy hooks are skipped with a warning;
        use :meth:`ainvalidate_session` from an async context to handle them.

        Shorthand for ``container.scope_context.invalidate_session(session_id)``.

        Args:
            session_id: The session ID to invalidate.  No-op if unknown.

        Returns:
            None

        Edge cases:
            - Unknown session_id → no-op, no error raised.
            - Async @PreDestroy hooks on session-scoped instances are skipped;
              call ainvalidate_session() from an async context instead.
        """
        self.scope_context.invalidate_session(session_id)

    async def ainvalidate_session(self, session_id: str) -> None:
        """Destroy a session cache and run both sync and async @PreDestroy hooks.

        Async mirror of :meth:`invalidate_session` — awaits async @PreDestroy
        hooks in addition to calling sync ones, before discarding the cache.

        Args:
            session_id: The session ID to invalidate.  No-op if unknown.

        Returns:
            None

        Edge cases:
            - Unknown session_id → no-op, no error raised.

        Async safety:  ✅ Safe — delegates to ScopeContext.ainvalidate_session()
                       which protects dict mutations with a threading.Lock (not
                       held across await points).
        """
        await self.scope_context.ainvalidate_session(session_id)

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

    def _teardown_plan(self) -> list[tuple[Any, AnyBinding]]:
        """Build the ordered list of ``(key, binding)`` pairs to tear down.

        Walks ``self._singleton_order`` in reverse — the append order IS the
        creation order (see the DESIGN comment on ``_singleton_order`` in
        ``__init__``), and every dependency is cached before its dependent,
        so reversing it yields reverse-dependency order: dependents are torn
        down before the dependencies they hold a reference to.

        Args:
            None.

        Returns:
            ``[(key, binding), ...]`` in teardown order. Never contains a key
            more than once, and never contains a key that is no longer in
            ``_singleton_cache`` (evicted by :meth:`override` /
            :meth:`reset_binding` since it was created).

        Edge cases:
            - Never-instantiated singleton → absent from ``_singleton_order``,
              never appears in the plan (nothing to tear down).
            - Same key recorded twice (should not happen — see
              :meth:`_record_singleton_creation`'s docstring for why the
              per-key lock makes this impossible in practice) → the second
              occurrence is dropped by the ``seen`` dedup guard, defensively.
            - Key evicted by ``override()``/``reset_binding()`` after being
              recorded → dropped (no longer in ``_singleton_cache``), so an
              instance the container no longer owns is never torn down.
            - A future code path seeds ``_singleton_cache`` without going
              through ``_instantiate_sync``/``_instantiate_async`` (today
              nothing does — see plan 004 Risks) → that key would be absent
              from ``_singleton_order`` entirely. The **fallback tail** below
              defends against this degrading to "never torn down": any key
              still in ``_singleton_cache`` but unseen by the main walk is
              appended at the end (reverse ``_bindings`` order), so it is
              torn down last rather than silently skipped.

        Thread safety: ⚠️ Reads ``_singleton_order``/``_singleton_cache``/
            ``_bindings`` without a lock. Shutdown is expected to run once,
            after concurrent creation has quiesced (mirrors the pre-existing
            contract of ``shutdown()``/``ashutdown()``, which never held a
            lock across the whole teardown loop either).
        """
        plan: list[tuple[Any, AnyBinding]] = []
        seen: set[Any] = set()
        # Reverse creation order == reverse-dependency order (deps cached
        # before their dependent — see _singleton_order's DESIGN comment).
        for key, binding in reversed(self._singleton_order):
            if key in seen:
                continue  # defensive dedup — see docstring Edge cases
            if key not in self._singleton_cache:
                continue  # evicted by override()/reset_binding() — not ours anymore
            seen.add(key)
            plan.append((key, binding))

        # ── Fallback tail: cached-but-unrecorded keys ─────────────────────
        # Defensive only — nothing in today's codebase seeds _singleton_cache
        # outside _instantiate_sync/_instantiate_async (verified: set_scoped
        # only writes request/session caches). Kept so a future seeding path
        # degrades to "torn down last" instead of "never torn down".
        if len(seen) < len(self._singleton_cache):
            key_to_binding = {self._get_cache_key(b): b for b in self._bindings}
            for key in list(reversed(list(self._singleton_cache.keys()))):
                if key in seen:
                    continue
                binding = key_to_binding.get(key)
                if binding is None:
                    continue  # cached instance with no matching binding — nothing to call
                seen.add(key)
                plan.append((key, binding))

        return plan

    # Sentinel exception used to let the async-@PreDestroy RuntimeError escape
    # _dispose_sync's generic-failure aggregation instead of being captured as
    # a ShutdownFailure — it is a programmer error (wrong shutdown() variant
    # called), not a teardown failure, so it must still crash loudly.
    class _AsyncHookInSyncShutdown(RuntimeError):
        """Internal marker: an async @PreDestroy hook was reached from shutdown()."""

    @staticmethod
    def _has_teardown_hook(binding: AnyBinding) -> bool:
        """Return ``True`` if *binding* has a teardown hook that would actually run.

        Guards the ``InstanceDisposed`` emission sites (`shutdown`,
        `ashutdown`, `_run_pre_destroy_for_scope`,
        `_arun_pre_destroy_for_scope`): a binding with no ``@PreDestroy``/
        ``@Disposes`` hook has nothing torn down, so no event is emitted
        (plan 009 §Edge cases — "Binding with no @PreDestroy/@Disposes hook
        emits no InstanceDisposed").

        Args:
            binding: The binding to inspect.

        Returns:
            ``True`` for a ``ClassBinding`` with a ``pre_destroy`` marker or
            a ``ProviderBinding`` with a ``disposer``; ``False`` otherwise.
        """
        if isinstance(binding, ProviderBinding):
            return binding.disposer is not None
        return isinstance(binding, ClassBinding) and binding.pre_destroy is not None

    def _dispose_sync(self, key: Any, binding: AnyBinding) -> None:
        """Run the sync disposer / ``@PreDestroy`` hook for one teardown entry.

        Args:
            key:     The singleton cache key (from :meth:`_get_cache_key`).
            binding: The binding whose instance is being torn down.

        Returns:
            None.

        Raises:
            _AsyncHookInSyncShutdown: The binding's ``@PreDestroy`` hook is
                ``async def`` — sync ``shutdown()`` cannot await it. Caught
                by :meth:`shutdown` and re-raised as a plain ``RuntimeError``
                with the original message, deliberately NOT aggregated into
                ``ShutdownError`` (see class docstring: it is a programmer
                error, not a teardown failure).
            Exception: Whatever the disposer / hook itself raises — caught
                by the caller (:meth:`shutdown`) and aggregated.

        Edge cases:
            - Instance already evicted from cache → caller (`shutdown`) never
              reaches here for that key — filtered by :meth:`_teardown_plan`.
        """
        if isinstance(binding, ProviderBinding):
            if binding.disposer is not None:
                instance = self._singleton_cache[key]
                binding.disposer(instance)
            return
        if not isinstance(binding, ClassBinding) or binding.pre_destroy is None:
            return
        instance = self._singleton_cache[key]
        if binding.pre_destroy.is_async:
            raise self._AsyncHookInSyncShutdown(
                f"@PreDestroy method '{binding.pre_destroy.fn_name}' on "
                f"'{binding.implementation.__name__}' is async — "
                f"use await container.ashutdown() instead."
            )
        getattr(instance, binding.pre_destroy.fn_name)()

    def _owner_label(self, binding: AnyBinding) -> str:
        """Return the human-readable ``ShutdownFailure.owner`` label for *binding*.

        Args:
            binding: The binding whose teardown hook/disposer just failed.

        Returns:
            ``"ClassName.hook_name"`` for a ``ClassBinding``'s ``@PreDestroy``,
            or ``"@Disposes(fn_name)"`` for a ``ProviderBinding``'s disposer.
        """
        if isinstance(binding, ProviderBinding):
            disposer_name = binding.disposer.__name__ if binding.disposer else "?"
            return f"@Disposes({disposer_name})"
        hook_name = binding.pre_destroy.fn_name if binding.pre_destroy else "?"
        return f"{binding.implementation.__name__}.{hook_name}"

    def _module_pre_destroy_owner_label(self, cls: type, hook: LifecycleMarker) -> str:
        """Return the ``ShutdownFailure.owner`` label for a module's ``@PreDestroy``.

        Args:
            cls:  The ``@Configuration`` module class whose hook failed.
            hook: The ``@PreDestroy`` marker found on *cls*.

        Returns:
            ``"ModuleClassName.hook_name"`` — same shape as
            :meth:`_owner_label`'s ``ClassName.hook_name`` for a singleton's
            ``@PreDestroy``, so failures read consistently regardless of
            which teardown phase produced them.
        """
        return f"{cls.__name__}.{hook.fn_name}"

    def shutdown(self) -> None:
        """Sync shutdown — tear down all cached singletons, then all installed
        ``@Configuration`` modules, in that order.

        Calls ``@PreDestroy`` hooks and ``@Disposes`` disposers on every
        cached singleton, walking :meth:`_teardown_plan` — dependents are
        torn down before the dependencies they may still reference, matching
        the convention Spring/.NET/Quarkus establish (see
        ``design/di-features-taxonomy/research/003-production-readiness-conventions.md``
        §3) and closing the gap flagged there for the Python DI ecosystem.

        Every hook runs even if an earlier one raises: failures are captured
        as ``ShutdownFailure`` entries and aggregated into one ``ShutdownError``
        at the end, rather than stopping at the first failure. Caches are
        always cleared, even when hooks fail — see ``finally`` below.

        Ordering: reverse **creation** order, which is a valid reverse
        topological order of the singleton dependency graph *as actually
        constructed* (including runtime-only edges like ``Lazy[T]``/
        ``Live[T]``/``Provider[T]``). ⚠️ Limitation: a singleton resolved
        late via ``Lazy[T]``/``Provider[T]`` — after some other singleton
        that will go on to reference it — is created (and therefore torn
        down) out of "true" dependency order; this is inherent to
        reverse-creation-order tracking (the same limitation .NET's
        ``IServiceProvider`` disposal has).

        Ordering (Plan 008/F5): singletons first (reverse creation order via
        :meth:`_teardown_plan`), THEN ``@Configuration`` modules (reverse
        install order, via ``reversed(self._installed_modules.items())``).
        Invariant: *nothing the container owns is alive when a module's
        teardown runs* — a module's ``@PreDestroy`` typically releases a
        resource (a pool, a client) that every singleton consumer of it has
        already been torn down by the time phase 2 starts. Modules whose
        record is ``owned=False`` (inherited by :meth:`copy`) or already
        ``disposed=True`` (idempotency — a second ``shutdown()`` call) are
        skipped.

        Raises:
            ShutdownError: One or more hooks/disposers raised (singleton
                phase) OR one or more module ``@PreDestroy`` hooks raised
                (module phase) — both phases feed the SAME failures list, so
                one ``ShutdownError`` aggregates both. ``exc.failures`` holds
                every captured :class:`~providify.exceptions.ShutdownFailure`
                (not just the first, and in teardown order); ``exc.__cause__``
                is the exception belonging to the earliest-*created* failing
                component — see the DESIGN comment above ``raise ShutdownError``
                below for why that (not the first one encountered during the
                reversed teardown walk) is the more useful root cause to chain.
            RuntimeError: A ``@PreDestroy`` hook (singleton OR module) is
                ``async def`` — use ``await container.ashutdown()`` instead.
                Raised immediately, NOT aggregated into ``ShutdownError``: it
                signals the wrong shutdown method was called, not a teardown
                failure. A module async-hook hit during the module phase
                still runs every already-visited singleton's hook (phase 1
                already completed) but stops the module phase at that point,
                same "stop the loop, escape un-aggregated" contract as the
                singleton phase.
        """
        failures: list[ShutdownFailure] = []
        async_hook_error: RuntimeError | None = None
        try:
            for key, binding in self._teardown_plan():
                has_hook = self._has_teardown_hook(binding)
                started = perf_counter_ns() if (self._hooks and has_hook) else 0
                try:
                    self._dispose_sync(key, binding)
                except self._AsyncHookInSyncShutdown as exc:
                    # Programmer error, not a teardown failure — stop the loop
                    # and let it escape un-aggregated (see docstring Raises).
                    # Nothing was disposed — no InstanceDisposed for this key.
                    async_hook_error = RuntimeError(str(exc))
                    break
                except (
                    Exception
                ) as exc:  # noqa: BLE001 — deliberately broad: aggregate ALL hook failures
                    failures.append(
                        ShutdownFailure(owner=self._owner_label(binding), exception=exc)
                    )
                    if self._hooks and has_hook:
                        self._emit(
                            InstanceDisposed(
                                interface=binding.interface,
                                implementation=self._instance_created_implementation(
                                    binding
                                ),
                                scope=binding.scope,
                                owner=self._owner_label(binding),
                                duration_ns=perf_counter_ns() - started,
                                error=exc,
                            )
                        )
                    continue
                if self._hooks and has_hook:
                    self._emit(
                        InstanceDisposed(
                            interface=binding.interface,
                            implementation=self._instance_created_implementation(
                                binding
                            ),
                            scope=binding.scope,
                            owner=self._owner_label(binding),
                            duration_ns=perf_counter_ns() - started,
                            error=None,
                        )
                    )

            # ── Phase 2 (Plan 008/F5): @Configuration module teardown ──────
            # Only runs if phase 1 did not already hit an async-hook bail-out
            # — an async hook found in phase 1 is a programmer error that
            # should surface immediately rather than let phase 2 run first.
            if async_hook_error is None:
                for cls, rec in reversed(list(self._installed_modules.items())):
                    if not rec.owned or rec.disposed:
                        continue  # copy()'d (not owned) or already torn down
                    hook = _find_pre_destroy(cls)
                    # Mark disposed BEFORE running the hook (not after) so a
                    # hook that raises still counts as "attempted" — a second
                    # shutdown() call must not retry a hook that already ran
                    # and already failed once (idempotency contract).
                    self._installed_modules[cls] = replace(rec, disposed=True)
                    if hook is None:
                        continue  # module with no @PreDestroy — skip, no error
                    if hook.is_async:
                        async_hook_error = RuntimeError(
                            f"@PreDestroy method '{hook.fn_name}' on module "
                            f"'{cls.__name__}' is async — "
                            f"use await container.ashutdown() instead."
                        )
                        break
                    try:
                        getattr(rec.instance, hook.fn_name)()
                    except (
                        Exception
                    ) as exc:  # noqa: BLE001 — aggregate ALL module hook failures too
                        failures.append(
                            ShutdownFailure(
                                owner=self._module_pre_destroy_owner_label(cls, hook),
                                exception=exc,
                            )
                        )
        finally:
            # Caches clear unconditionally — even on the async-hook bail-out
            # or when every remaining hook already ran — closing the leak the
            # old raise-on-first shutdown() had (see plan 004 Design).
            # Deliberately does NOT touch _installed_modules — see that
            # dict's DESIGN comment in __init__ for why (re-install after
            # shutdown must stay a no-op, not a re-registration).
            self._clear_caches()

        if async_hook_error is not None:
            raise async_hook_error
        if failures:
            # DESIGN: chain __cause__ to failures[-1], not failures[0].
            # `failures` is appended in *teardown* order (reverse-dependency,
            # i.e. dependents before dependencies) — so failures[-1] is the
            # failure belonging to the EARLIEST-created (most foundational)
            # component. That is deliberately the more useful root cause to
            # surface: a foundational dependency failing to tear down cleanly
            # (e.g. a DB connection that will not close) is often the actual
            # root of trouble, while dependents failing afterwards can be a
            # downstream symptom of the same resource still being unavailable.
            raise ShutdownError(failures) from failures[-1].exception

    async def _adispose(self, key: Any, binding: AnyBinding) -> None:
        """Run the disposer / ``@PreDestroy`` hook for one teardown entry (async).

        Async mirror of :meth:`_dispose_sync`: awaits async disposers/hooks,
        calls sync ones inline (no await needed).

        Args:
            key:     The singleton cache key (from :meth:`_get_cache_key`).
            binding: The binding whose instance is being torn down.

        Returns:
            None.

        Raises:
            Exception: Whatever the disposer / hook itself raises — caught by
                the caller (:meth:`ashutdown`) and aggregated.
        """
        if isinstance(binding, ProviderBinding):
            if binding.disposer is not None:
                instance = self._singleton_cache[key]
                if inspect.iscoroutinefunction(binding.disposer):
                    await binding.disposer(instance)
                else:
                    binding.disposer(instance)
            return
        if not isinstance(binding, ClassBinding) or binding.pre_destroy is None:
            return
        instance = self._singleton_cache[key]
        bound = getattr(instance, binding.pre_destroy.fn_name)
        if binding.pre_destroy.is_async:
            await bound()
        else:
            bound()

    async def ashutdown(self) -> None:
        """Async shutdown — tear down all cached singletons, then all
        installed ``@Configuration`` modules, in that order.

        Async mirror of :meth:`shutdown`: awaits async ``@PreDestroy`` hooks
        and async ``@Disposes`` disposers, calls sync ones inline. Same
        reverse-dependency-order teardown (:meth:`_teardown_plan`), same
        failure aggregation into ``ShutdownError``, same "caches always
        clear" guarantee.

        Ordering (Plan 008/F5): singletons first (reverse creation order),
        THEN ``@Configuration`` modules (reverse install order via
        ``reversed(self._installed_modules.items())``) — same invariant as
        :meth:`shutdown`: nothing the container owns is alive when a
        module's teardown runs. Modules with ``owned=False`` (copies) or
        already ``disposed=True`` (idempotency) are skipped. Unlike the sync
        path, an ``async def`` module ``@PreDestroy`` hook is simply
        awaited — there is no "wrong shutdown() variant" error here since
        this IS the async variant.

        Raises:
            ShutdownError: One or more hooks/disposers raised (singleton OR
                module phase — both feed the same failures list).
                ``exc.failures`` holds every captured
                :class:`~providify.exceptions.ShutdownFailure`;
                ``exc.__cause__`` is the earliest-created failing component's
                exception (see :meth:`shutdown`'s matching DESIGN comment).

        Example:
            await container.ashutdown()
        """
        failures: list[ShutdownFailure] = []
        try:
            for key, binding in self._teardown_plan():
                has_hook = self._has_teardown_hook(binding)
                started = perf_counter_ns() if (self._hooks and has_hook) else 0
                try:
                    await self._adispose(key, binding)
                except (
                    Exception
                ) as exc:  # noqa: BLE001 — deliberately broad: aggregate ALL hook failures
                    failures.append(
                        ShutdownFailure(owner=self._owner_label(binding), exception=exc)
                    )
                    if self._hooks and has_hook:
                        self._emit(
                            InstanceDisposed(
                                interface=binding.interface,
                                implementation=self._instance_created_implementation(
                                    binding
                                ),
                                scope=binding.scope,
                                owner=self._owner_label(binding),
                                duration_ns=perf_counter_ns() - started,
                                error=exc,
                            )
                        )
                    continue
                if self._hooks and has_hook:
                    self._emit(
                        InstanceDisposed(
                            interface=binding.interface,
                            implementation=self._instance_created_implementation(
                                binding
                            ),
                            scope=binding.scope,
                            owner=self._owner_label(binding),
                            duration_ns=perf_counter_ns() - started,
                            error=None,
                        )
                    )

            # ── Phase 2 (Plan 008/F5): @Configuration module teardown ──────
            for cls, rec in reversed(list(self._installed_modules.items())):
                if not rec.owned or rec.disposed:
                    continue  # copy()'d (not owned) or already torn down
                hook = _find_pre_destroy(cls)
                # Mark disposed BEFORE running the hook — see shutdown()'s
                # matching comment for the idempotency rationale.
                self._installed_modules[cls] = replace(rec, disposed=True)
                if hook is None:
                    continue  # module with no @PreDestroy — skip, no error
                bound = getattr(rec.instance, hook.fn_name)
                try:
                    if hook.is_async:
                        await bound()
                    else:
                        bound()
                except (
                    Exception
                ) as exc:  # noqa: BLE001 — aggregate ALL module hook failures too
                    failures.append(
                        ShutdownFailure(
                            owner=self._module_pre_destroy_owner_label(cls, hook),
                            exception=exc,
                        )
                    )
        finally:
            # Caches clear unconditionally — see shutdown()'s docstring.
            # Deliberately does NOT touch _installed_modules — see that
            # dict's DESIGN comment in __init__.
            self._clear_caches()

        if failures:
            # failures[-1] = earliest-created component — see shutdown()'s
            # matching DESIGN comment for the rationale.
            raise ShutdownError(failures) from failures[-1].exception

    def _clear_caches(self) -> None:
        """Clear all instance caches — called at the end of shutdown."""
        self._singleton_cache.clear()
        # Cleared alongside the cache it indexes so a second shutdown() call
        # sees an empty plan (idempotent no-op) rather than re-running hooks
        # for instances that no longer exist.
        self._singleton_order.clear()
        self.scope_context.clear_caches()

    # ── Scoped @PreDestroy callbacks ──────────────────────────────

    def _index_class_bindings_by_implementation(self) -> dict[Any, ClassBinding]:
        """Build a ``{implementation: binding}`` index over ``_bindings``.

        Shared by :meth:`_run_pre_destroy_for_scope` and
        :meth:`_arun_pre_destroy_for_scope` so both build the lookup once per
        scope-exit call instead of re-scanning ``_bindings`` per cached key
        (the old per-key linear scan was O(bindings × cached keys); this is
        O(bindings) once, then O(1) per cached key).

        Returns:
            ``{binding.implementation: binding}`` for every
            :class:`~providify.binding.ClassBinding` in ``_bindings``.

        Edge cases:
            - Two interfaces bound to the same implementation class (aliases)
              → both share one ``ClassBinding`` per distinct binding object;
              "first binding wins" if ``_bindings`` somehow contained more
              than one entry for the same implementation — they share the
              same ``pre_destroy`` hook by construction (decorator metadata
              lives on the class, not the binding), so which one wins is
              immaterial.
        """
        index: dict[Any, ClassBinding] = {}
        for binding in self._bindings:
            if not isinstance(binding, ClassBinding):
                continue
            # First binding wins — see docstring Edge cases.
            index.setdefault(binding.implementation, binding)
        return index

    def _run_pre_destroy_for_scope(self, cache: dict[Any, object]) -> None:
        """Run sync @PreDestroy hooks for all cached instances in *cache*, in
        reverse-dependency order.

        Called by :class:`~providify.scope.ScopeContext` just before a
        request or session scope cache is popped. Builds a
        ``{implementation: binding}`` index once, then walks
        ``reversed(list(cache.items()))`` and calls each binding's
        ``pre_destroy`` hook if present.

        Ordering: scope caches are insertion-ordered dicts populated by the
        same "dependency before dependent" rule as the singleton cache (see
        ``_instantiate_sync``, container.py ~line 1628-1629: a binding's
        dependencies resolve inside ``binding.create(self)`` and are cached
        before the binding itself is). Reversing the cache's insertion order
        therefore yields reverse-dependency order — dependents torn down
        before the dependencies they may still reference — with **zero new
        state**: no separate order log is needed here (unlike
        ``_singleton_order`` for the singleton scope) because request/session
        caches are short-lived, single-owner dicts, not shared across a
        creation/teardown lifetime spanning many scope frames.

        Async ``@PreDestroy`` hooks are skipped with a ``warnings.warn`` rather
        than raising — the sync context manager cannot await them.  Use the
        async context managers (``arequest()`` / ``asession()``) when async
        teardown is needed.

        Args:
            cache: The scope cache dict that is about to be discarded.
                   Keys are implementation classes (same as
                   :meth:`_get_cache_key` produces for ClassBinding).

        Returns:
            None

        Edge cases:
            - Empty cache                    → no-op.
            - Instance has no @PreDestroy    → silently skipped.
            - Async @PreDestroy encountered  → warning emitted, hook skipped.
            - Exception from a hook          → propagates after the cache is
                                               discarded (handled by finally).

        Thread safety:  ✅ Reads ``_bindings`` (no mutations during teardown).
        Async safety:   ✅ No await points — safe to call from sync context.

        Example:
            # Called automatically by ScopeContext — do not call directly.
        """
        index = self._index_class_bindings_by_implementation()
        # reversed(): dependents were inserted after their dependencies (see
        # docstring Ordering), so reversing the insertion order tears down
        # dependents first — matching the singleton-scope guarantee.
        for key, instance in reversed(list(cache.items())):
            binding = index.get(key)
            if binding is None or binding.pre_destroy is None:
                continue
            if binding.pre_destroy.is_async:
                # DESIGN: sync path cannot await — warn and skip rather than
                # crash.  The async path (_arun_pre_destroy_for_scope) handles
                # async hooks correctly; use arequest()/asession() when needed.
                # Nothing was disposed — no InstanceDisposed for this key.
                warnings.warn(
                    f"@PreDestroy method '{binding.pre_destroy.fn_name}' on "
                    f"'{binding.implementation.__name__}' is async — it will "
                    f"NOT be called in the sync request/session context. "
                    f"Use 'async with container.arequest():' or "
                    f"'async with container.asession():' instead.",
                    stacklevel=3,
                )
                continue
            started = perf_counter_ns() if self._hooks else 0
            getattr(instance, binding.pre_destroy.fn_name)()
            if self._hooks:
                self._emit(
                    InstanceDisposed(
                        interface=binding.interface,
                        implementation=binding.implementation,
                        scope=binding.scope,
                        owner=self._owner_label(binding),
                        duration_ns=perf_counter_ns() - started,
                        error=None,
                    )
                )

    async def _arun_pre_destroy_for_scope(self, cache: dict[Any, object]) -> None:
        """Run all @PreDestroy hooks (sync + async) for *cache* instances, in
        reverse-dependency order.

        Async mirror of :meth:`_run_pre_destroy_for_scope`.  Called by
        :class:`~providify.scope.ScopeContext` before an async request or
        session scope cache is popped.  Unlike the sync version, this method
        awaits async ``@PreDestroy`` hooks and calls sync ones normally.

        Ordering: same reverse-insertion-order guarantee as
        :meth:`_run_pre_destroy_for_scope` — see its docstring's Ordering
        paragraph.

        Args:
            cache: The scope cache dict that is about to be discarded.

        Returns:
            None

        Edge cases:
            - Empty cache                    → no-op.
            - Instance has no @PreDestroy    → silently skipped.
            - Sync @PreDestroy               → called normally (no await).
            - Async @PreDestroy              → awaited ✅.
            - Exception from a hook          → propagates.

        Async safety:   ✅ Awaits async hooks; sync hooks called inline.
        Thread safety:  ✅ Reads ``_bindings`` only; no mutations during teardown.

        Example:
            # Called automatically by ScopeContext — do not call directly.
        """
        index = self._index_class_bindings_by_implementation()
        for key, instance in reversed(list(cache.items())):
            binding = index.get(key)
            if binding is None or binding.pre_destroy is None:
                continue
            bound = getattr(instance, binding.pre_destroy.fn_name)
            started = perf_counter_ns() if self._hooks else 0
            if binding.pre_destroy.is_async:
                await bound()
            else:
                bound()
            if self._hooks:
                self._emit(
                    InstanceDisposed(
                        interface=binding.interface,
                        implementation=binding.implementation,
                        scope=binding.scope,
                        owner=self._owner_label(binding),
                        duration_ns=perf_counter_ns() - started,
                        error=None,
                    )
                )

    # ── Scope-leak validation ─────────────────────────────────────

    def _collect_class_var_hints(self, cls: type) -> dict[str, Any]:
        """Return class-level type hints that carry providify metadata, excluding ``__init__`` params.

        Shared by :meth:`_check_scope_violation` and :meth:`_get_dependencies` so both
        scope-validation and dependency-graph construction see the same set of
        class-level injection points.

        Args:
            cls: The class whose annotations are inspected via full MRO traversal
                 (``get_type_hints`` walks parent classes too).

        Returns:
            ``dict[attr_name → hint]`` — only entries that carry providify metadata
            (``Inject[T]``, ``Live[T]``, ``Lazy[T]``) and are NOT ``__init__``
            parameters.

        Raises:
            AnnotationResolutionError: A class attribute IS (or plausibly is)
                an injection point but its annotation cannot be evaluated —
                see :meth:`_resolve_class_annotations`, which is strict by
                construction (Phase 7: an attribute that resolves to
                "unknown" ambiguity is skipped, never silently treated as
                "no injection points"). Callers on the reporting tier
                (:meth:`_get_dependencies`) catch this and downgrade it to a
                warning; validators let it propagate.

        Edge cases:
            - cls has no annotations              → ``{}``
            - an injection-point attribute's annotation is unresolvable
              → ``AnnotationResolutionError``
            - name is an ``__init__`` param       → excluded (already handled by
                                                    the ``__init__``-based callers)
            - name has no providify metadata      → excluded
        """
        # include_extras=True is implicit in _resolve_class_annotations —
        # without it Annotated[T, InjectMeta(...)] would be stripped to bare
        # T and the metadata marker lost.
        hints = self._resolve_class_annotations(cls)

        # Exclude __init__ params — they're already validated / graphed via the
        # existing __init__-based path.  Keeping them here would double-count them.
        try:
            init_params = set(inspect.signature(cls.__init__).parameters.keys()) - {
                "self"
            }
        except (ValueError, TypeError):
            # __init__ not inspectable (rare — C-extension types). Safe empty set.
            init_params = set()

        # _unwrap_classvar strips ClassVar[Annotated[T, Meta()]] → Annotated[T, Meta()].
        # All downstream callers (_check_scope_violation, _get_dependencies) inspect
        # hints with `get_origin(hint) is Annotated` — they'd silently skip ClassVar
        # wrappers without this normalisation step.
        return {
            name: _unwrap_classvar(hint)
            for name, hint in hints.items()
            if name not in init_params and _has_providify_metadata(hint)
        }

    def _check_scope_violation(
        self,
        binding: ClassBinding,
        qualifier: str | type | None = None,
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

        Raises:
            LiveInjectionRequiredError: If any ``REQUEST``/``SESSION`` scoped
                dependency is injected without ``Live[T]``/``Instance[T]``.
            AnnotationResolutionError: An ``__init__`` parameter (or class
                var) IS (or plausibly is) an injection point but its
                annotation cannot be evaluated — see :meth:`_resolve_params`
                / :meth:`_collect_class_var_hints`. A validator that cannot
                read the annotations must not report "no leaks found"; it
                raises instead so the container never reports a clean bill
                of health it cannot prove. Phase 7 narrows this further: an
                unresolvable annotation on a parameter that is NOT an
                injection point no longer raises at all — nothing to
                validate for it.
        """
        leaks: list[ScopeLeak] = []
        # Accumulated Live[T] violations — raised as a group so the developer
        # sees all affected parameters at once, not just the first one.
        live_violations: list[LiveInjectionViolation] = []
        # Per-parameter resolution (Phase 7) preserves Annotated wrappers
        # automatically (include_extras=True is baked into _eval_annotation)
        # and applies the container's localns per parameter — the whole-
        # signature `localns` gap this method used to patch manually
        # (`_resolve_hints_or_raise`) no longer exists as a distinct fix;
        # every parameter gets it via `_resolve_params` -> `_build_localns()`.
        init_hints = self._resolve_params(
            binding.implementation.__init__,
            f"{binding.implementation.__name__}.__init__",
            owner=binding.implementation,
        )

        # DESIGN: merge __init__ hints with class-level annotation hints so the
        # scope-leak check covers ALL injection points on the class, not just
        # constructor parameters.  _collect_class_var_hints already excludes
        # names present in __init__, so the merge is collision-free.
        hints = {**init_hints, **self._collect_class_var_hints(binding.implementation)}

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
                    # or Instance[T] when held by a longer-lived component.
                    # Inject[T] and Lazy[T] both capture one instance at construction
                    # time — that instance becomes stale the moment the scope boundary
                    # rotates.  Instance[T] re-resolves on every .get() call (like
                    # Live[T]) so it is also safe here.
                    if not isinstance(inject_marker, LiveMeta | InstanceMeta):
                        live_violations.append(
                            LiveInjectionViolation(
                                binding=(binding.implementation, binding.scope),
                                dep=(base_type, dep.scope),
                                param_name=param_name,
                            )
                        )
                else:
                    # Non-scoped leak (e.g. SINGLETON holding a DEPENDENT dep).
                    # Instance[T] is exempt: the proxy defers resolution to .get()
                    # call time — the SINGLETON stores the proxy, never a resolved
                    # instance, so no stale reference is captured across scope boundaries.
                    if not isinstance(inject_marker, InstanceMeta):
                        leaks.append(
                            ScopeLeak(
                                binding=(binding.implementation, binding.scope),
                                reference=(dep.interface, dep.scope),
                            )
                        )

        if live_violations:
            raise LiveInjectionRequiredError(violations=live_violations)

        return leaks

    def _check_provider_scope_violation(
        self,
        binding: ProviderBinding,
    ) -> list[ScopeLeak]:
        """Inspect a provider function's parameters for scope leaks.

        Mirrors :meth:`_check_scope_violation` for :class:`ProviderBinding`.
        A ``@Provider(singleton=True)`` whose parameters include a
        ``REQUEST`` or ``SESSION`` scoped dependency without ``Live[T]``
        wrapping will silently capture a stale instance — the same risk
        that ``ClassBinding.validate()`` guards against in ``__init__``.

        Args:
            binding: The :class:`ProviderBinding` whose function parameters
                     are inspected for scope-narrowing dependency references.

        Returns:
            A list of :class:`~providify.metadata.ScopeLeak` instances, one
            per violating dependency.  Empty list means no leaks.

        Raises:
            LiveInjectionRequiredError: If any ``REQUEST`` or ``SESSION``
                scoped dependency is injected without ``Live[T]`` wrapping.
            AnnotationResolutionError: A parameter of *binding.fn* IS (or
                plausibly is) an injection point but its annotation cannot be
                resolved — see :meth:`_resolve_params`. A validator that
                cannot read the annotations must not report "no leaks
                found". Phase 7 narrows this further: an unresolvable
                annotation on a non-injected parameter no longer raises.

        Edge cases:
            - Provider with no parameters            → empty list, no error
            - Provider scope is DEPENDENT            → no leak risk, returns []
            - A parameter that IS an injection point but is unresolvable
              → ``AnnotationResolutionError``
            - Provider parameter is ``Live[T]``      → safe, not flagged
        """
        # DEPENDENT providers create a fresh instance every call — they never
        # hold a reference long enough to go stale.  Only SINGLETON and SESSION
        # can capture a narrower-scoped dep for longer than its lifetime.
        if binding.scope not in (Scope.SINGLETON, Scope.SESSION):
            return []

        leaks: list[ScopeLeak] = []
        live_violations: list[LiveInjectionViolation] = []

        # Per-parameter resolution (Phase 7) preserves Annotated wrappers
        # (include_extras=True baked into _eval_annotation) and applies the
        # container's localns per parameter automatically via _resolve_params.
        fn_hints = self._resolve_params(binding.fn, f"@Provider({binding.fn.__name__})")

        for param_name, hint in fn_hints.items():
            # Extract the injection marker BEFORE stripping Annotated — we need
            # to know whether the caller used Live[T] (safe) or bare type / Inject[T].
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

            dep_bindings = self._filter(base_type)
            for dep in dep_bindings:
                if not _is_scope_leak(parent_scope=binding.scope, dep_scope=dep.scope):
                    continue

                if dep.scope in (Scope.REQUEST, Scope.SESSION):
                    # REQUEST and SESSION scoped deps must be wrapped in Live[T].
                    # A singleton provider that receives a REQUEST-scoped param
                    # will call container.get(T) once and hold that instance —
                    # it becomes stale on the next request boundary.
                    if not isinstance(inject_marker, LiveMeta | InstanceMeta):
                        live_violations.append(
                            LiveInjectionViolation(
                                # ProviderBinding has no implementation class —
                                # use the provider function's name as an identifier.
                                binding=(
                                    type(f"@Provider({binding.fn.__name__})", (), {}),
                                    binding.scope,
                                ),
                                dep=(base_type, dep.scope),
                                param_name=param_name,
                            )
                        )
                else:
                    # Non-scoped leak (SINGLETON holding a narrower-scoped dep).
                    if not isinstance(inject_marker, InstanceMeta):
                        leaks.append(
                            ScopeLeak(
                                binding=(
                                    type(f"@Provider({binding.fn.__name__})", (), {}),
                                    binding.scope,
                                ),
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

        See also :meth:`validate` for full-graph checks (missing/ambiguous
        bindings, circular dependencies) beyond this scope-only tier.

        Returns:
            None

        Raises:
            ScopeViolationDetectedError: If any binding introduces a scope leak.
        """
        for binding in self._bindings:
            binding.validate(self)

    def validate_all(self) -> list[str]:
        """Validate all bindings and return a list of violation messages.

        Unlike :meth:`validate_bindings`, this method does NOT raise — it
        collects every violation across all bindings in a single pass and
        returns them as human-readable strings.  An empty list means the
        container is fully valid.

        This is the recommended pre-startup validation call for scope-tier
        issues; see also :meth:`validate` for full-graph checks (missing/
        ambiguous bindings, circular dependencies) beyond this scope-only tier.

        Sets ``_validated = True`` only when the returned list is empty,
        so the next ``get()`` call skips re-validation for valid containers.

        Returns:
            A list of violation message strings (one per failing binding).
            Empty list → all bindings are valid.

        Thread safety:  ⚠️ Not safe for concurrent mutation — call before the
                        app goes multi-threaded.
        Async safety:   ✅ No await points.

        Edge cases:
            - No bindings registered → returns [] immediately.
            - Partial validity → returns messages only for the failing bindings;
              valid bindings are silently accepted.
            - Calling validate_all() when _validated is already True still
              re-runs validation (idempotent but slightly redundant).

        Example:
            violations = container.validate_all()
            if violations:
                for msg in violations:
                    logger.error("DI violation: %s", msg)
                raise RuntimeError("Container has scope violations — aborting startup")
        """
        violations: list[str] = []
        for binding in self._bindings:
            try:
                binding.validate(self)
            except Exception as e:
                # Collect every violation message rather than stopping at the first.
                # The exception message contains the actionable fix suggestion.
                violations.append(str(e))
        # Only mark validated when everything is clean — a container with
        # violations must still re-validate after the caller fixes the bindings.
        if not violations:
            self._validated = True
        return violations

    @property
    def is_valid(self) -> bool:
        """Return True if :meth:`validate_bindings` has run successfully.

        Reflects whether ``_validated`` is True — i.e. at least one successful
        validation pass has completed since the last binding mutation.

        Note: this is a snapshot — it becomes False whenever a new binding is
        added (via ``bind()``, ``register()``, ``provide()``, ``override()``,
        or ``reset_binding()``), forcing re-validation on the next ``get()`` call.

        Returns:
            True  — container has been validated and no bindings were added since.
            False — container has never been validated, or a binding was added
                    after the last validation pass.

        Thread safety:  ✅ Reading a bool is atomic under the GIL.
        Async safety:   ✅ No shared mutable state accessed.

        Example:
            container.validate_all()
            assert container.is_valid is True
            container.bind(NewService, NewServiceImpl)
            assert container.is_valid is False  # mutation resets the flag
        """
        return self._validated

    def validate(self, *, raise_on_error: bool = True) -> ValidationReport:
        """Walk the ENTIRE declared dependency graph once and report every wiring defect.

        Unlike :meth:`validate_bindings` / :meth:`validate_all` (scope-leak
        tier only — reused verbatim here via ``binding.validate()``), this
        method additionally detects missing bindings, ambiguous bindings, and
        static circular dependencies — the three checks documented in plan
        003 §Design. Nothing is instantiated: no ``create()``, no cache
        write, no ``@PostConstruct``. See also :meth:`warm_up`, which is the
        "actually build it" counterpart.

        Two passes over ``self._bindings``, O(V) + O(E):

        1. **Scope tier** — ``binding.validate(self)`` is reused as-is; its
           structured exceptions (``ScopeViolationDetectedError``,
           ``LiveInjectionRequiredError``, ``AnnotationResolutionError``) are
           unpacked into one :class:`~providify.validation.ValidationIssue`
           per underlying violation, not one per exception, so a single
           binding with three scope leaks reports three issues.
        2. **Graph tier** — :meth:`_iter_injection_points` yields every
           statically-classified injection point; each is resolved against a
           call-local candidate memo (wrapping :meth:`_filter`) to detect
           missing/ambiguous bindings and to record adjacency edges for
           :meth:`_find_cycles`.

        A binding whose annotations fail to resolve (``AnnotationResolutionError``)
        contributes its ``UNRESOLVED_ANNOTATION`` issue and is **skipped**
        for edge-building — a partial graph, never a false "no dependencies".

        ⚠️ Profile-aware (plan 005): candidate resolution goes through
        :meth:`_filter` / :meth:`_binding_is_active`, the same predicate
        ``get()`` uses — so this method validates the graph **as it will
        actually be wired under the container's current** ``active_profiles``,
        not the graph you'd get by ignoring ``@Profile``. A binding whose only
        provider of some interface is gated by ``@Profile("prod")`` is
        reported as ``MISSING_BINDING`` when ``"prod"`` is not currently
        active, even though the binding is registered — call
        :meth:`activate_profile` (or construct with ``profiles=...``) before
        validating each deployment configuration you care about.

        Args:
            raise_on_error: When ``True`` (default), raises
                :class:`~providify.exceptions.ContainerValidationError` if
                the report contains any ``ERROR``-severity issue. Warnings
                never raise, regardless of this flag.

        Returns:
            The full :class:`~providify.validation.ValidationReport` —
            always built and returned, whether or not it is also raised.

        Raises:
            ContainerValidationError: ``raise_on_error`` is ``True`` and
                ``report.errors`` is non-empty.

        Thread safety:  ⚠️ Not safe for concurrent binding mutation — call
                        before the app goes multi-threaded, same caveat as
                        :meth:`validate_all`.
        Async safety:   ✅ No await points — pure introspection.

        Edge cases:
            - Empty container → ``ValidationReport(issues=(), checked_bindings=0)``.
            - ``self._validated`` is set ``True`` only when the report has
              **zero** issues (errors AND warnings), matching
              :meth:`validate_all`'s conservative rule.
            - Calling twice on an unchanged container yields two reports
              whose ``to_dict()`` outputs compare equal — no accumulated state.
            - REQUEST/SESSION-scoped bindings validate cleanly with no active
              scope context — nothing is instantiated, so ``_get_cache`` is
              never reached.

        Example:
            container.scan("myapp")
            container.validate()                                # raises on any error
            report = container.validate(raise_on_error=False)   # or inspect it
            for issue in report.errors:
                log.error("%s", issue.message)
        """
        # Local imports — validation.py never imports container.py (see its
        # module docstring), but container.py importing it at call time
        # here (rather than at module top) keeps the import graph obviously
        # one-directional to a reader scanning this method in isolation.
        from .exceptions import ContainerValidationError
        from .validation import IssueKind, Severity, ValidationIssue, ValidationReport
        from .validation import _unwrap_union as _validation_unwrap_union

        issues: list[ValidationIssue] = []
        binding_count = len(self._bindings)
        # Adjacency keyed by INDEX into self._bindings, not binding objects —
        # robust regardless of whether a binding type ever gains __eq__/__hash__
        # (plan 003 §Design "why one flat pass is enough").
        adjacency: dict[int, set[int]] = {i: set() for i in range(binding_count)}
        binding_index: dict[int, int] = {id(b): i for i, b in enumerate(self._bindings)}

        # Call-local memo — collapses repeated (type, qualifier, priority)
        # lookups across injection points that request the same dependency
        # (plan 003 §Risks "Performance"). Fresh every call: never shared
        # across validate() invocations, so a binding mutation between two
        # calls is always reflected.
        candidate_memo: dict[tuple[Any, Any, Any], list[AnyBinding]] = {}

        def memo_filter(
            base_type: Any, qualifier: Any, priority: Any
        ) -> list[AnyBinding]:
            try:
                key = (base_type, qualifier, priority)
                cached = candidate_memo.get(key)
            except TypeError:
                # base_type can be an Annotated[...] type (e.g. one member of
                # `Inject[T] | None`'s union re-unwrapped below) whose
                # metadata is a non-frozen @dataclass (InjectMeta etc.) —
                # Annotated.__hash__ hashes __metadata__, which raises for an
                # unhashable dataclass instance. Skip memoization for this
                # one lookup rather than crash the whole walk over a cache
                # optimisation; _filter() itself is a plain, cheap list scan.
                return self._filter(base_type, qualifier=qualifier, priority=priority)
            if cached is None:
                cached = self._filter(base_type, qualifier=qualifier, priority=priority)
                candidate_memo[key] = cached
            return cached

        def owner_of(b: AnyBinding) -> str:
            # Human-readable owner label — matches the vocabulary already
            # used by __repr__ on both binding types.
            if isinstance(b, ClassBinding):
                return b.implementation.__name__
            if isinstance(b, ProviderBinding):
                return f"@Provider({b.fn.__name__})"
            return _type_name(b.interface)  # pragma: no cover — exhaustive guard

        def candidate_name(c: AnyBinding) -> str:
            if isinstance(c, ClassBinding):
                return c.implementation.__name__
            if isinstance(c, ProviderBinding):
                return c.fn.__name__
            return _type_name(c.interface)  # pragma: no cover — exhaustive guard

        for idx, binding in enumerate(self._bindings):
            owner_name = owner_of(binding)

            # ── Pass 1: scope tier — reuse Binding.validate() verbatim ────
            unresolved = False
            try:
                binding.validate(self)
            except ScopeViolationDetectedError as exc:
                # One issue PER violating dependency, not one per exception —
                # a binding with three scope leaks must report three issues.
                for leak in exc.scope_violations:
                    issues.append(
                        ValidationIssue(
                            kind=IssueKind.SCOPE_LEAK,
                            severity=Severity.ERROR,
                            owner=owner_name,
                            message=(
                                f"Scope leak: {_type_name(leak.binding[0])} "
                                f"(scope={leak.binding[1].name}) holds a direct "
                                f"reference to {_type_name(leak.reference[0])} "
                                f"(scope={leak.reference[1].name}), which is "
                                f"shorter-lived. Fix: narrow the holder's scope, "
                                f"or wrap the dependency in Live[T]/Instance[T]."
                            ),
                            requested=_type_name(leak.reference[0]),
                        )
                    )
            except LiveInjectionRequiredError as exc:
                for v in exc.violations:
                    issues.append(
                        ValidationIssue(
                            kind=IssueKind.LIVE_REQUIRED,
                            severity=Severity.ERROR,
                            owner=owner_name,
                            message=(
                                f"'{v.param_name}' in {v.binding[0].__name__} "
                                f"(scope={v.binding[1].name}) injects "
                                f"{v.dep[0].__name__} (scope={v.dep[1].name}) "
                                f"without Live[T]. Fix: change "
                                f"`{v.param_name}: Inject[{v.dep[0].__name__}]` "
                                f"-> `{v.param_name}: Live[{v.dep[0].__name__}]`."
                            ),
                            param_name=v.param_name,
                            requested=_type_name(v.dep[0]),
                        )
                    )
            except AnnotationResolutionError as exc:
                issues.append(
                    ValidationIssue(
                        kind=IssueKind.UNRESOLVED_ANNOTATION,
                        severity=Severity.ERROR,
                        owner=owner_name,
                        message=str(exc),
                        param_name=exc.param_name,
                    )
                )
                # A validator that cannot read the annotations must not
                # report "no dependencies" — skip edge-building for this
                # binding entirely rather than silently under-reporting.
                unresolved = True

            if unresolved:
                continue

            # ── Pass 2: graph tier — missing / ambiguous / cycle edges ────
            try:
                points = list(self._iter_injection_points(binding))
            except AnnotationResolutionError as exc:
                # Defensive — _iter_injection_points shares the same
                # _resolve_params/_collect_class_var_hints calls pass 1 just
                # made, so this should already have surfaced above. Handled
                # identically in case a future refactor decouples the two.
                issues.append(
                    ValidationIssue(
                        kind=IssueKind.UNRESOLVED_ANNOTATION,
                        severity=Severity.ERROR,
                        owner=owner_name,
                        message=str(exc),
                        param_name=exc.param_name,
                    )
                )
                continue

            for point_owner, param_name, spec, has_default in points:
                # ── InjectInstances[T] / all=True — get_all() semantics ──
                # Missing is never reported ([] is a legal answer); every
                # candidate becomes a cycle edge (get_all resolves them all).
                if spec.multi:
                    for c in memo_filter(spec.base_type, spec.qualifier, None):
                        c_idx = binding_index.get(id(c))
                        if c_idx is not None:
                            adjacency[idx].add(c_idx)
                    continue

                # ── Instance[T] / Event[T] — caller-parameterised proxies ──
                # Deferred to call time; "no binding today" is a legal
                # design (InstanceProxy.resolvable() exists for this), so
                # WARNING not ERROR, and never a cycle edge.
                if spec.caller_parameterised:
                    if not memo_filter(spec.base_type, None, None):
                        issues.append(
                            ValidationIssue(
                                kind=IssueKind.MISSING_BINDING_DEFERRED,
                                severity=Severity.WARNING,
                                owner=point_owner,
                                message=(
                                    f"'{param_name}' requests "
                                    f"{_type_name(spec.base_type)} with no "
                                    f"matching binding today. Legal — "
                                    f"Instance[T]/Event[T] resolve their "
                                    f"qualifier at call time; use "
                                    f".resolvable() to guard."
                                ),
                                param_name=param_name,
                                requested=_type_name(spec.base_type),
                            )
                        )
                    continue

                # ── T1 | T2 / Optional[T] — eager union ──────────────────
                # ERROR only when NO member resolves (NoneType-optional
                # already short-circuited via spec.optional below). Edge
                # points at the FIRST member with a candidate, mirroring
                # the declaration-order rule at container.py:2342.
                union_result = _validation_unwrap_union(spec.base_type)
                if union_result is not None:
                    union_members, _ = union_result
                    first_matched: list[AnyBinding] | None = None
                    any_matched = False
                    for member in union_members:
                        member_candidates = memo_filter(
                            member, spec.qualifier, spec.priority
                        )
                        if member_candidates:
                            any_matched = True
                            if first_matched is None:
                                first_matched = member_candidates
                    if not any_matched:
                        if not spec.optional:
                            issues.append(
                                ValidationIssue(
                                    kind=IssueKind.MISSING_BINDING,
                                    severity=Severity.ERROR,
                                    owner=point_owner,
                                    message=(
                                        f"'{param_name}' requests "
                                        f"{_type_name(spec.base_type)} but no "
                                        f"member of the union has a matching "
                                        f"binding. Fix: bind at least one "
                                        f"member, or add a default value."
                                    ),
                                    param_name=param_name,
                                    requested=_type_name(spec.base_type),
                                )
                            )
                    elif not spec.deferred and first_matched:
                        best = max(first_matched, key=lambda c: c.priority or 0)
                        c_idx = binding_index.get(id(best))
                        if c_idx is not None:
                            adjacency[idx].add(c_idx)
                    continue

                # ── Plain eager single: Inject[T], bare T, NamedMeta, ────
                # ── DelegateMeta, Lazy[T], Live[T] ───────────────────────
                candidates = memo_filter(spec.base_type, spec.qualifier, spec.priority)
                if spec.excludes_self:
                    # Mirrors the self-exclusion in _resolve_hint_sync's
                    # delegate branch (container.py:2316-2322) — otherwise
                    # every @Decorator would report a bogus self-cycle.
                    candidates = [
                        c
                        for c in candidates
                        if not (
                            isinstance(binding, ClassBinding)
                            and isinstance(c, ClassBinding)
                            and c.implementation is binding.implementation
                        )
                    ]

                if not candidates:
                    if spec.optional:
                        pass  # T | None / InjectMeta(optional=True) — legal None
                    elif has_default:
                        issues.append(
                            ValidationIssue(
                                kind=IssueKind.MISSING_BINDING_DEFAULTED,
                                severity=Severity.WARNING,
                                owner=point_owner,
                                message=(
                                    f"'{param_name}' requests "
                                    f"{_type_name(spec.base_type)} with no "
                                    f"matching binding; falls back to its "
                                    f"default value at runtime. Bind it "
                                    f"explicitly to silence this warning."
                                ),
                                param_name=param_name,
                                requested=_type_name(spec.base_type),
                            )
                        )
                    else:
                        issues.append(
                            ValidationIssue(
                                kind=IssueKind.MISSING_BINDING,
                                severity=Severity.ERROR,
                                owner=point_owner,
                                message=(
                                    f"'{param_name}' requests "
                                    f"{_type_name(spec.base_type)} but no "
                                    f"binding is registered and no default "
                                    f"value exists. Fix: "
                                    f"container.bind({_type_name(spec.base_type)}, "
                                    f"...) or give the parameter a default."
                                ),
                                param_name=param_name,
                                requested=_type_name(spec.base_type),
                            )
                        )
                    continue

                # ── Ambiguity — runtime picks max(candidates, key=priority);
                # a tie at the max is a silent, arbitrary first-registered
                # pick. Only single-valued points reach this line (multi/
                # caller-parameterised already `continue`d above).
                max_priority = max((c.priority or 0) for c in candidates)
                ties = [c for c in candidates if (c.priority or 0) == max_priority]
                if len(ties) > 1:
                    issues.append(
                        ValidationIssue(
                            kind=IssueKind.AMBIGUOUS_BINDING,
                            severity=Severity.ERROR,
                            owner=point_owner,
                            message=(
                                f"'{param_name}' requests "
                                f"{_type_name(spec.base_type)} which has "
                                f"{len(ties)} candidates tied at priority "
                                f"{max_priority}: "
                                f"{', '.join(candidate_name(c) for c in ties)}. "
                                f"The runtime pick is arbitrary (first "
                                f"registered). Fix: give one candidate a "
                                f"higher @Priority, or add a qualifier to "
                                f"disambiguate."
                            ),
                            param_name=param_name,
                            requested=_type_name(spec.base_type),
                            candidates=tuple(candidate_name(c) for c in ties),
                        )
                    )

                if not spec.deferred:
                    # Edge points at the SAME max-priority candidate get()
                    # will actually pick — exactly the one construction uses.
                    best = max(candidates, key=lambda c: c.priority or 0)
                    c_idx = binding_index.get(id(best))
                    if c_idx is not None:
                        adjacency[idx].add(c_idx)

        # ── Cycle detection — colour-marked DFS over the adjacency map ────
        for cycle in self._find_cycles(adjacency):
            names = [_type_name(self._bindings[i].interface) for i in cycle]
            path = " → ".join([*names, names[0]])
            issues.append(
                ValidationIssue(
                    kind=IssueKind.CIRCULAR_DEPENDENCY,
                    severity=Severity.ERROR,
                    owner=names[0],
                    message=(
                        f"Circular dependency detected: {path}. Break the "
                        f"cycle by introducing Lazy[T] on one edge, or by "
                        f"restructuring the dependency."
                    ),
                )
            )

        report = ValidationReport(issues=tuple(issues), checked_bindings=binding_count)

        # Conservative rule matching validate_all() (container.py:3613-3614):
        # only mark validated when the report is COMPLETELY clean (errors
        # AND warnings) — a warnings-only report still means the developer
        # has something to look at before trusting the graph is final.
        if not report.issues:
            self._validated = True

        if raise_on_error and report.errors:
            raise ContainerValidationError(report)

        return report

    def _find_cycles(self, adjacency: dict[int, set[int]]) -> list[tuple[int, ...]]:
        """Find every distinct cycle in *adjacency*, canonicalised and deduplicated.

        Colour-marked (white/grey/black) depth-first search over binding
        indices. A back-edge to a GREY node closes a cycle; the cycle is
        canonicalised by rotating it to start at its lowest index so the
        same cycle entered from any of its member nodes dedupes to one entry.

        Args:
            adjacency: ``dict[binding_index -> set[binding_index]]`` — the
                out-edges built by :meth:`validate`'s graph-tier pass.

        Returns:
            A list of cycles, each a tuple of binding indices in traversal
            order (NOT yet closed back to the start — callers append
            ``cycle[0]`` themselves when rendering a path). A self-cycle
            ``A -> A`` is a one-element tuple ``(idx_A,)``. Empty list when
            the graph is acyclic.

        Thread safety:  N/A — pure function of its argument, no shared state.
        Async safety:   N/A — no awaits.

        Edge cases:
            - Empty adjacency              → ``[]``.
            - Self-loop (``A`` depends on itself) → one 1-element cycle.
            - Two disjoint cycles          → two entries, one per component.
            - A 3+ node cycle entered from different start nodes during the
              DFS  → reported exactly once (canonical-rotation dedupe).
        """
        # 0 = unvisited, 1 = on the current DFS path (grey), 2 = fully
        # explored (black) — the standard cycle-detection colouring.
        WHITE, GREY, BLACK = 0, 1, 2
        color: dict[int, int] = dict.fromkeys(adjacency, WHITE)
        # Explicit stack of nodes on the CURRENT DFS path — used to slice out
        # the exact cycle when a back-edge to a grey node is found.
        path_stack: list[int] = []
        # node -> its position in path_stack, for O(1) cycle slicing.
        position: dict[int, int] = {}
        found: list[tuple[int, ...]] = []
        seen_canonical: set[tuple[int, ...]] = set()

        def canonicalize(cycle: list[int]) -> tuple[int, ...]:
            # Rotate so the lowest index leads — makes A→B→A and B→A→B
            # compare equal regardless of which node the DFS happened to
            # visit first (plan 003 §Design "reported once, canonicalised").
            start = cycle.index(min(cycle))
            return tuple(cycle[start:] + cycle[:start])

        def dfs(node: int) -> None:
            color[node] = GREY
            path_stack.append(node)
            position[node] = len(path_stack) - 1
            for neighbor in adjacency.get(node, ()):
                if color.get(neighbor, WHITE) == WHITE:
                    dfs(neighbor)
                elif color.get(neighbor) == GREY:
                    # Back-edge to a node still on the current path — the
                    # slice from its first occurrence to here IS the cycle.
                    cycle = path_stack[position[neighbor] :]
                    canon = canonicalize(cycle)
                    if canon not in seen_canonical:
                        seen_canonical.add(canon)
                        found.append(canon)
                # BLACK neighbor: already fully explored via a non-cyclic
                # path — no new cycle information (standard DFS colouring).
            path_stack.pop()
            del position[node]
            color[node] = BLACK

        for start_node in adjacency:
            if color[start_node] == WHITE:
                dfs(start_node)

        return found

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

        Edge cases:
            - Class-var hints for a ``ClassBinding`` fail to resolve
              (``AnnotationResolutionError`` from :meth:`_collect_class_var_hints`)
              → downgraded to a logged warning; the class-var deps are omitted
              from the returned list but ``__init__`` deps are unaffected. This
              tier is reporting/enrichment (``describe()``), not wiring, so a
              partial graph beats aborting the whole traversal.
        """
        if isinstance(binding, ClassBinding):
            deps = self._collect_dependencies(
                fn=binding.implementation.__init__,
                qualifier=binding.qualifier,
                priority=binding.priority,
            )
            # Extend with class-level annotated attributes — these are injection
            # points too (var: Inject[T]), but invisible to _collect_dependencies
            # which only reads __init__.  The helper already filters to hints that
            # carry providify metadata and excludes __init__ param names.
            #
            # Tier downgrade: _collect_class_var_hints is strict (raises
            # AnnotationResolutionError) because its OTHER caller is a
            # validator. Here we are building a dependency graph for
            # describe() — advisory/reporting, not wiring — so a resolution
            # failure is downgraded to a logged warning and a partial graph,
            # rather than aborting the whole graph traversal.
            try:
                class_var_hints = self._collect_class_var_hints(binding.implementation)
            except AnnotationResolutionError as exc:
                logger.warning(
                    "Dependency graph for '%s' is incomplete: %s",
                    binding.implementation.__name__,
                    exc,
                )
                class_var_hints = {}
            for hint in class_var_hints.values():
                resolved = self._resolve_dependency(
                    hint,
                    qualifier=binding.qualifier,
                    priority=binding.priority,
                )
                if resolved is not None:
                    deps.append(resolved)
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

    def _iter_injection_points(
        self, binding: AnyBinding
    ) -> Iterator[tuple[str, str, _HintSpec, bool]]:
        """Yield every statically-classified injection point declared by *binding*.

        The graph-tier counterpart to :meth:`_get_dependencies`: where that
        method silently swallows unresolvable/unbound dependencies (a
        reporting-tier helper tolerant by design), this one is STRICT — it
        propagates :class:`~providify.exceptions.AnnotationResolutionError`
        rather than downgrading it to a logged warning, because its only
        caller (:meth:`validate`) must never report "no dependencies" for a
        binding it could not actually read.

        Covers both binding shapes:
            - :class:`~providify.binding.ClassBinding`: ``__init__``
              parameters (via :meth:`_resolve_params`, seeded with
              ``owner=implementation`` for PEP-695 generics) PLUS class-level
              annotated attributes (via :meth:`_collect_class_var_hints`).
            - :class:`~providify.binding.ProviderBinding`: the provider
              function's parameters.

        Args:
            binding: The binding whose injection points are enumerated.

        Yields:
            ``(owner_name, param_name, hint_spec, has_default)`` tuples:
                - ``owner_name``: e.g. ``"OrderService.__init__"`` for a
                  constructor parameter, ``"OrderService"`` for a class-var,
                  or ``"@Provider(make_db)"`` for a provider parameter.
                - ``param_name``: the parameter or class-attribute name.
                - ``hint_spec``: the :class:`~providify.validation._HintSpec`
                  returned by :func:`~providify.validation._classify_hint`
                  — hints that are NOT injection points are never yielded.
                - ``has_default``: whether the parameter has a default value
                  (always ``False`` for class-var points — they have no
                  default fallback at all, see :meth:`_inject_class_vars_sync`).

        Raises:
            AnnotationResolutionError: An annotation IS (or plausibly is) an
                injection point but cannot be evaluated — see
                :meth:`_resolve_params` / :meth:`_collect_class_var_hints`.
            TypeError: *binding* is neither a ``ClassBinding`` nor a
                ``ProviderBinding``.

        Edge cases:
            - ``*args: T`` / ``**kwargs: T`` → never yielded. Deliberate,
              documented divergence from :meth:`_collect_kwargs_sync`'s
              runtime behaviour is not needed here because
              :func:`~providify._annotations.resolve_params` already
              excludes ``VAR_POSITIONAL``/``VAR_KEYWORD`` parameters —
              stated explicitly here since a reader of THIS method should
              not have to trace into ``_annotations.py`` to confirm it.
            - A parameter/class-attribute with no providify marker → not
              yielded (``_classify_hint`` returns ``None`` for it).
        """
        # Import here (not at module top) to avoid a real circular import —
        # validation.py never imports container.py, but container.py's
        # module-level imports are already dense; keeping this one scoped to
        # its only two callers (_iter_injection_points, validate) documents
        # that the dependency is one-directional and narrow.
        from .validation import _classify_hint

        if isinstance(binding, ClassBinding):
            impl = binding.implementation
            init_owner = f"{impl.__name__}.__init__"
            init_hints = self._resolve_params(impl.__init__, init_owner, owner=impl)
            sig = inspect.signature(impl.__init__)
            for param_name, hint in init_hints.items():
                spec = _classify_hint(hint)
                if spec is None:
                    continue
                param = sig.parameters.get(param_name)
                has_default = bool(
                    param is not None and param.default is not inspect.Parameter.empty
                )
                yield init_owner, param_name, spec, has_default

            # Class-var injection points — invisible to __init__'s signature,
            # so they need their own owner label (no ".__init__" suffix: an
            # attribute is not a method parameter).
            class_var_owner = impl.__name__
            for attr_name, hint in self._collect_class_var_hints(impl).items():
                spec = _classify_hint(hint)
                if spec is None:
                    continue
                # Class vars have NO default fallback — _inject_class_vars_sync
                # raises LookupError unconditionally for an unresolved one
                # (container.py, _resolve_hint_sync -> self.get() -> raise).
                yield class_var_owner, attr_name, spec, False

        elif isinstance(binding, ProviderBinding):
            provider_owner = f"@Provider({binding.fn.__name__})"
            fn_hints = self._resolve_params(binding.fn, provider_owner)
            sig = inspect.signature(binding.fn)
            for param_name, hint in fn_hints.items():
                spec = _classify_hint(hint)
                if spec is None:
                    continue
                param = sig.parameters.get(param_name)
                has_default = bool(
                    param is not None and param.default is not inspect.Parameter.empty
                )
                yield provider_owner, param_name, spec, has_default

        else:
            raise TypeError(
                f"No _iter_injection_points implementation found for binding "
                f"type '{type(binding).__name__}'. Expected ClassBinding or "
                f"ProviderBinding."
            )

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
        """Install a ``@Configuration`` module synchronously — transitively.

        Resolves ``module_cls``'s ``depends_on=`` closure into a deterministic
        install order (deps first — :func:`providify.modules.resolve_install_order`)
        and installs every not-yet-installed class in that order. Each newly
        installed module is instantiated with its constructor dependencies
        injected (Spring-style), has its ``@PostConstruct`` hook run (if any),
        has every ``@Provider``-decorated method registered as a bound-method
        binding, and is recorded in ``_installed_modules`` so its
        ``@PreDestroy`` hook (if any) participates in :meth:`shutdown`.

        Idempotent: a class already present in ``_installed_modules`` (from
        an earlier ``install()``/``ainstall()``/``scan()``) is skipped — its
        providers are NOT re-registered. This is the container's dedup
        authority (plan 008 §Design, fixing the historical
        ``install()`` + ``scan()`` double-registration bug); the scanner's
        own dedup set is now only a same-session fast-path shortcut.

        Args:
            module_cls: A class decorated with ``@Configuration``. Its
                transitive ``depends_on`` closure is installed first.

        Returns:
            None

        Raises:
            TypeError:        If *module_cls* is not decorated with
                               ``@Configuration``, OR if a class reachable via
                               ``depends_on=`` is not itself ``@Configuration``
                               (naming it).
            ModuleCycleError: The ``depends_on`` graph contains a cycle.
                               Detected by :func:`resolve_install_order`
                               **before** any instantiation, so a cycle
                               leaves the container completely untouched —
                               no partial installation.
            LookupError:      If any constructor dependency of a module being
                               installed has no binding.
            RuntimeError:      If any constructor dependency is async-only —
                               use :meth:`ainstall` instead.

        Edge cases:
            - ``shutdown()`` then ``install(M)`` again → ``M`` is still in
              ``_installed_modules`` (with ``disposed=True``) → skipped, NOT
              re-installed. A container is not fully reusable for module
              re-installation after ``shutdown()`` — use :meth:`copy` or a
              fresh ``DIContainer`` instead.

        Example:
            container.bind(Settings, AppSettings)
            container.install(RepoModule)   # installs InfraModule first
        """
        if not _has_configuration_module(module_cls):
            raise TypeError(
                f"{module_cls.__name__} must be decorated with @Configuration."
            )
        # Cycle/type-error detection happens for the WHOLE closure before any
        # instantiation — resolve_install_order raises before we touch the
        # loop below, so a cycle leaves _installed_modules (and _bindings)
        # completely unchanged (plan 008 §Design "Install pipeline").
        for cls in resolve_install_order([module_cls]):
            if cls in self._installed_modules:
                continue  # already installed — dedup authority, see docstring
            self._install_one(cls)

    async def ainstall(self, module_cls: type) -> None:
        """Install a ``@Configuration`` module asynchronously — transitively.

        Async mirror of :meth:`install`: same transitive ``depends_on``
        ordering, same dedup, same ``@PostConstruct``/``@PreDestroy``
        participation — but resolves constructor dependencies and runs an
        ``async def`` ``@PostConstruct`` hook via the async resolution path.
        Use when a module (or one of its dependencies)'s constructor has
        async-only dependencies (i.e. deps that require ``aget()``).

        Args:
            module_cls: A class decorated with ``@Configuration``. Its
                transitive ``depends_on`` closure is installed first.

        Returns:
            None

        Raises:
            TypeError:        If *module_cls* is not decorated with
                               ``@Configuration``, OR a ``depends_on`` class
                               is not itself ``@Configuration`` (naming it).
            ModuleCycleError: The ``depends_on`` graph contains a cycle —
                               detected before any instantiation.
            LookupError:      If any constructor dependency of a module being
                               installed has no binding.

        Example:
            await container.ainstall(RepoModule)
        """
        if not _has_configuration_module(module_cls):
            raise TypeError(
                f"{module_cls.__name__} must be decorated with @Configuration."
            )
        for cls in resolve_install_order([module_cls]):
            if cls in self._installed_modules:
                continue
            await self._ainstall_one(cls)

    def _install_one(self, cls: type) -> None:
        """Instantiate, ``@PostConstruct``, register providers, and record ONE
        already-order-resolved ``@Configuration`` class (sync path).

        Shared tail of :meth:`install`'s per-class loop — factored out so the
        ordering/dedup logic in ``install()`` stays readable and this single
        class's install steps are unit-testable in isolation if needed.

        Args:
            cls: A ``@Configuration`` class, not yet in ``_installed_modules``.

        Returns:
            None

        Raises:
            LookupError:  If any constructor dependency of *cls* has no binding.
            RuntimeError: If any constructor dependency, or the
                          ``@PostConstruct`` hook, is async-only.
        """
        instance = self._resolve_constructor(cls)
        self._run_post_construct_sync(instance, _find_post_construct(cls))
        self._register_module_providers(cls, instance)
        # Recorded AFTER providers are registered — matches the "install
        # order == dict insertion order" contract other code relies on
        # (_installed_modules docstring, role 2/3); registration itself
        # cannot fail once construction/PostConstruct succeeded, so ordering
        # here vs. before registration is not otherwise observable.
        self._installed_modules[cls] = _ModuleRecord(
            instance=instance, owned=True, disposed=False
        )

    async def _ainstall_one(self, cls: type) -> None:
        """Async mirror of :meth:`_install_one`.

        Args:
            cls: A ``@Configuration`` class, not yet in ``_installed_modules``.

        Returns:
            None

        Raises:
            LookupError: If any constructor dependency of *cls* has no binding.
        """
        instance = await self._resolve_constructor_async(cls)
        await self._run_post_construct_async(instance, _find_post_construct(cls))
        self._register_module_providers(cls, instance)
        self._installed_modules[cls] = _ModuleRecord(
            instance=instance, owned=True, disposed=False
        )

    def _register_module_providers(self, module_cls: type, instance: object) -> None:
        """Register every ``@Provider``-decorated method from a module instance.

        Iterates over the class's own attributes (not inherited ones) to find
        ``@Provider``-decorated methods. ``vars()`` gives the raw unbound functions,
        which carry ``ProviderMetadata`` directly on their ``__dict__``.

        Also wires ``@Disposes`` methods to their corresponding ``ProviderBinding``.

        Args:
            module_cls: The ``@Configuration`` class to inspect.
            instance:   The live module instance — getattr returns bound methods.

        Returns:
            None
        """
        for name, fn in vars(module_cls).items():
            if name == "__init__":
                continue
            # Unwrap @property so that @Provider @property works naturally.
            effective_fn = fn.fget if isinstance(fn, property) else fn
            if (
                callable(effective_fn)
                and _get_provider_metadata(effective_fn) is not None
            ):
                if isinstance(fn, property):
                    # Wrap the property getter as a bound-method-like callable.
                    # functools.wraps copies __module__, __globals__, __annotations__,
                    # and __wrapped__ so that ProviderBinding can evaluate the
                    # return-type annotation in the correct namespace.
                    captured = fn
                    bound_instance = instance

                    @functools.wraps(effective_fn)
                    def _prop_provider(prop=captured, obj=bound_instance) -> Any:
                        return prop.fget(obj)

                    # Copy provider metadata (stamped in __dict__ by @Provider)
                    _prop_provider.__dict__.update(effective_fn.__dict__)
                    self.provide(_prop_provider)
                else:
                    # getattr returns a bound method — self is the live module instance.
                    self.provide(getattr(instance, name))

        # Wire @Disposes teardown methods to their ProviderBinding
        for name, fn in vars(module_cls).items():
            if not callable(fn):
                continue
            disposes_marker = _get_disposes_marker(fn)
            if disposes_marker is None:
                continue
            disposed_type = disposes_marker.disposed_type
            for binding in self._bindings:
                if isinstance(binding, ProviderBinding) and _interface_matches(
                    binding.interface, disposed_type
                ):
                    binding.disposer = getattr(instance, name)
                    break

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

    # ── Introspection (Feature 10) ────────────────────────────────

    def get_binding(
        self,
        interface: type,
        *,
        qualifier: str | type | None = None,
        priority: int | None = None,
    ) -> AnyBinding:
        """Return the highest-priority binding for *interface* without instantiating it.

        A pure read — does NOT trigger ``validate_bindings()`` or create any
        instances. Useful for test introspection, migration scripts, and
        tooling that inspects the container registry.

        Args:
            interface:  The type to look up (concrete class or interface).
            qualifier:  If given, only bindings registered with this qualifier
                        are considered.
            priority:   If given, only bindings with this exact priority value
                        are considered.

        Returns:
            The highest-priority matching :class:`~providify.binding.AnyBinding`.

        Raises:
            LookupError: If no binding is registered for *interface* with
                         the given qualifier / priority.

        Edge cases:
            - Called before ``get()``       → does NOT trigger validate_bindings().
            - exact_only self-bindings      → included when *interface* is the
                                              concrete class itself.
            - No bindings at all            → raises LookupError.

        Thread safety:  ✅ Read-only; no shared state mutated.
        Async safety:   ✅ No await points.

        Example:
            binding = container.get_binding(UserRepository)
            print(binding.scope)  # Scope.SINGLETON
        """
        return self._get_best_candidate(
            interface, qualifier=qualifier, priority=priority
        )

    def get_all_bindings(
        self,
        interface: type,
        *,
        qualifier: str | type | None = None,
    ) -> list[AnyBinding]:
        """Return all registered bindings for *interface* without instantiating them.

        A pure read — does NOT trigger ``validate_bindings()`` or create any
        instances. Returns an empty list (never raises LookupError) when no
        bindings match.

        Args:
            interface: The type to look up.
            qualifier: If given, only bindings with this qualifier are returned.

        Returns:
            A list of all matching bindings, in registration order.
            Returns ``[]`` if none are registered.

        Edge cases:
            - No bindings match → returns [] (differs from get_all() which raises).
            - Called before get() → does NOT trigger validate_bindings().

        Thread safety:  ✅ Read-only; no shared state mutated.
        Async safety:   ✅ No await points.

        Example:
            bindings = container.get_all_bindings(NotificationService)
            for b in bindings:
                print(b.scope, b.qualifier)
        """
        return self._filter(interface, qualifier=qualifier)

    # ── Observability hooks (F6) ────────────────────────────────────

    def add_hook(
        self, event_type: type, callback: Callable[[Any], None]
    ) -> Callable[[], None]:
        """Register *callback* to run whenever an event of exactly *event_type* is emitted.

        Dispatch is by exact type (``type(event) is event_type``), never
        ``isinstance`` — a hook registered for a supertype (e.g. ``object``)
        never receives ``InstanceCreated``/``InstanceDisposed``/``ScopeEntered``/
        ``ScopeExited`` events. Registering the same callback twice makes it
        fire twice per matching event.

        Contract (see ``providify/observability.py`` and this class's
        ``_emit`` for the enforcement):
            - Callbacks are **sync-only** and called **inline**, in the
              caller's thread/task — they must not block.
            - Callbacks run **outside** every container lock (the per-key
              singleton lock, the async per-key lock) — never inside one.
            - A callback that raises is caught, logged at WARNING through
              this module's logger, and does NOT interrupt resolution or
              shutdown; other hooks for the same event still run.
            - A callback must NOT call back into ``container.get()``/
              ``aget()`` for a DIFFERENT container, or resolve across
              threads — that re-entrancy is the caller's problem to avoid
              (re-resolving the SAME already-cached singleton from within
              its own ``InstanceCreated`` hook is safe: emission happens
              after the cache write and outside the per-key lock).

        Args:
            event_type: One of ``InstanceCreated``, ``InstanceDisposed``,
                ``ScopeEntered``, ``ScopeExited`` (or any type — unmatched
                types simply never fire).
            callback: A callable taking one positional argument (the event
                instance).

        Returns:
            A zero-argument callable that unsubscribes this exact
            registration when called — equivalent to (but more convenient
            than) a matching :meth:`remove_hook` call.

        Thread safety:  ⚠️ ``self._hooks`` is a plain dict/list, not
                        lock-protected. Registering hooks concurrently with
                        resolution is a data race on the list `add_hook`
                        appends to; register hooks during startup, before
                        the container serves concurrent traffic (same
                        caveat as other container-mutation methods, e.g.
                        :meth:`bind`).
        Async safety:   ✅ No await points.

        Example:
            unsubscribe = container.add_hook(InstanceCreated, on_created)
            ...
            unsubscribe()  # stop observing
        """
        self._hooks.setdefault(event_type, []).append(callback)

        def _unsubscribe() -> None:
            self.remove_hook(event_type, callback)

        return _unsubscribe

    def remove_hook(self, event_type: type, callback: Callable[[Any], None]) -> bool:
        """Remove one registration of *callback* for *event_type*.

        Args:
            event_type: The exact event type the callback was registered under.
            callback: The callback object to remove (identity/equality match,
                same semantics as ``list.remove``).

        Returns:
            ``True`` if a registration was found and removed, ``False`` if
            *callback* was not registered for *event_type* (never raises).

        Thread safety:  ⚠️ Same caveat as :meth:`add_hook`.
        Async safety:   ✅ No await points.
        """
        callbacks = self._hooks.get(event_type)
        if not callbacks or callback not in callbacks:
            return False
        callbacks.remove(callback)
        return True

    def _emit(self, event: object) -> None:
        """Dispatch *event* to every hook registered for its exact type.

        Iterates a **tuple snapshot** of the registered callback list —
        not the live list — so a hook that itself calls ``add_hook`` (or
        `remove_hook`) during emission never mutates the sequence this
        loop is iterating (edge case documented in plan 009 §Edge cases).

        A raising callback is swallowed and logged at WARNING; it never
        breaks resolution or shutdown, and other callbacks for the same
        event still run. This is a deliberate divergence from SQLAlchemy/
        Django (which propagate listener exceptions) — a broken telemetry
        hook must never abort teardown (see plan 009 §Design, "Decision").

        Args:
            event: One of the four ``providify.observability`` event
                instances.

        Returns:
            None

        Thread safety:  ✅ Read-only over `self._hooks` other than the
                        tuple-snapshot copy; safe alongside concurrent
                        resolution (though not alongside concurrent
                        `add_hook`/`remove_hook` — see those methods).
        Async safety:   ✅ No await points — callbacks are sync by contract.
        """
        for cb in tuple(self._hooks.get(type(event), ())):
            try:
                cb(event)
            except Exception:  # noqa: BLE001 — telemetry must never break the caller
                logger.warning(
                    "[DIContainer] telemetry hook %r raised for %r",
                    cb,
                    event,
                    exc_info=True,
                )

    # ── Override (Feature 5) ──────────────────────────────────────

    def override(self, interface: Any, implementation: type) -> None:
        """Replace all bindings for *interface* with a new implementation.

        Removes every :class:`~providify.binding.ClassBinding` where
        ``binding.interface == interface``, evicts the previous implementations
        from the singleton cache, then calls :meth:`bind` to register the
        replacement.

        Resets validation state so scope-leak checks run again on the next
        ``get()`` / ``aget()`` call.

        Intended for test overrides — swapping a real service with a fake
        without rebuilding the container.

        Args:
            interface:      The interface type to override.
            implementation: The replacement concrete class.

        Returns:
            None

        Edge cases:
            - Interface not registered → no-op (no error raised).  The new
              binding is still registered via ``bind()``.
            - Only :class:`~providify.binding.ClassBinding` entries are removed.
              :class:`~providify.binding.ProviderBinding` entries for the same
              interface are left in place.
            - Singleton cache entries for the *previous* implementation are
              evicted so the new impl gets a fresh start.

        Thread safety:  ⚠️ Not safe for concurrent use — call before the app
                        goes multi-threaded.
        Async safety:   ✅ No await points.

        Example:
            container.bind(Notifier, RealNotifier)
            container.override(Notifier, FakeNotifier)  # test swap
            svc = container.get(Notifier)               # FakeNotifier
        """
        # Collect implementation classes for cache eviction before mutating
        # _bindings — otherwise we'd evict entries we've already removed.
        to_evict: list[Any] = [
            b.implementation
            for b in self._bindings
            if isinstance(b, ClassBinding) and b.interface is interface
        ]

        # Remove all ClassBinding entries that map *interface* → anything.
        # DESIGN: list comprehension (rebuild) rather than in-place removal so
        # iteration order is preserved and there is no index-shift bug.
        self._bindings = [
            b
            for b in self._bindings
            if not (isinstance(b, ClassBinding) and b.interface is interface)
        ]

        # Evict previous singletons — stale instances must not survive the swap.
        for key in to_evict:
            self._singleton_cache.pop(key, None)
            self._singleton_locks.pop(key, None)
            self._async_singleton_locks.pop(key, None)

        # Drop evicted keys from the creation log too — otherwise a later
        # shutdown() would find them in _singleton_order's fallback-free main
        # walk... except _teardown_plan already filters on `key not in
        # _singleton_cache`, so this is defense-in-depth: it keeps the log's
        # size bounded (no unbounded growth across repeated override() calls
        # in a long-lived test session) rather than changing shutdown() semantics.
        evicted = set(to_evict)
        self._singleton_order = [
            e for e in self._singleton_order if e[0] not in evicted
        ]

        # Register the replacement via the public bind() API — which also
        # resets _validated and _localns_cache.
        self.bind(interface, implementation)

    # ── Reset binding (Feature 12) ────────────────────────────────

    def reset_binding(
        self,
        interface: Any,
        *,
        qualifier: str | type | None = None,
    ) -> int:
        """Remove all bindings for *interface* and evict cached instances.

        Mirrors :meth:`override` but without registering a replacement.
        After this call, :meth:`is_resolvable` returns ``False`` for
        *interface* with the given qualifier.

        Args:
            interface:  The interface type to remove.
            qualifier:  If given, only bindings with this exact qualifier are
                        removed.  ``None`` removes ALL bindings for *interface*
                        regardless of qualifier.

        Returns:
            The number of bindings removed (0 means not found — no error raised).

        Edge cases:
            - Interface not registered                → returns 0, no error.
            - Singleton cache evicted for removed keys → subsequent get() creates
                                                         a fresh instance (if
                                                         re-bound before then).
            - Both ClassBinding and ProviderBinding entries are removed.

        Thread safety:  ⚠️ Not safe for concurrent use.
        Async safety:   ✅ No await points.

        Example:
            container.register(MyService)
            container.get(MyService)          # caches singleton
            n = container.reset_binding(MyService)
            assert n == 1
            assert not container.is_resolvable(MyService)
        """
        to_remove = [
            b
            for b in self._bindings
            if _interface_matches(b.interface, interface)
            and (qualifier is None or b.qualifier == qualifier)
        ]

        if not to_remove:
            return 0

        # Collect cache keys before removal
        to_evict: list[Any] = []
        for b in to_remove:
            if isinstance(b, ClassBinding):
                to_evict.append(b.implementation)
            else:
                to_evict.append(b.fn)  # type: ignore[union-attr]

        # Rebuild _bindings without the removed entries
        remove_set = set(id(b) for b in to_remove)
        self._bindings = [b for b in self._bindings if id(b) not in remove_set]

        # Evict cached instances
        for key in to_evict:
            self._singleton_cache.pop(key, None)
            self._singleton_locks.pop(key, None)
            self._async_singleton_locks.pop(key, None)

        # Drop evicted keys from the creation log too — see override()'s
        # matching comment; _teardown_plan already filters these out via the
        # `_singleton_cache` membership check, this just bounds log growth.
        evicted = set(to_evict)
        self._singleton_order = [
            e for e in self._singleton_order if e[0] not in evicted
        ]

        # Reset validation so scope checks run again
        self._validated = False
        self._invalidate_type_caches()

        return len(to_remove)

    # ── copy() ────────────────────────────────────────────────────

    def copy(self) -> DIContainer:
        """Return a new container with the same bindings but no shared cache state.

        Creates a structurally identical container suitable for test overrides —
        the caller can ``override()`` or ``reset_binding()`` on the copy without
        affecting the original.  All binding objects are shared (shallow copy)
        which is safe because ``AnyBinding`` attributes are immutable after
        ``__init__``.

        The copy starts unvalidated (``_validated = False``) so scope-leak
        checks run afresh on its first ``get()`` call.  Singleton caches,
        scope contexts, and lock dicts are NOT shared — each container manages
        its own instance lifecycle independently.

        Returns:
            A new ``DIContainer`` with a shallow copy of ``_bindings`` and
            fresh (empty) caches and locks.

        Thread safety:  ⚠️ Not safe to call concurrently with mutations on
                        the source container — ``list(self._bindings)`` iterates
                        the list under no lock.  Call ``copy()`` during startup
                        before the container goes multi-threaded.
        Async safety:   ✅ No await points.

        Edge cases:
            - Empty container → copy is also empty, no error.
            - Singletons already cached in the source container are NOT present
              in the copy — the copy must re-instantiate them on first get().
            - Scanner is not copied — the copy gets its own DefaultContainerScanner
              that points at itself.  Auto-scan (``scan=`` constructor arg) does
              NOT re-run on copy.

        Example:
            base = build_app_container()
            test_c = base.copy()
            test_c.override(Database, FakeDatabase)
            svc = test_c.get(UserService)  # gets FakeDatabase
            svc2 = base.get(UserService)   # still gets real Database ✅
        """
        # DESIGN: use __new__ to bypass __init__ entirely.
        # __init__ scans modules and sets up the full wiring; we only want
        # to clone the binding list.  __new__ gives us a blank instance that
        # we populate manually — the same pattern used by pickle.__reduce__.
        #
        # Tradeoffs:
        #   ✅ No module re-scan, no double-registration from the auto-scan path
        #   ✅ All caches start empty — predictable state for test isolation
        #   ❌ Fragile if __init__ adds new attributes in the future — this
        #      method must be kept in sync.  Mitigation: the test suite covers
        #      copy() behaviour, so attribute additions cause obvious test failures.
        new = DIContainer.__new__(DIContainer)
        # Shallow-copy the binding list — bindings are immutable value objects
        new._bindings = list(self._bindings)
        # Fresh caches — no state bleeds from source to copy
        new._singleton_cache = {}
        # Fresh (empty) creation log — the copy has never instantiated
        # anything, so its teardown order must start blank too; sharing the
        # source's log would make the copy's shutdown() replay the source's
        # (possibly already-cleared) history against instances it never
        # created.
        new._singleton_order = []
        new._singleton_locks = {}
        new._singleton_lock_guard = threading.Lock()
        new._async_singleton_locks = {}
        # Fresh scope context wired to the new container's own PreDestroy callbacks
        new.scope_context = ScopeContext(
            on_scope_exit=new._run_pre_destroy_for_scope,
            on_scope_exit_async=new._arun_pre_destroy_for_scope,
            on_invalidate_session=new._run_pre_destroy_for_scope,
            on_invalidate_session_async=new._arun_pre_destroy_for_scope,
        )
        # Fresh scanner pointing at the new container
        new._scanner = DefaultContainerScanner(new)
        # Not validated — scope checks run on first get()
        new._validated = False
        # localns/hints caches reset — built from _bindings, must reflect the
        # copy's own list, and NEVER share the parent's dict object (a
        # binding mutation on one container must not silently poison the
        # other's cached hints). Set directly rather than via
        # `_invalidate_type_caches()` because `new` is constructed with
        # `__new__` — `_hints_cache` does not exist yet for that method to
        # clear.
        new._localns_cache = None
        new._hints_cache = {}
        # Copy runtime state introduced in v0.3.0 (+ v1.2.0's _active_profiles)
        new._enabled_alternatives = set(self._enabled_alternatives)
        # @Profile — inherited by value; the copy owns an independent
        # frozenset reference, so activate_profile()/deactivate_profile()
        # on either container never affects the other (plan 005 §Design).
        new._active_profiles = self._active_profiles
        new._interceptor_classes = list(self._interceptor_classes)
        new._observers = {}  # observers are re-registered as instances are created
        new._tracked_dependents = []
        # Observability hooks (Plan 009/F6): telemetry is CONFIGURATION, not
        # instance state — a copy() made for test overrides should keep
        # observing the same callbacks the source container does, unlike
        # caches/locks/tracked-dependents above which are all instance state
        # reset to empty. Copies the per-type lists (not just the outer
        # dict) so appending to one container's hook list never mutates the
        # other's. `_clear_caches()` must NOT touch `_hooks` — hooks are not
        # a cache.
        new._hooks = {k: list(v) for k, v in self._hooks.items()}
        # DESIGN (Plan 008/F5): dedup/install-order HISTORY is inherited —
        # replace(r, owned=False) for every record — so the copy never
        # re-registers a module's providers it already holds bindings for
        # (its _bindings list was shallow-copied above, providers and all).
        # OWNERSHIP is not inherited: owned=False means the copy's
        # shutdown()/ashutdown() will skip every module's @PreDestroy hook —
        # the copy never created these instances, so it must never dispose
        # them (mirrors _singleton_order's ownership reasoning above: a
        # shared history, but only the source disposes what it created).
        new._installed_modules = {
            cls: replace(rec, owned=False)
            for cls, rec in self._installed_modules.items()
        }
        return new

    # ── snapshot() / restore() (Feature: pytest integration, plan 007) ──

    def snapshot(self) -> ContainerSnapshot:
        """Capture the container's current mutable state for later ``restore()``.

        Companion to :meth:`copy` — where ``copy()`` clones bindings into a
        *new*, independent container, ``snapshot()``/``restore()`` round-trip
        state on the *same* container, which is what test overrides need
        when the system under test resolves via ``DIContainer.current()`` or
        holds a reference to this exact instance (``copy()`` cannot help
        there — see plan 007 §Alternatives).

        Cross-reference: this method shares ``copy()``'s *"fragile if
        __init__ adds new attributes — must be kept in sync"* caveat
        (:meth:`copy`, above). If a future ``__init__`` adds a new mutable
        attribute, it must be captured here AND restored in
        :meth:`restore` — three call sites (``__init__``, this method,
        ``restore()``) now need to agree.

        Returns:
            A :class:`ContainerSnapshot` — an opaque value object. Only
            supported use is passing it back to :meth:`restore` on this
            same container.

        Thread safety:  ⚠️ Not safe under concurrent resolution — reads
                        ``_bindings``/``_singleton_cache``/etc. without a
                        lock, mirroring ``copy()``.
        Async safety:   ✅ No await points.

        Example:
            snap = container.snapshot()
            container.override(Clock, FakeClock)
            ...
            container.restore(snap)  # Clock override undone
        """
        return ContainerSnapshot(
            bindings=tuple(self._bindings),
            singleton_cache=dict(self._singleton_cache),
            singleton_order=tuple(self._singleton_order),
            enabled_alternatives=frozenset(self._enabled_alternatives),
            active_profiles=self._active_profiles,
            interceptor_classes=tuple(self._interceptor_classes),
        )

    def restore(self, snapshot: ContainerSnapshot) -> None:
        """Write a previously captured :class:`ContainerSnapshot` back onto this container.

        Restores bindings, the singleton cache/order, enabled alternatives,
        active profiles, and registered interceptor classes verbatim, then
        invalidates the derived type caches (``_invalidate_type_caches()``)
        and resets ``_validated = False`` so ``_localns_cache``/
        ``_hints_cache`` are rebuilt from the restored binding list on the
        next resolution.

        Cross-reference: shares ``copy()``'s *"fragile if __init__ adds new
        attributes — must be kept in sync"* caveat (:meth:`copy`) — this
        method, :meth:`snapshot`, and ``__init__`` must all agree on which
        attributes are part of a container's mutable state.

        Instances created **after** the snapshot was taken are dropped
        without teardown — ``restore()`` writes ``_singleton_cache`` back
        wholesale, so any singleton instantiated in the override window
        simply vanishes; no ``@PreDestroy``/``@Disposes`` runs for it. This
        is by design for configuration-level test overrides (see plan 007
        §Risks) — use a per-test container with ``shutdown()`` for lifecycle
        correctness.

        ``_singleton_locks`` is not part of the snapshot (it is pure derived
        state — stale entries are harmless, lazily recreated on next
        resolution) but entries whose key is absent from the restored cache
        are dropped here to bound growth.

        Args:
            snapshot: A value previously returned by :meth:`snapshot`,
                **on this same container** — restoring a snapshot captured
                from a different container is not supported (no guard is
                installed; it is a private-ish escape hatch, not a public
                contract).

        Returns:
            None

        Thread safety:  ⚠️ Not safe under concurrent resolution — mutates
                        ``_bindings``/``_singleton_cache``/etc. without a
                        lock, mirroring ``copy()``.
        Async safety:   ✅ No await points.

        Edge cases:
            - Restoring the same snapshot twice is idempotent — no error.
            - A snapshot taken from container A restored into container B
              is not guarded against — documented only.

        Example:
            snap = container.snapshot()
            container.bind(Greeter, Greeter)
            container.restore(snap)
            assert not container.is_resolvable(Greeter)
        """
        self._bindings = list(snapshot.bindings)
        self._singleton_cache = dict(snapshot.singleton_cache)
        self._singleton_order = list(snapshot.singleton_order)
        self._enabled_alternatives = set(snapshot.enabled_alternatives)
        self._active_profiles = snapshot.active_profiles
        self._interceptor_classes = list(snapshot.interceptor_classes)

        # Drop lock entries for cache keys no longer present after restore —
        # bounds growth. Stale locks for still-present keys are harmless
        # (same lock protects the same key either way).
        stale_keys = [
            key for key in self._singleton_locks if key not in self._singleton_cache
        ]
        for key in stale_keys:
            del self._singleton_locks[key]
        stale_async_keys = [
            key
            for key in self._async_singleton_locks
            if key not in self._singleton_cache
        ]
        for key in stale_async_keys:
            del self._async_singleton_locks[key]

        self._invalidate_type_caches()
        self._validated = False

    # ── __repr__ (Feature 14) ─────────────────────────────────────

    def __repr__(self) -> str:
        """Return a human-readable summary with per-scope binding counts.

        Shows counts only for scopes that have at least one binding so the
        output stays compact.

        Returns:
            A string like:
            ``DIContainer(singleton=3, request=2, dependent=6, validated=True)``

        Example:
            repr(container)
            # → 'DIContainer(singleton=2, dependent=4, validated=False)'

        Thread safety:  ✅ Read-only; iterates _bindings under GIL.
        Async safety:   ✅ No await points.
        """
        # DESIGN: collections.Counter maps Scope → count in a single pass.
        # Only scopes with count > 0 are shown — keeps output minimal.
        counts: collections.Counter[str] = collections.Counter(
            b.scope.name.lower() for b in self._bindings
        )
        scope_parts = ", ".join(
            f"{name}={count}"
            for name, count in [
                ("singleton", counts.get("singleton", 0)),
                ("request", counts.get("request", 0)),
                ("session", counts.get("session", 0)),
                ("dependent", counts.get("dependent", 0)),
            ]
            if count > 0
        )
        if scope_parts:
            return f"DIContainer({scope_parts}, validated={self._validated})"
        return f"DIContainer(validated={self._validated})"
