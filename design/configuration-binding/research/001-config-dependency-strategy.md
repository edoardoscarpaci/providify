# Research 001 — Config Binding Dependency Strategy
Date: 2026-08-24 · Freshness matters: **yes** — Pydantic/YAML ecosystem changes frequently; version support windows are critical.

## Question
For a providify `@Configuration` feature (typed config objects from env/YAML/JSON sources):
1. What is Pydantic v2's current stable version, recommended pattern for env/file loading, APIs, Python support, and license?
2. What is the current best-practice YAML library in Python?
3. Are there lightweight alternatives that avoid heavy dependencies for basic typed config binding?

## Findings

### Pydantic v2 (Core + Settings)
- **Current stable versions** (August 2026): Pydantic 2.13.4 (core, May 2026); pydantic-settings 2.15.0 (separate package, August 7, 2026) — [pydantic on PyPI](https://pypi.org/project/pydantic/) / [pydantic-settings on PyPI](https://pypi.org/project/pydantic-settings/)
- **Recommended pattern**: `BaseSettings` from pydantic-settings (now a first-class separate package) with `settings_customise_sources()` classmethod to compose multiple config sources (env vars, files) with priority ordering — [Pydantic Settings docs](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)
- **File loading APIs**: Dedicated source classes for structured formats:
  - `JsonConfigSettingsSource(settings_cls, json_file='config.json', json_file_encoding='utf-8')` — [pydantic-settings sources](https://deepwiki.com/pydantic/pydantic-settings/3-settings-sources)
  - `YamlConfigSettingsSource(settings_cls, yaml_file='config.yaml')` — supports YAML via optional `yaml` extra
  - `TomlConfigSettingsSource`, `PyprojectTomlConfigSettingsSource` also available
  - Environment variables: automatic field binding with `Field(validation_alias='ENV_NAME')` — [Pydantic Settings docs](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)
- **Python support**: Pydantic 2.13.4 requires Python ≥3.9; pydantic-settings 2.15.0 requires Python ≥3.10 (supports up to 3.14) — [pydantic PyPI](https://pypi.org/project/pydantic/), [pydantic-settings PyPI](https://pypi.org/project/pydantic-settings/)
- **License**: MIT (OSI Approved) for both — [Pydantic GitHub](https://github.com/pydantic/pydantic/blob/main/LICENSE)
- **Note on dependencies**: pydantic-settings brings `pydantic` as a core dep. YAML support requires optional `PyYAML` (via `pip install pydantic-settings[yaml]`). JSON is stdlib.

### YAML Library Recommendation
- **PyYAML** (industry standard): Actively maintained, widely used, but `yaml.load()` has security risks → always use `yaml.safe_load()`. Strips comments on round-trip (load → modify → save). Adheres to YAML 1.1 — [PyYAML guide](https://jsonparser.ai/blog/yaml/parse-yaml-python/)
- **ruamel.yaml** (alternative): YAML 1.2 compliant, preserves comments and key order on round-trip, heavier dependency, better for round-trip workflows — [ruamel.yaml comparison](https://www.oreateai.com/blog/choosing-between-ruamelyaml-and-pyyaml-a-comprehensive-comparison/2ca85e856751622588a46a00a9a8e664)
- **Recommendation for providify**: PyYAML (via pydantic-settings[yaml] extra) is the pragmatic choice for config *reading* (not round-trip editing). ruamel.yaml only if users need YAML 1.2 or round-trip preservation.

### Lightweight Alternatives (Avoiding Full Pydantic)
| Option | Approach | Python Support | Dependencies | YAML/JSON? | License | Evidence |
|---|---|---|---|---|---|---|
| **python-decouple** | Manual type coercion + stdlib dataclass | 3.6+ | 0 core deps | No (only .env/.ini) | MIT | [PyPI](https://pypi.org/project/python-decouple/), v3.8 (Mar 2023) |
| **minimal-configclasses** | @configclass decorator over stdlib dataclass | 3.8+ | 0 for 3.11+; tomli for <3.11 | Partial (TOML via tomllib) | [GitHub](https://github.com/drivendataorg/minimal-configclasses) | No JSON/YAML direct support |
| **environs** | Type conversion + attrs-based | 3.6+ | attrs (lightweight) | No direct file support | BSD | Community recommendation [IoFlood](https://ioflood.com/blog/python-yaml-parser/) |
| **Stdlib dataclasses + manual casting** | Pure stdlib, explicit typing | 3.7+ | 0 | Requires manual json.load()/yaml.safe_load() + type assignment | N/A | Built-in; verbose but zero-dependency |

- **Tradeoffs**: 
  - Full Pydantic path: ~2-3 dependencies (pydantic + optional PyYAML), but rich validation/coercion/error messages, Spring-like DX.
  - python-decouple: Ultra-lightweight, .env-focused, no YAML/JSON; requires manual file handling + type casting.
  - Minimal dataclasses: Zero-dependency, verbose type coercion, but aligns with providify's "minimal core" philosophy.

## Version/Compatibility Notes
- **Pydantic v2 EOL for v1**: Pydantic v1 receives critical bug fixes + security patches only; v2 is the active development line with no breaking changes planned within minor releases — [Pydantic Version Policy](https://pydantic.dev/docs/validation/latest/get-started/version-policy/)
- **pydantic-settings separation**: BaseSettings moved to pydantic-settings package starting with Pydantic v2; it is *not* in pydantic core — design decision to allow independent versioning
- **PyYAML security**: Use `safe_load()` for untrusted input; `load()` can execute arbitrary Python code — [PyYAML guide](https://jsonparser.ai/blog/yaml/parse-yaml-python/)
- **Python 3.10+ gap**: providify currently targets users on 3.9+; pydantic-settings requires 3.10+. If 3.9 compatibility is required, Pydantic core (3.9+) can be used directly or pair with lighter alternatives.

## Evidence Gaps
- **Pydantic v2 deprecation timeline**: No public roadmap found for when/if `BaseSettings` might be deprecated in favor of a lighter core approach (worth monitoring).
- **Benchmarks on validation speed**: No authoritative 2026 performance data found comparing Pydantic vs. manual dataclass casting under high-volume config loading (likely negligible for CLI/startup contexts).
- **Worth a separate brief**: Full audit of FastAPI/Starlette config patterns (both use pydantic-settings) and how they compare to providify's DI model.

## Librarian's Note
**What the sources indicate**: 
1. **Pydantic v2 + pydantic-settings is the industry standard** for typed config binding in Python (Spring @ConfigurationProperties analog). It is stable, actively maintained, and provides first-class YAML/JSON/TOML support via `settings_customise_sources()`.
2. **PyYAML remains the safe choice** for YAML parsing in 2026 (use `safe_load()` only), with ruamel.yaml as a heavier alternative if round-trip preservation is needed.
3. **No true lightweight alternative exists** that covers env + YAML + JSON + type coercion without rolling custom code or pulling in pydantic anyway. The tradeoff is: **add 2–3 transitive dependencies (pydantic ecosystem) for rich DX, or maintain manual casting**. Given providify's mission to be a "minimal" DI library, either a) pull pydantic-settings as an optional feature, b) provide stdlib-only path with explicit guidance on manual casting, or c) do both (minimal core + rich pydantic-backed mode).

**Python version note**: pydantic-settings requires 3.10+; if 3.9 support is required, consider either Pydantic core alone (3.9+) or python-decouple + manual file handling as fallback.
