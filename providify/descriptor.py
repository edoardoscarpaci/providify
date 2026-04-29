from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .metadata import Scope, _is_scope_leak

if TYPE_CHECKING:
    pass


# ─────────────────────────────────────────────────────────────────
#  BindingDescriptor — immutable snapshot of a single binding
# ─────────────────────────────────────────────────────────────────


@dataclass
class BindingDescriptor:
    """
    A plain, serialisable snapshot of a single binding and its full
    recursive dependency tree.

    Produced by ``Binding.describe()`` — never constructed directly by
    callers. Rendering to ASCII is done via ``__repr__``.

    Attributes:
        interface:      Fully-qualified name of the interface type.
        implementation: Fully-qualified name of the concrete type.
        scope:          Lifecycle scope of this binding.
        qualifier:      Optional named qualifier (Jakarta-style @Named).
        priority:       Optional binding priority — higher value wins when multiple candidates match.
        dependencies:   Recursively resolved dependency descriptors.

    Thread safety:  ✅ Frozen dataclass — immutable after construction.
    Async safety:   ✅ No shared mutable state.

    Edge cases:
        - Circular deps → caller must guard; descriptor does NOT detect cycles.
        - No dependencies → ``dependencies`` is an empty tuple.
        - Unknown scope → raises ValueError in ``_is_scope_leak()``.

    Example:
        descriptor = my_binding.describe(container)
        print(descriptor)           # → ASCII tree
        print(repr(descriptor))     # → same ASCII tree
        d = descriptor.to_dict()    # → plain dict for JSON/YAML
    """

    interface: str
    implementation: str
    scope: Scope
    qualifier: str | type | None = None
    priority: int | None = None
    dependencies: tuple[BindingDescriptor, ...] = field(default_factory=tuple)

    @property
    def scope_leak(self) -> bool:
        """
        True if any **direct** dependency is shorter-lived than this binding.

        Only checks one level deep — this describes the binding's own health,
        not the health of its entire subtree. Deeper leaks are visible on
        their own descriptor when inspected directly.

        Returns:
            True if at least one direct dependency has a shorter scope.

        Edge cases:
            - No dependencies → always False.
            - Equal scopes    → False (not a leak).
        """
        # Compare each direct dep's scope against this binding's scope
        return any(_is_scope_leak(self.scope, dep.scope) for dep in self.dependencies)

    # ── ASCII rendering ───────────────────────────────────────────────────────

    def __repr__(self) -> str:
        """
        Render the full dependency tree as a human-readable ASCII tree.

        Output format:
            Interface [scope] (qualifier)  ⚠️ SCOPE LEAK
            ├── DepA [scope]
            │   └── DepB [scope]  ⚠️ SCOPE LEAK
            └── DepC [scope]

        Returns:
            Multi-line ASCII string. Single trailing newline.
        """
        lines: list[str] = []
        self._render(lines, prefix="", is_last=True, is_root=True)
        return "\n".join(lines)

    def _render(
        self,
        lines: list[str],
        prefix: str,
        is_last: bool,
        is_root: bool,
        parent_scope: Scope | None = None,
    ) -> None:
        """
        Recursively append tree lines into ``lines``.

        Args:
            lines:        Accumulator — each call appends one or more lines.
            prefix:       Indentation string built up as we recurse deeper.
            is_last:      Whether this node is the last sibling — controls
                          whether we draw └── or ├──.
            is_root:      Root node gets no branch connector.
            parent_scope: Scope of the parent node — used to flag scope leaks.
        """
        # ── Build the connector for this node ────────────────────────────────
        # Root has no connector; children use └── or ├── depending on position.
        if is_root:
            connector = ""
        else:
            connector = "└── " if is_last else "├── "

        # ── Format the node label ─────────────────────────────────────────────
        _q = (
            getattr(self.qualifier, "__name__", str(self.qualifier))
            if self.qualifier
            else None
        )
        qualifier_str = f" ({_q})" if _q else ""
        priority_str = f"Priority({self.priority})" if self.priority else ""
        leak_flag = (
            "  ⚠️  SCOPE LEAK"
            if parent_scope is not None
            and _is_scope_leak(parent_scope=parent_scope, dep_scope=self.scope)
            else ""
        )
        label = (
            f"{self.interface} [{self.scope.name}]{qualifier_str}{priority_str}"
            f" → {self.implementation}{leak_flag}"
        )

        lines.append(f"{prefix}{connector}{label}")

        # ── Recurse into dependencies ─────────────────────────────────────────
        # Extend the prefix so child branches align under their parent label.
        # Last child uses spaces (no continuing vertical bar); others use │.
        child_prefix = prefix if is_root else prefix + ("    " if is_last else "│   ")

        for i, dep in enumerate(self.dependencies):
            dep._render(
                lines,
                prefix=child_prefix,
                is_last=(i == len(self.dependencies) - 1),
                is_root=False,
                # Pass THIS node's scope so the child can flag itself if needed
                parent_scope=self.scope,
            )

    # ── Serialisation ─────────────────────────────────────────────────────────

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
            "interface": self.interface,
            "implementation": self.implementation,
            "scope": self.scope.name,
            "qualifier": (
                getattr(self.qualifier, "__qualname__", str(self.qualifier))
                if self.qualifier
                else None
            ),
            "scope_leak": self.scope_leak,
            "priority": self.priority,
            "dependencies": [d.to_dict() for d in self.dependencies],
        }


