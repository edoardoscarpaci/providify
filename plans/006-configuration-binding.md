# Plan 006 — Configuration binding: env / YAML / JSON / TOML → typed objects (`@ConfigProperties`) (F3)

Implements backlog item **F3** (🟡 should, complexity M — `BACKLOG.md:38`): *"Confirmed pain point
in interview. Removes hand-written `@Provider` factories that read env/config manually — the #1 gap
scout identified."*

Dependency and API grounding: `design/configuration-binding/research/001-config-dependency-strategy.md`
(hereafter **research 001**). Industry framing: table-stakes across python-dependency-injector,
Spring and FastAPI+pydantic — research 001 §Findings/§Librarian's Note, cited via `BACKLOG.md:38`.

## Goal

A typed settings object is declared once and resolved from the container like any other bean, with
no hand-written factory:

```python
@ConfigProperties(prefix="db", sources=(EnvSource(), YamlSource("config.yaml", required=False)))
@dataclass(frozen=True)
class DbSettings:
    url: str
    pool_size: int = 5
    replicas: tuple[str, ...] = ()

container.bind_config(DbSettings)          # …or container.scan("myapp") picks it up
container.get(DbSettings)                  # DbSettings(url='postgres://…', pool_size=20, …)
```

`DB__URL=postgres://…`, `DB__POOL_SIZE=20` and a `config.yaml` section `db:` all feed the same
object; later sources win. Every field failure is reported in **one** aggregated
`ConfigBindingError`, at `bind_config()`/first-`get()` time, not on first use of the value.

If the target class exposes `model_validate` (pydantic v2 and anything API-compatible), providify
hands it the merged mapping verbatim and performs **no** coercion of its own — so a pydantic
`BaseModel` works as a `@ConfigProperties` target with full pydantic validation, **without
providify depending on pydantic**.

## Non-goals

- **No runtime dependency on pydantic, pydantic-settings, or PyYAML in the core install.** See
  §Design/§Alternatives — providify's `pyproject.toml` declares no `dependencies` key at all today
  (`pyproject.toml:1-46`), and this plan keeps it that way. YAML lands behind a
  `providify[yaml]` extra.
- **No validation framework.** providify coerces to the declared type or raises. Constraints
  (ranges, regexes, cross-field rules) are the target type's job — which is precisely what makes
  the pydantic hand-off valuable.
- **No config hot-reload / `@RefreshScope`.** Config is bound once, at singleton scope. A future
  `container.reload_config(cls)` is possible but out of scope.
- **No secret masking in `repr()`**, no vault/SSM/etcd sources, no remote config.
- **No `.env`-file source.** `.env` parsing has its own escaping/quoting rules; users can point
  `python-dotenv` at the process env before constructing the container. Revisit on demand.
- **No profile-aware source selection** (`config-{profile}.yaml`). Deliberately deferred so Plan
  006 does not depend on Plan 005 (F2) and vice versa; it is the obvious follow-up once both land.
- **No change to `@Configuration`.** `@Configuration` stays exactly what it is documented to be — a
  grouping namespace for `@Provider` methods, explicitly *"NOT a Spring-style config bean"*
  (`providify/decorator/module.py:8-15`, `:50-52`). See §Alternatives.
- **No new `Binding` subclass.** Config binding reuses `provide(fn, returns=cls)`
  (`container.py:686-729`).
- **No CLI-argument source.**
- **No version bump** in `pyproject.toml`'s `version` — `CHANGELOG.md` entry only (the
  `optional-dependencies` table *is* edited, step 16).

## Design

### Dependency strategy: stdlib core, pydantic as a *target*, YAML as an extra

Research 001 §Librarian's Note lays out three paths — (a) pydantic-settings as an optional feature,
(b) stdlib-only with manual casting, (c) *"both (minimal core + rich pydantic-backed mode)"*. This
plan takes (c), but with a twist that avoids maintaining two binders:

> providify owns **source loading + merging**. The **target type** owns validation.

```
 EnvSource      ─┐
 YamlSource     ─┤
 JsonSource     ─┼─►  merge (deep, later wins)  ─►  normalise keys  ─►  select prefix subtree
 TomlSource     ─┤                                                            │
 DictSource     ─┘                                                            ▼
                                              ┌───────────────────────────────┴──────────┐
                                              │ target has `model_validate`?             │
                                              ├── yes ─► cls.model_validate(mapping)      │  ← pydantic v2 path
                                              └── no  ─► coerce per resolved type hint,   │  ← stdlib dataclass path
                                                         then cls(**kwargs)               │
                                              └──────────────────────────────────────────┘
```

