from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, TypeVar

T = TypeVar("T")


class Scope(Enum):
    """
    Lifecycle scopes for DI-managed components.
    Mirrors Jakarta CDI's built-in scopes.

    DEPENDENT:    New instance every resolution   → @Component
    SINGLETON:    One instance for the entire app → @Singleton
    REQUEST:      One instance per active request → @RequestScoped
    SESSION:      One instance per active session → @SessionScoped
    """

    DEPENDENT = auto()  # default — new instance each time
    SINGLETON = auto()  # one instance ever
    REQUEST = auto()  # one instance per request context
    SESSION = auto()  # one instance per session context

    def scope_rank(self) -> int:
        """Helper method to get scope rank for comparison."""
        return _scope_rank(self)


_SCOPE_RANK = {
    Scope.SINGLETON: 1,
    Scope.SESSION: 2,
    Scope.REQUEST: 3,
    Scope.DEPENDENT: 4,
}


def _scope_rank(scope: Scope) -> int:
    return _SCOPE_RANK[scope]


@dataclass(frozen=True)
class ScopeLeak:
    binding: tuple[type, Scope]
    reference: tuple[type, Scope]


@dataclass(frozen=True)
class LiveInjectionViolation:
    """Records a single case where a REQUEST/SESSION dep was not wrapped in Live[T].

    Produced by the container's scope-violation check when a longer-lived
    component (e.g. SINGLETON) injects a REQUEST or SESSION scoped dep via
    Inject[T], Lazy[T], or a bare type annotation — all of which capture a
    single instance at construction time and become stale across scope boundaries.

    Attributes:
        binding:    (owning class, its scope) — the component that declared the dep.
        dep:        (dep type, dep scope)      — the scoped dependency being injected.
        param_name: Constructor parameter name where the violation occurred.
    """

    binding: tuple[type, Scope]
    dep: tuple[type, Scope]
    # Tracks which parameter the violation came from — used in error messages
    # so developers can find the exact injection point without reading stack traces.
    param_name: str


_DI_METADATA_ATTR = "__di_metadata__"  # storage slot only — not a semantic key
_DI_PROVIDER_ATTR = "__di_provider__"  # storage slot only
_DI_CONFIGURATION_ATTR = "__di_module__"

# ── @ConfigProperties — env/YAML/JSON/TOML → typed object marker ─
_CONFIG_PROPERTIES_ATTR = "__di_config_properties__"

# ── @Qualifier — typed qualifier annotation marker ────────────────
_QUALIFIER_MARKER_ATTR = "__di_qualifier_marker__"

# ── @Alternative — deployment-time bean replacement marker ───────
_ALTERNATIVE_ATTR = "__di_alternative__"

# ── @Profile — deployment-time profile activation marker ─────────
_PROFILE_ATTR = "__di_profile__"

# ── @Decorator — bean decorator marker ───────────────────────────
_DECORATOR_ATTR = "__di_decorator__"

# ── @Stereotype — composed annotation bundle marker ──────────────
_STEREOTYPE_ATTR = "__di_stereotype__"


class QualifierMarker:
    """Stamps a class as a CDI-style qualifier annotation (@Qualifier parity)."""

    __slots__ = ()


class AlternativeMarker:
    """Stamps a class as a deployment-time alternative bean (@Alternative parity)."""

    __slots__ = ()


class DecoratorMarker:
    """Stamps a class as a CDI-style bean decorator (@Decorator parity)."""

    __slots__ = ()


@dataclass(frozen=True)
class StereotypeMetadata:
    """Holds composed annotation metadata for a @Stereotype class."""

    scope: Any = (
        None  # Scope — None means DEPENDENT (resolved lazily to avoid forward ref)
    )
    qualifier: Any = None  # str | type | None
    priority: int = 0
    inherited: bool = False

    def resolved_scope(self) -> Scope:
        """Return the effective scope, defaulting to DEPENDENT."""
        return self.scope if self.scope is not None else Scope.DEPENDENT