# ─────────────────────────────────────────────────────────────────
#  DIContainerDescriptor — grouped snapshot of the full container
# ─────────────────────────────────────────────────────────────────


@dataclass
class DIContainerDescriptor:
    """
    A grouped, serialisable snapshot of all bindings in a container.

    Produced by ``DIContainer.describe()``. Provides both structured access
    (per-scope properties) and a rendered ASCII view (via ``__repr__``).

    Attributes:
        validated: Whether the container had been validated at describe() time.
        bindings:  All binding descriptors, in registration order.

    Thread safety:  ✅ Immutable after construction — all properties are
                    computed from the frozen ``bindings`` tuple.
    Async safety:   ✅ Pure data — safe to read from any context.
    """

    validated: bool
    bindings: tuple[BindingDescriptor, ...] = field(default_factory=tuple)

    # ── Per-scope views ───────────────────────────────────────────

    @property
    def dependent_bindings(self) -> list[BindingDescriptor]:
        """All DEPENDENT-scoped binding descriptors."""
        return [b for b in self.bindings if b.scope == Scope.DEPENDENT]

    @property
    def singleton_bindings(self) -> list[BindingDescriptor]:
        """All SINGLETON-scoped binding descriptors."""
        return [b for b in self.bindings if b.scope == Scope.SINGLETON]

    @property
    def session_bindings(self) -> list[BindingDescriptor]:
        """All SESSION-scoped binding descriptors."""
        return [b for b in self.bindings if b.scope == Scope.SESSION]

    @property
    def request_bindings(self) -> list[BindingDescriptor]:
        """All REQUEST-scoped binding descriptors."""
        return [b for b in self.bindings if b.scope == Scope.REQUEST]

    # ── Rendering ─────────────────────────────────────────────────

    def render(self) -> str:
        """
        Render all bindings grouped by scope into a human-readable ASCII block.

        Each scope group is introduced by a ``[SCOPE_NAME]`` header, followed
        by one entry per binding rendered via ``str(binding)`` (which calls
        ``BindingDescriptor.__repr__`` and produces the full dependency subtree).
        Scope groups with no bindings are omitted entirely to keep the output
        clean.

        Returns:
            A multi-line string. Empty string if there are no bindings at all.

        Thread safety:  ✅ Read-only — no mutation of shared state.
        Async safety:   ✅ Pure computation — safe to call from any context.

        Edge cases:
            - No bindings at all       → returns empty string.
            - Scope group is empty     → that group's header is skipped entirely.
            - Single binding in group  → rendered with ``└──`` connector.

        Example:
            descriptor = container.describe()
            print(descriptor.render())
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
            ("[SESSION]", self.session_bindings),
            ("[REQUEST]", self.request_bindings),
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
                continuation = "    " if i == last_idx else "│   "

                lines.append(f"{connector}{binding_lines[0]}")
                for extra_line in binding_lines[1:]:
                    lines.append(f"{continuation}{extra_line}")
            lines.append("\n")

        return "\n".join(lines)

    def __repr__(self) -> str:
        """
        Return the human-readable grouped rendering of the container's bindings.

        Delegates to :meth:`render` so that ``str(descriptor)`` and
        ``repr(descriptor)`` both show the grouped ASCII view.

        Returns:
            Multi-line string produced by :meth:`render`.

        Example:
            descriptor = container.describe()
            print(descriptor)
        """
        return self.render()

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
            "validated": self.validated,
            "singleton_bindings": [b.to_dict() for b in self.singleton_bindings],
            "session_bindings": [b.to_dict() for b in self.session_bindings],
            "request_bindings": [b.to_dict() for b in self.request_bindings],
            "dependent_bindings": [b.to_dict() for b in self.dependent_bindings],
        }
