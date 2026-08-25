from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import overload

from ..metadata import _DI_CONFIGURATION_ATTR, ConfigurationMetadata

# ─────────────────────────────────────────────────────────────────
#  DIModule / @Configuration — grouping namespace for @Provider methods
#
#  MENTAL MODEL: providify is Jakarta-CDI-first. The default way to register
#  a bean is to annotate the class you own with @Component / @Singleton —
#  NOT to wrap it in a producer. @Provider is providify's @Produces; it
#  exists for the types you can't annotate (third-party/stdlib classes,
#  interfaces, values needing imperative construction). @Configuration is
#  merely an optional namespace that groups several such @Provider methods.
#  It is NOT a Spring-style "config bean" you reach for by default. See
#  PROVIDERS.md for the full decision rule.
#
#  DESIGN: @Configuration marks a class as a DI configuration module.
#  The module class:
#    1. Groups related @Provider methods. A provider method's own
#       parameters are injected from the container (it is registered as a
#       bound method, so `self` is pre-filled) — no __init__ needed:
#           @Provider(singleton=True)
#           def pool(self, cfg: AppConfig) -> DatabasePool: ...
#    2. MAY declare constructor dependencies — the container injects them
#       at install() time (Spring-style convenience, unlike Guice where
#       modules are plain objects with no injection). Useful only when many
#       providers share the same injected config object via self.
#
#  Usage (lean / preferred — per-method injection):
#      @Configuration
#      class DatabaseModule:
#          @Provider(singleton=True)
#          def connection_pool(self, config: AppConfig) -> DatabasePool:
#              return DatabasePool(config.db_url)
#
#      container.scan("myapp")        # auto-installs @Configuration classes
#      container.install(DatabaseModule)  # …or install explicitly
#
#  DESIGN (Plan 008/F5): ordering between modules — depends_on=, explicit
#  over inferred.
#
#  A module whose __init__ or @Provider methods need a type produced by
#  ANOTHER module must declare it:
#      @Configuration(depends_on=[InfraModule])
#      class RepoModule:
#          def __init__(self, pool: DatabasePool): ...   # from InfraModule
#
#  container.install(RepoModule) then transitively installs InfraModule
#  first (providify.modules.resolve_install_order). Rejected alternative:
#  inferring order from provider/constructor parameter types.
#    ✅ inferred: zero authoring cost
#    ❌ inferred: a provider method's own params are resolved LAZILY at
#       first get() — most real edges are invisible at install time; only
#       eagerly-resolved __init__ params would be visible, and Lazy[T]/
#       Live[T]/Provider[T] edges are invisible by construction either way.
#       A missing edge silently mis-orders installation instead of failing
#       loudly. Rejected — see plans/008-module-startup-ordering.md
#       §Design decision for the full comparison table.
#  ✅ explicit depends_on=: cycle → named ModuleCycleError at install time,
#     strictly before any instantiation (no partial installation).
#
#  Lifecycle: install() now runs the module's @PostConstruct (once, after
#  __init__, before its @Provider methods are registered) and records the
#  instance so shutdown()/ashutdown() can run its @PreDestroy — in exact
#  reverse install order, strictly AFTER every singleton has been torn down
#  (container.py's _installed_modules teardown phase 2).
#
#  Thread safety:  ✅ Safe — @Configuration only stamps a marker on the class.
#  Async safety:   ✅ Safe — stateless marker, no async state.
# ─────────────────────────────────────────────────────────────────


@overload
def Configuration(cls: type) -> type: ...
@overload
def Configuration(
    cls: None = None, *, depends_on: type | Sequence[type] = ()
) -> Callable[[type], type]: ...


