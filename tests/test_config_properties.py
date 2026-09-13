"""Failing tests (RED mode) for Plan 006 — `@ConfigProperties` decorator and
`DIContainer.bind_config()` integration.

Encodes `plans/006-configuration-binding.md` Steps 10 and 13, before
`providify/decorator/config.py`, `providify/metadata.py`'s
`ConfigPropertiesMetadata`, and `DIContainer.bind_config()` exist.

Covered:
    Step 10 — decorator returns same class; metadata carries prefix/sources;
              default sources; prefix normalisation; TypeError guard;
              marker not inherited by subclass.
    Step 13 — bind_config() + get() returns bound instance; singleton
              semantics; sources= override; laziness; override(); validate();
              injection into a @Singleton component.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from providify import (
    Component,
    ConfigProperties,
    DIContainer,
    DictSource,
    EnvSource,
    Singleton,
)
from providify.metadata import _get_config_properties

# ─────────────────────────────────────────────────────────────────
#  Step 10 — decorator contract
# ─────────────────────────────────────────────────────────────────


class TestConfigPropertiesDecorator:
    def test_returns_same_class(self) -> None:
        @ConfigProperties()
        @dataclass(frozen=True)
        class Settings:
            value: str = "x"

        assert Settings.__name__ == "Settings"

    def test_metadata_carries_prefix_and_sources(self) -> None:
        sources = (DictSource({"a": 1}),)

        @ConfigProperties(prefix="db", sources=sources)
        @dataclass(frozen=True)
        class Settings:
            value: str = "x"

        meta = _get_config_properties(Settings)

        assert meta.prefix == "db"
        assert meta.sources == sources

    def test_default_sources_is_env_source_tuple(self) -> None:
        @ConfigProperties()
        @dataclass(frozen=True)
        class Settings:
            value: str = "x"

        meta = _get_config_properties(Settings)

        assert len(meta.sources) == 1
        assert isinstance(meta.sources[0], EnvSource)

    def test_prefix_is_lowercased_and_stripped(self) -> None:
        @ConfigProperties(prefix="  DB  ")
        @dataclass(frozen=True)
        class Settings:
            value: str = "x"

        meta = _get_config_properties(Settings)

        assert meta.prefix == "db"

    def test_bind_config_on_non_decorated_class_raises_type_error(
        self, container: DIContainer
    ) -> None:
        @dataclass(frozen=True)
        class NotDecorated:
            value: str = "x"

        with pytest.raises(TypeError):
            container.bind_config(NotDecorated)

    def test_marker_not_inherited_by_subclass(self) -> None:
        @ConfigProperties(prefix="db")
        @dataclass(frozen=True)
        class Base:
            value: str = "x"

        class Sub(Base):
            pass

        with pytest.raises(TypeError):
            _get_config_properties(Sub)


# ─────────────────────────────────────────────────────────────────
#  Step 13 — container integration
# ─────────────────────────────────────────────────────────────────


class TestBindConfigContainerIntegration:
    def test_bind_config_then_get_returns_bound_instance(self, container: DIContainer) -> None:
        @ConfigProperties(prefix="db", sources=(DictSource({"db": {"url": "x"}}),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str

        container.bind_config(DbSettings)

        result = container.get(DbSettings)

        assert result.url == "x"

    def test_bound_config_is_singleton(self, container: DIContainer) -> None:
        @ConfigProperties(prefix="db", sources=(DictSource({"db": {"url": "x"}}),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str

        container.bind_config(DbSettings)

        first = container.get(DbSettings)
        second = container.get(DbSettings)

        assert first is second

    def test_sources_override_at_call_site(self, container: DIContainer) -> None:
        @ConfigProperties(prefix="db", sources=(EnvSource(),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str

        container.bind_config(DbSettings, sources=[DictSource({"db": {"url": "overridden"}})])

        result = container.get(DbSettings)

        assert result.url == "overridden"

    def test_factory_is_lazy_no_source_read_before_first_get(self, container: DIContainer) -> None:
        calls = []

        class CountingSource(DictSource):
            def load(self):
                calls.append(1)
                return super().load()

        @ConfigProperties(prefix=None, sources=(EnvSource(),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str = "default"

        container.bind_config(DbSettings, sources=[CountingSource({})])

        assert calls == []  # not read yet — registration only

        container.get(DbSettings)

        assert calls == [1]

    def test_container_swap_still_works(self, container: DIContainer) -> None:
        """A config binding is an ordinary ProviderBinding, so `override()` —
        documented (and separately tested in test_mutation.py::
        test_override_skips_provider_bindings) to leave ProviderBinding
        entries in place — does not swap it. The documented mechanism for
        replacing *any* binding, provider or class, is reset_binding() +
        bind()/provide(); that mechanism works unchanged for config bindings.
        """

        @ConfigProperties(prefix="db", sources=(DictSource({"db": {"url": "x"}}),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str

        @Component()
        @dataclass(frozen=True)
        class OverrideSettings(DbSettings):
            url: str = "overridden"

        container.bind_config(DbSettings)
        container.reset_binding(DbSettings)
        container.bind(DbSettings, OverrideSettings)

        result = container.get(DbSettings)
        assert isinstance(result, OverrideSettings)
        assert result.url == "overridden"

    def test_validate_reports_config_binding_as_present(self, container: DIContainer) -> None:
        @ConfigProperties(prefix="db", sources=(DictSource({"db": {"url": "x"}}),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str

        container.bind_config(DbSettings)

        report = container.validate()

        assert report.ok is True

    def test_config_properties_class_injected_into_singleton_component(
        self, container: DIContainer
    ) -> None:
        @ConfigProperties(prefix="db", sources=(DictSource({"db": {"url": "x"}}),))
        @dataclass(frozen=True)
        class DbSettings:
            url: str

        @Singleton
        class Service:
            def __init__(self, settings: DbSettings) -> None:
                self.settings = settings

        container.bind_config(DbSettings)
        container.register(Service)

        service = container.get(Service)

        assert service.settings.url == "x"