@dataclass(frozen=True)
class ConfigPropertiesMetadata:
    """Holds the prefix/sources stamped by `@ConfigProperties(...)`.

    Stored directly on the class's own `__dict__` via `_CONFIG_PROPERTIES_ATTR`
    — same storage guarantees as every other marker in this module (picklable,
    GC-safe, multiprocess-safe, debuggable via plain attribute access).

    `sources` is typed `tuple[Any, ...]` rather than `tuple[ConfigSource, ...]`
    deliberately — importing `providify.config.ConfigSource` here would give
    `metadata.py` a dependency on `config.py`, which `config.py` itself must
    never have on `container.py`/`binding.py` (the module-isolation rule in
    plan 006 §Design/Module layout). This is the same dodge already used for
    `StereotypeMetadata.scope` above (`Any` instead of `Scope`, avoiding a
    forward reference some callers would need to resolve eagerly).

    Thread safety: Frozen dataclass — safe to share across threads once built.
    Async safety:  Pure data — no shared mutable state.

    Attributes:
        prefix: Normalised (lower-cased, stripped) prefix selecting a subtree
            of the merged configuration mapping, or `None` to bind the whole
            mapping.
        sources: The `ConfigSource` instances to load, in order — later
            sources win on key conflicts (deep merge, later wins).
    """

    prefix: str | None
    sources: tuple[Any, ...]


@dataclass(frozen=True)
class ProfileMetadata:
    """Holds the profile activation expressions stamped by `@Profile(...)`.

    Stored directly on the class/function's `__dict__` via `_PROFILE_ATTR` —
    same storage guarantees as every other marker in this module (picklable,
    GC-safe, multiprocess-safe, debuggable via plain attribute access).

    The TYPE is the signal, not the attribute name — `isinstance(meta,
    ProfileMetadata)` is the only valid membership check, matching
    `StereotypeMetadata` and `DIMetadata` above.

    A plain `tuple[str, ...]` (not a richer structure) is deliberately the
    binding-level cache (`ClassBinding.profiles` / `ProviderBinding.profiles`
    — see `binding.py`) rather than this dataclass, because `_filter()`
    would otherwise dereference `.expressions` on every resolution's hot
    path for no additional information the tuple doesn't already carry
    (plan 005 §Design, "Alternatives considered").

    Thread safety: Frozen dataclass — safe to share across threads once built.
    Async safety:  Pure data — no shared mutable state.

    Attributes:
        expressions: Normalised (stripped, lower-cased) profile literals, in
            declaration order, `"!"`-prefixed for negation. Empty only
            transiently — `Profile()` with no arguments raises before a
            `ProfileMetadata` with an empty tuple is ever constructed.
    """

    expressions: tuple[str, ...]


class DIMetadata:
    """
    Holds all DI metadata for a decorated class.
    Stored directly on the class via __dict__ — picklable, GC-safe,
    multiprocess-safe, debuggable.

    The TYPE is the signal — not the attribute name:
        isinstance(meta, DIMetadata)   ✅ semantic check
        "__di_metadata__" in dict      ❌ string check — not needed
    """

    __slots__ = ("scope", "qualifier", "priority", "inherited", "track")

    def __init__(
        self,
        scope: Scope,
        qualifier: str | type | None = None,
        priority: int = 0,
        inherited: bool = False,
        track: bool = False,
    ) -> None:
        self.scope = scope
        self.qualifier = qualifier
        self.priority = priority
        self.inherited = inherited
        self.track = track

    def merge(self, **updates: Any) -> DIMetadata:
        """Immutable merge — returns new instance with updated fields."""
        return DIMetadata(
            scope=updates.get("scope", self.scope),
            qualifier=updates.get("qualifier", self.qualifier),
            priority=updates.get("priority", self.priority),
            inherited=updates.get("inherited", self.inherited),
            track=updates.get("track", self.track),
        )

    def __repr__(self) -> str:
        qualifier_display = getattr(self.qualifier, "__name__", repr(self.qualifier))
        return (
            f"DIMetadata(scope={self.scope.name}, qualifier={qualifier_display}, "
            f"priority={self.priority}, inherited={self.inherited}, track={self.track})"
        )

    # ── Pickle support — explicit for clarity ─────────────────────
    def __getstate__(self) -> dict[str, Any]:
        return {s: getattr(self, s) for s in self.__slots__}

    def __setstate__(self, state: dict[str, Any]) -> None:
        for key, val in state.items():
            object.__setattr__(self, key, val)

    @classmethod
    def default(cls) -> DIMetadata:
        """Factory method for default metadata values."""
        return cls(
            scope=Scope.DEPENDENT,
            qualifier=None,
            priority=0,
            inherited=False,
            track=False,
        )


