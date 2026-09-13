"""Failing tests (RED mode) for Plan 006 — configuration sources.

Encodes plan `plans/006-configuration-binding.md` Step 1 (and Step 18): the
`ConfigSource` protocol and the five frozen-dataclass sources (`DictSource`,
`EnvSource`, `JsonSource`, `TomlSource`, `YamlSource`) as specified in
§Design/Module layout, before `providify/config.py` exists.

Every test here is expected to fail with an ImportError/AttributeError until
the corresponding implementation steps (2, 3, 18) land.

Covered:
    Step 1  — DictSource, EnvSource (+ delimiter variant), JsonSource,
              TomlSource, YamlSource happy paths; required/missing file;
              malformed file handling; YAML safe_load (no code execution).
    Step 18 — actionable ConfigBindingError when PyYAML is absent.
"""

from __future__ import annotations

import builtins
import os

import pytest

from providify import (
    ConfigBindingError,
    DictSource,
    EnvSource,
    JsonSource,
    TomlSource,
    YamlSource,
)

# ─────────────────────────────────────────────────────────────────
#  DictSource
# ─────────────────────────────────────────────────────────────────


class TestDictSource:
    def test_normalises_keys_to_lowercase(self) -> None:
        """DictSource is the test seam — it still lower-cases top-level keys
        so it behaves consistently with the other sources per §Design.
        """
        source = DictSource({"A": 1})

        assert source.load() == {"a": 1}

    def test_load_returns_nested_mapping_unchanged_besides_key_case(self) -> None:
        source = DictSource({"DB": {"URL": "x"}})

        assert source.load() == {"db": {"URL": "x"}} or source.load() == {"db": {"url": "x"}}
        # DictSource itself only need normalise its own top-level keys;
        # deep normalisation is a separate concern (Step 5) — both shapes
        # are acceptable here, this test only pins non-crashing behaviour.


# ─────────────────────────────────────────────────────────────────
#  EnvSource
# ─────────────────────────────────────────────────────────────────


