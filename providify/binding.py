from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    TypeAlias,
    get_args,
    get_origin,
)

from ._annotations import _annotation_namespaces, _eval_annotation, _raw_annotations
from .decorator.lifecycle import _find_post_construct, _find_pre_destroy
from .descriptor import BindingDescriptor
from .exceptions import (
    ClassBindingNotDecoratedError,
    ProviderBindingNotDecoratedError,
    ScopeViolationDetectedError,
)
from .metadata import (
    DIMetadata,
    ProviderMetadata,
    RequiresMarker,
    Scope,
    _get_metadata,
    _get_profile_expressions,
    _get_provider_metadata,
    _get_requires_markers,
    _is_fallback,
)
from .utils import _closing_args, _is_generic_subtype, _open_type_params, _type_name

if TYPE_CHECKING:
    from .container import DIContainer


# ─────────────────────────────────────────────────────────────────
#  Binding — abstract base for all binding strategies
# ─────────────────────────────────────────────────────────────────


class Binding(ABC):
    """
    Abstract base for all binding types in the DI container.

    Maps an interface to a strategy for producing an instance of that type.
    Subclasses implement sync, async, and describe variants alongside a
    validation step.
    """

    @abstractmethod
    def validate(self, container: DIContainer) -> None:
        """
        Assert that this binding is resolvable within the given container.

        Args:
            container: The container to validate against.
        """
        ...

    @abstractmethod
    def create(self, container: DIContainer, *, requested: Any = None) -> Any:
        """
        Synchronously construct and return an instance for this binding.

        Args:
            container: The container used to resolve transitive dependencies.
            requested: The closed alias being resolved (e.g. ``Repo[User]``)
                — plan 016. Consulted ONLY by an open :class:`ProviderBinding`
                (``self.type_params`` non-empty), which needs it to fill its
                factory's ``type[T]`` parameter and to key its singleton
                cache per closed alias. Reserved (accepted and ignored) by
                every other binding kind, including :class:`ClassBinding` —
                keyword-only with a default so a third-party ``Binding``
                subclass that does not know about open generics still works
                unchanged.

        Returns:
            A fully constructed and injected instance of the bound type.
        """
        ...

    @abstractmethod
    async def acreate(self, container: DIContainer, *, requested: Any = None) -> Any:
        """
        Asynchronously construct and return an instance for this binding.

        Args:
            container: The container used to resolve transitive dependencies.
            requested: See :meth:`create` — identical contract, async mirror.

        Returns:
            A fully constructed and injected instance of the bound type.
        """
        ...

    @abstractmethod
    def describe(
        self,
        container: DIContainer,
        _visited: frozenset[type] | None = None,
    ) -> BindingDescriptor:
        """
        Build a recursive ``BindingDescriptor`` snapshot of this binding.

        Args:
            container: The container used to look up dependency bindings.
            _visited:  Internal cycle guard — do not pass from call sites.

        Returns:
            A fully populated ``BindingDescriptor`` for this binding and its
            entire dependency subtree.
        """
        ...


# ─────────────────────────────────────────────────────────────────
#  ClassBinding — constructor injection for decorated classes
# ─────────────────────────────────────────────────────────────────