class ProviderMetadata:
    """
    Holds all DI metadata for a @Provider function.
    Stored directly on the function via __dict__ — same guarantees.

    Scope resolution priority (highest wins):
        1. ``scope`` — explicit Scope value, covers all four scopes
        2. ``singleton=True`` — shorthand for Scope.SINGLETON (backward compat)
        3. default — Scope.DEPENDENT (new instance on every resolution)
    """

    __slots__ = ("qualifier", "priority", "singleton", "is_async", "scope", "returns")

    def __init__(
        self,
        qualifier: str | type | None = None,
        priority: int = 0,
        singleton: bool = False,
        is_async: bool = False,
        # Explicit scope — when set, overrides singleton flag.
        # Allows @Provider to produce REQUEST or SESSION scoped values,
        # mirroring Jakarta CDI's @Produces @RequestScoped pattern.
        scope: Scope | None = None,
        # Explicit interface override (see `binding._normalize_explicit_interface`
        # for the full accepted-shapes table: a type, a parameterised generic
        # alias, an ``Annotated[...]`` wrapper, or a zero-arg callable deferred
        # until `ProviderBinding` construction). Stored raw and unvalidated here
        # — validation happens exactly once, at `ProviderBinding.__init__`, so a
        # deferred callable is never invoked merely by decorating a function.
        returns: Any = None,
    ) -> None:
        self.qualifier = qualifier
        self.priority = priority
        self.singleton = singleton
        self.is_async = is_async
        self.scope = scope
        self.returns = returns

    def merge(self, **updates: Any) -> ProviderMetadata:
        return ProviderMetadata(
            qualifier=updates.get("qualifier", self.qualifier),  # type: ignore[arg-type]
            priority=updates.get("priority", self.priority),
            singleton=updates.get("singleton", self.singleton),
            is_async=updates.get("is_async", self.is_async),
            scope=updates.get("scope", self.scope),
            returns=updates.get("returns", self.returns),
        )

    def __repr__(self) -> str:
        return (
            f"ProviderMetadata(qualifier={self.qualifier!r}, "
            f"priority={self.priority}, singleton={self.singleton}, "
            f"scope={self.scope}, is_async={self.is_async}, "
            f"returns={self.returns!r})"
        )

    def __getstate__(self) -> dict[str, Any]:
        return {s: getattr(self, s) for s in self.__slots__}

    def __setstate__(self, state: dict[str, Any]) -> None:
        # Defensive: a pickle written by <=1.1.0 has no "returns" key (the slot
        # did not exist yet). Seed every slot missing from `state` with the
        # constructor default instead of leaving it unset, which would raise
        # AttributeError on first read rather than at unpickle time.
        defaults = ProviderMetadata()
        for slot in self.__slots__:
            object.__setattr__(self, slot, state.get(slot, getattr(defaults, slot)))

    @classmethod
    def default(cls) -> ProviderMetadata:
        """Factory method for default metadata values."""
        return cls(qualifier=None, priority=0, singleton=False, is_async=False)


class ConfigurationMetadata:
    """
    Holds all DI metadata for a @Configuration class.
    Stored directly on the class via __dict__ — same guarantees.

    Attributes:
        depends_on: Other ``@Configuration`` classes that must be installed
            before this one — read by ``providify.modules.module_dependencies``
            to build the install-order DAG (plan 008/F5). Empty tuple means
            "no explicit ordering constraint" — the module can install at any
            point relative to others (subject to whatever else pulls it in
            transitively). Defaults to ``()`` so every pre-plan-008
            ``ConfigurationMetadata()`` construction site (bare/parens-less
            ``@Configuration`` usage) stays valid without modification.
    """

    __slots__ = ("depends_on",)

    def __init__(self, depends_on: tuple[type, ...] = ()) -> None:
        self.depends_on = depends_on


# ─────────────────────────────────────────────────────────────────
#  Accessors — all go through these, never raw __dict__ access
# ─────────────────────────────────────────────────────────────────


def _has_configuration_module(cls: type) -> bool:
    """Return True if *cls* was decorated with @Configuration.

    Uses own __dict__ only — does not walk MRO — so subclasses of a
    @Configuration class are not treated as modules themselves.
    """
    return bool(_get_configuration_module(cls))


