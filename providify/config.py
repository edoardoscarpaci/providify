"""Configuration binding: env / YAML / JSON / TOML → typed objects.

Implements plan `plans/006-configuration-binding.md` — the pure-function
core behind `@ConfigProperties` / `DIContainer.bind_config()`.

DESIGN — module isolation (plan §Design/Module layout):
    This module imports **stdlib + `providify.exceptions` ONLY**. It must
    never import `container.py` or `binding.py` — the same isolation rule
    `validation.py` already follows in this codebase. `config.py` is the
    "source loading + merging" half of the feature; `container.py`'s
    `bind_config()` is the thin integration glue that turns it into a
    provider registration (`provide(fn, returns=cls)`, no new binding type).

DESIGN — pipeline (plan §Design/Design):
    Every `ConfigSource.load()` returns a nested ``Mapping[str, Any]``. The
    pipeline is::

        load each source (in order)
            → deep-merge (later source wins; no list concatenation)
            → normalise keys (lower-case every string key at every level)
            → select prefix subtree (``None`` = whole mapping)
            → branch on ``hasattr(cls, "model_validate")``:
                - yes → ``cls.model_validate(mapping)`` (pydantic v2 path,
                  providify performs NO coercion of its own)
                - no  → coerce each declared field per the coercion table,
                  then ``cls(**kwargs)`` (stdlib path)

DESIGN — pydantic hand-off is duck-typed (research 001 §Pydantic v2):
    `hasattr(cls, "model_validate")` is checked by name only — providify
    never imports pydantic, never pins its version, and never appears in a
    consumer's dependency resolution alongside it. The tradeoff (documented,
    accepted): any class that happens to expose a same-named method is
    treated as the pydantic path. The failure mode is benign — providify
    just stops coercing and lets the target's own `model_validate` raise.

Thread safety: Every public function here is a pure transformation over its
    arguments — no shared mutable state — safe to call concurrently from any
    thread. `EnvSource.load()` reads `os.environ`, which is process-global
    and not itself synchronized by providify; this mirrors `os.environ`'s
    own thread-safety contract, not a new concern this module introduces.
Async safety: All I/O here (file reads) is synchronous. `bind_config_object`
    is called from inside a `@Provider(singleton=True)` factory
    (`DIContainer.bind_config()`), which the container may invoke from a
    sync or async resolution path — either way, this module performs no
    `await`s and blocks the calling thread for the duration of file I/O,
    same as any other synchronous provider factory.
"""

from __future__ import annotations

import enum
import inspect
import json
import os
import tomllib
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from .exceptions import ConfigBindingError, ConfigIssue

# ─────────────────────────────────────────────────────────────────
#  ConfigSource protocol + the five frozen-dataclass sources
# ─────────────────────────────────────────────────────────────────


@runtime_checkable
class ConfigSource(Protocol):
    """Structural contract every configuration source implements.

    A single-method `Protocol` (not an ABC) deliberately — third-party
    sources need only implement `load()`, with no providify import required
    to be recognised as a valid source (plan §Risks, "New public surface is
    permanent" — a single-method Protocol keeps this extensible).

    Thread safety: Implementations should be safe to call from any thread —
        `load()` is expected to be a pure read with no shared mutable state.
    Async safety: `load()` is synchronous by contract; providify never
        awaits a source.
    """

    def load(self) -> Mapping[str, Any]:
        """Return this source's configuration as a nested mapping.

        Returns:
            A `Mapping[str, Any]` — possibly nested, possibly empty. Never
            `None`.

        Raises:
            ConfigBindingError: If this source cannot be loaded at all
                (unreadable/malformed file, missing optional dependency) —
                raised immediately, since there is nothing left to attempt
                for a source that failed to load.
        """
        ...