class ClassBinding(Binding):
    """Binding that instantiates a concrete class via constructor injection.

    Reads DI metadata from the ``@Component`` / ``@Singleton`` decorator on
    *implementation* and stores lifecycle hooks discovered by MRO walk.

    Thread safety:  ✅ Safe — all attributes are set once in ``__init__``
                    and never mutated.  Caching lives in the container, not here.
    Async safety:   ✅ Safe — ``acreate`` is a coroutine; no shared async state.

    Edge cases:
        - ``interface == implementation`` is valid (self-registration via
          ``container.register()``).
        - If *implementation* has both a sync and async ``@PostConstruct``,
          ``_find_post_construct`` raises — only one hook is allowed.
        - ``pre_destroy`` is ``None`` for classes without ``@PreDestroy``.
    """

    def __init__(
        self,
        interface: type,
        implementation: type,
        *,
        exact_only: bool = False,
    ) -> None:
        """Create a class binding between *interface* and *implementation*.

        Validates the subclass relationship, reads DI metadata, and discovers
        lifecycle hooks. Raises immediately on misconfiguration so errors
        surface at registration time, not at resolution time.

        Args:
            interface:      The abstract type (or base class) the container will
                            resolve. Callers use this type in ``container.get()``.
            implementation: The concrete class to instantiate. Must be a
                            subclass of *interface* and decorated with
                            ``@Component`` or ``@Singleton``.
            exact_only:     When ``True`` this binding only participates in
                            lookups where the requested type is *exactly*
                            ``interface`` — it is excluded from supertype sweeps
                            such as ``get_all(BaseClass)``.  Set to ``True`` for
                            auto-generated self-bindings so that they do not
                            pollute ``get_all`` queries on parent interfaces.

        Returns:
            None

        Raises:
            TypeError: If *implementation* is not a subclass of *interface*.
            ClassBindingNotDecoratedError: If *implementation* has no DI
                metadata — i.e. it was not decorated with ``@Component`` or
                ``@Singleton``.

        Edge cases:
            - ``exact_only=True`` with ``interface == implementation`` is the
              canonical self-binding pattern — safe and expected.
            - ``exact_only=True`` with ``interface != implementation`` is valid
              but unusual; the binding will only be found by callers that request
              the concrete type directly, not the interface.
        """
        # DESIGN: use _is_generic_subtype instead of plain issubclass so that
        # parameterised interfaces like Repository[User] are accepted.
        # issubclass(UserRepository, Repository[User]) raises TypeError at runtime
        # because Python's issubclass does not accept generic aliases as the second
        # argument.  _is_generic_subtype extracts the origin type for the subclass
        # check and then validates the type args via an __orig_bases__ MRO walk.
        if not _is_generic_subtype(implementation, interface):
            raise TypeError(
                f"{implementation.__name__} must be a subclass of {_type_name(interface)}"
            )

        self.interface = interface
        self.implementation = implementation
        # Controls participation in supertype sweeps — see docstring above.
        self.exact_only = exact_only
        # DESIGN: resolved once at construction — _filter() runs per
        # resolution and must not re-read markers (plan 005 §Design).
        self.profiles: tuple[str, ...] = _get_profile_expressions(implementation)
        # DESIGN: same "resolved once at construction" rule as .profiles
        # above — _filter()'s fallback post-step reads .fallback on every
        # call and must never re-read the marker off the class itself
        # (plan 017 §Design, mirrors plan 015's .conditions comment below).
        self.fallback: bool = _is_fallback(implementation)
        # DESIGN: same "resolved once at construction" rule as .profiles
        # above — _binding_is_active() reads .conditions on every _filter()
        # call, and must never re-read markers off the class itself (plan
        # 015 §Design). The hot-path short-circuit is `not b.conditions`.
        self.conditions: tuple[RequiresMarker, ...] = _get_requires_markers(implementation)

        meta: DIMetadata | None = _get_metadata(implementation)
        if meta is None:
            raise ClassBindingNotDecoratedError(implementation)

        self.scope = meta.scope
        self.qualifier = meta.qualifier
        self.priority = meta.priority
        self.post_construct = _find_post_construct(implementation)
        self.pre_destroy = _find_pre_destroy(implementation)

    def __repr__(self) -> str:
        if self.qualifier:
            q_display = getattr(self.qualifier, "__name__", repr(self.qualifier))
            qualifier_part = f", qualifier={q_display}"
        else:
            qualifier_part = ""
        # _type_name handles both concrete types (__name__) and generic aliases (str())
        return (
            f"ClassBinding("
            f"{_type_name(self.interface)} → {self.implementation.__name__}, "
            f"scope={self.scope.name}"
            f"{qualifier_part})"
        )

    def validate(self, container: DIContainer) -> None:
        """Check this binding for scope leaks against the container's registry.

        Args:
            container: The container whose binding registry is searched for
                each dependency type declared in ``__init__``.

        Returns:
            None

        Raises:
            ScopeViolationDetectedError: If any direct dependency has a
                narrower scope than this binding (e.g. a ``SINGLETON`` depending
                on a ``REQUEST``-scoped component).
            LiveInjectionRequiredError: If a ``REQUEST``/``SESSION`` scoped
                dependency is injected without ``Live[T]``/``Instance[T]``.
            AnnotationResolutionError: If the implementation's annotations
                cannot be resolved by the container — validation cannot run
                for it, so it is reported as a failure rather than silently
                passed.
        """
        scope_violations = container._check_scope_violation(self)
        if scope_violations:
            raise ScopeViolationDetectedError(scope_violations=scope_violations)

    def create(self, container: DIContainer, *, requested: Any = None) -> Any:
        """Instantiate the implementation class synchronously via constructor injection.

        Args:
            container: The active ``DIContainer``, used to resolve every
                ``__init__`` parameter of :attr:`implementation`.
            requested: Reserved for open-generic ``ProviderBinding``s (plan
                016) — a ``ClassBinding`` never supports open aliases (see
                plan 016 §Non-goals: "Changing ClassBinding... — not this
                plan"), so this is accepted and silently ignored.

        Returns:
            A fully constructed instance of :attr:`implementation` with all
            dependencies injected and ``@PostConstruct`` invoked.

        Raises:
            RuntimeError: If ``@PostConstruct`` is ``async def`` — use
                :meth:`acreate` (via ``container.aget()``) instead.
            CircularDependencyError: If resolving this class would close
                a dependency cycle.
            LookupError: If any required ``__init__`` parameter has no binding.
        """
        instance = container._resolve_constructor(self.implementation)
        container._run_post_construct_sync(instance, self.post_construct)
        return instance

    async def acreate(self, container: DIContainer, *, requested: Any = None) -> Any:
        """Instantiate the implementation class asynchronously via constructor injection.

        Async mirror of :meth:`create`. Both sync and async ``@PostConstruct``
        hooks are handled — async hooks are awaited, sync hooks called normally.

        Args:
            container: The active ``DIContainer``, used to resolve every
                ``__init__`` parameter of :attr:`implementation`.
            requested: Reserved for open-generic ``ProviderBinding``s — see
                :meth:`create`. Ignored here.

        Returns:
            A fully constructed instance of :attr:`implementation` with all
            dependencies injected and ``@PostConstruct`` invoked.

        Raises:
            CircularDependencyError: If resolving this class would close
                a dependency cycle.
            LookupError: If any required ``__init__`` parameter has no binding.
        """
        instance = await container._resolve_constructor_async(self.implementation)
        await container._run_post_construct_async(instance, self.post_construct)
        return instance

    def describe(
        self,
        container: DIContainer,
        _visited: frozenset[type] | None = None,
    ) -> BindingDescriptor:
        """
        Build a full recursive ``BindingDescriptor`` for this binding.

        Args:
            container: The DI container — used to look up dependency bindings.
            _visited:  Internal cycle guard — do not pass from call sites.
                       Tracks interface types already on the current path.

        Returns:
            A fully annotated ``BindingDescriptor`` tree.

        Raises:
            RecursionError: If a circular dependency exists and the container
                            does not raise ``CircularDependencyError`` itself.

        Edge cases:
            - No dependencies → descriptor has empty ``dependencies`` tuple.
            - Dependency not registered → skipped with a sentinel descriptor
              showing ``[CYCLE DETECTED]``.
        """
        # ── Cycle guard ───────────────────────────────────────────────────────
        # Uses frozenset (immutable) so each recursive path is independent.
        visited = _visited or frozenset()
        if self.interface in visited:
            return BindingDescriptor(
                interface=f"{_type_name(self.interface)} [CYCLE DETECTED]",
                implementation="—",
                scope=self.scope,
            )

        visited = visited | {self.interface}

        dep_descriptors: list[BindingDescriptor] = [
            dep_binding.describe(container, _visited=visited)
            for dep_binding in container._get_dependencies(self)
        ]

        return BindingDescriptor(
            interface=_type_name(self.interface),
            implementation=self.implementation.__name__,
            scope=self.scope,
            qualifier=self.qualifier,
            dependencies=tuple(dep_descriptors),
        )