def Configuration(
    cls: type | None = None,
    *,
    depends_on: type | Sequence[type] = (),
) -> type | Callable[[type], type]:
    """Mark a class as a DI configuration module — a grouping namespace for
    related ``@Provider`` methods.

    ``@Provider`` is providify's ``@Produces`` (Jakarta CDI). Prefer annotating
    the classes you own with ``@Component`` / ``@Singleton`` directly; use
    ``@Provider`` — optionally grouped in a ``@Configuration`` class — only for
    types you can't annotate. ``@Configuration`` is *not* a Spring-style config
    bean you reach for by default. See ``PROVIDERS.md``.

    Dual-form decorator — usable bare, with empty parens, or with keyword
    arguments (same shape as ``@Provider``/``@Component``):
        @Configuration                          # bare
        @Configuration()                        # empty parens — identical to bare
        @Configuration(depends_on=[InfraModule]) # explicit ordering constraint

    When the module is installed (via ``container.install()`` or auto-installed
    by ``container.scan()``), the container:

    1. Transitively installs every module named in ``depends_on=`` first, in
       a deterministic order (``providify.modules.resolve_install_order``).
    2. Instantiates the module class, injecting any constructor deps
       (Spring-style — optional, useful when providers share a config object).
    3. Runs the module's ``@PostConstruct`` hook, if any (once).
    4. Finds every ``@Provider``-decorated method on the class and registers
       each as a bound-method binding, so ``self`` is pre-filled and each
       method's remaining parameters are injected from the container.
    5. Records the instance so its ``@PreDestroy`` hook, if any, runs at
       ``shutdown()``/``ashutdown()`` — in exact reverse install order,
       after every singleton has been torn down.

    Notes:
        - The module class is NOT registered as a component itself —
          it exists only to group providers.
        - A provider method's parameters are injected — no ``__init__`` needed
          unless several providers share the same injected object.
        - ``scan()`` auto-installs ``@Configuration`` classes (deduplicated by
          class identity, now via the container's ``_installed_modules``); an
          explicit ``install()`` afterward is unnecessary and a no-op.
        - Constructor deps (if any) must already be registered before install
          (they are resolved eagerly). For async-only constructor deps, use
          ``await container.ainstall()`` instead.
        - ``depends_on`` is looked up via the class's own ``__dict__`` (not
          inherited) — a plain subclass of a ``@Configuration`` class is not
          itself treated as a module.

    Args:
        cls: The class to mark as a configuration module (bare-usage form).
            ``None`` when called with keyword arguments — the decorator then
            returns a closure to be applied to the class.
        depends_on: Other ``@Configuration`` classes that must be installed
            before this one. Accepts a single class, a list, or a tuple —
            all normalise to a ``tuple[type, ...]``. Defaults to ``()``
            (no ordering constraint).

    Returns:
        The same class, with a ``ConfigurationMetadata`` marker stamped on
        it (bare-usage form) — or a decorator closure that does the same
        when called with keyword arguments.

    Raises:
        ModuleCycleError: Not raised here — raised later, at
            ``container.install()``/``ainstall()`` time, if the declared
            ``depends_on`` edges (across all installed modules) form a cycle.

    Example:
        @Configuration
        class InfraModule:
            @Provider(singleton=True)
            def db_pool(self, settings: Settings) -> DatabasePool:
                return DatabasePool(settings.db_url)   # settings injected

        @Configuration(depends_on=[InfraModule])
        class RepoModule:
            def __init__(self, pool: DatabasePool) -> None:
                self.pool = pool   # resolvable — InfraModule installs first
    """
    # Normalise depends_on to a tuple — accepts a single class, list, or
    # tuple. A bare class is not a Sequence[type] in the type-checker's
    # eyes (Sequence[type] means "sequence of classes", not "is a class"),
    # so `isinstance(depends_on, type)` is the correct discriminator —
    # checking `isinstance(depends_on, Sequence)` would also be true for a
    # class in principle (metaclasses can implement __len__/__getitem__),
    # so class-ness is checked first and wins.
    normalized_depends_on: tuple[type, ...] = (
        (depends_on,) if isinstance(depends_on, type) else tuple(depends_on)
    )

    def _decorate(target: type) -> type:
        setattr(
            target,
            _DI_CONFIGURATION_ATTR,
            ConfigurationMetadata(depends_on=normalized_depends_on),
        )
        return target

    if cls is not None:
        # Bare usage: @Configuration (no call) — cls is the class itself.
        return _decorate(cls)
    # Call usage: @Configuration() or @Configuration(depends_on=[...]) —
    # cls is None, return a decorator closure for Python to apply next.
    return _decorate