@dataclass(frozen=True)
class DictSource:
    """In-memory mapping source — the test seam (plan §Design).

    Normalises only its own **top-level** keys to lower-case on `load()`;
    deep normalisation across all levels is the merged-pipeline's job
    (`_normalise_keys`, applied once after every source has been merged),
    not each individual source's.

    Attributes:
        mapping: The raw mapping to hand back from `load()`.

    Example:
        >>> DictSource({"A": 1}).load()
        {'a': 1}
    """

    mapping: Mapping[str, Any] = field(default_factory=dict)

    def load(self) -> dict[str, Any]:
        """Return `mapping` with top-level string keys lower-cased.

        Returns:
            A shallow copy of `mapping`, top-level keys lower-cased.
        """
        return {(k.lower() if isinstance(k, str) else k): v for k, v in self.mapping.items()}


@dataclass(frozen=True)
class EnvSource:
    """Process-environment source — `PREFIX__FIELD` nesting by default.

    `os.environ` values are always `str` — the coercion table
    (`_coerce`/plan §Design) is what makes e.g. `pool_size: int` work from a
    literal `"20"`.

    Attributes:
        delimiter: Segment separator for nested keys. Default `"__"`
            matches pydantic-settings' `env_nested_delimiter` idiom (plan
            §Risks — a public-API default, confirmed against the primary
            internal consumer before merging).

    Edge cases:
        - A key that is *only* the delimiter (e.g. `DB__`), or that has an
          empty segment anywhere, drops the empty segment(s) rather than
          crashing.
        - A segment collision — one env var wants a scalar where another
          already built a nested mapping (or vice versa) — silently skips
          the later, colliding variable rather than raising; env var
          layout collisions are a deployment bug, not something providify
          can safely recover a "correct" answer for at load time.

    Example:
        >>> import os
        >>> os.environ["DB__URL"] = "postgres://x"
        >>> EnvSource().load()
        {'db': {'url': 'postgres://x'}}
    """

    delimiter: str = "__"

    def load(self) -> dict[str, Any]:
        """Read `os.environ` and nest keys by `delimiter`.

        Returns:
            A nested `dict[str, Any]`, string values throughout (env vars
            are always strings — the coercion table converts them later).
        """
        result: dict[str, Any] = {}
        for raw_key, value in os.environ.items():
            segments = [seg.lower() for seg in raw_key.split(self.delimiter) if seg]
            if not segments:
                continue
            node = result
            collided = False
            for seg in segments[:-1]:
                nxt = node.get(seg)
                if nxt is None:
                    nxt = {}
                    node[seg] = nxt
                if not isinstance(nxt, dict):
                    # A scalar already occupies this path segment (set by an
                    # earlier, colliding env var) — skip this variable rather
                    # than silently overwriting or raising (see Edge cases).
                    collided = True
                    break
                node = nxt
            if not collided:
                node[segments[-1]] = value
        return result


def _read_required_file(path: str | Path, required: bool) -> Path | None:
    """Resolve *path* to a `Path`, applying the shared required/missing rule.

    Args:
        path: File path, as given to a file-backed `ConfigSource`.
        required: Whether a missing file should raise.

    Returns:
        The resolved `Path` if it exists; `None` if it does not exist and
        `required` is `False` (caller should return `{}`).

    Raises:
        ConfigBindingError: If the file does not exist and `required` is
            `True` — single issue naming the path.
    """
    p = Path(path)
    if p.exists():
        return p
    if required:
        raise ConfigBindingError(
            str(p), [ConfigIssue(str(p), "required config file not found", str(p))]
        )
    return None


def _require_mapping(data: Any, p: Path) -> dict[str, Any]:
    """Validate that a parsed config document's top level is a mapping.

    Args:
        data: The value returned by the format-specific parser.
        p: The source path — used for the error message.

    Returns:
        `data` as a `dict`, when it is a mapping.

    Raises:
        ConfigBindingError: If `data` is not a mapping (e.g. a top-level
            JSON/YAML list) — a config document must be a mapping.
    """
    if not isinstance(data, Mapping):
        raise ConfigBindingError(
            str(p),
            [
                ConfigIssue(
                    str(p),
                    f"expected a mapping at the top level, got {type(data).__name__}",
                    str(p),
                )
            ],
        )
    return dict(data)


