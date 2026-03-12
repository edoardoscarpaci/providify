from __future__ import annotations

# ── Generic type utilities ─────────────────────────────────────────────────
#
# Python's typing system distinguishes between:
#   - concrete types    (e.g. UserRepository)      → isinstance(x, type) is True
#   - generic aliases   (e.g. Repository[User])    → isinstance(x, type) is False
#                                                     get_origin(x) returns the origin
#
# The DI container needs to handle both because users annotate constructor
# parameters with parameterized generics (Repository[User]) and the container
# must find the correct binding (UserRepository which extends Repository[User]).
#
# DESIGN: Three functions cover the whole problem:
#   _type_name        — safe display name, works for both forms
#   _is_generic_subtype — "does this concrete class satisfy a generic interface?"
#   _interface_matches  — "does this binding's interface satisfy a request?"
#
# Tradeoffs:
#   ✅ No third-party dependency — pure stdlib typing introspection
#   ✅ Handles nested generics correctly via __orig_bases__ MRO walk
#   ❌ Type args are compared with == (exact match) — no covariance/contravariance
#      e.g. Repository[Dog] does NOT match Repository[Animal] even if Dog ⊆ Animal
#   ❌ TypeVar-parameterised aliases (e.g. Repository[T]) won't match concrete
#      requests — TypeVars are resolved at class-definition time, not here

from typing import Any, get_args, get_origin


def _type_name(tp: Any) -> str:
    """Return a human-readable name for any type, including generic aliases.

    Handles both concrete types (``UserRepository``) and generic aliases
    (``Repository[User]``).

    DESIGN: uses ``isinstance(tp, type)`` rather than ``hasattr(tp, "__name__")``.
    ``_GenericAlias`` (the runtime form of ``Repository[User]``) is NOT a ``type``
    instance, but it does expose ``__name__`` via ``__getattr__`` delegation to its
    origin type — meaning ``hasattr(Repo[Item], "__name__")`` is True and
    ``Repo[Item].__name__`` returns ``"Repo"`` instead of ``"Repo[Item]"``.
    Checking ``isinstance(tp, type)`` avoids this false positive.

    Args:
        tp: Any type object — concrete type, generic alias, or anything else.

    Returns:
        ``tp.__name__`` for concrete types; ``str(tp)`` for everything else.

    Edge cases:
        - Concrete type with ``__name__``         → returns ``__name__``
        - Generic alias like ``Repository[User]`` → returns ``str(tp)`` in full
        - ``None`` or other non-types             → returns ``str(tp)``

    Thread safety:  ✅ Pure function — reads only, no shared state.
    Async safety:   ✅ No awaits, no shared mutable state.

    Example:
        _type_name(UserRepository)   → "UserRepository"
        _type_name(Repository[User]) → "Repository[User]"
    """
    # Only concrete types (isinstance(tp, type) is True) have a meaningful
    # __name__.  Generic aliases delegate __name__ via __getattr__ to their
    # origin, which would give us "Repo" instead of "Repo[Item]".
    if isinstance(tp, type):
        return tp.__name__
    return str(tp)