def _get_configuration_module(cls: type) -> ConfigurationMetadata | None:
    val = cls.__dict__.get(_DI_CONFIGURATION_ATTR)
    return val if isinstance(val, ConfigurationMetadata) else None


def _has_config_properties(cls: type) -> bool:
    """Return True if *cls* was decorated with @ConfigProperties.

    Uses own __dict__ only — does not walk MRO — so a subclass of a
    @ConfigProperties class carries no marker and must re-declare, matching
    `_is_alternative`'s non-inheriting `__dict__` lookup (`:448-450`).
    """
    return isinstance(
        cls.__dict__.get(_CONFIG_PROPERTIES_ATTR), ConfigPropertiesMetadata
    )


def _get_config_properties(cls: type) -> ConfigPropertiesMetadata:
    """Read `ConfigPropertiesMetadata` from *cls*'s own `__dict__`.

    Unlike most `_get_*` accessors in this module (which return `None` for an
    unmarked object), this one raises — `bind_config()`'s whole contract is
    "the class must be `@ConfigProperties`-decorated", and a `TypeError` here
    is what makes that guard a one-line call rather than a
    `None`-check-then-raise at every call site (mirrors `register()`'s guard
    at `container.py:720-721`).

    Args:
        cls: The class to inspect.

    Returns:
        The `ConfigPropertiesMetadata` stamped by `@ConfigProperties`.

    Raises:
        TypeError: If *cls* (in its own `__dict__`, non-inherited) carries no
            `@ConfigProperties` marker.
    """
    val = cls.__dict__.get(_CONFIG_PROPERTIES_ATTR)
    if not isinstance(val, ConfigPropertiesMetadata):
        raise TypeError(f"{cls.__name__} is not decorated with @ConfigProperties.")
    return val


def _get_own_metadata(cls: type) -> DIMetadata | None:
    """
    Reads DIMetadata from a class's OWN __dict__ only.
    Never walks MRO — use _get_metadata() for inherited lookup.

    isinstance() is the signal — a dict or any other type is ignored.
    """
    val = cls.__dict__.get(_DI_METADATA_ATTR)
    # ✅ isinstance — type is the signal, not the attribute name
    return val if isinstance(val, DIMetadata) else None


def _has_own_metadata(cls: type) -> bool:
    """Checks if a class has its own
    DIMetadata without walking MRO."""
    return _get_own_metadata(cls) is not None


def _get_metadata(cls: type) -> DIMetadata | None:
    """
    Reads DIMetadata from a class or its parents (if inherited=True).
    Own metadata always wins over inherited.
    """
    # Own metadata — highest priority
    meta = _get_own_metadata(cls)
    if meta is not None:
        return meta

    # Walk MRO for opt-in inherited parent
    for base in cls.__mro__[1:]:
        meta = _get_own_metadata(base)
        if meta is not None and meta.inherited:
            return meta

    return None


def _has_metadata(cls: type) -> bool:
    """Checks if a class or its parents (if inherited=True) have DIMetadata."""
    return _get_metadata(cls) is not None


def _set_metadata(cls: type, meta: DIMetadata) -> None:
    """
    Stamps DIMetadata onto a class's own __dict__.
    Only entry point for writing class metadata.
    """
    # type: ignore needed — __dict__ is a mappingproxy on classes
    # vars() gives us the actual dict for writing
    setattr(cls, _DI_METADATA_ATTR, meta)  # type: ignore[index]


def _get_provider_metadata(fn: Any) -> ProviderMetadata | None:
    """
    Reads ProviderMetadata from a provider function or bound method.
    isinstance() is the signal — raw dicts are ignored.

    For bound methods (from @Configuration classes), metadata lives on
    fn.__func__.__dict__ because bound method objects have an empty __dict__.

    Both __dict__ accesses use getattr(..., None) as a safety guard.
    C-level callables encountered while walking the MRO (e.g. object.__new__,
    classmethod_descriptors from vars(object)) are builtin_function_or_method
    objects that do NOT expose __dict__ — a direct access raises AttributeError.
    The same risk applies to fn.__func__ if it resolves to a C-level function.
    """
    # Guard: C-level callables in vars(object) / vars(type) have no __dict__
    d = getattr(fn, "__dict__", None)
    val = d.get(_DI_PROVIDER_ATTR) if d is not None else None

    if val is None:
        # Bound method — metadata lives on __func__, not on the method object.
        # Also guard __func__.__dict__: a classmethod wrapping a C function
        # would have __func__ pointing to a builtin with no __dict__.
        func = getattr(fn, "__func__", None)
        if func is not None:
            fd = getattr(func, "__dict__", None)
            if fd is not None:
                val = fd.get(_DI_PROVIDER_ATTR)

    return val if isinstance(val, ProviderMetadata) else None


