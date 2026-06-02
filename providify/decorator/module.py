from __future__ import annotations

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
#  Thread safety:  ✅ Safe — @Configuration only stamps a marker on the class.
#  Async safety:   ✅ Safe — stateless marker, no async state.
# ─────────────────────────────────────────────────────────────────


def Configuration(cls: type) -> type:
    """Mark a class as a DI configuration module — a grouping namespace for
    related ``@Provider`` methods.

    ``@Provider`` is providify's ``@Produces`` (Jakarta CDI). Prefer annotating
    the classes you own with ``@Component`` / ``@Singleton`` directly; use
    ``@Provider`` — optionally grouped in a ``@Configuration`` class — only for
    types you can't annotate. ``@Configuration`` is *not* a Spring-style config
    bean you reach for by default. See ``PROVIDERS.md``.

    When the module is installed (via ``container.install()`` or auto-installed
    by ``container.scan()``), the container:

    1. Instantiates the module class, injecting any constructor deps
       (Spring-style — optional, useful when providers share a config object).
    2. Finds every ``@Provider``-decorated method on the class.
    3. Registers each as a bound-method binding, so ``self`` is pre-filled and
       each method's remaining parameters are injected from the container.

    Notes:
        - The module class is NOT registered as a component itself —
          it exists only to group providers.
        - A provider method's parameters are injected — no ``__init__`` needed
          unless several providers share the same injected object.
        - ``scan()`` auto-installs ``@Configuration`` classes (deduplicated by
          class identity); an explicit ``install()`` afterward is unnecessary.
        - Constructor deps (if any) must already be registered before install
          (they are resolved eagerly). For async-only constructor deps, use
          ``await container.ainstall()`` instead.

    Args:
        cls: The class to mark as a configuration module.

    Returns:
        The same class, with a ``ConfigurationMetadata`` marker stamped on it.

    Example:
        @Configuration
        class InfraModule:
            @Provider(singleton=True)
            def db_pool(self, settings: Settings) -> DatabasePool:
                return DatabasePool(settings.db_url)   # settings injected
    """
    setattr(cls, _DI_CONFIGURATION_ATTR, ConfigurationMetadata())
    return cls