class TestEnvSource:
    def test_default_delimiter_nests_double_underscore(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default nesting convention is `PREFIX__FIELD`, per §Design example."""
        monkeypatch.setattr(os, "environ", {})
        monkeypatch.setenv("DB__URL", "postgres://x")
        monkeypatch.setenv("DB__POOL_SIZE", "20")
        monkeypatch.setenv("LOG_LEVEL", "debug")

        result = EnvSource().load()

        assert result == {
            "db": {"url": "postgres://x", "pool_size": "20"},
            "log_level": "debug",
        }

    def test_custom_delimiter_variant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`EnvSource(delimiter="_")` splits on a single underscore instead."""
        monkeypatch.setattr(os, "environ", {})
        monkeypatch.setenv("DB_URL", "postgres://x")

        result = EnvSource(delimiter="_").load()

        assert result == {"db": {"url": "postgres://x"}}

    def test_key_that_is_only_the_delimiter_is_dropped_not_crashing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`DB__` (empty trailing segment) must never crash — segment dropped."""
        monkeypatch.setenv("DB__", "x")

        result = EnvSource().load()

        # Must not raise; exact shape is an implementation detail beyond
        # "does not crash", so only assert it returns a mapping.
        assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────
#  JsonSource
# ─────────────────────────────────────────────────────────────────


class TestJsonSource:
    def test_loads_json_file_as_nested_mapping(self, tmp_path) -> None:
        path = tmp_path / "c.json"
        path.write_text('{"db": {"url": "x", "pool_size": 5}}')

        result = JsonSource(path).load()

        assert result == {"db": {"url": "x", "pool_size": 5}}

    def test_missing_file_required_false_returns_empty_mapping(self, tmp_path) -> None:
        path = tmp_path / "missing.json"

        result = JsonSource(path, required=False).load()

        assert result == {}

    def test_missing_file_required_true_raises_config_binding_error(self, tmp_path) -> None:
        path = tmp_path / "missing.json"

        with pytest.raises(ConfigBindingError):
            JsonSource(path, required=True).load()

    def test_malformed_json_raises_config_binding_error_naming_path(self, tmp_path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")

        with pytest.raises(ConfigBindingError) as exc:
            JsonSource(path).load()

        assert str(path) in str(exc.value)

    def test_top_level_list_raises_config_binding_error(self, tmp_path) -> None:
        """A config document must be a mapping — a top-level list is invalid."""
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]")

        with pytest.raises(ConfigBindingError):
            JsonSource(path).load()


# ─────────────────────────────────────────────────────────────────
#  TomlSource
# ─────────────────────────────────────────────────────────────────


class TestTomlSource:
    def test_loads_toml_file_as_nested_mapping(self, tmp_path) -> None:
        path = tmp_path / "c.toml"
        path.write_text('[db]\nurl = "x"\npool_size = 5\n')

        result = TomlSource(path).load()

        assert result == {"db": {"url": "x", "pool_size": 5}}

    def test_missing_file_required_false_returns_empty_mapping(self, tmp_path) -> None:
        path = tmp_path / "missing.toml"

        result = TomlSource(path, required=False).load()

        assert result == {}

    def test_missing_file_required_true_raises_config_binding_error(self, tmp_path) -> None:
        path = tmp_path / "missing.toml"

        with pytest.raises(ConfigBindingError):
            TomlSource(path, required=True).load()

    def test_malformed_toml_raises_config_binding_error_naming_path(self, tmp_path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text("not = = valid")

        with pytest.raises(ConfigBindingError) as exc:
            TomlSource(path).load()

        assert str(path) in str(exc.value)


# ─────────────────────────────────────────────────────────────────
#  YamlSource
# ─────────────────────────────────────────────────────────────────


class TestYamlSource:
    def test_loads_yaml_file_as_nested_mapping(self, tmp_path) -> None:
        path = tmp_path / "c.yaml"
        path.write_text("db:\n  pool_size: 5\n")

        result = YamlSource(path).load()

        assert result == {"db": {"pool_size": 5}}

    def test_missing_file_required_false_returns_empty_mapping(self, tmp_path) -> None:
        path = tmp_path / "missing.yaml"

        result = YamlSource(path, required=False).load()

        assert result == {}

    def test_missing_file_required_true_raises_config_binding_error(self, tmp_path) -> None:
        path = tmp_path / "missing.yaml"

        with pytest.raises(ConfigBindingError):
            YamlSource(path, required=True).load()

    def test_malformed_yaml_raises_config_binding_error_naming_path(self, tmp_path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("db: [unclosed")

        with pytest.raises(ConfigBindingError) as exc:
            YamlSource(path).load()

        assert str(path) in str(exc.value)

    def test_empty_yaml_file_is_treated_as_empty_mapping(self, tmp_path) -> None:
        """safe_load returns None for an empty document — must become {}."""
        path = tmp_path / "empty.yaml"
        path.write_text("")

        result = YamlSource(path).load()

        assert result == {}

    def test_top_level_list_raises_config_binding_error(self, tmp_path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- a\n- b\n")

        with pytest.raises(ConfigBindingError):
            YamlSource(path).load()

    def test_python_object_tag_does_not_execute_and_raises(self, tmp_path) -> None:
        """Proves `safe_load` is used, not `load` — `!!python/object:` must
        never execute arbitrary code, only fail to parse (research 001
        §Version/Compatibility).
        """
        path = tmp_path / "evil.yaml"
        path.write_text("db: !!python/object:providify.exceptions.providifyError {}\n")

        with pytest.raises(ConfigBindingError):
            YamlSource(path).load()


# ─────────────────────────────────────────────────────────────────
#  Step 18 — actionable error when PyYAML is absent
# ─────────────────────────────────────────────────────────────────


class TestYamlSourceMissingDependency:
    def test_missing_pyyaml_raises_actionable_config_binding_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`YamlSource.load()` must import `yaml` lazily inside the method and
        convert `ImportError` into a `ConfigBindingError` telling the user to
        `pip install providify[yaml]` (§Design/YamlSource).
        """
        path = tmp_path / "c.yaml"
        path.write_text("db:\n  url: x\n")

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("No module named 'yaml'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(ConfigBindingError) as exc:
            YamlSource(path).load()

        assert "providify[yaml]" in str(exc.value)