@dataclass(frozen=True)
class JsonSource:
    """JSON file source — stdlib `json`, no extra dependency.

    Attributes:
        path: Path to the `.json` file.
        required: If `True` (default), a missing file raises
            `ConfigBindingError`. If `False`, a missing file yields `{}`.

    Example:
        >>> JsonSource("config.json", required=False).load()
        {}
    """

    path: str | Path
    required: bool = True

    def load(self) -> dict[str, Any]:
        """Parse the JSON file at `path` into a nested mapping.

        Returns:
            `{}` if the file is missing and `required` is `False`;
            otherwise the parsed top-level mapping.

        Raises:
            ConfigBindingError: Missing + `required=True`; malformed JSON
                (`__cause__` chained to `json.JSONDecodeError`); or a
                top-level value that is not a mapping.
        """
        p = _read_required_file(self.path, self.required)
        if p is None:
            return {}
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigBindingError(
                str(p), [ConfigIssue(str(p), f"failed to parse JSON: {e}", str(p))]
            ) from e
        return _require_mapping(data, p)


@dataclass(frozen=True)
class TomlSource:
    """TOML file source — stdlib `tomllib` (guaranteed on the ≥3.12 floor).

    Attributes:
        path: Path to the `.toml` file.
        required: If `True` (default), a missing file raises
            `ConfigBindingError`. If `False`, a missing file yields `{}`.
    """

    path: str | Path
    required: bool = True

    def load(self) -> dict[str, Any]:
        """Parse the TOML file at `path` into a nested mapping.

        Returns:
            `{}` if the file is missing and `required` is `False`;
            otherwise the parsed top-level mapping.

        Raises:
            ConfigBindingError: Missing + `required=True`; malformed TOML
                (`__cause__` chained to `tomllib.TOMLDecodeError`).
        """
        p = _read_required_file(self.path, self.required)
        if p is None:
            return {}
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigBindingError(
                str(p), [ConfigIssue(str(p), f"failed to parse TOML: {e}", str(p))]
            ) from e
        return _require_mapping(data, p)


@dataclass(frozen=True)
class YamlSource:
    """YAML file source — behind the `providify[yaml]` extra (PyYAML).

    `yaml.safe_load` **only** (research 001 §Version/Compatibility —
    `yaml.load()` can execute arbitrary Python code via `!!python/...`
    tags). `yaml` is imported lazily, inside `load()`, so the core install
    never imports it — a missing PyYAML surfaces as an actionable
    `ConfigBindingError` naming the `providify[yaml]` extra, not an
    `ImportError` at `import providify` time.

    Attributes:
        path: Path to the `.yaml`/`.yml` file.
        required: If `True` (default), a missing file raises
            `ConfigBindingError`. If `False`, a missing file yields `{}`.

    Edge cases:
        - An empty YAML document → `yaml.safe_load` returns `None` →
          treated as `{}`.
        - A top-level list (or any non-mapping) → `ConfigBindingError`.
    """

    path: str | Path
    required: bool = True

    def load(self) -> dict[str, Any]:
        """Parse the YAML file at `path` into a nested mapping.

        Returns:
            `{}` if the file is missing and `required` is `False`, or if
            the file parses to an empty document; otherwise the parsed
            top-level mapping.

        Raises:
            ConfigBindingError: Missing + `required=True`; PyYAML not
                installed (`__cause__` chained to `ImportError`, message
                names `providify[yaml]`); malformed YAML (`__cause__`
                chained to `yaml.YAMLError`, includes a `!!python/...` tag,
                which `safe_load` refuses to construct); a top-level value
                that is not a mapping.
        """
        p = _read_required_file(self.path, self.required)
        if p is None:
            return {}

        # Lazy import — see class docstring. Converting ImportError into a
        # ConfigBindingError here is what keeps `import providify` clean of
        # any third-party dependency while still giving an actionable error
        # the moment a caller actually constructs a YamlSource and reads it.
        try:
            import yaml
        except ImportError as e:
            raise ConfigBindingError(
                str(p),
                [
                    ConfigIssue(
                        str(p),
                        "YAML support requires: pip install providify[yaml]",
                        str(p),
                    )
                ],
            ) from e

        try:
            data = yaml.safe_load(p.read_text())
        except yaml.YAMLError as e:
            raise ConfigBindingError(
                str(p), [ConfigIssue(str(p), f"failed to parse YAML: {e}", str(p))]
            ) from e

        if data is None:
            return {}
        return _require_mapping(data, p)