# ─────────────────────────────────────────────────────────────────
#  Return-annotation resolution helper
# ─────────────────────────────────────────────────────────────────


def _resolve_return_annotation(annotation: Any, fn: Callable[..., Any]) -> Any:
    """Evaluate a provider's return annotation **in isolation** and validate it.

    A provider binding needs exactly one thing from *fn*'s annotations: the
    return type, which becomes the binding's interface. Resolving it through
    the per-annotation primitive (rather than a whole-signature
    ``get_type_hints(fn)``) means a parameter whose type is unresolvable at
    registration time — a ``TYPE_CHECKING``-only import, a function-local
    class — cannot affect it. Parameter types are resolved later and lazily,
    by the container's ``_resolve_params``, against ``_build_localns()``.

    Args:
        annotation: The raw, unevaluated return annotation from
                    ``inspect.get_annotations(fn, eval_str=False)``. A ``str``
                    under PEP-563 (``from __future__ import annotations``),
                    a real object otherwise.
        fn:         The provider function. Supplies the globals namespace the
                    annotation is evaluated against, via
                    ``_annotation_namespaces`` (which also seeds any PEP-695
                    ``__type_params__``).

    Returns:
        The resolved type, or a parameterised generic alias such as
        ``Repository[User]``. ``Annotated[X, ...]`` is unwrapped to ``X``.

    Raises:
        TypeError: If the annotation cannot be evaluated (naming the provider,
            the annotation, and the fix), or if it evaluates to something that
            is not a type.

    Edge cases:
        - Quoted forward reference (``-> "Foo"``) and nested refs inside a
          generic (``-> Repository["User"]``) → handled by ``get_type_hints``
          inside ``_eval_annotation``; no manual re-evaluation loop needed.
        - Non-``str`` annotation (module without PEP-563) → passed through
          unchanged by ``_eval_annotation``, then validated identically.
        - Unresolvable name (function-local or ``TYPE_CHECKING``-only class)
          → ``TypeError``.
        - ``-> Annotated[Foo, ...]`` → unwrapped to ``Foo``, matching the
          ``include_extras=False`` behaviour callers relied on before.
    """
    # WHY: a binding interface must be a type. Registering an unresolved
    # ``str`` instead — which the original ``eval``-once code silently did —
    # corrupts every container path that reads ``interface.__name__`` and
    # surfaces as an unrelated failure at resolution time (see
    # tests/test_forward_ref_provider.py). Fail here, where we can name the
    # provider and the annotation.
    #
    # DESIGN: no container is available at registration time, so localns is
    # empty — only *fn*'s own module globals are in scope. That is exactly
    # the namespace the previous implementation used; the interface type must
    # be importable at module level regardless, since bindings outlive the
    # frame that declared them.
    globalns, localns = _annotation_namespaces(fn, {})

    # `get_type_hints` normalises a bare `None` annotation to `NoneType`; the
    # holder trick in `_eval_annotation` only sees strings, so replicate that
    # one conversion here for modules that do not use PEP-563.
    if annotation is None:
        return type(None)

    try:
        value = _eval_annotation(annotation, globalns, localns)
    except Exception as exc:
        raise TypeError(
            f"Provider '{fn.__name__}' declares an unresolvable return type "
            f"annotation {annotation!r}. Make the type importable at module "
            f"level (a TYPE_CHECKING-only or function-local class cannot be "
            f"resolved at runtime)."
        ) from exc

    # `_eval_annotation` always passes include_extras=True (the container's
    # parameter paths depend on the markers surviving). A return annotation
    # has no use for them — the qualifier comes from @Provider(qualifier=...),
    # never from the return type — so strip the wrapper to keep the interface
    # identical to what the whole-signature call used to produce.
    if get_origin(value) is Annotated:
        value = get_args(value)[0]

    # Concrete types and parameterised generic aliases (Repository[User], whose
    # get_origin is the generic class) are both legal interfaces; anything else
    # is not.
    if not isinstance(value, type) and get_origin(value) is None:
        raise TypeError(
            f"Provider '{fn.__name__}' return type annotation {annotation!r} "
            f"resolved to {value!r} ({type(value).__name__}), not a type."
        )

    return value