The pydantic hand-off is a **duck-typed** `hasattr(cls, "model_validate")` check — providify never
imports pydantic, never pins its version, and never appears in a consumer's dependency resolution
alongside it. Research 001 §Pydantic v2 records `model_validate` as the stable v2 entry point and
MIT licensing for both packages; research 001 §Version/Compatibility confirms no Python-floor
conflict (pydantic-settings needs ≥3.10; providify pins `requires-python = ">=3.12"`,
`pyproject.toml:10`) — so a consumer who *wants* pydantic-settings can still use it, they simply do
not get it forced on them.

Formats and their cost:

| source | parser | dependency |
|---|---|---|
| `EnvSource` | `os.environ` | stdlib |
| `JsonSource` | `json` | stdlib |
| `TomlSource` | `tomllib` | stdlib (guaranteed on the ≥3.12 floor, `pyproject.toml:10`) |
| `YamlSource` | `yaml.safe_load` | **`providify[yaml]` extra → PyYAML** |
| `DictSource` | — | stdlib (test seam) |

PyYAML over ruamel.yaml per research 001 §YAML Library Recommendation (*"PyYAML … is the pragmatic
choice for config reading (not round-trip editing); ruamel.yaml only if users need YAML 1.2 or
round-trip preservation"*). `yaml.safe_load` **only** — research 001 §Version/Compatibility,
*"`load()` can execute arbitrary Python code"*. `YamlSource.load()` imports `yaml` lazily inside
the method and converts `ImportError` into an actionable
`ConfigBindingError("YAML support requires: pip install providify[yaml]")`.

### The merged mapping: one normalisation, one prefix meaning

Every source returns a nested `Mapping[str, Any]`. A single normalisation pass lower-cases **every**
string key at **every** level, so `DB__POOL_SIZE`, `db.pool_size` (YAML) and `"DB": {"Pool_Size":}`
(JSON) all land on the same key. `prefix` then selects a subtree:

```
EnvSource(delimiter="__")   DB__URL=x, DB__POOL_SIZE=20, LOG_LEVEL=debug
                            → {"db": {"url": "x", "pool_size": "20"}, "log_level": "debug"}

YamlSource("config.yaml")   db: {pool_size: 5}
                            → {"db": {"pool_size": 5}}

merge (later wins, deep)    → {"db": {"url": "x", "pool_size": "20"}, "log_level": "debug"}
prefix="db"                 → {"url": "x", "pool_size": "20"}
coerce + construct          → DbSettings(url="x", pool_size=20)
```

Rules, all deliberately boring:

- **Deep merge**: two mappings at the same key merge recursively; any non-mapping value replaces
  whatever was there. No list concatenation — a later list *replaces* an earlier one.
- **`prefix=None`** (default) → the whole merged mapping is the binding input.
- **Missing prefix subtree** → `{}`, so a target whose fields all have defaults still binds. A
  target with a required field raises `ConfigBindingError` naming the missing field — the useful
  failure.
- **Prefix subtree present but not a mapping** → `ConfigBindingError`.
- **Extra keys** in the subtree that the target does not declare are **ignored** (they belong to
  another consumer of the same file). This is a decision, not an oversight — an "unknown key" strict
  mode is a plausible later `@ConfigProperties(strict=True)` knob.

### Coercion table (stdlib target path only)

Field types come from `typing.get_type_hints(cls, include_extras=False)` — `config.py` stays isolated
from `container.py` (the `validation.py` pattern the scout recorded) and therefore does **not** reuse
the container's `_resolve_class_annotations`/`_hints_cache`.

| declared type | accepted raw | result |
|---|---|---|
| `str` | anything | `str(value)` (already-`str` passes through) |
| `int` / `float` | `str`, `int`, `float` | parsed; `ValueError` → aggregated issue |
| `bool` | `bool`, or `str` in `{1,true,yes,on}` / `{0,false,no,off}` (case-insensitive) | `True`/`False`; anything else → issue |
| `pathlib.Path` | `str`, `Path` | `Path(value)` |
| `list[X]`, `tuple[X, ...]`, `set[X]`, `frozenset[X]` | real sequence, or comma-separated `str` | each element coerced as `X` |
| `dict[str, X]` | mapping | values coerced as `X` |
| `X \| None` | absent, `None`, `""` → `None`; else coerce as `X` | |
| `Enum` subclass | value first, then `.name` (case-insensitive) | member |
| `Literal[...]` | membership test after `str` comparison | value or issue |
| nested `@ConfigProperties` / dataclass | mapping | recursive bind of the sub-mapping |
| anything else | — | passed through **verbatim**, documented, no issue |

Research 001 §Lightweight Alternatives explicitly grades this path *"verbose type coercion, but
aligns with providify's 'minimal core' philosophy"* — the table above is the bounded, documented
version of that verbosity, and the pydantic path exists for anyone who wants more.

### Container integration: `provide(factory, returns=cls)`, no new binding type

`container.provide()` already supports an explicit interface override that takes priority over the
return annotation (`container.py:686-729`, priority rules at `:692-698` — shipped in v1.1.1 per
`BACKLOG.md:68-69`). Config binding is therefore *exactly* a provider registration:

```python
def bind_config(self, cls: type, *, sources: Sequence[ConfigSource] | None = None) -> None:
    meta = _get_config_properties(cls)          # raises TypeError if not decorated
    effective = tuple(sources) if sources is not None else meta.sources

    @Provider(singleton=True)
    def _config_factory() -> Any:
        return bind_config_object(cls, effective, prefix=meta.prefix)

    self.provide(_config_factory, returns=cls)
```

Consequences, all free:

- Singleton scope, cached, torn down and ordered by Plan 004's `_singleton_order` like any provider.
- Visible to `describe()`, `validate()` (Plan 003), `override()` and `reset_binding()` with no
  special-casing.
- The `sources=` call-site override mirrors `provide(fn, returns=...)`'s own
  override-at-call-site precedence (`container.py:692-698`) — which is precisely the pattern the
  scout flagged as the one to follow. It is the test seam: `bind_config(DbSettings,
  sources=[DictSource({...})])` needs no env or files.
- **Binding is lazy** by default: the factory runs on first `get()`. `bind_config(..., eager=True)`
  is *not* added — `container.warm_up()` and `container.validate()` already exist for
  fail-at-startup, and adding a third knob duplicates them.

`scan()` picks `@ConfigProperties` classes up via a fourth branch in `_scan_module`'s dispatch
(`scanner.py:122-136`), deduped by class identity exactly like `_autoregister_configurator`'s
`_installed_configurations` set (`scanner.py:71-83`, `:235-262`) — a `set[type]` is required here
for the same reason recorded there: the registered object is a *closure*, never identical to
anything reachable from `vars(cls)`.

### Errors

`ConfigBindingError(providifyError)` in `providify/exceptions.py`, following the aggregation
precedent this codebase already set twice (`ContainerValidationError.report`, `ShutdownError.failures`
— `exceptions.py:204-249`, Plan 004 step 5):

```python
@dataclass(frozen=True)
class ConfigIssue:
    field: str          # "pool_size" | "db.replicas[1]"
    message: str        # "expected int, got 'twenty'"
    source: str | None  # "EnvSource(DB__POOL_SIZE)" | "YamlSource(config.yaml)"

class ConfigBindingError(providifyError):
    def __init__(self, target: str, issues: Sequence[ConfigIssue]) -> None: ...
    # .target, .issues, message = one line per issue
```

**Every** field is attempted before raising — a config file with four typos reports four issues, not
one. Source-loading failures (unreadable file, malformed YAML, missing PyYAML) raise
`ConfigBindingError` immediately with a single issue, because there is nothing left to attempt.

### Module layout

```
providify/config.py        ← new. Pure: sources, merge, normalise, coerce, bind_config_object.
                             Imports stdlib + providify.metadata + providify.exceptions ONLY.
                             MUST NOT import container.py (the validation.py isolation pattern).
providify/metadata.py      ← + _CONFIG_PROPERTIES_ATTR, ConfigPropertiesMetadata, accessors
providify/decorator/config.py  ← new. @ConfigProperties (mirrors decorator/module.py's shape)
providify/exceptions.py    ← + ConfigIssue, ConfigBindingError
providify/container.py     ← + bind_config()
providify/scanner.py       ← + _autoregister_config_properties()
```

`@ConfigProperties` gets its own decorator module rather than joining `decorator/module.py`, because
the whole point (§Alternatives) is that it is **not** `@Configuration` and the two must not read as
neighbours.

### Naming

`@ConfigProperties` — MicroProfile Config's `@ConfigProperties`, the Jakarta-family analogue of
Spring's `@ConfigurationProperties`. Chosen over:

- `@Configuration` — taken, and the collision is the whole hazard (see §Alternatives).
- `@Settings` — pydantic-flavoured; providify's stated frame is Jakarta-first (`decorator/module.py:8`).
- `@ConfigurationProperties` — Spring's exact spelling; longer, and reads even closer to
  `@Configuration`.

### Alternatives considered

- **Hard dependency on `pydantic` + `pydantic-settings`.** ✅ The industry standard — research 001
  §Findings/§Librarian's Note #1: *"Pydantic v2 + pydantic-settings is the industry standard for
  typed config binding in Python"*, with `settings_customise_sources()`, `JsonConfigSettingsSource`
  and `YamlConfigSettingsSource` composing multi-source binding out of the box (research 001
  §Pydantic v2, *File loading APIs*); ✅ MIT-licensed, actively maintained (2.13.4 core / 2.15.0
  settings, August 2026 — research 001 §Pydantic v2); ✅ no Python-floor conflict (research 001
  §Version/Compatibility). ❌ providify ships with **zero** runtime dependencies today
  (`pyproject.toml` has no `dependencies` key; `:37-42` lists only dev tools) — adding
  pydantic + the compiled `pydantic-core` wheel to every install contradicts the library's
  minimal-core mission and imposes a pydantic version constraint on every consumer, including those
  who never touch config. ❌ It also inverts ownership: `BaseSettings` wants to *be* the settings
  base class, which would make every providify config object inherit from a third-party type.
  **Rejected as a hard dependency; adopted as the preferred optional target type** via the
  `model_validate` hand-off, which is the strictly better half of the deal.
- **Re-implementing pydantic-style validation in providify.** ✅ zero dependencies, full control.
  ❌ Unbounded scope for a complexity-M backlog item, and research 001 §Lightweight Alternatives
  finds *"no true lightweight alternative exists that covers env + YAML + JSON + type coercion
  without rolling custom code or pulling in pydantic anyway"*. **Rejected** — providify coerces to
  the declared type and stops there.
- **Overloading `@Configuration` as the config-binding decorator** (literally what `BACKLOG.md:38`
  says: *"typed `@Configuration` objects"*). ✅ no new decorator; matches the backlog wording. ❌
  `@Configuration` is documented at length as a *provider grouping namespace* and explicitly
  *"NOT a Spring-style 'config bean' you reach for by default"* (`decorator/module.py:8-15`,
  `:50-52`), a framing recorded as a deliberate project decision. Overloading it would make
  `install()` (`container.py:4606-4632`) have to branch on which kind of `@Configuration` it got.
  **Rejected** — the backlog wording predates the Jakarta framing; `@ConfigProperties` is a new,
  unambiguous decorator.
- **A dedicated `ConfigBinding(Binding)` subclass.** ✅ first-class in `describe()`; ✅ could carry
  source metadata for introspection. ❌ a third binding type to keep in sync across `_filter`,
  `Binding.validate`, `_teardown_plan`, `copy()`, the descriptors and the scanner — when
  `provide(fn, returns=cls)` (`container.py:686-729`) already expresses "this callable produces this
  interface" precisely. **Rejected.**
- **`ruamel.yaml` instead of PyYAML.** ❌ heavier, and its advantage (comment/order preservation on
  round-trip) is irrelevant for read-only config — research 001 §YAML Library Recommendation.
  **Rejected.**
- **`python-decouple` / `environs` for the env layer.** ❌ Both are `.env`/env-only with no
  YAML/JSON file support (research 001 §Lightweight Alternatives table), so providify would still
  need its own file sources and merge — a dependency that removes almost nothing. **Rejected.**
- **Binding eagerly inside `bind_config()`** rather than in a provider factory. ✅ config errors
  surface at registration, with the registering line in the traceback. ❌ forces every config source
  (files, env) to be readable at import/registration time, which breaks `container.copy()`-based
  tests and any "register now, configure later" flow; `warm_up()`/`validate()` already provide
  fail-fast on demand. **Rejected.**

## Steps

1. [x] `tests/test_config_sources.py` — new file. Failing tests for each source in isolation:
   `DictSource({"A": 1})` → `{"a": 1}`; `EnvSource()` with `monkeypatch.setenv` for
   `DB__URL`/`DB__POOL_SIZE`/`LOG_LEVEL` → the nested/normalised shape in §Design;
   `EnvSource(delimiter="_")` variant; `JsonSource(tmp_path/"c.json")`;
   `TomlSource(tmp_path/"c.toml")`; `YamlSource(tmp_path/"c.yaml")`;
   `required=False` + missing file → `{}`; `required=True` + missing file → `ConfigBindingError`;
   malformed JSON/YAML/TOML → `ConfigBindingError` naming the path; a YAML file containing
   `!!python/object:` must **not** execute (proves `safe_load`, research 001 §Version/Compatibility).
2. [x] `providify/exceptions.py` — add, after `ShutdownError` (`exceptions.py:~249`, the tail of the
   aggregation family): `@dataclass(frozen=True) class ConfigIssue(field, message, source)` and
   `class ConfigBindingError(providifyError)` holding `.target` / `.issues`, message = header +
   one line per issue. Docstring in project style (purpose, Attributes, Example), mirroring
   `ContainerValidationError`'s.
3. [x] `providify/config.py` — new module, part 1: the `ConfigSource` protocol
   (`def load(self) -> Mapping[str, Any]`) and the five frozen-dataclass sources
   (`DictSource`, `EnvSource`, `JsonSource`, `TomlSource`, `YamlSource`). `YamlSource.load()`
   imports `yaml` **inside the method** and re-raises `ImportError` as a `ConfigBindingError`
   telling the user to `pip install providify[yaml]`; uses `yaml.safe_load` only. Complete
   docstrings per CLAUDE.md (Args/Returns/Raises/Thread safety/Async safety/Edge cases/Example).
   **Must not import `container.py` or `binding.py`.**
4. [x] `tests/test_config_binding.py` — new file. Failing tests for merge + normalise + prefix:
   deep merge of two nested mappings (later wins); a scalar replacing a mapping and vice versa; a
   list replacing a list (no concatenation); mixed-case keys collapsing to one; `prefix=None`
   returns the whole mapping; a missing prefix subtree yields `{}`; a prefix pointing at a scalar
   raises `ConfigBindingError`; extra undeclared keys are ignored.
5. [x] `providify/config.py` — part 2: `_normalise_keys()`, `_deep_merge()`, `_select_prefix()`.
6. [x] `tests/test_config_binding.py` — failing tests for the coercion table: one test per row of
   §Design, both the happy path and the failure; plus `int` from `"20"`, `bool` from every accepted
   token and one rejected token, `list[int]` from `"1,2,3"` **and** from `[1,2,3]`,
   `tuple[str, ...]` from `"a, b"`, `Path`, `Enum` by value and by name, `Literal`,
   `str | None` from absent / `None` / `""`, a nested dataclass from a sub-mapping, and an
   un-coercible declared type (e.g. `Callable`) passing through verbatim.
7. [x] `providify/config.py` — part 3: `_coerce(value, hint, path) -> tuple[Any, ConfigIssue | None]`
   implementing the table. Never raises for a single field — returns an issue so the caller can
   aggregate.
8. [x] `tests/test_config_binding.py` — failing tests for `bind_config_object()`:
   all-defaults target with an empty mapping → constructed; a missing **required** field → one
   `ConfigBindingError` naming that field; **three** bad fields → one error carrying **three**
   `ConfigIssue`s (aggregation, not fail-fast); `issue.source` names the originating source;
   a frozen dataclass target; a plain (non-dataclass) class with an annotated `__init__`;
   `include_extras` / `Annotated[...]` hints unwrap correctly.
9. [x] `providify/config.py` — part 4: `bind_config_object(cls, sources, *, prefix=None) -> Any`.
   Loads each source in order → normalise → deep-merge → select prefix → branch on
   `hasattr(cls, "model_validate")`:
   - **pydantic path**: `cls.model_validate(mapping)`, wrapping any raised exception in
     `ConfigBindingError` with a single issue that preserves `str(exc)` and chains `__cause__`.
     A DESIGN comment must state the duck-typing rationale (research 001 §Pydantic v2 — providify
     never imports or pins pydantic).
   - **stdlib path**: `typing.get_type_hints(cls)` → coerce each declared field present in the
     mapping → aggregate issues → `cls(**kwargs)`.
10. [x] `tests/test_config_properties.py` — new file. Failing tests for the decorator:
    `@ConfigProperties()` returns the same class; metadata carries `prefix` and `sources`;
    default `sources` is `(EnvSource(),)`; `prefix` is normalised (lower-cased, stripped);
    a non-decorated class passed to `bind_config()` raises `TypeError` (mirroring `install()`'s
    guard at `container.py:4629-4630`); the marker is **not** inherited by a subclass
    (`__dict__` lookup, matching `_is_alternative` at `metadata.py:413-415`).
11. [x] `providify/metadata.py` — add `_CONFIG_PROPERTIES_ATTR = "__di_config_properties__"` beside
    `_DI_CONFIGURATION_ATTR` (`metadata.py:73`), `@dataclass(frozen=True) class
    ConfigPropertiesMetadata(prefix: str | None, sources: tuple[Any, ...])` beside
    `StereotypeMetadata` (`:106-119`), and `_get_config_properties(cls)` /
    `_has_config_properties(cls)` accessors mirroring `_get_configuration_module`
    (`metadata.py:283-285`). `sources` is typed `tuple[Any, ...]` to keep `metadata.py` free of a
    `config.py` import (the module already uses this dodge for `StereotypeMetadata.scope`,
    `metadata.py:110-112`).
12. [x] `providify/decorator/config.py` — new module. `ConfigProperties(*, prefix=None, sources=None)`
    returning a class decorator that stamps `ConfigPropertiesMetadata`. Module-level DESIGN comment
    in the shape of `decorator/module.py:5-41`, whose **first paragraph must state that this is not
    `@Configuration`** and cross-link `decorator/module.py:8-15`. Full docstring with an example
    covering env + YAML.
13. [x] `tests/test_config_properties.py` — failing tests for container integration:
    `bind_config(DbSettings)` then `get(DbSettings)` returns a bound instance; the instance is a
    **singleton** (two `get()`s return the same object); `bind_config(cls, sources=[DictSource(...)])`
    overrides the decorator's sources; the factory is **lazy** (no source is read until the first
    `get()` — assert with a counting `DictSource` subclass); `container.override(DbSettings, other)`
    still works; `container.validate()` (Plan 003) reports the binding as present;
    a `@ConfigProperties` class injected into a `@Singleton` component resolves normally.
