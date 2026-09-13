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
# DESIGN: Six functions cover the whole problem:
#   _type_name           — safe display name, works for both forms
#   _is_generic_subtype  — "does this concrete class satisfy a generic interface?"
#   _interface_matches   — "does this binding's interface satisfy a request?"
#   _open_type_params     — "is this alias open (Repo[T]), closed (Repo[User]),
#                            or plain (Repo)?" (plan 016)
#   _closing_args         — "what TypeVar->concrete mapping closes an open
#                            binding's alias into a closed request?" (plan 016)
#   _type_arg_param       — "does this hint have the exact shape type[X] for
#                            a TypeVar X?" (plan 016, factory type-arg delivery)
#
# Tradeoffs:
#   ✅ No third-party dependency — pure stdlib typing introspection
#   ✅ Handles nested generics correctly via __orig_bases__ MRO walk
#   ❌ Type args are compared with == (exact match) — no covariance/contravariance
#      e.g. Repository[Dog] does NOT match Repository[Animal] even if Dog ⊆ Animal
#   ❌ An open binding's TypeVar args (Repo[T]) are wildcards against a CLOSED
#      request (Repo[User], plan 016) — but a request-side TypeVar
#      (get(Repo[T])) is still compared literally, never a wildcard; see
#      _interface_matches's docstring table below for the full rule.
import sys
from typing import Any, ForwardRef, TypeVar, get_args, get_origin


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
        ``tp.__name__`` for concrete types; ``"Origin[Arg, ...]"`` (each part
        recursively short-named) for a parameterised generic alias; ``str(tp)``
        for anything else (plan 016 — see Edge cases for why recursion,
        not a raw ``str(tp)``, is needed for a generic alias).

    Edge cases:
        - Concrete type with ``__name__``         → returns ``__name__``.
        - Generic alias like ``Repository[User]`` → returns ``"Repository[User]"``
          — SHORT names, not Python's own ``str(Repository[User])`` (plan
          016 fix): the stdlib repr is fully module-qualified
          (``"mymod.Repository[mymod.User]"``), which made every
          ``LookupError``/cycle/validate message showing a generic alias
          needlessly noisy, and disagreed with this very docstring's example
          — never previously exercised because no closed-alias factory
          existed to trigger a resolve-time message before open bindings
          (plan 016) made ``Repo[User]``-shaped ``CircularDependencyError``
          messages routine.
        - Arg is a ``TypeVar`` (open alias, ``Repo[T]``) → recurses into
          ``str(tp)`` for it (no ``get_origin``, no ``__name__``), producing
          the TypeVar's own ``repr`` (``"~T"``) — so ``Repo[T]`` renders
          ``"Repo[~T]"``, matching the pre-plan-016 rendering exactly.
        - Nested generic arg (``Outer[Repo[Product]]``) → recurses fully,
          short names at every level.
        - Origin without ``__name__`` (exotic — e.g. a runtime-generated
          alias) → falls back to the plain ``str(tp)`` for the WHOLE alias,
          never a half-short/half-qualified hybrid.
        - ``None`` or other non-types/non-aliases → returns ``str(tp)``.

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

    origin = get_origin(tp)
    if origin is not None:
        args = get_args(tp)
        origin_name = getattr(origin, "__name__", None)
        if origin_name is not None and args:
            # Recurse so every level (nested generics, TypeVars) renders
            # short — never a half-qualified hybrid string.
            return f"{origin_name}[{', '.join(_type_name(a) for a in args)}]"

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

    Covers five structural combinations:

    +--------------------------+----------------------+---------------------------------+
    | binding_interface        | requested            | strategy                        |
    +==========================+======================+==================================+
    | concrete (Repository)    | concrete (Repository)| issubclass                      |
    +--------------------------+----------------------+---------------------------------+
    | concrete (UserRepo)      | generic (Repo[User]) | _is_generic_subtype MRO walk    |
    +--------------------------+----------------------+---------------------------------+
    | generic (Repo[User])     | generic (Repo[User]) | origin issubclass + args ==     |
    +--------------------------+----------------------+---------------------------------+
    | generic-open (Repo[T])   | generic-closed        | identical origin + closing args |
    |                          | (Repo[User])          | (wildcard — plan 016)           |
    +--------------------------+----------------------+---------------------------------+
    | generic (Repo[User])     | concrete (Repository)| issubclass(origin, requested)   |
    +--------------------------+----------------------+---------------------------------+

    Args:
        binding_interface: The type stored on the :class:`~providify.binding.AnyBinding`.
        requested:         The type passed to ``container.get()`` or extracted from
                           a type hint.

    Returns:
        True if the binding is a valid candidate for the requested type.

    Edge cases:
        - Either argument is not a type or generic alias → TypeError caught → False
        - Binding-side TypeVar args (``Repo[T]``) are wildcards against a
          CLOSED request (plan 016) — but a request-side TypeVar
          (``get(Repo[T])``) is still compared literally: it never matches an
          open binding, because ``_closing_args`` rejects any requested arg
          that still carries free type parameters. ⚠️
        - A **bare** request (``Repo``, no args at all) matching an open
          binding (``Repo[T]``) is intentionally kept ``True`` here (the
          fourth branch below, unchanged since before plan 016) so
          ``@Disposes(Repo)`` and ``reset_binding(Repo)`` keep working
          structurally — the "bare request must not resolve/instantiate an
          open binding" rule lives one layer up, in the lookup-only
          ``DIContainer._binding_serves`` (plan 016 §Design "Where the bare
          guard lives"), not here.

    Example:
        _interface_matches(UserRepository, Repository[User])  → True
        _interface_matches(Repository[User], Repository[User])→ True
        _interface_matches(UserRepository, Repository)        → True
        _interface_matches(Repository[User], Repository)      → True
        _interface_matches(Repository[TypeVar("T")], Repository[User]) → True (plan 016)
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
            if get_args(binding_interface) == get_args(requested):
                # Literal match — covers both closed==closed (the pre-plan-016
                # rule) and open==open (`get(Repo[T])` against `Repo[T]`,
                # which is NOT a wildcard match — see Edge cases above).
                return True
            # Plan 016 wildcard branch: binding_interface may be an open alias
            # (Repo[T]) whose TypeVars can close into requested's concrete
            # args. `_closing_args` returns None for every non-match reason
            # (not open, different origin, unclosed request, inconsistent
            # repeated TypeVar, bound/constraint violation) — a single
            # predicate covers all of row 1's non-goals in one call.
            return _closing_args(binding_interface, requested) is not None

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


# ─────────────────────────────────────────────────────────────────
#  Open-generic bindings — plan 016
#
#  DESIGN: Autofac's "resolve-time factory" model, not .NET's type
#  substitution — Python has no runtime "instantiate the closed generic
#  class with substituted parameters", and providify's factories need the
#  closed type argument AS A VALUE (`get_repository(entity_cls)`), not as a
#  class to instantiate. See plans/016-open-generic-binding.md §Design for
#  the full eight-row semantics table this module implements one row of.
# ─────────────────────────────────────────────────────────────────


def _open_type_params(interface: Any) -> tuple[TypeVar, ...]:
    """Classify a generic alias as open, closed, or plain — row 8.

    An "open alias" is a parameterised generic whose args are ALL plain
    ``TypeVar``s (``Repo[T]``, ``Pair[A, B]``, ``Pair[T, T]``). Anything else
    with free type parameters (``Repo[list[T]]``, ``Pair[str, T]``, a
    ``ParamSpec``/``TypeVarTuple`` arg) is a "partially-open" shape that this
    plan explicitly does not support (see plan 016 §Non-goals) — providify
    has no substitution mechanism for it, so registering one would silently
    create a binding that can never match any closed request. Raising here,
    at registration, surfaces that immediately instead of at first resolve.

    Args:
        interface: Any type or generic alias — typically a
            :class:`~providify.binding.ProviderBinding`'s resolved interface.

    Returns:
        ``()`` for a concrete type or an already-closed alias
        (``Repo[User]``, ``__parameters__ == ()``); the tuple of TypeVars,
        in declaration order, for an open alias (``Repo[T]`` → ``(T,)``,
        ``Pair[A, B]`` → ``(A, B)``, ``Pair[T, T]`` → ``(T, T)``).

    Raises:
        TypeError: *interface* has free type parameters but is not all
            plain ``TypeVar`` args — names the alias and the fix.

    Edge cases:
        - Concrete type / non-generic ``interface`` → ``()`` immediately,
          never even reads ``__parameters__``.
        - ``Repo`` (bare, unparameterised generic class) → ``get_origin`` is
          ``None`` → ``()``, same as a concrete type (`bind()`'s existing
          behaviour, untouched by this plan).
        - ``Pair[T, T]`` (repeated TypeVar) → accepted here; the *closing*
          consistency check (does ``Pair[int, str]`` disagree on ``T``?) is
          `_closing_args`'s job, not this classifier's.
        - PEP 695 ``class Box[X]`` → identical ``get_args``/``__parameters__``
          shape to a module-level ``TypeVar`` alias — no special-casing
          needed here (**[R001:49]**).
        - ``ParamSpec``/``TypeVarTuple`` arg → ``type(a) is TypeVar`` is
          ``False`` for both → falls into the ``TypeError`` branch.

    Thread safety:  ✅ Pure function — reads only, no shared state.
    Async safety:   ✅ No awaits, no shared mutable state.

    Example:
        _open_type_params(Repo)          → ()
        _open_type_params(Repo[User])    → ()
        _open_type_params(Repo[T])       → (T,)
        _open_type_params(Pair[A, B])    → (A, B)
        _open_type_params(Repo[list[T]]) → raises TypeError
    """
    if isinstance(interface, type) or get_origin(interface) is None:
        return ()
    args = get_args(interface)
    # __parameters__ is the alias's OWN free-TypeVar list — empty for a fully
    # closed alias like Repo[User] even though get_args still returns (User,).
    free = getattr(interface, "__parameters__", ())
    if not free:
        return ()
    # Strict `type(a) is TypeVar` (not `isinstance`) — a ParamSpec or
    # TypeVarTuple arg would otherwise slip through as "not a TypeVar
    # instance" only by accident of a future typing_extensions subclass;
    # the exact-type check is the documented, intentional guard (**[R001:33]**).
    if all(type(a) is TypeVar for a in args):
        return tuple(args)
    raise TypeError(
        f"{_type_name(interface)} mixes concrete type arguments with "
        f"TypeVars. An open-generic binding interface must be all-TypeVar "
        f"or all-concrete — e.g. Repo[T] (open) or Repo[User] (closed), "
        f"never Repo[list[T]] or Pair[str, T] (partially open). Register "
        f"two separate bindings instead, or fully close the alias."
    )


def _typevar_accepts(tp: TypeVar, arg: Any) -> bool:
    """Return True if *arg* satisfies *tp*'s ``bound=``/constraints — row 7.

    A private helper for :func:`_closing_args`: decides whether one
    ``(TypeVar, concrete-arg)`` pair is legal, in isolation from the rest of
    the closing-args mapping (repeated-TypeVar consistency is checked by the
    caller, not here).

    Args:
        tp:  The binding-side ``TypeVar`` (from the open alias).
        arg: The corresponding concrete argument from the closed request.

    Returns:
        ``True`` when *arg* satisfies *tp*'s bound/constraints, OR when the
        check cannot run at all (permissive by design — see Edge cases).
        ``False`` only for a **provable** violation.

    Edge cases:
        - *arg* is not a runtime type (``Literal[...]``, a string forward
          ref, ...) → permissive ``True``; a check that cannot run must
          never block resolution (plan 016 §Design row 7).
        - *arg* is itself a subscripted generic (``Repo[Entity]``) → checked
          against ``get_origin(arg)`` (``Repo``), and the bound is checked
          against ``get_origin(bound) or bound`` (**[R001:87-88]**) so a
          subscripted bound (``TypeVar("B", bound=Repo[Entity])``) never
          raises ``TypeError`` from a bare ``issubclass``.
        - ``tp.__constraints__`` non-empty → **identity/equality membership
          only** (``arg not in tp.__constraints__``) — no subclassing
          (**[R001:92]**): a ``str`` subclass does NOT satisfy
          ``TypeVar("C", str, bytes)``.
        - ``tp.__bound__`` raises ``NameError`` (a lazily-evaluated PEP 695
          bound whose forward reference cannot resolve yet) or ``issubclass``
          raises ``TypeError`` (a non-``@runtime_checkable`` ``Protocol``
          bound) → permissive ``True`` (**[R001:143]**); static checkers own
          this case, not the runtime container.
        - ``TypeVar("Bounded", bound="Entity")`` (old-style STRING bound,
          pre-PEP-695) → Python stores this as an unevaluated
          ``typing.ForwardRef``, forever — unlike PEP 695's lazy-but-later-
          resolved bound, nothing in stdlib ever resolves it automatically.
          Resolved here via ``eval`` against ``tp``'s OWN declaring module's
          globals (``sys.modules[tp.__module__]`` — a ``TypeVar`` records
          ``__module__`` at creation, same as a class) so
          ``TypeVar("Bounded", bound="Entity")`` correctly finds a
          module-level ``Entity`` declared AFTER the ``TypeVar`` itself
          (the common top-of-file ``TypeVar`` idiom). Unresolvable → same
          permissive ``NameError`` handling as the PEP 695 case.

    Thread safety:  ✅ Pure function — no shared state.
    Async safety:   ✅ No awaits.
    """
    # Repo[Entity] as a requested arg -> check against Repo (its origin), not
    # the unhashable/uncomparable alias object itself.
    check_target = get_origin(arg) or arg
    if not isinstance(check_target, type):
        # Literal[...], strings, etc. — cannot check; permissive.
        return True
    if tp.__constraints__:
        # Identity/equality membership only — no subclassing (**[R001:92]**).
        return arg in tp.__constraints__
    try:
        bound = tp.__bound__
    except NameError:
        # PEP 695 lazily-evaluated bound with an unresolvable forward ref.
        return True
    if bound is None:
        return True
    if isinstance(bound, ForwardRef):
        # Old-style string bound (TypeVar(name, bound="Entity")) — stdlib
        # never resolves this on its own; do it ourselves against tp's own
        # declaring module, permissively falling back to True on failure.
        module = sys.modules.get(tp.__module__)
        try:
            bound = eval(bound.__forward_arg__, vars(module) if module else {})  # noqa: S307
        except NameError:
            return True
    try:
        return issubclass(check_target, get_origin(bound) or bound)
    except TypeError:
        # e.g. bound is a non-@runtime_checkable Protocol — issubclass raises.
        return True


def _closing_args(binding_interface: Any, requested: Any) -> dict[TypeVar, Any] | None:
    """Return the ``{TypeVar: concrete}`` mapping that closes *binding_interface*
    into *requested*, or ``None`` when it does not close it — row 1/7/8.

    The single predicate behind the open-binding wildcard match
    (:func:`_interface_matches`), the lookup-layer bare-request guard
    (``DIContainer._binding_serves``), and the factory type-argument delivery
    (``ProviderBinding.type_args_for``) — one function, three call sites, so
    they can never disagree about whether a given closed request is served.

    Args:
        binding_interface: The (potentially open) alias stored on a
            :class:`~providify.binding.ProviderBinding` — e.g. ``Repo[T]``.
        requested: The type or alias being looked up — e.g. ``Repo[User]``.

    Returns:
        ``{T: User}`` (etc.) when *requested* is a fully closed alias that
        *binding_interface* can produce; ``None`` for every non-match reason
        (see Edge cases) — deliberately a single sentinel, not an exception,
        because "does not serve this request" is an ordinary lookup outcome,
        not an error.

    Edge cases:
        - *binding_interface* is not open (``_open_type_params(...) == ()``)
          → ``None`` — a closed/concrete binding is never a wildcard.
        - ``get_origin(requested) is not get_origin(binding_interface)`` →
          ``None`` — **identical origin required**. A subclass origin
          (``Sub[T]`` serving ``Repo[User]`` where ``class Sub(Repo[T])``) is
          NOT accepted: positional delivery is only sound when both sides
          share the same parameter list (plan 016 §Design "Match rule", the
          ``Flip``/``Pair`` counter-example) — a documented first-cut limit.
        - Arg counts differ → ``None``.
        - Any requested arg still carries free type parameters
          (``getattr(arg, "__parameters__", ())`` non-empty, or
          ``type(arg) is TypeVar``) → ``None`` — the request itself is not
          closed (``get(Repo[T])`` never wildcard-matches, row 1's ❌).
        - A repeated ``TypeVar`` (``Pair[T, T]``) closes to two different
          args (``Pair[int, str]``) → ``None``; a consistent repeat
          (``Pair[int, int]``) → ``{T: int}``.
        - A ``bound=``/constraint violation on any pair → ``None`` (see
          :func:`_typevar_accepts`) — a **non-match**, letting a sibling
          binding serve the same request instead of raising.

    Thread safety:  ✅ Pure function — no shared state.
    Async safety:   ✅ No awaits.

    Example:
        _closing_args(Repo[T], Repo[User])        → {T: User}
        _closing_args(Repo[User], Repo[Order])    → None (not open)
        _closing_args(Pair[T, T], Pair[int, str]) → None (inconsistent)
        _closing_args(Sub[T], Repo[User])         → None (origin mismatch)
    """
    type_params = _open_type_params(binding_interface)
    if not type_params:
        return None
    if get_origin(requested) is not get_origin(binding_interface):
        return None

    bind_args = get_args(binding_interface)
    req_args = get_args(requested)
    if len(bind_args) != len(req_args):
        return None

    mapping: dict[TypeVar, Any] = {}
    for tp, arg in zip(bind_args, req_args, strict=True):
        # The request itself must be fully closed — a request-side TypeVar
        # is always literal, never a wildcard (row 1's explicit non-goal).
        if getattr(arg, "__parameters__", ()) or type(arg) is TypeVar:
            return None
        if tp in mapping and mapping[tp] != arg:
            # Repeated TypeVar (Pair[T, T]) closing inconsistently
            # (Pair[int, str]) — the two occurrences disagree on T.
            return None
        if not _typevar_accepts(tp, arg):
            return None
        mapping[tp] = arg
    return mapping


def _type_arg_param(hint: Any) -> TypeVar | None:
    """Return the ``TypeVar`` inside a ``type[X]``-shaped hint, or ``None`` — row 3.

    Recognises the EXACT shape ``type[X]`` / ``typing.Type[X]`` where ``X``
    is a plain ``TypeVar`` — the one factory-parameter spelling that receives
    an open binding's closed type argument by name (plan 016 §Design "the
    minimal seam"). Nothing else — including ``type[X] | None`` — matches,
    so a forgotten-but-optional parameter is never silently "helped".

    Args:
        hint: An already-evaluated (non-string) type hint.

    Returns:
        The ``TypeVar`` when *hint* is exactly ``type[X]`` for a plain
        ``TypeVar`` ``X``; ``None`` for anything else.

    Edge cases:
        - ``type[T] | None`` → ``None`` (E16) — ``get_origin`` of the union
          is ``UnionType``/``Union``, not ``type``; shape must be EXACT.
        - ``type[User]`` (concrete, not a TypeVar) → ``None`` — this is a
          plain "give me the User class" hint, resolved the normal way.
        - ``type`` (bare, unparameterised) → ``get_origin(type)`` is
          ``None`` → ``None``.
        - ``type[T, S]`` (impossible — ``type[]`` takes exactly one arg) →
          would return ``None`` via the length check; included for
          completeness, not reachable through normal annotations.

    Thread safety:  ✅ Pure function — no shared state.
    Async safety:   ✅ No awaits.

    Example:
        _type_arg_param(type[T])         → T
        _type_arg_param(type[User])      → None
        _type_arg_param(type[T] | None)  → None
    """
    if get_origin(hint) is not type:
        return None
    args = get_args(hint)
    if len(args) != 1:
        return None
    arg = args[0]
    return arg if type(arg) is TypeVar else None