def _normalize_explicit_interface(returns: Any, fn: Callable[..., Any]) -> Any:
    """Resolve a ``returns=`` override into a concrete binding interface.

    Applied identically at both entry points that accept ``returns=``
    (``@Provider`` and ``container.provide()``) via ``ProviderBinding``, so
    the two call sites can never disagree about what a given override means.

    WHY type-before-callable: ``returns=SomeClass`` means "the interface is
    ``SomeClass``", never "call ``SomeClass()`` to obtain the interface" —
    even though classes are themselves callable. A factory that *returns a
    type object* is not something providify can bind anyway, so there is no
    ambiguity to preserve by calling it.

    WHY strings are rejected: accepting ``returns="Repository[User]"`` would
    re-introduce the namespace question (whose globals? the caller's frame?
    ``fn.__globals__``?) that this feature exists to let callers escape. The
    deferred-callable form (``returns=lambda: Repository[User]``) covers
    every case a string would, without the ambiguity.

    Args:
        returns: The raw value passed to ``returns=``. Resolved in this
            order:
            - a type (``isinstance(v, type)``) → used directly, never called.
            - ``Annotated[X, ...]`` → unwrapped to ``X``, then re-validated.
              Checked before the generic-alias case below because
              ``get_origin(Annotated[X, ...])`` is itself ``Annotated``
              (not ``None``), so it would otherwise be mistaken for an
              already-parameterised interface.
            - a parameterised generic alias (``get_origin(v) is not None``),
              e.g. ``Repository[User]`` → used directly.
            - any other callable (lambda, function, ``functools.partial``)
              → called with zero args; the result is re-run through this
              same table exactly once (no recursion into a callable that
              itself returns a callable).
            - a ``str`` → rejected; see WHY above.
            - anything else → rejected.
        fn: The provider function the override was attached to, used only to
            name the provider in error messages.

    Returns:
        A type or a parameterised generic alias, suitable to assign directly
        to ``ProviderBinding.interface``.

    Raises:
        TypeError: If *returns* is a ``str``; if the (possibly deferred)
            value is not a type or a parameterised generic alias; or if
            invoking a deferred callable raises (chained via ``from exc``).

    Edge cases:
        - ``returns=Repository`` (bare, unparameterised generic class) is
          legal — ``isinstance(..., type)`` is True — and binds under the
          unparameterised interface exactly as ``-> Repository`` would.
        - ``functools.partial`` is not a type and has no ``get_origin``, so
          it falls into the "other callable" branch and is invoked.
        - A deferred callable returning ``Annotated[X, ...]`` is unwrapped to
          ``X``, since the result is re-run through the full table.
        - A deferred callable returning another callable raises — the table
          is applied only once, deliberately, to keep the failure mode
          predictable rather than silently chasing an arbitrary call chain.
    """
    # str is rejected before the generic "any other callable" branch would
    # otherwise swallow it (str is not callable-as-a-zero-arg-factory in any
    # useful sense, but checking type/get_origin first keeps the ordering
    # explicit and matches the table in the plan verbatim).
    if isinstance(returns, str):
        raise TypeError(
            f"Provider '{fn.__name__}' was given returns={returns!r}, a str. "
            f"String forward refs are not accepted by returns= — use the "
            f"deferred-callable form instead, e.g. returns=lambda: {returns}."
        )

    if isinstance(returns, type):
        value = returns
    elif get_origin(returns) is Annotated:
        # Checked before the generic-alias branch below: Annotated[...]'s own
        # get_origin() is Annotated (not None), so it would otherwise be
        # mistaken for an already-parameterised interface like Repository[User].
        value = get_args(returns)[0]
    elif get_origin(returns) is not None:
        value = returns
    elif callable(returns):
        try:
            value = returns()
        except Exception as exc:
            raise TypeError(
                f"Provider '{fn.__name__}' was given a deferred returns= "
                f"callable that raised while resolving the interface."
            ) from exc
        # One level only: unwrap Annotated, but do not recurse into a
        # callable-returning-callable — see Edge cases above.
        if get_origin(value) is Annotated:
            value = get_args(value)[0]
    else:
        raise TypeError(
            f"Provider '{fn.__name__}' was given returns={returns!r} "
            f"({type(returns).__name__}), which is neither a type, a "
            f"parameterised generic alias, nor a zero-arg callable."
        )

    # Same final predicate as `_resolve_return_annotation` (binding.py:419) —
    # an explicitly given interface can never be weaker than a derived one,
    # so every downstream consumer sees exactly the same shape either way.
    if not isinstance(value, type) and get_origin(value) is None:
        raise TypeError(
            f"Provider '{fn.__name__}' returns={returns!r} resolved to "
            f"{value!r} ({type(value).__name__}), not a type."
        )

    return value