def _is_generic_subtype(implementation: type, interface: Any) -> bool:
    """Return True if *implementation* satisfies the parameterised *interface*.

    Walks the entire MRO of *implementation* (via ``__orig_bases__``) looking
    for a base whose origin and type-args exactly match *interface*.

    For non-generic *interface* this degrades to a plain ``issubclass`` check,
    so callers do not need a separate code path.

    Args:
        implementation: A concrete class (must be a real ``type``).
        interface:      Either a concrete type or a parameterised generic alias
                        such as ``Repository[User]``.

    Returns:
        True if *implementation* directly or indirectly extends *interface*.

    Raises:
        TypeError: If ``issubclass`` raises (e.g. *implementation* is not a class).

    Edge cases:
        - interface is not generic (no get_origin)    → plain issubclass
        - implementation not subclass of origin type  → False immediately
        - type args mismatch                          → False
        - generic base defined on a parent class      → found via MRO walk ✅
        - partial parameterisation (TypeVars remain)  → compared literally;
          TypeVar('T') != User, so it won't match     ⚠️

    Example:
        class Repository(Generic[T]): ...
        class UserRepository(Repository[User]): ...

        _is_generic_subtype(UserRepository, Repository[User])  → True
        _is_generic_subtype(UserRepository, Repository[int])   → False
    """
    origin = get_origin(interface)

    if origin is None:
        # Not a generic alias — plain subclass check is sufficient
        return issubclass(implementation, interface)

    # Quick rejection: origin type must be in the MRO
    if not issubclass(implementation, origin):
        return False

    expected_args = get_args(interface)

    # Walk every class in the MRO and check __orig_bases__ for each.
    # __orig_bases__ preserves type arguments; __bases__ strips them.
    #
    # DESIGN: iterate the full MRO rather than just implementation.__orig_bases__
    # so that multi-level inheritance is handled:
    #   UserRepository → TypedRepository[User] → Repository[User]
    # Without this walk only direct bases would be checked.
    for cls in implementation.__mro__:
        for base in getattr(cls, "__orig_bases__", ()):
            if get_origin(base) is origin and get_args(base) == expected_args:
                return True

    return False


def _interface_matches(binding_interface: Any, requested: Any) -> bool:
    """Return True if *binding_interface* satisfies *requested* as a DI lookup.

    Covers four structural combinations:

    +------------------------+----------------------+-------------------------------+
    | binding_interface      | requested            | strategy                      |
    +========================+======================+===============================+
    | concrete (Repository)  | concrete (Repository)| issubclass                    |
    +------------------------+----------------------+-------------------------------+
    | concrete (UserRepo)    | generic (Repo[User]) | _is_generic_subtype MRO walk  |
    +------------------------+----------------------+-------------------------------+
    | generic (Repo[User])   | generic (Repo[User]) | origin issubclass + args ==   |
    +------------------------+----------------------+-------------------------------+
    | generic (Repo[User])   | concrete (Repository)| issubclass(origin, requested) |
    +------------------------+----------------------+-------------------------------+

    Args:
        binding_interface: The type stored on the :class:`~providify.binding.AnyBinding`.
        requested:         The type passed to ``container.get()`` or extracted from
                           a type hint.

    Returns:
        True if the binding is a valid candidate for the requested type.

    Edge cases:
        - Either argument is not a type or generic alias → TypeError caught → False
        - requested is generic with TypeVar args         → literal comparison;
          will not match concrete parameterisations      ⚠️

    Example:
        _interface_matches(UserRepository, Repository[User])  → True
        _interface_matches(Repository[User], Repository[User])→ True
        _interface_matches(UserRepository, Repository)        → True
        _interface_matches(Repository[User], Repository)      → True
    """
    req_origin = get_origin(requested)
    bind_origin = get_origin(binding_interface)

    try:
        if req_origin is None and bind_origin is None:
            # Both concrete types — standard issubclass check
            return issubclass(binding_interface, requested)

        if req_origin is not None and bind_origin is not None:
            # Both parameterised generics.
            # Origins must be compatible AND type args must match exactly.
            # DESIGN: exact args match only — no covariance.
            if not issubclass(bind_origin, req_origin):
                return False
            return get_args(binding_interface) == get_args(requested)

        if req_origin is not None and bind_origin is None:
            # requested is generic (Repository[User]), binding is concrete (UserRepository).
            # Check if binding_interface implements the full parameterised interface.
            return _is_generic_subtype(binding_interface, requested)

        # req_origin is None and bind_origin is not None:
        # requested is concrete (Repository), binding is generic (Repository[User]).
        # The origin type must be a subclass of the requested concrete type.
        return issubclass(bind_origin, requested)

    except TypeError:
        # issubclass raises TypeError for non-type arguments (e.g. non-class objects).
        # Treat as non-match rather than propagating — callers iterate many bindings
        # and one bad binding should not crash the whole resolution.
        return False
