from __future__ import annotations
from dataclasses import dataclass
from enum import Enum, auto
from typing import TypeVar, Type, Any

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
    binding: tuple[Type, Scope]
    reference: tuple[Type, Scope]


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

    binding: tuple[Type, Scope]
    dep: tuple[Type, Scope]
    # Tracks which parameter the violation came from — used in error messages
    # so developers can find the exact injection point without reading stack traces.
    param_name: str


_DI_METADATA_ATTR = "__di_metadata__"  # storage slot only — not a semantic key
_DI_PROVIDER_ATTR = "__di_provider__"  # storage slot only
_DI_CONFIGURATION_ATTR = "__di_module__"


class DIMetadata:
    """
    Holds all DI metadata for a decorated class.
    Stored directly on the class via __dict__ — picklable, GC-safe,
    multiprocess-safe, debuggable.

    The TYPE is the signal — not the attribute name:
        isinstance(meta, DIMetadata)   ✅ semantic check
        "__di_metadata__" in dict      ❌ string check — not needed
    """

    __slots__ = ("scope", "qualifier", "priority", "inherited")

    def __init__(
        self,
        scope: Scope,
        qualifier: str | None = None,
        priority: int = 0,
        inherited: bool = False,
    ) -> None:
        self.scope = scope
        self.qualifier = qualifier
        self.priority = priority
        self.inherited = inherited

    def merge(self, **updates: Any) -> DIMetadata:
        """Immutable merge — returns new instance with updated fields."""
        return DIMetadata(
            scope=updates.get("scope", self.scope),
            qualifier=updates.get("qualifier", self.qualifier),
            priority=updates.get("priority", self.priority),
            inherited=updates.get("inherited", self.inherited),
        )

    def __repr__(self) -> str:
        return (
            f"DIMetadata(scope={self.scope.name}, qualifier={self.qualifier!r}, "
            f"priority={self.priority}, inherited={self.inherited})"
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
        return cls(scope=Scope.DEPENDENT, qualifier=None, priority=0, inherited=False)


class ProviderMetadata:
    """
    Holds all DI metadata for a @Provider function.
    Stored directly on the function via __dict__ — same guarantees.

    Scope resolution priority (highest wins):
        1. ``scope`` — explicit Scope value, covers all four scopes
        2. ``singleton=True`` — shorthand for Scope.SINGLETON (backward compat)
        3. default — Scope.DEPENDENT (new instance on every resolution)
    """

    __slots__ = ("qualifier", "priority", "singleton", "is_async", "scope")

    def __init__(
        self,
        qualifier: str | None = None,
        priority: int = 0,
        singleton: bool = False,
        is_async: bool = False,
        # Explicit scope — when set, overrides singleton flag.
        # Allows @Provider to produce REQUEST or SESSION scoped values,
        # mirroring Jakarta CDI's @Produces @RequestScoped pattern.
        scope: Scope | None = None,
    ) -> None:
        self.qualifier = qualifier
        self.priority = priority
        self.singleton = singleton
        self.is_async = is_async
        self.scope = scope

    def merge(self, **updates: Any) -> ProviderMetadata:
        return ProviderMetadata(
            qualifier=updates.get("qualifier", self.qualifier),
            priority=updates.get("priority", self.priority),
            singleton=updates.get("singleton", self.singleton),
            is_async=updates.get("is_async", self.is_async),
            scope=updates.get("scope", self.scope),
        )

    def __repr__(self) -> str:
        return (
            f"ProviderMetadata(qualifier={self.qualifier!r}, "
            f"priority={self.priority}, singleton={self.singleton}, "
            f"scope={self.scope}, is_async={self.is_async})"
        )

    def __getstate__(self) -> dict[str, Any]:
        return {s: getattr(self, s) for s in self.__slots__}

    def __setstate__(self, state: dict[str, Any]) -> None:
        for key, val in state.items():
            object.__setattr__(self, key, val)

    @classmethod
    def default(cls) -> ProviderMetadata:
        """Factory method for default metadata values."""
        return cls(qualifier=None, priority=0, singleton=False, is_async=False)


class ConfigurationMetadata:
    """
    Holds all DI metadata for a @Configuration class.
    Stored directly on the function via __dict__ — same guarantees.
    """

    __slots__ = ()


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