14. [x] `providify/container.py` — add `bind_config(cls, *, sources=None)` immediately after
    `provide()` (`container.py:729`), implemented exactly as the closure in §Design. Docstring must
    document: the `sources` precedence (call site > decorator), the singleton/lazy semantics, the
    `TypeError` on an undecorated class, the `ConfigBindingError` raised at **first resolution**
    (not at registration), and a "see also `warm_up()` / `validate()` for fail-at-startup" pointer.
    Uses the existing `provide(..., returns=cls)` path — **no** direct `_bindings.append`.
15. [x] `providify/scanner.py:122-136` — add a fourth dispatch branch:
    `elif inspect.isclass(obj) and _has_config_properties(obj): self._autoregister_config_properties(obj)`,
    placed **before** the `_has_configuration_module` branch (`:135`) so a class carrying both
    markers is treated as config. Add `_autoregister_config_properties(cls)` deduped by a
    `self._bound_configs: set[type]` initialised in `__init__` beside `_installed_configurations`
    (`scanner.py:83`) — same rationale as the comment at `scanner.py:71-83` (the registered object
    is a closure, so `b.fn is fn` can never match). Test: scanning the same module twice registers
    one binding.
16. [x] `pyproject.toml` — add
    `[project.optional-dependencies]` with `yaml = ["PyYAML>=6.0"]` (research 001 §YAML Library
    Recommendation / §Pydantic v2 *Note on dependencies*), and add `PyYAML>=6.0` **and**
    `pydantic>=2.13` (research 001 §Pydantic v2 — 2.13.4 is current stable) to
    `[dependency-groups].dev` (`pyproject.toml:37-42`) so both integration paths are tested. The
    `[project]` table still gains **no** `dependencies` key.