def _has_provider_metadata(fn: Any) -> bool:
    """Checks if a function has ProviderMetadata."""
    return _get_provider_metadata(fn) is not None


def _set_provider_metadata(fn: Any, meta: ProviderMetadata) -> None:
    """Stamps ProviderMetadata onto a provider function."""
    setattr(fn, _DI_PROVIDER_ATTR, meta)


def _is_decorated(obj: Any) -> bool:
    """
    Checks if a class or function has valid DI metadata.
    isinstance() is the signal — raw dicts are treated as undecorated.
    """
    if isinstance(obj, type):
        return _get_own_metadata(obj) is not None
    if callable(obj):
        return _get_provider_metadata(obj) is not None
    return False


# ─────────────────────────────────────────────────────────────────
#  Qualifier marker helpers
# ─────────────────────────────────────────────────────────────────


def _is_qualifier_annotation(cls: type) -> bool:
    """Return True if *cls* was decorated with @Qualifier."""
    return isinstance(cls.__dict__.get(_QUALIFIER_MARKER_ATTR), QualifierMarker)


def _set_qualifier_marker(cls: type) -> None:
    """Stamp *cls* as a custom qualifier annotation."""
    setattr(cls, _QUALIFIER_MARKER_ATTR, QualifierMarker())


# ─────────────────────────────────────────────────────────────────
#  Alternative marker helpers
# ─────────────────────────────────────────────────────────────────


def _is_alternative(cls: type) -> bool:
    """Return True if *cls* was decorated with @Alternative."""
    return isinstance(cls.__dict__.get(_ALTERNATIVE_ATTR), AlternativeMarker)


def _set_alternative_marker(cls: type) -> None:
    """Stamp *cls* as an alternative bean."""
    setattr(cls, _ALTERNATIVE_ATTR, AlternativeMarker())


# ─────────────────────────────────────────────────────────────────
#  Profile marker helpers
# ─────────────────────────────────────────────────────────────────


def _get_profile_metadata(obj: Any) -> ProfileMetadata | None:
    """Read `ProfileMetadata` from *obj*'s own `__dict__`, never the MRO.

    One helper covers every shape `@Profile` can be applied to (plan 005
    step 4): a class (`__dict__` is a `mappingproxy`, read-only but
    readable), a plain module-level function, and a bound method from a
    `@Configuration` class — bound method objects have an empty `__dict__`
    of their own, so the marker is looked up on `__func__.__dict__` instead,
    mirroring `_get_provider_metadata`'s bound-method fallback above.

    Deliberately does **not** walk `cls.__mro__` — a subclass of a
    `@Profile`d class must re-declare its own expressions (plan 005 §Edge
    cases), matching `_is_alternative`'s non-inheriting `__dict__` lookup.

    Args:
        obj: A class, function, or bound method to inspect. Any object
            without a `__dict__` (e.g. a C-level builtin) is treated as
            unmarked rather than raising.

    Returns:
        The `ProfileMetadata` stamped by `@Profile`, or `None` if *obj*
        (or its own `__dict__`, non-inherited) carries no marker.

    Thread safety: Pure read — safe to call from any thread.
    Async safety:  Pure read — no await points.

    Example:
        >>> _get_profile_metadata(Foo)  # not decorated
        None
    """
    d = getattr(obj, "__dict__", None)
    val = d.get(_PROFILE_ATTR) if d is not None else None

    if val is None:
        # Bound method — marker lives on __func__, not on the method object
        # (bound methods do not own a __dict__ of their own).
        func = getattr(obj, "__func__", None)
        if func is not None:
            fd = getattr(func, "__dict__", None)
            if fd is not None:
                val = fd.get(_PROFILE_ATTR)

    return val if isinstance(val, ProfileMetadata) else None


