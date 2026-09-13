"""Profile activation semantics for Plan 005 (`@Profile`, env-driven `@Alternative`).

Pure data + pure functions — mirrors `validation.py`'s role for Plan 003:
nothing in this module touches a `DIContainer`, a `Binding`, or any I/O
besides a single `os.environ.get()` read inside `resolve_active_profiles`.

Import direction: `container.py` -> `profiles.py`, and `binding.py` reads
`ProfileMetadata`/`_get_profile_expressions` from `metadata.py` (which does
NOT import this module — the tuple of expressions is passed through, never
the raw string). This module must NEVER import `container.py` or
`binding.py` — that would create a cycle, matching the isolation
`validation.py` established (plan 005 §Design).
"""

from __future__ import annotations

import os
from collections.abc import Iterable

#: Name of the environment variable consulted by `resolve_active_profiles`
#: when no explicit `profiles=` argument is given to `DIContainer.__init__`.
#: Public API the moment it ships — see plan 005 §Risks for the naming
#: assumption (modelled on Spring's `SPRING_PROFILES_ACTIVE`).
ENV_VAR = "PROVIDIFY_PROFILES"


def _normalise(name: str) -> str:
    """Strip surrounding whitespace and lower-case a single profile name.

    Applied at both ends of the matching relation — inside `@Profile(...)`
    and inside `resolve_active_profiles`/`parse_profiles` — so that
    `"PROD"` and `"prod"` are always the same profile (plan 005 §Design).

    Args:
        name: A raw profile literal, possibly leading with `"!"` for
            negation. The `"!"` prefix is preserved; only the remainder
            (and any surrounding whitespace on the whole string) is
            normalised.

    Returns:
        The stripped, lower-cased profile literal.

    Thread safety: Pure function — safe to call from any thread.
    Async safety:  Pure function — safe to call from any coroutine.

    Edge cases:
        - `"  PROD  "` -> `"prod"`.
        - `"!PROD"` -> `"!prod"` (negation marker untouched, body lowered).

    Example:
        >>> _normalise("  PROD  ")
        'prod'
        >>> _normalise("!Prod")
        '!prod'
    """
    return name.strip().lower()


def matches(expressions: tuple[str, ...], active: frozenset[str]) -> bool:
    """Evaluate a binding's profile expression against an active profile set.

    Semantics (plan 005 §Design truth table): OR across *expressions*; a
    literal prefixed with `"!"` matches when the (unprefixed) profile is
    **not** in *active*. An empty *expressions* tuple always matches — that
    is the "unprofiled binding" case, and callers should short-circuit on it
    before calling this function on the hot path (see
    `DIContainer._binding_is_active`).

    Args:
        expressions: Already-normalised profile literals (lower-cased,
            stripped, `"!"`-prefixed for negation) as produced by
            `@Profile(...)` / `_get_profile_expressions`.
        active: The container's current active profile set. Already
            normalised by `resolve_active_profiles` / `activate_profile`.

    Returns:
        `True` if *expressions* is empty, or if at least one expression
        evaluates to `True` against *active*.

    Raises:
        Nothing — a malformed expression (bare `"!"`, empty string) cannot
        reach this function because `@Profile(...)` rejects it at
        decoration time.

    Thread safety: Pure function — safe to call from any thread.
    Async safety:  Pure function — safe to call from any coroutine.

    Edge cases:
        - `expressions=()` -> `True` regardless of *active* (unprofiled).
        - `("!prod",)` against `frozenset()` -> `True` (nothing is "prod").
        - `("dev", "!prod")` against `{"prod"}` -> `False` (both terms false).

    Example:
        >>> matches(("prod",), frozenset({"prod"}))
        True
        >>> matches(("!prod",), frozenset({"prod"}))
        False
    """
    if not expressions:
        return True
    for expr in expressions:
        if expr.startswith("!"):
            if expr[1:] not in active:
                return True
        elif expr in active:
            return True
    return False


def parse_profiles(raw: str | None) -> frozenset[str]:
    """Parse a comma/whitespace-separated profile list into a normalised set.

    Used to interpret the `PROVIDIFY_PROFILES` environment variable's raw
    string value. Each comma-separated segment is stripped and lower-cased;
    empty segments (from leading/trailing/doubled commas) are dropped.

    Args:
        raw: The raw string to parse, or `None` (e.g. an unset environment
            variable). `""` and `None` are both treated as "no profiles".

    Returns:
        A `frozenset` of normalised profile names. Empty when *raw* is
        `None`, `""`, or contains only commas/whitespace.

    Thread safety: Pure function — safe to call from any thread.
    Async safety:  Pure function — safe to call from any coroutine.

    Edge cases:
        - `None` -> `frozenset()`.
        - `""` -> `frozenset()`.
        - `"a,,b"` -> `{"a", "b"}` — empty segments dropped.
        - `" PROD , Eu "` -> `{"prod", "eu"}`.

    Example:
        >>> parse_profiles("prod, eu")
        frozenset({'prod', 'eu'})
        >>> parse_profiles(None)
        frozenset()
    """
    if not raw:
        return frozenset()
    return frozenset(_normalise(segment) for segment in raw.split(",") if segment.strip())


def resolve_active_profiles(explicit: Iterable[str] | None) -> frozenset[str]:
    """Resolve the container's active profile set from an explicit arg or the env.

    Precedence (plan 005 §Design):
        - *explicit* is not `None` -> used verbatim, normalised, **even when
          empty** — an explicit `()` means "no profiles" and wins over a
          set `PROVIDIFY_PROFILES`.
        - *explicit* is `None` -> `PROVIDIFY_PROFILES` is read via
          `os.environ.get` and parsed with `parse_profiles`.

    Args:
        explicit: The `profiles=` argument passed to `DIContainer.__init__`
            (or `None` if the caller did not pass one). Any iterable of raw
            profile name strings — normalisation happens here, not at the
            call site.

    Returns:
        A normalised `frozenset[str]` of active profile names.

    Thread safety: Pure function — the only I/O is a single `os.environ.get`
        read, which is safe to call from any thread (no environment
        mutation happens here).
    Async safety:  Safe — no await points, no shared mutable state.

    Edge cases:
        - `explicit=()` with the env var set -> `frozenset()` (explicit
          empty wins).
        - `explicit=None` with the env var unset -> `frozenset()`.
        - `explicit=["Prod"]` -> `frozenset({"prod"})` (normalised).

    Example:
        >>> import os
        >>> os.environ["PROVIDIFY_PROFILES"] = "prod,eu"
        >>> resolve_active_profiles(None)
        frozenset({'prod', 'eu'})
        >>> resolve_active_profiles(())
        frozenset()
    """
    if explicit is not None:
        return frozenset(_normalise(name) for name in explicit)
    return parse_profiles(os.environ.get(ENV_VAR))