17. [x] `tests/test_config_pydantic.py` — new file, guarded by
    `pytest.importorskip("pydantic")`. A `BaseModel` target with a constrained field binds through
    the `model_validate` path; a value violating a pydantic constraint raises `ConfigBindingError`
    whose `__cause__` is the pydantic `ValidationError` and whose message preserves pydantic's text;
    providify performs **no** coercion on that path (a field declared `int` receiving `"20"` is
    handed to pydantic as the string and pydantic coerces it); `import providify` alone does not
    import `pydantic` (assert `"pydantic" not in sys.modules` in a subprocess).
18. [x] `tests/test_config_sources.py` — a test asserting the actionable error when PyYAML is
    absent: monkeypatch `builtins.__import__` (or `sys.modules["yaml"] = None`) so importing `yaml`
    fails, then assert `YamlSource(...).load()` raises `ConfigBindingError` whose message contains
    `providify[yaml]`.
19. [x] `providify/__init__.py` — export `ConfigProperties`, `ConfigSource`, `DictSource`,
    `EnvSource`, `JsonSource`, `TomlSource`, `YamlSource`, `ConfigBindingError`, `ConfigIssue`,
    `ConfigPropertiesMetadata`. Add to `__all__` in the existing grouped order (`__init__.py:3-86`):
    a new `# Configuration binding` group after `# Module` (`:31-32`), and the two exceptions inside
    the existing exception group (`:36-46`). Add the matching import lines
    (`from .config import ...`, `from .decorator.config import ConfigProperties`) to the block at
    `__init__.py:89-157`.