def _get_profile_expressions(obj: Any) -> tuple[str, ...]:
    """Return *obj*'s `@Profile` expressions, or `()` if unmarked.

    Thin convenience wrapper around `_get_profile_metadata` — the shape
    every caller actually wants (`ClassBinding.profiles` /
    `ProviderBinding.profiles`, `_binding_is_active`'s consumers never need
    the wrapping dataclass, only the tuple).

    Args:
        obj: A class, function, or bound method to inspect.

    Returns:
        `()` for an unprofiled object; otherwise the normalised expression
        tuple exactly as stamped by `@Profile(...)`.

    Thread safety: Pure read — safe to call from any thread.
    Async safety:  Pure read — no await points.

    Example:
        >>> _get_profile_expressions(SomeUnmarkedClass)
        ()
    """
    meta = _get_profile_metadata(obj)
    return meta.expressions if meta is not None else ()


def _set_profile_marker(obj: Any, expressions: tuple[str, ...]) -> None:
    """Stamp `ProfileMetadata(expressions)` onto *obj*'s own `__dict__`.

    Sole write path for the profile marker — mirrors `_set_alternative_marker`
    and `_set_metadata` above ("only entry point for writing"). Callers
    (`@Profile` in `decorator/scope.py`) are responsible for normalising and
    validating *expressions* (and merging with any pre-existing marker)
    before calling this function — it performs no validation of its own.

    Args:
        obj: A class or function to stamp. `setattr` works uniformly for
            both — classes and functions both support arbitrary attribute
            assignment.
        expressions: The final (already normalised/merged) expression tuple
            to store.

    Returns:
        None

    Thread safety: ⚠️ Not synchronized — stamping the same object from two
        threads concurrently is a plain last-write-wins race, identical to
        every other marker setter in this module. Decoration happens at
        import time, on a single thread, in every documented usage.
    Async safety:  Same caveat as thread safety — no async-specific concern
        beyond the general one above.
    """
    setattr(obj, _PROFILE_ATTR, ProfileMetadata(expressions))


# ─────────────────────────────────────────────────────────────────
#  Decorator bean marker helpers
# ─────────────────────────────────────────────────────────────────


def _is_decorator_bean(cls: type) -> bool:
    """Return True if *cls* was decorated with @Decorator."""
    return isinstance(cls.__dict__.get(_DECORATOR_ATTR), DecoratorMarker)


def _set_decorator_marker(cls: type) -> None:
    """Stamp *cls* as a bean decorator."""
    setattr(cls, _DECORATOR_ATTR, DecoratorMarker())


# ─────────────────────────────────────────────────────────────────
#  Stereotype marker helpers
# ─────────────────────────────────────────────────────────────────


def _get_stereotype(cls: type) -> StereotypeMetadata | None:
    """Return the StereotypeMetadata if *cls* was decorated with @Stereotype."""
    val = cls.__dict__.get(_STEREOTYPE_ATTR)
    return val if isinstance(val, StereotypeMetadata) else None


def _set_stereotype(cls: type, meta: StereotypeMetadata) -> None:
    """Stamp a StereotypeMetadata onto *cls*."""
    setattr(cls, _STEREOTYPE_ATTR, meta)


def _is_scope_leak(parent_scope: Scope, dep_scope: Scope) -> bool:
    """
    Return True when a dependency is shorter-lived than its parent.

    A longer-lived binding (e.g. SINGLETON) holding a reference to a
    shorter-lived one (e.g. TRANSIENT) is a scope leak — the shorter-lived
    instance gets effectively promoted to the parent's longer lifetime.

    Args:
        parent_scope: Scope of the binding that declares the dependency.
        dep_scope:    Scope of the dependency being injected.

    Returns:
        True if ``dep_scope`` is shorter-lived than ``parent_scope``.

    Edge cases:
        - Equal scopes → False (not a leak).
        - dep_scope > parent_scope → False (dep outlives parent, safe).
    """
    # SINGLETON=1, SESSION=2, REQUEST=3, DEPENDENT=4 (higher rank = shorter-lived).
    # A leak occurs when the dep is shorter-lived (higher rank) than the parent.
    # Using > because: SINGLETON(1) parent + DEPENDENT(4) dep → 4 > 1 → True ✅
    # The previous < was inverted — it flagged dep-outlives-parent as a leak instead.
    return dep_scope.scope_rank() > parent_scope.scope_rank()
