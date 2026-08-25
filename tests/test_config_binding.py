"""Failing tests (RED mode) for Plan 006 — merge/normalise/prefix, coercion,
and `bind_config_object()`.

Encodes `plans/006-configuration-binding.md` Steps 4, 6, 8: the pure-function
core of `providify/config.py` before it exists.

Covered:
    Step 4  — deep merge (later wins, no list concatenation), key
              normalisation, prefix selection.
    Step 6  — the coercion table, one test per row (+ documented edge cases).
    Step 8  — `bind_config_object()`: defaults, required-field aggregation,
              multi-issue aggregation, issue.source, frozen dataclass,
              annotated __init__, Annotated[...] unwrapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import pytest

from providify import ConfigBindingError, DictSource
from providify.config import (
    _deep_merge,
    _normalise_keys,
    _select_prefix,
    bind_config_object,
)

# ─────────────────────────────────────────────────────────────────
#  Step 4 — merge / normalise / prefix
# ─────────────────────────────────────────────────────────────────


class TestDeepMerge:
    def test_deep_merge_of_two_nested_mappings_later_wins(self) -> None:
        a = {"db": {"url": "x", "pool_size": 5}}
        b = {"db": {"pool_size": 20}}

        result = _deep_merge(a, b)

        assert result == {"db": {"url": "x", "pool_size": 20}}

    def test_scalar_replaces_mapping(self) -> None:
        a = {"db": {"url": "x"}}
        b = {"db": "disabled"}

        assert _deep_merge(a, b) == {"db": "disabled"}

    def test_mapping_replaces_scalar(self) -> None:
        a = {"db": "disabled"}
        b = {"db": {"url": "x"}}

        assert _deep_merge(a, b) == {"db": {"url": "x"}}

    def test_list_replaces_list_no_concatenation(self) -> None:
        a = {"replicas": ["a", "b"]}
        b = {"replicas": ["c"]}

        assert _deep_merge(a, b) == {"replicas": ["c"]}


class TestNormaliseKeys:
    def test_mixed_case_keys_collapse_to_one(self) -> None:
        result = _normalise_keys({"DB": {"Pool_Size": 5}})

        assert result == {"db": {"pool_size": 5}}


class TestSelectPrefix:
    def test_prefix_none_returns_whole_mapping(self) -> None:
        mapping = {"db": {"url": "x"}, "log_level": "debug"}

        assert _select_prefix(mapping, None) == mapping

    def test_missing_prefix_subtree_yields_empty_mapping(self) -> None:
        mapping = {"db": {"url": "x"}}

        assert _select_prefix(mapping, "cache") == {}

    def test_prefix_pointing_at_scalar_raises_config_binding_error(self) -> None:
        mapping = {"db": "disabled"}

        with pytest.raises(ConfigBindingError):
            _select_prefix(mapping, "db")

    def test_extra_undeclared_keys_are_ignored_by_select_prefix(self) -> None:
        """§Design: extra keys are ignored downstream by bind_config_object,
        but select_prefix itself just returns the whole subtree untouched.
        """
        mapping = {"db": {"url": "x", "unused_by_target": 1}}

        assert _select_prefix(mapping, "db") == {"url": "x", "unused_by_target": 1}


# ─────────────────────────────────────────────────────────────────
#  Step 6 — coercion table
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _StrTarget:
    value: str


@dataclass(frozen=True)
class _IntTarget:
    value: int


@dataclass(frozen=True)
class _FloatTarget:
    value: float


@dataclass(frozen=True)
class _BoolTarget:
    value: bool


@dataclass(frozen=True)
class _PathTarget:
    value: Path


@dataclass(frozen=True)
class _ListIntTarget:
    value: list[int]


@dataclass(frozen=True)
class _TupleStrTarget:
    value: tuple[str, ...]


@dataclass(frozen=True)
class _SetIntTarget:
    value: set[int]


@dataclass(frozen=True)
class _FrozensetIntTarget:
    value: frozenset[int]


@dataclass(frozen=True)
class _DictStrIntTarget:
    value: dict[str, int]


@dataclass(frozen=True)
class _OptionalStrTarget:
    value: str | None = "default"


class _Color(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass(frozen=True)
class _EnumTarget:
    value: _Color


@dataclass(frozen=True)
class _LiteralTarget:
    value: Literal["a", "b", "c"]


@dataclass(frozen=True)
class _NestedInner:
    host: str


@dataclass(frozen=True)
class _NestedOuter:
    inner: _NestedInner


class _Uncoercible:
    """A type with no coercion rule — used to prove pass-through-verbatim."""


@dataclass(frozen=True)
class _UncoercibleTarget:
    value: _Uncoercible


class TestCoercionTable:
    def test_str_passes_through_and_coerces_non_str(self) -> None:
        result = bind_config_object(_StrTarget, [DictSource({"value": 5})])

        assert result.value == "5"

    def test_int_from_str(self) -> None:
        result = bind_config_object(_IntTarget, [DictSource({"value": "20"})])

        assert result.value == 20

    def test_int_from_bad_str_raises_config_binding_error(self) -> None:
        with pytest.raises(ConfigBindingError):
            bind_config_object(_IntTarget, [DictSource({"value": "twenty"})])

    def test_float_from_str(self) -> None:
        result = bind_config_object(_FloatTarget, [DictSource({"value": "1.5"})])

        assert result.value == 1.5

    @pytest.mark.parametrize(
        "raw",
        ["1", "true", "yes", "on", "TRUE", "Yes", "ON"],
    )
    def test_bool_from_every_accepted_truthy_token(self, raw: str) -> None:
        result = bind_config_object(_BoolTarget, [DictSource({"value": raw})])

        assert result.value is True

    @pytest.mark.parametrize(
        "raw",
        ["0", "false", "no", "off", "FALSE"],
    )
    def test_bool_from_every_accepted_falsy_token(self, raw: str) -> None:
        result = bind_config_object(_BoolTarget, [DictSource({"value": raw})])

        assert result.value is False

    def test_bool_from_rejected_token_raises_config_binding_error(self) -> None:
        with pytest.raises(ConfigBindingError):
            bind_config_object(_BoolTarget, [DictSource({"value": "maybe"})])

    def test_path_from_str(self) -> None:
        result = bind_config_object(_PathTarget, [DictSource({"value": "/tmp/x"})])

        assert result.value == Path("/tmp/x")

    def test_list_int_from_comma_separated_str(self) -> None:
        result = bind_config_object(_ListIntTarget, [DictSource({"value": "1,2,3"})])

        assert result.value == [1, 2, 3]

    def test_list_int_from_real_sequence(self) -> None:
        result = bind_config_object(_ListIntTarget, [DictSource({"value": [1, 2, 3]})])

        assert result.value == [1, 2, 3]

    def test_tuple_str_from_comma_separated_str_with_spaces(self) -> None:
        result = bind_config_object(_TupleStrTarget, [DictSource({"value": "a, b"})])

        assert result.value == ("a", "b")

    def test_set_int_from_comma_separated_str(self) -> None:
        result = bind_config_object(_SetIntTarget, [DictSource({"value": "1,2,2"})])

        assert result.value == {1, 2}

    def test_frozenset_int_from_real_sequence(self) -> None:
        result = bind_config_object(
            _FrozensetIntTarget, [DictSource({"value": [1, 2, 2]})]
        )

        assert result.value == frozenset({1, 2})

    def test_dict_str_int_from_mapping(self) -> None:
        result = bind_config_object(
            _DictStrIntTarget, [DictSource({"value": {"a": "1", "b": 2}})]
        )

        assert result.value == {"a": 1, "b": 2}

    @pytest.mark.parametrize("raw", [None, ""])
    def test_optional_str_absent_or_none_or_empty_becomes_none(self, raw) -> None:
        source_mapping = {} if raw is None else {"value": raw}
        result = bind_config_object(_OptionalStrTarget, [DictSource(source_mapping)])

        assert result.value is None

    def test_optional_str_present_coerces_as_inner_type(self) -> None:
        result = bind_config_object(_OptionalStrTarget, [DictSource({"value": "hi"})])

        assert result.value == "hi"

    def test_enum_by_value(self) -> None:
        result = bind_config_object(_EnumTarget, [DictSource({"value": "red"})])

        assert result.value is _Color.RED

    def test_enum_by_name_case_insensitive(self) -> None:
        result = bind_config_object(_EnumTarget, [DictSource({"value": "BLUE"})])

        assert result.value is _Color.BLUE

    def test_literal_membership(self) -> None:
        result = bind_config_object(_LiteralTarget, [DictSource({"value": "b"})])

        assert result.value == "b"

    def test_literal_non_member_raises_config_binding_error(self) -> None:
        with pytest.raises(ConfigBindingError):
            bind_config_object(_LiteralTarget, [DictSource({"value": "z"})])

    def test_nested_dataclass_from_sub_mapping(self) -> None:
        result = bind_config_object(
            _NestedOuter, [DictSource({"inner": {"host": "x"}})]
        )

        assert result.inner == _NestedInner(host="x")

    def test_uncoercible_declared_type_passes_through_verbatim(self) -> None:
        """§Design: anything not in the table passes through verbatim, no
        issue raised — the target's problem, not providify's.
        """
        sentinel = _Uncoercible()
        result = bind_config_object(
            _UncoercibleTarget, [DictSource({"value": sentinel})]
        )

        assert result.value is sentinel


# ─────────────────────────────────────────────────────────────────
#  Step 8 — bind_config_object()
# ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AllDefaults:
    a: str = "x"
    b: int = 5


@dataclass(frozen=True)
class _RequiredField:
    required: str
    optional: int = 1


@dataclass(frozen=True)
class _ThreeBadFields:
    one: int
    two: bool
    three: Literal["x", "y"]


class _PlainAnnotatedInit:
    def __init__(self, host: str, port: int = 80) -> None:
        self.host = host
        self.port = port


@dataclass(frozen=True)
class _AnnotatedTarget:
    value: Annotated[int, "some metadata"]


class TestBindConfigObject:
    def test_all_defaults_target_constructed_from_empty_mapping(self) -> None:
        result = bind_config_object(_AllDefaults, [DictSource({})])

        assert result == _AllDefaults(a="x", b=5)

    def test_missing_required_field_raises_one_error_naming_it(self) -> None:
        with pytest.raises(ConfigBindingError) as exc:
            bind_config_object(_RequiredField, [DictSource({})])

        assert "required" in str(exc.value)
        assert len(exc.value.issues) == 1

    def test_three_bad_fields_aggregate_into_one_error_with_three_issues(self) -> None:
        with pytest.raises(ConfigBindingError) as exc:
            bind_config_object(
                _ThreeBadFields,
                [DictSource({"one": "not-an-int", "two": "maybe", "three": "nope"})],
            )

        assert len(exc.value.issues) == 3

    def test_issue_source_names_the_originating_source(self) -> None:
        with pytest.raises(ConfigBindingError) as exc:
            bind_config_object(_RequiredField, [DictSource({})])

        issue = exc.value.issues[0]
        assert issue.source is not None

    def test_frozen_dataclass_target_binds(self) -> None:
        result = bind_config_object(_RequiredField, [DictSource({"required": "hi"})])

        assert result == _RequiredField(required="hi", optional=1)

    def test_plain_class_with_annotated_init_binds(self) -> None:
        result = bind_config_object(
            _PlainAnnotatedInit, [DictSource({"host": "example.com"})]
        )

        assert result.host == "example.com"
        assert result.port == 80

    def test_annotated_hint_unwraps_correctly(self) -> None:
        """`typing.get_type_hints(cls, include_extras=...)` must unwrap
        `Annotated[int, ...]` down to `int` for coercion purposes.
        """
        result = bind_config_object(_AnnotatedTarget, [DictSource({"value": "5"})])

        assert result.value == 5