20. [x] `README.md`, `docs/agents/usage-rules.md`, `CHANGELOG.md` — a README section (env + YAML
    example, the `providify[yaml]` extra, the pydantic hand-off), a usage rule (*"declare settings
    with `@ConfigProperties`; do not hand-write a `@Provider` that reads `os.environ`"*, plus
    *"`@ConfigProperties` is not `@Configuration`"*), and an `### Added` CHANGELOG entry under
    `[Unreleased]` naming the new extra.

## Edge cases

- No sources produce anything and every field has a default → target constructed from defaults.
- No sources produce anything and a field is required → `ConfigBindingError` with one issue naming
  the field, not a bare `TypeError` from `cls(**{})`.
- `prefix` subtree absent → `{}` (then the two rules above apply).
- `prefix` subtree present but a scalar → `ConfigBindingError`.
- Extra keys in the subtree the target does not declare → ignored.
- Same key in two sources → the later source in the tuple wins; a scalar overwrites a mapping.
- `EnvSource` value is always a `str` → the coercion table is what makes `pool_size: int` work.
- `EnvSource` with a key that is *only* the delimiter (`DB__`) or has an empty segment → segment
  dropped; never crashes.
- A file source with `required=False` and a missing path → `{}`, no issue.
- A file source with `required=True` and a missing path → `ConfigBindingError` naming the path.
- Malformed YAML/JSON/TOML → `ConfigBindingError` naming the path, `__cause__` chained to the
  parser's exception.
