from __future__ import annotations

from ..metadata import _CONFIG_PROPERTIES_ATTR, ConfigPropertiesMetadata

# ─────────────────────────────────────────────────────────────────
#  @ConfigProperties — typed settings bound from env/YAML/JSON/TOML
#
#  THIS IS NOT @Configuration. `@Configuration` (`decorator/module.py:8-15`)
#  is a grouping namespace for `@Provider` methods — explicitly documented
#  as "NOT a Spring-style 'config bean' you reach for by default". They read
#  as neighbours only in the backlog wording (`BACKLOG.md:38` literally says
#  "typed @Configuration objects"); this plan deliberately keeps them
#  unambiguous, separate decorators in separate modules (plan 006
#  §Alternatives, "Overloading @Configuration as the config-binding
#  decorator" — rejected). If a class ever carries both markers, the scanner
#  treats it as `@ConfigProperties` (dispatch order, `scanner.py`) — that
#  combination is unsupported/ambiguous, not a supported dual-role class.
#
#  DESIGN: @ConfigProperties marks a class as a typed configuration target.
#  The class itself declares nothing about *how* it is bound — it is plain
#  data (a dataclass, a plain class with an annotated __init__, or a
#  pydantic BaseModel). The decorator only stamps two things:
#    1. `prefix`    — which subtree of the merged configuration mapping
#                      belongs to this class (`None` = the whole mapping).
#    2. `sources`    — which `ConfigSource`s to read, in order (later wins).
#
#  `container.bind_config(cls)` (or `container.scan()`, which discovers
#  `@ConfigProperties` classes automatically) is what turns this marker into
#  an actual singleton binding — see `container.py:bind_config` and
#  `providify/config.py:bind_config_object` for the load → merge → normalise
#  → prefix → coerce/model_validate pipeline.
#
#  Usage:
#      @ConfigProperties(prefix="db", sources=(EnvSource(), YamlSource("config.yaml", required=False)))
#      @dataclass(frozen=True)
#      class DbSettings:
#          url: str
#          pool_size: int = 5
#
#      container.bind_config(DbSettings)   # …or container.scan("myapp")
#      container.get(DbSettings)           # DbSettings(url=..., pool_size=...)
#
#  Thread safety:  ✅ Safe — @ConfigProperties only stamps a marker on the class.
#  Async safety:   ✅ Safe — stateless marker, no async state.
# ─────────────────────────────────────────────────────────────────


def ConfigProperties(
    *, prefix: str | None = None, sources: tuple[object, ...] | None = None
):
    """Mark a class as a typed configuration-binding target.

    This is **not** `@Configuration` — see the module-level DESIGN comment
    above. `@ConfigProperties` declares *what* a settings object looks like
    and where its values come from; `container.bind_config(cls)` (or
    `container.scan()`) is what actually performs the binding.

    Args:
        prefix: Selects a subtree of the merged configuration mapping —
            normalised (lower-cased, stripped) before storage. `None`
            (default) binds the whole merged mapping.
        sources: `ConfigSource` instances to read, in order — later sources
            win on key conflicts (deep merge). Defaults to `(EnvSource(),)`
            when omitted — a settings class with no explicit sources reads
            the process environment by default, the lowest-ceremony source.

    Returns:
        A class decorator that stamps `ConfigPropertiesMetadata` on the
        decorated class and returns it unchanged.

    Example — env + YAML, later source wins:
        @ConfigProperties(
            prefix="db",
            sources=(EnvSource(), YamlSource("config.yaml", required=False)),
        )
        @dataclass(frozen=True)
        class DbSettings:
            url: str
            pool_size: int = 5
            replicas: tuple[str, ...] = ()

        container.bind_config(DbSettings)
        container.get(DbSettings)   # DB__URL / DB__POOL_SIZE / config.yaml's db: section
    """
    # Import here, not at module top-level, to keep config.py's own import
    # (stdlib + providify.metadata + providify.exceptions ONLY, plan
    # §Design/Module layout) independent of decorator/config.py — this
    # decorator is the one place allowed to bridge the two.
    from ..config import EnvSource

    effective_sources = tuple(sources) if sources is not None else (EnvSource(),)
    normalised_prefix = prefix.strip().lower() if prefix is not None else None

    def decorator(cls: type) -> type:
        setattr(
            cls,
            _CONFIG_PROPERTIES_ATTR,
            ConfigPropertiesMetadata(
                prefix=normalised_prefix, sources=effective_sources
            ),
        )
        return cls

    return decorator