# ─────────────────────────────────────────────────────────────────
#  ProviderBinding — factory function injection
# ─────────────────────────────────────────────────────────────────


class ProviderBinding(Binding):
    """Binding that delegates instance creation to a plain function (factory).

    The function's return type annotation becomes the resolved interface.
    Supports both sync and async provider functions — async status is detected
    once at registration time via ``inspect.iscoroutinefunction``, not at
    each resolution.

    Thread safety:  ✅ Safe — all attributes are set once in ``__init__``
                    and never mutated.
    Async safety:   ✅ Safe — ``is_async`` is a plain bool set at init time;
                    no shared mutable state across tasks.

    Edge cases:
        - Provider with no return annotation → raises ``TypeError`` at
          registration, unless ``returns=`` is given.
        - Provider decorated ``@Provider(singleton=False)`` → ``Scope.DEPENDENT``,
          a new instance is created on every resolution.
        - Provider decorated ``@Provider(singleton=True)`` → ``Scope.SINGLETON``,
          the result is cached by the container after first call.
        - ``validate()`` is a no-op — provider functions have no constructor
          dependencies to inspect for scope leaks.

    Open generic bindings (plan 016):
        A ``returns=Repo[T]`` (or ``@Provider(returns=Repo[T])``, or a plain
        ``-> Repo[T]`` return annotation) whose alias args are ALL plain
        ``TypeVar``s registers an OPEN binding — one that serves ANY closed
        request sharing its origin (``get(Repo[User])``, ``get(Repo[Order])``,
        ...) via the "Autofac resolve-time factory" model: the factory is
        called once PER closed alias, receiving the closed type through a
        ``type[T]``-annotated parameter (matched by TypeVar NAME, not
        identity — see :meth:`type_args_for`). ``self.type_params`` is the
        non-empty marker of this; ``()`` for every other binding (concrete
        class, closed alias). See ``plans/016-open-generic-binding.md`` for
        the full eight-row semantics table.

        Edge cases:
            - A partially-open alias (``Repo[list[T]]``, ``Pair[str, T]``) —
              free type parameters that are not ALL plain TypeVars — raises
              ``TypeError`` here (via :func:`_open_type_params`), at
              registration, naming the alias: providify has no substitution
              mechanism for a mixed alias, so registering one would create a
              binding that can never match any request (plan 016 §Non-goals).
    """

    def __init__(self, fn: Callable[..., Any], *, returns: Any = None) -> None:
        """Create a provider binding from a decorated factory function.

        Reads ``ProviderMetadata`` from *fn*, resolves the interface, and
        detects whether the provider is async.

        Interface resolution priority (highest wins):
            1. ``returns`` — the keyword given directly to this constructor
               (i.e. ``container.provide(fn, returns=...)``'s call-site override).
            2. ``meta.returns`` — ``@Provider(returns=...)``'s decoration-time
               override.
            3. ``fn``'s resolved return annotation.

        When an override from level 1 or 2 is present, the return annotation
        is **not read, not evaluated, and not validated** — a caller who
        already told providify what the factory produces must not also have
        to repair an unrelated static annotation (``-> Any``, a
        ``TYPE_CHECKING``-only forward ref) to register.

        Args:
            fn: A callable decorated with ``@Provider``. May be a regular
                function or ``async def``. Must declare a return type
                annotation, unless an interface override is in effect.
            returns: Call-site interface override — see precedence above and
                ``_normalize_explicit_interface`` for the accepted shapes.

        Returns:
            None

        Raises:
            ProviderBindingNotDecoratedError: If *fn* has no ``ProviderMetadata``
                — i.e. it was not decorated with ``@Provider``.
            TypeError: If no interface override is in effect and *fn* has no
                return type annotation, since the return type is used as the
                resolved interface — or if that annotation is a string that
                cannot be resolved to a type (e.g. a quoted forward reference
                to a function-local or ``TYPE_CHECKING``-only class). Also
                raised by ``_normalize_explicit_interface`` when an override
                is present but cannot be resolved to a type or generic alias.
        """
        meta: ProviderMetadata | None = _get_provider_metadata(fn)
        if meta is None:
            # A call-site `returns=` fully determines the interface without
            # ever consulting metadata, so an undecorated function is legal
            # in that one case — the whole point of `returns=` on provide()
            # is registering a plain factory someone else wrote, which was
            # never decorated with `@Provider` and may not even be ours to
            # decorate. Absent that override, undecorated is still an error:
            # scope/qualifier/priority have nowhere else to come from.
            if returns is None:
                raise ProviderBindingNotDecoratedError(fn)
            meta = ProviderMetadata.default()

        # Call-site override wins over the decoration-time override, which
        # wins over the annotation. `None` means "not given" at every level
        # (see module docs for why `None` is safe as a sentinel here).
        override = returns if returns is not None else meta.returns
        if override is not None:
            interface = _normalize_explicit_interface(override, fn)
        else:
            # DESIGN: read the RAW return annotation and evaluate only it.
            #
            # ✅ A parameter whose type is unresolvable at registration time (a
            #    TYPE_CHECKING-only import, a class defined inside a test function)
            #    can no longer prevent the provider from being registered — the
            #    whole-signature get_type_hints(fn) it replaces was all-or-nothing,
            #    which is exactly why this method needed a try/except fallback.
            # ✅ One resolution path instead of two, so the happy path and the
            #    fallback can no longer disagree about what the interface is.
            # ❌ Parameter annotations are no longer validated at registration;
            #    they surface at resolution instead. That is deliberate and matches
            #    the container: parameters resolve against _build_localns(), which
            #    does not exist yet here, so registration-time validation of them
            #    was never trustworthy anyway.
            raw_annotations = _raw_annotations(fn)
            # Membership test, not `.get(...) is None`: `def p() -> None` in a module
            # WITHOUT PEP-563 yields the literal object `None` as its annotation, which
            # is a *declared* return type (get_type_hints normalises it to NoneType) and
            # must not be confused with "no return annotation at all".
            if "return" not in raw_annotations:
                raise TypeError(f"Provider '{fn.__name__}' must declare a return type hint.")
            interface = _resolve_return_annotation(raw_annotations["return"], fn)

        self.interface = interface
        self.fn = fn
        # Row 8 (plan 016) — computed ONCE at registration, never in
        # _filter() (same "resolve markers once" rule as .profiles below).
        # Raises TypeError for a partially-open alias (mixed TypeVar/concrete
        # args) — the ONE place this validation happens, so @Provider(returns=),
        # provide(returns=), and a bare `-> Repo[T]` return annotation can
        # never disagree about whether an interface is open, closed, or illegal.
        self.type_params: tuple[Any, ...] = _open_type_params(interface)
        # DESIGN: resolved once at construction — _filter() runs per
        # resolution and must not re-read markers (plan 005 §Design).
        self.profiles: tuple[str, ...] = _get_profile_expressions(fn)
        # DESIGN: same "resolved once at construction" rule as .profiles
        # above — _filter()'s fallback post-step reads .fallback on every
        # call and must never re-read the marker off fn itself (plan 017
        # §Design). `fn` may be a bound `@Configuration` method, so
        # `_is_fallback` falls back to `fn.__func__.__dict__`, matching how
        # `_get_profile_expressions` already reads bound methods above.
        self.fallback: bool = _is_fallback(fn)
        # DESIGN: same "resolved once at construction" rule as .profiles
        # above (plan 015 §Design) — _binding_is_active() reads .conditions
        # on every _filter() call.
        self.conditions: tuple[RequiresMarker, ...] = _get_requires_markers(fn)

        # Detect async at registration time — avoids repeated inspect calls
        # on every resolution. iscoroutinefunction is cheap but registrations
        # run once; resolutions run many times.
        self.is_async: bool = inspect.iscoroutinefunction(fn)

        # DESIGN: scope resolution priority — explicit scope wins, then
        # singleton flag, then DEPENDENT as the safe default.
        # REQUEST and SESSION scopes are now supported: the container's
        # existing _get_cache() routing handles all four scopes uniformly.
        # The provider factory runs once per scope lifetime and its result
        # is cached exactly like a ClassBinding — no extra logic needed.
        if meta.scope is not None:
            self.scope = meta.scope
        elif meta.singleton:
            self.scope = Scope.SINGLETON
        else:
            self.scope = Scope.DEPENDENT
        self.qualifier = meta.qualifier
        self.priority = meta.priority
        # Set by container.install() when a @Disposes method is found
        self.disposer: Callable[..., Any] | None = None

    def __repr__(self) -> str:
        if self.qualifier:
            q_display = getattr(self.qualifier, "__name__", repr(self.qualifier))
            qualifier_part = f", qualifier={q_display}"
        else:
            qualifier_part = ""
        async_part = ", async" if self.is_async else ""
        # plan 016: an open binding's interface already renders its TypeVars
        # via _type_name (e.g. "Repo[~T]"), but a debugger scanning a long
        # repr() list benefits from a keyword flag, same style as ", async".
        open_part = ", open" if self.type_params else ""
        # _type_name handles both concrete types (__name__) and generic aliases (str())
        return (
            f"ProviderBinding("
            f"{_type_name(self.interface)} ← {self.fn.__name__}, "
            f"scope={self.scope.name}"
            f"{qualifier_part}"
            f"{async_part}"
            f"{open_part})"
        )

    def type_args_for(self, requested: Any) -> dict[str, Any]:
        """Return ``{TypeVar.__name__: concrete}`` for closing *requested* — row 3.

        The bridge between the lookup-layer wildcard match (:func:`_closing_args`,
        keyed by ``TypeVar`` OBJECT) and the factory-parameter fill
        (``DIContainer._collect_kwargs_sync/_async``, matched by ``TypeVar``
        NAME) — see the module docstring's "Open generic bindings" section
        for why name, not identity, is the right key: a PEP 695
        ``def factory[T](entity: type[T])`` and a module-level
        ``returns=Repo[T]`` use two DIFFERENT ``T`` objects that share a name.

        Args:
            requested: The type or alias being resolved — e.g. ``Repo[User]``.

        Returns:
            ``{}`` when :attr:`type_params` is empty (every closed binding —
            the overwhelmingly common case costs one dict-construction and
            nothing else) or when *requested* does not close this binding's
            interface; otherwise ``{"T": User}`` etc., one entry per TypeVar
            in :attr:`type_params`.

        Edge cases:
            - Called on a closed binding → always ``{}`` — `_closing_args`
              returns ``None`` immediately (`_open_type_params` gate).
            - *requested* does not close this binding (wrong origin, bound
              violation, ...) → ``{}``, NOT an exception — the container's
              specificity/bound-as-non-match rules already decided this
              binding should not have been picked; a defensive empty dict
              here means a caller that got here anyway (a bug elsewhere)
              fails at the factory's own missing-parameter error, not here.

        Thread safety:  ✅ Pure function of ``self`` and *requested* — no
                        shared state.
        Async safety:   ✅ No awaits.
        """
        if not self.type_params:
            return {}
        mapping = _closing_args(self.interface, requested)
        if mapping is None:
            return {}
        return {tp.__name__: arg for tp, arg in mapping.items()}

    def validate(self, container: DIContainer) -> None:
        """Check this provider binding for scope leaks against the container.

        A ``@Provider(singleton=True)`` whose function parameters include a
        ``REQUEST`` or ``SESSION`` scoped dependency without ``Live[T]``
        wrapping will silently capture a stale instance across scope boundaries.
        This method detects that pattern and raises before any resolution occurs.

        Args:
            container: The container whose binding registry is searched for
                each parameter type declared in the provider function.

        Returns:
            None

        Raises:
            ScopeViolationDetectedError: If any parameter references a
                narrower-scoped binding in a non-safe way.
            LiveInjectionRequiredError: If any ``REQUEST`` or ``SESSION``
                scoped parameter is injected without ``Live[T]`` wrapping.
            AnnotationResolutionError: If the provider function's annotations
                cannot be resolved by the container — validation cannot run
                for it, so it is reported as a failure rather than silently
                passed.

        Edge cases:
            - ``DEPENDENT`` scoped providers → no-op (no stale-reference risk).
            - Provider with no parameters   → no-op (nothing to check).
        """
        scope_violations = container._check_provider_scope_violation(self)
        if scope_violations:
            raise ScopeViolationDetectedError(scope_violations=scope_violations)

    def create(self, container: DIContainer, *, requested: Any = None) -> Any:
        """Invoke the provider function synchronously with all dependencies injected.

        Args:
            container: The active ``DIContainer``, used to resolve every
                parameter of :attr:`fn`.
            requested: The closed alias being resolved (plan 016) — e.g.
                ``Repo[User]``. Consulted only when :attr:`type_params` is
                non-empty (an open binding): supplies the closed type
                argument the factory's ``type[T]`` parameter needs, and
                overrides the cycle-detection key (see :meth:`type_args_for`
                and the Raises entry below).

        Returns:
            The value returned by :attr:`fn`.

        Raises:
            RuntimeError: If :attr:`fn` is ``async def`` — the container guards
                against this before calling ``create()``; use :meth:`acreate`
                via ``container.aget()`` instead.
            CircularDependencyError: If the provider's return type is already
                on the resolution stack. For an open binding this uses the
                CLOSED alias (*requested*) as the cycle key, not the open
                interface — otherwise a factory for ``Repo[T]`` that
                legitimately resolves ``Repo[Order]`` while producing
                ``Repo[User]`` would trip a false cycle (both would push the
                same ``Repo[T]`` key).
            LookupError: If any required parameter of :attr:`fn` has no binding.
        """
        return container._call_provider(
            self.fn,
            type_args=self.type_args_for(requested),
            cycle_key=requested if self.type_params else None,
        )

    async def acreate(self, container: DIContainer, *, requested: Any = None) -> Any:
        """Invoke the provider function asynchronously with all dependencies injected.

        Handles both sync and async provider functions transparently — async
        providers are awaited, sync providers are called normally.

        Args:
            container: The active ``DIContainer``, used to resolve every
                parameter of :attr:`fn`.
            requested: See :meth:`create` — identical contract, async mirror.

        Returns:
            The value returned (or awaited) from :attr:`fn`.

        Raises:
            CircularDependencyError: If the provider's return type is already
                on the resolution stack — see :meth:`create`'s Raises entry
                for the open-binding cycle-key override.
            LookupError: If any required parameter of :attr:`fn` has no binding.
        """
        return await container._call_provider_async(
            self.fn,
            type_args=self.type_args_for(requested),
            cycle_key=requested if self.type_params else None,
        )

    def describe(
        self,
        container: DIContainer,
        _visited: frozenset[type] | None = None,
    ) -> BindingDescriptor:
        """
        Build a full recursive ``BindingDescriptor`` for this binding.

        Args:
            container: The DI container — used to look up dependency bindings.
            _visited:  Internal cycle guard — do not pass from call sites.

        Returns:
            A fully annotated ``BindingDescriptor`` tree.

        Edge cases:
            - No dependencies → descriptor has empty ``dependencies`` tuple.
        """
        # ── Cycle guard ───────────────────────────────────────────────────────
        visited = _visited or frozenset()
        if self.interface in visited:
            return BindingDescriptor(
                interface=f"{_type_name(self.interface)} [CYCLE DETECTED]",
                implementation="—",
                scope=self.scope,
            )

        visited = visited | {self.interface}

        dep_descriptors: list[BindingDescriptor] = [
            dep_binding.describe(container, _visited=visited)
            for dep_binding in container._get_dependencies(self)
        ]

        return BindingDescriptor(
            interface=_type_name(self.interface),
            implementation=self.fn.__name__,
            scope=self.scope,
            qualifier=self.qualifier,
            dependencies=tuple(dep_descriptors),
        )


# ─────────────────────────────────────────────────────────────────
#  AnyBinding — union type alias used throughout the container
#
#  DESIGN: union TypeAlias — the container only ever holds AnyBinding,
#  never bare concrete types. Adding a new binding strategy means
#  implementing Binding here only — DIContainer needs no changes.
#  TypeAlias makes this explicit to the type checker; without it, a
#  bare assignment looks like a runtime variable, not a type alias.
# ─────────────────────────────────────────────────────────────────

AnyBinding: TypeAlias = ClassBinding | ProviderBinding  # noqa: UP040
