"""Pure ordering logic for ``@Configuration`` module installation (Plan 008/F5).

DESIGN: this module must NEVER import ``container.py`` — the same isolation
pattern already used by ``validation.py``, ``profiles.py`` and ``config.py``
(see ``validation.py``'s module docstring and plans/006-configuration-binding.md
§227-236). ``resolve_install_order`` is a pure graph algorithm over
``ConfigurationMetadata.depends_on`` edges; it has no business knowing how a
container instantiates anything. That separation buys two things:
    ✅ Cycle/order logic is unit-testable with zero container setup — see
       tests/test_module_order.py, which imports nothing from container.py.
    ✅ No import cycle: container.py imports resolve_install_order from here,
       so the reverse import would be circular.
The cost is that this module cannot special-case anything container-specific
(e.g. "skip classes already installed") — that filtering is the container's
job (``DIContainer.install``/``ainstall``), which loops over the order this
module returns and skips entries already in ``self._installed_modules``.

Import direction: `container.py` -> `modules.py` -> `metadata.py` /
`exceptions.py`. Mirrors `validation.py`'s stated import direction exactly.
"""

from __future__ import annotations

from collections.abc import Sequence

from .exceptions import ModuleCycleError
from .metadata import _get_configuration_module, _has_configuration_module


def module_dependencies(cls: type) -> tuple[type, ...]:
    """Return the ``depends_on`` classes declared on a ``@Configuration`` class.

    Args:
        cls: A class decorated with ``@Configuration``.

    Returns:
        The ``depends_on`` tuple stamped by the decorator, or ``()`` if *cls*
        was never decorated with ``@Configuration`` at all (defensive — the
        real guard against non-module classes lives in
        :func:`resolve_install_order`, which raises ``TypeError`` instead of
        silently returning empty).

    Edge cases:
        - *cls* has no ``@Configuration`` marker → ``()``, matching the
          "no dependencies" case rather than raising, since this is also
          used defensively before the marker is guaranteed to exist.
    """
    meta = _get_configuration_module(cls)
    return meta.depends_on if meta is not None else ()


def resolve_install_order(roots: Sequence[type]) -> list[type]:
    """Topologically sort *roots* and their transitive ``depends_on`` closure.

    Deterministic DFS **post-order**: for each root (in the order given), its
    dependencies are visited first (in ``depends_on`` declaration order),
    recursively, and a class is appended to the result only after all of its
    own dependencies have already been appended. That is exactly "deps
    first, dependent last" — the shape ``container.install()`` needs to
    register providers in a resolvable order (plan 008 §Design/"Install
    pipeline").

    Args:
        roots: The ``@Configuration`` classes to install — typically a single
            class from ``container.install(M)``, but callers may pass several
            (e.g. a future multi-root entry point) and shared dependencies
            are still installed exactly once.

    Returns:
        A ``list[type]`` in install order — every class in *roots* and its
        transitive ``depends_on`` closure, each appearing exactly once, deps
        strictly before dependents.

    Raises:
        ModuleCycleError: *roots* (or their transitive closure) contain a
            ``depends_on`` cycle. ``exc.cycle`` names every class in the
            cycle, in traversal order, first and last element identical
            (e.g. ``(A, B, C, A)``). Raised as soon as the DFS revisits a
            class already on the current path — no root is even partially
            processed once its walk begins hitting a cycle deeper in the
            graph, matching the "cycle leaves nothing installed" contract
            container.install() relies on.
        TypeError: A class reachable via ``depends_on`` is not itself
            decorated with ``@Configuration`` — naming the offending class,
            since silently treating it as "no further deps" would produce a
            wrong, silently-truncated order instead of a loud failure.

    Edge cases:
        - A root with empty ``depends_on`` → singleton list ``[root]``.
        - Two roots sharing a dependency → the shared dependency is visited
          (and appended) once, on whichever root reaches it first; the
          second root's DFS finds it already in ``seen`` and skips re-adding
          it, but its position (already before both roots) still satisfies
          both roots' ordering constraint.
        - Self-dependency (``depends_on=(Self,)``) → a length-2 cycle
          ``(Self, Self)``.
        - Diamond (``D`` depends on ``B`` and ``C``, both depend on ``A``) →
          ``A`` appears once, before ``B`` and ``C``; ``B`` appears before
          ``C`` because ``depends_on`` declaration order is preserved.
    """
    order: list[type] = []
    seen: set[type] = set()
    # `path` is the current DFS stack — used only for cycle detection/
    # reporting. A class already fully processed (in `seen`, appended to
    # `order`) but NOT on `path` is a legitimate shared dependency, not a
    # cycle — that distinction is why cycle detection checks `path`, not `seen`.
    path: list[type] = []

    def visit(cls: type) -> None:
        if cls in seen:
            return  # already fully processed via another branch — not a cycle
        if cls in path:
            # Cycle: reconstruct it from the point cls first appeared on the
            # path through to the end, then back to cls, so the reported
            # cycle names only the classes actually forming the loop (not
            # unrelated ancestors earlier on the path).
            start = path.index(cls)
            cycle = [*path[start:], cls]
            raise ModuleCycleError(cycle)
        if not _has_configuration_module(cls):
            raise TypeError(
                f"{cls.__name__} is referenced via depends_on= but is not "
                f"decorated with @Configuration. Every class named in "
                f"depends_on= must itself be a @Configuration module."
            )

        path.append(cls)
        try:
            for dep in module_dependencies(cls):
                visit(dep)
        finally:
            path.pop()

        # Post-order: append only after every dependency is already in order.
        seen.add(cls)
        order.append(cls)

    for root in roots:
        visit(root)

    return order