# ─────────────────────────────────────────────────────────────────
#  Merge / normalise / prefix (plan §Design/"The merged mapping")
# ─────────────────────────────────────────────────────────────────


def _normalise_keys(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively lower-case every string key at every level.

    Applied once, to the fully deep-merged mapping — not per-source — so
    `DB__POOL_SIZE` (env), `db.pool_size` (YAML) and `{"DB": {"Pool_Size":}}`
    (JSON) all collapse onto the same key regardless of which source they
    came from (plan §Design).

    Args:
        mapping: A (possibly nested) mapping to normalise.

    Returns:
        A new nested `dict` with every string key lower-cased. Non-mapping
        values (including lists) are left exactly as-is — only mapping
        *keys* are touched, never list elements or scalar values.
    """
    result: dict[str, Any] = {}
    for k, v in mapping.items():
        key = k.lower() if isinstance(k, str) else k
        result[key] = _normalise_keys(v) if isinstance(v, Mapping) else v
    return result


def _deep_merge(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge two mappings — keys in `b` win, recursively.

    Two mappings at the same key merge recursively; any non-mapping value
    (including a `list`) simply replaces whatever was there — there is no
    list concatenation (plan §Design/"Rules, all deliberately boring").

    Args:
        a: The earlier (lower-priority) mapping.
        b: The later (higher-priority) mapping — its values win on conflict.

    Returns:
        A new merged `dict`. Neither `a` nor `b` is mutated.
    """
    result: dict[str, Any] = dict(a)
    for k, v in b.items():
        existing = result.get(k)
        if isinstance(existing, Mapping) and isinstance(v, Mapping):
            result[k] = _deep_merge(existing, v)
        else:
            result[k] = v
    return result


def _select_prefix(mapping: Mapping[str, Any], prefix: str | None) -> Mapping[str, Any]:
    """Select the subtree of `mapping` rooted at `prefix`.

    Args:
        mapping: The fully merged, normalised configuration mapping.
        prefix: A dotted-free, single-segment key selecting a subtree, or
            `None` to select the whole mapping (plan §Design default).

    Returns:
        `mapping` unchanged when `prefix` is `None`; `{}` when `prefix` is
        absent from `mapping` (so an all-defaults target still binds); the
        subtree at `prefix` otherwise — extra keys inside it that the
        eventual target does not declare are returned untouched, ignored
        only by the caller (`bind_config_object`), not by this function.

    Raises:
        ConfigBindingError: If `prefix` is present but maps to a non-mapping
            (scalar) value — a config document that names the right prefix
            but not as a subtree is a genuine misconfiguration, not
            silently treated as empty.
    """
    if prefix is None:
        return mapping
    key = prefix.lower() if isinstance(prefix, str) else prefix
    if key not in mapping:
        return {}
    subtree = mapping[key]
    if not isinstance(subtree, Mapping):
        raise ConfigBindingError(
            f"prefix '{prefix}'",
            [
                ConfigIssue(
                    prefix,
                    f"expected a mapping at prefix '{prefix}', got {subtree!r}",
                    None,
                )
            ],
        )
    return subtree


# ─────────────────────────────────────────────────────────────────
#  Coercion table (plan §Design/"Coercion table")
# ─────────────────────────────────────────────────────────────────

_UNION_ORIGINS = (typing.Union, types.UnionType)
_SEQUENCE_ORIGINS = (list, tuple, set, frozenset)
_ORIGIN_CTORS: dict[Any, Any] = {
    list: list,
    tuple: tuple,
    set: set,
    frozenset: frozenset,
}

_TRUTHY_TOKENS = {"1", "true", "yes", "on"}
_FALSY_TOKENS = {"0", "false", "no", "off"}


def _is_optional_hint(hint: Any) -> bool:
    """Return True if `hint` is `X | None` (or `Optional[X]`)."""
    return typing.get_origin(hint) in _UNION_ORIGINS and type(None) in typing.get_args(hint)


def _coerce(value: Any, hint: Any, path: str, source: str | None) -> tuple[Any, list[ConfigIssue]]:
    """Coerce `value` to `hint`, per plan §Design/"Coercion table".

    Never raises for a single field — every failure comes back as a
    `ConfigIssue` in the returned list so the caller (`_construct_from_mapping`)
    can aggregate every bad field into one `ConfigBindingError` instead of
    failing on the first one.

    Args:
        value: The raw value from the merged configuration mapping.
        hint: The declared type hint for this field (already unwrapped of
            `Annotated[...]` by the caller's `get_type_hints(..., include_extras=False)`).
        path: Dotted/indexed path used for `ConfigIssue.field` on failure,
            e.g. `"pool_size"` or `"db.replicas[1]"`.
        source: Human-readable originating-source label for `ConfigIssue.source`.

    Returns:
        `(coerced_value, [])` on success, or `(None, [issue, ...])` on
        failure — one or more issues (nested-target failures can produce
        several at once).

    Edge cases:
        - `X | None` with `value is None` or `value == ""` → `(None, [])`.
        - A declared type with no rule in the table → passed through
          verbatim, undocumented types are the target's problem, not
          providify's (plan §Design, deliberately bounded).
    """
    origin = typing.get_origin(hint)

    # `X | None` — absent is handled by the caller (_construct_from_mapping);
    # here we only handle a *present* None/"" value, or delegate to the
    # inner type otherwise.
    if origin in _UNION_ORIGINS:
        inner = [a for a in typing.get_args(hint) if a is not type(None)]
        if value is None or value == "":
            return None, []
        if len(inner) == 1:
            return _coerce(value, inner[0], path, source)
        # A Union with more than one non-None member has no table rule —
        # pass through verbatim (bounded scope, plan §Design).
        return value, []

    if hint is str:
        return str(value), []

    if hint is int:
        if isinstance(value, int | float | str) and not isinstance(value, bool):
            try:
                return int(value), []
            except (ValueError, TypeError):
                pass
        return None, [ConfigIssue(path, f"expected int, got {value!r}", source)]

    if hint is float:
        if isinstance(value, int | float | str):
            try:
                return float(value), []
            except (ValueError, TypeError):
                pass
        return None, [ConfigIssue(path, f"expected float, got {value!r}", source)]

    if hint is bool:
        if isinstance(value, bool):
            return value, []
        if isinstance(value, str):
            token = value.strip().lower()
            if token in _TRUTHY_TOKENS:
                return True, []
            if token in _FALSY_TOKENS:
                return False, []
        return None, [ConfigIssue(path, f"expected bool, got {value!r}", source)]

    if hint is Path:
        if isinstance(value, str | Path):
            return Path(value), []
        return None, [ConfigIssue(path, f"expected a path, got {value!r}", source)]

    if origin in _SEQUENCE_ORIGINS:
        args = typing.get_args(hint)
        elem_hint = args[0] if args else Any
        if isinstance(value, str):
            items: list[Any] = [v.strip() for v in value.split(",")] if value.strip() else []
        elif isinstance(value, list | tuple | set | frozenset):
            items = list(value)
        else:
            return None, [ConfigIssue(path, f"expected a sequence, got {value!r}", source)]
        coerced: list[Any] = []
        issues: list[ConfigIssue] = []
        for i, item in enumerate(items):
            c, item_issues = _coerce(item, elem_hint, f"{path}[{i}]", source)
            if item_issues:
                issues.extend(item_issues)
            else:
                coerced.append(c)
        if issues:
            return None, issues
        return _ORIGIN_CTORS[origin](coerced), []

    if origin is dict:
        args = typing.get_args(hint)
        val_hint = args[1] if len(args) > 1 else Any
        if not isinstance(value, Mapping):
            return None, [ConfigIssue(path, f"expected a mapping, got {value!r}", source)]
        coerced_map: dict[Any, Any] = {}
        issues = []
        for k, v in value.items():
            c, item_issues = _coerce(v, val_hint, f"{path}.{k}", source)
            if item_issues:
                issues.extend(item_issues)
            else:
                coerced_map[k] = c
        if issues:
            return None, issues
        return coerced_map, []

    if inspect.isclass(hint) and issubclass(hint, enum.Enum):
        try:
            return hint(value), []
        except ValueError:
            pass
        name = str(value).upper()
        for member in hint:
            if member.name.upper() == name:
                return member, []
        return None, [
            ConfigIssue(path, f"invalid value {value!r} for enum {hint.__name__}", source)
        ]

    if origin is Literal:
        allowed = typing.get_args(hint)
        for a in allowed:
            if str(a) == str(value):
                return a, []
        return None, [ConfigIssue(path, f"expected one of {allowed!r}, got {value!r}", source)]

    if inspect.isclass(hint) and isinstance(value, Mapping):
        # Nested @ConfigProperties / dataclass / plain-class target — recurse.
        # Same duck-typed pydantic hand-off as the top level: a nested field
        # whose declared type also exposes model_validate skips coercion too.
        if hasattr(hint, "model_validate"):
            try:
                return hint.model_validate(value), []
            except Exception as e:  # noqa: BLE001 — wrapped, not swallowed
                return None, [ConfigIssue(path, str(e), source)]
        return _construct_from_mapping(hint, value, source, path_prefix=f"{path}.")

    # Anything else — no rule in the table, pass through verbatim
    # (documented, no issue — plan §Design, deliberately bounded scope).
    return value, []


def _construct_from_mapping(
    cls: type, mapping: Mapping[str, Any], source: str | None, path_prefix: str = ""
) -> tuple[Any, list[ConfigIssue]]:
    """Coerce every declared field of `cls` from `mapping`, then construct it.

    Used both for the top-level stdlib bind path (`bind_config_object`) and
    recursively for nested dataclass/plain-class fields (`_coerce`'s last
    branch) — the exact same field-by-field aggregation applies either way.

    `typing.get_type_hints(cls, include_extras=False)` deliberately, per
    plan §Design/"Coercion table" — `include_extras=False` is what makes
    `Annotated[int, ...]` resolve straight to `int` with no separate
    unwrapping step.

    Args:
        cls: The dataclass or plain annotated-`__init__` class to construct.
        mapping: The (already prefix-selected) configuration subtree.
        source: Human-readable originating-source label, attached to every
            `ConfigIssue` produced here.
        path_prefix: Dotted prefix prepended to each field's path — empty at
            the top level, `"parent."` when called recursively for a nested
            field named `parent`.

    Returns:
        `(instance, [])` on success, or `(None, [issue, ...])` — every field
        failure collected, not just the first (plan §Errors, "Every field is
        attempted before raising").

    Edge cases:
        - `X | None` field absent from `mapping` → explicitly bound to
          `None` (per the coercion table's "absent → None" rule for optional
          fields), even when the field also declares a non-None Python
          default — the coercion table's rule is deliberately the one that
          wins here (plan §Design/"Coercion table").
        - Any other field absent, with a constructor default → left unset,
          so the class's own default applies.
        - Any other field absent, with no constructor default → one
          `ConfigIssue` naming it, `"missing required field"`.
    """
    hints = typing.get_type_hints(cls, include_extras=False)
    try:
        sig = inspect.signature(cls)
    except (TypeError, ValueError) as e:
        return None, [
            ConfigIssue(path_prefix or cls.__name__, f"cannot inspect {cls!r}: {e}", source)
        ]

    kwargs: dict[str, Any] = {}
    issues: list[ConfigIssue] = []

    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        full_path = f"{path_prefix}{name}"
        hint = hints.get(name, Any)

        if name in mapping:
            coerced, field_issues = _coerce(mapping[name], hint, full_path, source)
            if field_issues:
                issues.extend(field_issues)
            else:
                kwargs[name] = coerced
        elif _is_optional_hint(hint):
            # Coercion table: "absent ... → None" for X | None fields.
            kwargs[name] = None
        elif param.default is inspect.Parameter.empty:
            issues.append(ConfigIssue(full_path, "missing required field", source))
        # else: no value supplied, field has a constructor default — leave
        # it unset so the class's own default takes effect.

    if issues:
        return None, issues

    try:
        return cls(**kwargs), []
    except Exception as e:  # noqa: BLE001 — wrapped as a ConfigIssue, not swallowed
        return None, [ConfigIssue(path_prefix or cls.__name__, f"construction failed: {e}", source)]


def _sources_label(sources: Sequence[Any]) -> str | None:
    """Build a human-readable `ConfigIssue.source` label from every source.

    DESIGN: providify's deep-merge pipeline loses per-key provenance by
    construction — a merged mapping has no memory of which source
    contributed which key. Rather than build a separate provenance-tracking
    structure (unbounded complexity for a complexity-M backlog item, plan
    §Non-goals), every issue produced by one `bind_config_object()` call
    is attributed to the *set* of sources that call was given — precise
    enough to point a caller at "one of these" without over-promising
    exact per-field attribution.

    Args:
        sources: The sources passed to `bind_config_object`.

    Returns:
        A comma-joined `repr()` of every source, or `None` if `sources` is
        empty.
    """
    if not sources:
        return None
    return ", ".join(repr(s) for s in sources)


def bind_config_object(cls: type, sources: Sequence[Any], *, prefix: str | None = None) -> Any:
    """Load, merge and bind `sources` into an instance of `cls`.

    The full pipeline described in this module's docstring: load every
    source in order, deep-merge (later wins), normalise keys, select the
    `prefix` subtree, then construct `cls` — via `cls.model_validate(...)`
    when `cls` exposes it (pydantic v2 duck-typed hand-off, no coercion of
    providify's own), or via the stdlib coercion table otherwise.

    Args:
        cls: The target type — a dataclass, a plain class with an annotated
            `__init__`, or a pydantic `BaseModel` (or anything else exposing
            `model_validate`).
        sources: `ConfigSource` instances to load, in order — later sources
            win on key conflicts.
        prefix: Selects a subtree of the merged mapping, or `None` (default)
            to bind the whole mapping.

    Returns:
        A constructed instance of `cls`.

    Raises:
        ConfigBindingError: A source failed to load; the prefix subtree is
            present but not a mapping; on the stdlib path, one or more
            declared fields could not be coerced or a required field is
            missing (every failure aggregated into one error, plan §Errors);
            on the pydantic path, `model_validate` raised (wrapped, with
            `__cause__` chained to the original exception).

    Example:
        >>> bind_config_object(DbSettings, [EnvSource()], prefix="db")
        DbSettings(url='postgres://x', pool_size=20, replicas=())
    """
    merged: dict[str, Any] = {}
    for s in sources:
        merged = _deep_merge(merged, s.load())
    merged = _normalise_keys(merged)
    subtree = _select_prefix(merged, prefix)

    target_name = getattr(cls, "__name__", str(cls))
    source_label = _sources_label(sources)

    if hasattr(cls, "model_validate"):
        # DESIGN: duck-typed hand-off (research 001 §Pydantic v2) — checked
        # by attribute name only, so providify never imports or pins
        # pydantic. Once this branch is taken, providify performs NO
        # coercion of its own: the raw merged mapping is handed to the
        # target verbatim, and pydantic (or anything API-compatible) owns
        # validation entirely.
        try:
            return cls.model_validate(subtree)
        except Exception as e:  # noqa: BLE001 — wrapped, not swallowed
            raise ConfigBindingError(
                target_name, [ConfigIssue("<root>", str(e), source_label)]
            ) from e

    instance, issues = _construct_from_mapping(cls, subtree, source_label)
    if issues:
        raise ConfigBindingError(target_name, issues)
    return instance