- YAML file containing a `!!python/…` tag → parse error from `safe_load`, **never** code execution.
- PyYAML not installed and a `YamlSource` is used → `ConfigBindingError` telling the user to install
  `providify[yaml]`; core install is unaffected as long as no `YamlSource` is constructed.
- Target exposes `model_validate` → providify coerces **nothing** and passes the raw merged mapping;
  a pydantic `ValidationError` is wrapped as `ConfigBindingError` with `__cause__` preserved.
- Empty YAML file → `yaml.safe_load` returns `None` → treated as `{}`.
- JSON/YAML top level is a list, not a mapping → `ConfigBindingError` (a config document must be a
  mapping).
- `@ConfigProperties` class subclassed → the subclass carries **no** marker (`__dict__` lookup);
  it must re-declare. Consistent with `@Alternative`/`@Configuration` (`metadata.py:413-415`,
  `:283-285`).
- Class carrying both `@ConfigProperties` and `@Configuration` → treated as config by the scanner
  (branch order, step 15); documented as unsupported/ambiguous.
- Two `@ConfigProperties` classes sharing one `prefix` → both bind independently from the same
  subtree; extra-key tolerance is what makes this work.
- `container.copy()` → the config binding is an ordinary `ProviderBinding`, so it copies like any
  other; the copy re-runs the factory on its first `get()` (its singleton cache starts empty,
  `container.py:5034`).
