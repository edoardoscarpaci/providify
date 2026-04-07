from __future__ import annotations

import logging
import importlib
import inspect
import pkgutil
from abc import ABC, abstractmethod
from types import ModuleType
from typing import TYPE_CHECKING, Any, Generic, get_origin

from .binding import ClassBinding, ProviderBinding
from .metadata import (
    _has_own_metadata,
    _has_provider_metadata,
    _has_configuration_module,
)

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Imported only for type-checking to avoid a circular import at runtime:
    # container → scanner → container would cause an ImportError.
    from .container import DIContainer


class ContainerScanner(ABC):
    """Abstract base class that defines the scanning interface.

    Subclass this to implement custom discovery strategies — for example,
    scanning a database of registered classes instead of walking Python modules.
    """

    @abstractmethod
    def scan(self, module: str | ModuleType, *, recursive: bool = False) -> None:
        """Scan a module and register every discovered component with the container.

        Args:
            module: Either a fully-qualified module name (``"myapp.services"``) or
                an already-imported ``ModuleType`` object.
            recursive: When ``True``, also walks all sub-packages of *module*
                using :func:`pkgutil.walk_packages`.

        Returns:
            None

        Raises:
            ModuleNotFoundError: If *module* is a string that cannot be imported.
        """
        ...


class DefaultContainerScanner(ContainerScanner):
    """Default scanner — discovers ``@Component``, ``@Singleton``, and ``@Provider``
    decorated objects in a module tree and registers them with the container.

    Uses :func:`inspect.getmembers` to walk each module's public namespace.
    Only objects whose defining module matches the scanned module are registered,
    so re-exported symbols from third-party packages are silently skipped.
    """

    def __init__(self, container: DIContainer) -> None:
        """Initialise the scanner with a reference to the owning container.

        Args:
            container: The :class:`~providify.container.DIContainer` instance
                that discovered bindings will be registered into.
        """
        self._container = container

    # ── Public API ────────────────────────────────────────────────

    def scan(self, module: str | ModuleType, *, recursive: bool = False) -> None:
        """Scan *module* and register all DI-annotated members.

        Args:
            module: A dotted module name or an already-imported module object.
            recursive: If ``True``, sub-packages are discovered and scanned via
                :func:`pkgutil.walk_packages`.

        Returns:
            None

        Raises:
            ModuleNotFoundError: If *module* is a string and cannot be imported.
        """
        if isinstance(module, str):
            module = importlib.import_module(module)

        self._scan_module(module)
        if recursive:
            self._scan_recursive(module)

    # ── Internal helpers ──────────────────────────────────────────
    def _scan_module(self, module: ModuleType) -> None:
        """Inspect all public members of *module* and register eligible ones.

        A member is skipped if:
        - its name starts with ``_`` (private / dunder), or
        - it was defined in a *different* module (i.e. it is a re-export).

        Args:
            module: The already-imported module to inspect.

        Returns:
            None
        """
        for name, obj in inspect.getmembers(module):
            # Skip private / dunder names
            if name.startswith("_"):
                continue

            # Skip symbols re-exported from other modules to avoid double-registration
            if inspect.getmodule(obj) is not module:
                continue

            if inspect.isclass(obj) and _has_own_metadata(obj):
                self._autoregister_class(obj)
            elif inspect.isfunction(obj) and _has_provider_metadata(obj):
                self._autoregister_provider(obj)
            elif inspect.isclass(obj) and _has_configuration_module(obj):
                self._autoregister_configurator(obj)

    def _scan_recursive(self, package: ModuleType) -> None:
        """Walk all sub-packages of *package* and scan each one.

        Sub-modules that fail to import emit a warning and are skipped rather
        than raising — this keeps a single bad module from breaking the whole scan.

        Args:
            package: The root package whose ``__path__`` is used to locate
                sub-modules via :func:`pkgutil.walk_packages`.

        Returns:
            None
        """
        package_path = getattr(package, "__path__", None)

        # Plain modules (not packages) have no __path__ — nothing to recurse into
        if package_path is None:
            return

        for module_info in pkgutil.walk_packages(
            path=package_path, prefix=package.__name__ + "."
        ):
            try:
                submodule = importlib.import_module(module_info.name)
                self._scan_module(submodule)
            except ImportError as e:
                LOGGER.warning(
                    f"[DIContainer] Warning: could not import '{module_info.name}': {e}"
                )

    def _autoregister_class(self, cls: type) -> None:
        """Register a DI-annotated class into the container, skipping duplicates.

        If *cls* implements one or more abstract base classes it is bound against
        each of those interfaces. Otherwise it is self-bound (bound to itself).

        Args:
            cls: The decorated class to register.

        Returns:
            None
        """
        bindings = self._container._bindings

        # Guard against scanning the same module twice
        if any(
            isinstance(b, ClassBinding) and b.implementation is cls for b in bindings
        ):
            return

        interfaces = self._find_interfaces(cls)
        if interfaces:
            # Bind the class against each abstract base it implements.
            # This includes both ABC-style abstract bases and parameterised
            # generic bases like Repository[User].
            for interface in interfaces:
                bindings.append(ClassBinding(interface, cls))
        else:
            # No abstract base — self-bind so the class can be resolved directly
            bindings.append(ClassBinding(cls, cls))

        # Invalidate the localns cache — bindings were appended directly without
        # going through bind()/register() which would have reset it themselves.
        self._container._localns_cache = None
        self._container._validated = False

    def _autoregister_provider(self, fn: Any) -> None:
        """Register a DI-annotated provider function, skipping duplicates.

        Args:
            fn: The decorated provider callable (sync or async).

        Returns:
            None
        """
        bindings = self._container._bindings

        # Guard against scanning the same module twice
        if any(isinstance(b, ProviderBinding) and b.fn is fn for b in bindings):
            return

        self._container.provide(fn)

    def _autoregister_configurator(self, cls: type) -> None:
        """Register a DI-annotated provider function, skipping duplicates.

        Args:
            fn: The decorated provider callable (sync or async).

        Returns:
            None
        """
        bindings = self._container._bindings

        # Guard against scanning the same module twice
        for name, fn in vars(cls).items():
            if (
                callable(fn)
                and name != "__init__"
                and _has_provider_metadata(fn)
                and any(isinstance(b, ProviderBinding) and b.fn is fn for b in bindings)
            ):
                return

        self._container.install(cls)

    def _find_interfaces(self, cls: type) -> list[Any]:
        """Return all interfaces that *cls* should be bound against during auto-scan.

        Collects two distinct kinds of interface:

        1. **Abstract base classes** — any class in the full MRO (excluding
           ``object``) for which :func:`inspect.isabstract` returns ``True``.
           This covers the classic ``ABCMeta`` / ``@abstractmethod`` pattern.

        2. **Parameterised generic bases** — entries in ``cls.__orig_bases__``
           whose :func:`~typing.get_origin` is a concrete type (not bare
           ``Generic``).  This covers the ``class UserRepository(Repository[User])``
           pattern where ``Repository`` is a plain ``Generic[T]`` class without any
           ``@abstractmethod``, and therefore *not* caught by ``inspect.isabstract``.

        DESIGN: only the **direct** ``__orig_bases__`` of *cls* are examined for
        generics (not the whole MRO).  Walking the full MRO would double-register
        generic parents of parents, which creates duplicate bindings.  For example:

            class TypedRepo(Repository[User]): ...    # has __orig_bases__ = [Repository[User]]
            class UserRepository(TypedRepo): ...      # has __orig_bases__ = [TypedRepo]

        Scanning ``UserRepository`` would correctly add ``TypedRepo`` (abstract check)
        but NOT ``Repository[User]`` a second time.

        Tradeoffs:
            ✅ Handles both ABC-style and Generic-style interfaces automatically
            ✅ Only direct generic bases examined — no accidental double-registration
            ❌ Bare ``Generic[T]`` itself is skipped (origin is ``Generic``) — correct,
               since ``Generic`` is an implementation detail, not a DI interface
            ❌ TypeVar-parameterised bases (e.g. ``Repository[T]``) are included as-is;
               the args comparison in ``_interface_matches`` will require an exact
               match, so ``Repository[T]`` ≠ ``Repository[User]`` at resolution time  ⚠️

        Args:
            cls: The decorated class to inspect.

        Returns:
            A list of interfaces (concrete types or generic aliases), possibly empty.
            Duplicates between the two collection strategies are suppressed.

        Edge cases:
            - cls has no abstract bases and no generic bases → returns ``[]``
            - cls inherits from both an ABC and a generic → both are included
            - cls has a generic base where origin equals an abstract base already
              found → parameterised form is still added (different interface)
        """
        # ── 1. Abstract base classes from the full MRO ────────────────────────
        abstract_bases: list[Any] = [
            base
            for base in cls.__mro__[1:]
            if base is not object and inspect.isclass(base) and inspect.isabstract(base)
        ]

        # ── 2. Parameterised generic bases from cls's OWN __orig_bases__ ──────
        # __orig_bases__ preserves the full generic form; __bases__ strips args.
        # We check only cls's direct bases (not ancestors) to prevent the
        # MRO-duplication problem described above.
        generic_bases: list[Any] = []
        for base in getattr(cls, "__orig_bases__", ()):
            origin = get_origin(base)
            # Skip non-generic bases (origin is None) and bare Generic[T]
            # (origin is Generic) — Generic itself is not a DI interface.
            if origin is None or origin is Generic:
                continue
            generic_bases.append(base)

        # Deduplicate: if a generic base is already represented by its origin in
        # abstract_bases, still include the parameterised form — it is a distinct
        # interface (Repository[User] ≠ Repository for lookup purposes).
        seen = set(id(i) for i in abstract_bases)
        return abstract_bases + [b for b in generic_bases if id(b) not in seen]