- `container.shutdown()` → a config object with no `@Disposes`/`@PreDestroy` needs no teardown;
  Plan 004's ordering applies unchanged.
- `scan()` run twice over the same module → one binding (dedupe set, step 15).

## Verification

```bash
cd /home/edoardo/projects/providify
uv sync                                              # picks up the new dev deps (step 16)
uv run pytest tests/test_config_sources.py tests/test_config_binding.py \
              tests/test_config_properties.py tests/test_config_pydantic.py -q
uv run pytest -q                                     # full suite — no regressions
uv run ruff check providify tests
uv run ruff format --check providify tests
```

Core-install guard — providify must still import with **no** third-party package present:

```bash
cd /home/edoardo/projects/providify
uv run python -c "
import sys, providify
assert 'pydantic' not in sys.modules, 'providify must not import pydantic'
assert 'yaml' not in sys.modules, 'providify must not import yaml at import time'
print('core import clean')
"
```

Manual smoke check (must print `DbSettings(url='postgres://x', pool_size=20, ...)`):

```bash
cd /home/edoardo/projects/providify
DB__URL=postgres://x DB__POOL_SIZE=20 DB__REPLICAS=a,b uv run python -c "
from dataclasses import dataclass
from providify import DIContainer, ConfigProperties, EnvSource
@ConfigProperties(prefix='db', sources=(EnvSource(),))
@dataclass(frozen=True)
class DbSettings:
    url: str
    pool_size: int = 5
    replicas: tuple[str, ...] = ()
c = DIContainer(); c.bind_config(DbSettings); print(c.get(DbSettings))
"
```

## Risks

- ⚠️ **ASSUMPTION — the team wants pydantic as an *optional target*, not a hard dependency.**
  Research 001 §Librarian's Note offers three paths and does not choose; this plan picks (c)
  "minimal core + rich pydantic-backed mode" on the grounds that `pyproject.toml` currently declares
  **zero** runtime dependencies (`:1-46`) and the library markets itself as minimal. If the team
  would rather take the standard path, the correct change is to depend on `pydantic-settings>=2.15`
  and delete `providify/config.py`'s coercion half entirely — a materially smaller feature, but a
  posture change that must be an explicit product decision, not a plan-level one. **Decide this
  before step 3.**
- ⚠️ **ASSUMPTION — the env-var nesting convention is `PREFIX__FIELD` with `__` as the delimiter.**
  Matches pydantic-settings' `env_nested_delimiter` default idiom, but nothing in this repo or in
  research 001 fixes it. It is a constructor argument (`EnvSource(delimiter=...)`), so it is
  changeable — but the *default* is public API from day one. Confirm against the primary internal
  consumer (`varco_core`, `BACKLOG.md:3`) before merging.
- ⚠️ **ASSUMPTION — `@ConfigProperties` is the right name.** `BACKLOG.md:38` literally says *"typed
  `@Configuration` objects"*, which this plan deliberately does not do (see §Alternatives). If the
  team prefers `@Settings`, it is a rename before step 12 and free; after release it is a breaking
  change.
- ⚠️ **ASSUMPTION — duck-typing `model_validate` is acceptable.** It gives pydantic support with no
  import and no version pin, but it silently claims **any** class exposing that method name. The
  behaviour is documented and the failure mode is benign (providify skips coercion and lets the
  target complain). Invariant: *if `model_validate` exists, providify passes the mapping through
  untouched and never coerces.* Step 17 locks this in.
- **The coercion table is a maintenance surface.** Research 001 §Lightweight Alternatives calls the
  stdlib path *"verbose type coercion"*; every new type users ask for (`datetime`, `timedelta`,
  `UUID`, `Decimal`, `IPv4Address`) is a table row plus tests. Invariant that bounds it:
  *anything not in the table passes through verbatim and is the target's problem.* Resist growing
  the table case-by-case — past ~3 additional requests, the honest answer is "use a pydantic model".
- **Config errors surface at first `get()`, not at `bind_config()`.** A misconfigured deployment
  fails on first resolution rather than at boot — unless the app calls `warm_up()` or `validate()`.
  This must be loud in `bind_config()`'s docstring and in `docs/agents/usage-rules.md` (step 20).
  ⚠️ ASSUMPTION — `validate()` (Plan 003) does **not** instantiate anything by design
  (`plans/003-startup-graph-validation.md:40-41`), so it will **not** catch a bad config value; only
  `warm_up()` will. Verify this is acceptable, or the usage rule must say `warm_up()`, not
  `validate()`.
- **`PyYAML>=6.0` is an unpinned lower bound.** Research 001 §YAML Library Recommendation records
  PyYAML as actively maintained but gives no current version number; `>=6.0` is the conservative
  floor. Confirm the resolved version in `uv.lock` after step 16 and record it in the PR.
- **Adding `[project.optional-dependencies]` changes the packaging surface.** The table does not
  exist today (`pyproject.toml:1-46` goes straight from `[project.urls]` to `[dependency-groups]`);
  verify `uv build` still produces a valid wheel and that `hatchling`'s
  `[tool.hatch.build.targets.wheel] packages = ["providify"]` (`:48-49`) still picks up the two new
  modules.
- **New public surface is permanent.** `ConfigSource` (a `Protocol`), the five source dataclasses,
  `ConfigIssue` and `ConfigPropertiesMetadata` all enter the API in `__all__` at step 19. Keeping
  the sources frozen dataclasses with keyword-only optional fields, and `ConfigSource` a
  single-method Protocol, means third-party sources are possible without providify changing — and
  that adding a field later stays backward compatible, whereas adding a *required* one would not.
