# Plan 005 — Profile-based provider activation (`@Profile`, env-driven `@Alternative`) (F2)

Implements backlog item **F2** (🟡 should, complexity S — `BACKLOG.md:37`): *"Natural extension of
the existing `@Alternative` mechanism — smallest lift of the should-haves. Removes manual toggling
for env-specific wiring."*

Industry grounding: Spring `@Profile` / `@Conditional` — research 001 §Differentiators #7
(`design/di-features-taxonomy/research/001-table-stakes-vs-differentiators.md`), cited via
`BACKLOG.md:37`.

## Goal

A class or `@Provider` function may declare **when it is eligible for resolution**:

```python
@Profile("prod")
@Singleton
class RealMailer(Mailer): ...

@Profile("dev", "test")            # OR — active in either
@Singleton
class ConsoleMailer(Mailer): ...

@Profile("!prod")                  # negation
@Provider(singleton=True)
def fake_clock() -> Clock: ...
```

The container's **active profile set** comes from an explicit constructor argument, from the
`PROVIDIFY_PROFILES` environment variable, or from imperative `activate_profile()` calls:

```python
container = DIContainer(profiles=("prod",))     # explicit wins
container = DIContainer()                        # reads PROVIDIFY_PROFILES=prod,eu
container.activate_profile("debug")              # imperative, mirrors enable_alternative()
container.active_profiles                        # frozenset({"prod", "eu", "debug"})
```

Bindings whose profile expression does not match are invisible to `get()`, `get_all()`,
`is_resolvable()` and `validate()` — exactly as a non-enabled `@Alternative` is today
(`container.py:1039-1043`).

`@Profile` also becomes the **declarative activator for `@Alternative`**: an `@Alternative` class
that also carries `@Profile` is governed by its profile instead of requiring an imperative
`enable_alternative()` call. That is the "env-driven `@Alternative`" half of F2.

## Non-goals

- **No boolean expression language.** Spring 5.1's `@Profile("a & (b | !c)")` needs a parser and an
  AST. F2 ships OR-of-literals with a leading `!` negation only — enough for every wiring case in
  `BACKLOG.md:37`, and forward-compatible (a parser can be added later without changing the
  decorator signature).
- **No `@Conditional` / predicate-based activation** (`@ConditionalOnProperty`, `@ConditionalOnClass`).
  Separate, larger feature; profiles are the 90 % case.
- **No profile-scoped configuration file selection** (`config-{profile}.yaml`). That is a natural
  Plan 006 (F3) follow-up and is deliberately kept out of both plans so neither depends on the other.
- **No profile inheritance / profile groups** (Spring `spring.profiles.group.*`). Users compose by
  listing several literals.
- **No change to how `enable_alternative()` / `disable_alternative()` work** for alternatives that
  carry *no* `@Profile`. Their semantics (`container.py:1095-1116`) are untouched; `@Profile` is
  additive and composes with them.
- **No profile information in `BindingDescriptor` / `describe()`.** Reporting-tier change, not
  needed to make the feature work; would grow the descriptor's public surface for no behavioural gain.
- **No registration-time filtering.** Inactive bindings stay in `_bindings` (see §Alternatives).
- **No version bump** in `pyproject.toml` — `CHANGELOG.md` entry only.
- **No new runtime dependency.** `os.environ` is stdlib.

## Design

### Where the filter lives: `_filter()`, not registration

`@Alternative` already establishes the pattern — a per-container *activation set* consulted inside
`_filter()` on every resolution (`container.py:465` for the set, `container.py:1039-1043` for the
clause). `@Profile` extends the same clause rather than inventing a second mechanism. Consequences,
all desirable:

- `is_resolvable()` (`container.py:1046-1091`) delegates to `_filter()`, so it becomes profile-aware
  for free.
- `validate()` (Plan 003) walks candidates through `_filter()`, so a graph is validated **as it will
  actually be wired under the active profiles** — no extra work.
- `container.copy()` + a different profile set is a legal test pattern (the copy shares `_bindings`
  semantics but owns its own activation state, mirroring `container.py:5059`).

### The activation predicate

Today (`container.py:1039-1043`):

```python
and (
    not (isinstance(b, _ClassBinding) and _is_alternative(b.implementation))
    or b.implementation in self._enabled_alternatives
)
```

After this plan, one uniform clause over a precomputed per-binding attribute:

```python
and self._binding_is_active(b)
```

with

```
_binding_is_active(b) ⟺  profile_ok(b) AND alternative_ok(b)

profile_ok(b)      : b.profiles == ()               → True   (unprofiled = always eligible)
                     any expression in b.profiles matches self._active_profiles

alternative_ok(b)  : b.source not @Alternative      → True
                     b.profiles != ()               → True   (← NEW: @Profile is the activator)
                     b.source in self._enabled_alternatives
```

Reading: *"an unprofiled binding is always eligible; a profiled binding is eligible only when its
expression matches; an `@Alternative` additionally needs an activator, which is either its
`@Profile` or an imperative `enable_alternative()`."*

`b.source` is `ClassBinding.implementation` (`binding.py:183`) or `ProviderBinding.fn`
(`binding.py:654`). Both `@Alternative` and `@Profile` markers are read from `__dict__` — the
non-inherited lookup `_is_alternative` already uses (`metadata.py:413-415`).

### Precomputing `binding.profiles`

Reading the marker on every `_filter()` call would add a `getattr` + `isinstance` per binding per
resolution to an already O(N) hot path. Instead each binding resolves its expressions **once, at
construction**:

```python
# binding.py — ClassBinding.__init__ (after line 183) and ProviderBinding.__init__ (after line 654)
self.profiles: tuple[str, ...] = _get_profile_expressions(<implementation|fn>)
```

Every `ClassBinding` / `ProviderBinding` in the codebase is built through these two `__init__`s
(`container.py:649`, `:662`, `:684`, `:729`; `scanner.py:194`, `:204`, `:207`), so the attribute is
universally present and `_filter()` needs no `getattr` default.

`_filter()` then does `frozenset` intersection work only for the (rare) bindings that carry
expressions — an empty tuple short-circuits.

### Profile expression semantics

`providify/profiles.py` — a new pure module with **no import of `container.py`**, mirroring the
isolation `validation.py` established (flagged by the scout as the codebase's stated pattern).

```python
def matches(expressions: tuple[str, ...], active: frozenset[str]) -> bool:
    """OR over expressions; a leading '!' negates a single literal."""

def parse_profiles(raw: str | None) -> frozenset[str]:
    """Split a comma/whitespace-separated env value into a normalised profile set."""

def resolve_active_profiles(explicit: Iterable[str] | None) -> frozenset[str]:
    """explicit is not None → use it verbatim (even if empty);
       explicit is None     → parse os.environ.get(ENV_VAR)."""

ENV_VAR = "PROVIDIFY_PROFILES"
```

| expressions | active set | result |
|---|---|---|
| `()` | anything | `True` (unprofiled) |
| `("prod",)` | `{"prod"}` | `True` |
| `("prod",)` | `{"dev"}` | `False` |
| `("dev", "test")` | `{"test"}` | `True` (OR) |
| `("!prod",)` | `{"dev"}` | `True` |
| `("!prod",)` | `{"prod"}` | `False` |
| `("!prod",)` | `frozenset()` | `True` (nothing active ⇒ not prod) |
| `("prod",)` | `frozenset()` | `False` |
| `("dev", "!prod")` | `{"prod"}` | `False` — `dev` fails, `!prod` fails |

Profile names are normalised (stripped, lower-cased) at **both** ends — in `@Profile(...)` and in
`resolve_active_profiles` — so `"PROD"` and `"prod"` are the same profile. Empty names and bare
`"!"` are rejected at decoration time with `ValueError` (fail at import, not at resolution).

### Container state and API

```python
# container.py __init__ — new keyword-only parameter, defaults to None
def __init__(self, *, scan=None, recursive=True, profiles: Iterable[str] | None = None) -> None
```

added to the "Jakarta CDI parity state" block (`container.py:463-471`), right beside
`_enabled_alternatives`:

```python
self._active_profiles: frozenset[str] = resolve_active_profiles(profiles)
```

Precedence, deliberately explicit:

| `profiles=` | `PROVIDIFY_PROFILES` | active set |
|---|---|---|
| `None` (default) | unset | `frozenset()` |
| `None` | `"prod,eu"` | `{"prod", "eu"}` |
| `("prod",)` | ignored | `{"prod"}` |
| `()` | ignored | `frozenset()` — *explicitly* no profiles |

Public methods, named and shaped to mirror `enable_alternative` / `disable_alternative`
(`container.py:1095-1116`) exactly, including the `self._validated = False` reset at
`container.py:1105`/`:1116`:

```python
@property
def active_profiles(self) -> frozenset[str]      # read-only snapshot
def activate_profile(self, name: str) -> None
def deactivate_profile(self, name: str) -> None
```

They are placed in the same "@Alternative — deployment-time bean activation" section, which is
renamed to "@Alternative / @Profile — deployment-time bean activation".

`_invalidate_type_caches()` is **not** called — no binding was added or removed, so `_localns_cache`
and `_hints_cache` stay valid. Only `_validated` is reset, because the reachable graph changed.

### The `@Alternative`-on-a-provider asymmetry (fixed here)

The clause at `container.py:1041` guards on `isinstance(b, _ClassBinding)`, so an `@Alternative`
marker on a `@Provider` function is silently ignored — such a provider is *always* active, the exact
opposite of what the decorator promises (`scope.py:666-683`: "disabled by default"). Since this plan
makes providers profile-aware anyway, the uniform `_binding_is_active(b)` predicate reads the marker
from `ProviderBinding.fn` too, closing the hole. This is a **behaviour change** for any code that
stamped `@Alternative` on a provider function and relied on it doing nothing — flagged in
§Risks and required in the `CHANGELOG.md` entry (step 18).

### Alternatives considered

- **Filter at registration time** — drop non-matching classes in `register()`/`provide()`/the
  scanner instead of at resolution. ✅ zero cost in the `_filter()` hot path; ✅ smaller
  `_bindings` list. ❌ profiles become frozen at registration, so `activate_profile()` after
  `scan()` is impossible and the very common "build once, `copy()` per test with a different
  profile" flow breaks; ❌ `describe()` / `validate()` can never report what is *inactive*;
  ❌ diverges from the model `@Alternative` already established at `container.py:1039-1043`,
  leaving two different activation mechanisms in one container. **Rejected.**
- **No new decorator — drive `@Alternative` from the environment only** (read `PROVIDIFY_PROFILES`,
  auto-call `enable_alternative()` on matching classes). ✅ zero new public surface, literally the
  "env-driven `@Alternative`" phrasing in `BACKLOG.md:37`. ❌ `@Alternative` is *disabled by
  default*, but a `@Profile("dev")` bean must be **active** in dev with no imperative call — the
  semantics are inverted; ❌ `@Alternative` is class-only (`container.py:1041`) so `@Provider`
  functions could never participate; ❌ no way to express "active in dev **or** test", or "active
  everywhere except prod". **Rejected** — but the composition rule above keeps the two features
  joined, which is the useful part of the backlog phrasing.
- **Full boolean expression language** (`"a & (b | !c)"`, Spring ≥5.1). ✅ maximal expressiveness.
  ❌ requires a tokenizer + parser + precedence rules + its own error type for ~0 evidenced demand;
  the decorator signature chosen here (`*expressions: str`) can gain it later without a break.
  **Rejected.**
- **Profiles as qualifiers** (`@Named("prod")` + resolve with `qualifier="prod"`). ✅ zero new
  machinery. ❌ conflates *selection* (which of several beans do I want here?) with *activation*
  (does this bean exist at all in this deployment?) — every injection point would have to name the
  profile, which is exactly the "manual toggling for env-specific wiring" `BACKLOG.md:37` asks to
  remove. **Rejected.**
- **Storing `ProfileMetadata` on the binding instead of a plain `tuple[str, ...]`.** ✅ symmetrical
  with `DIMetadata`/`StereotypeMetadata` (`metadata.py:106-119`). ❌ `_filter()` would dereference
  `b.profiles.expressions` on the hot path for no added information — the tuple *is* the whole
  metadata. **Rejected** for the binding attribute; `ProfileMetadata` still exists as the
  class/function marker type (the codebase's "the TYPE is the signal" convention, `metadata.py:128-131`).

## Steps

1. [x] `tests/test_profiles.py` — new file. Failing tests for the pure matcher, no container
   involved: every row of the §Design truth table for `matches()`; `parse_profiles` over `None`,
   `""`, `"prod"`, `"prod,eu"`, `"prod, eu"`, `" PROD , Eu "` (→ `{"prod", "eu"}`), `"a,,b"`
   (empty segments dropped); `resolve_active_profiles(None)` reads `PROVIDIFY_PROFILES` via
   `monkeypatch.setenv`, `resolve_active_profiles(())` returns an empty set **without** reading the
   env, `resolve_active_profiles(["Prod"])` normalises to `{"prod"}`.
2. [x] `providify/profiles.py` — new module. `ENV_VAR`, `_normalise(name)`, `matches()`,
   `parse_profiles()`, `resolve_active_profiles()`. Pure: imports only `os` and `typing`; **must
   not import `container.py` or `binding.py`** (the `validation.py` isolation pattern). Complete
   docstrings per CLAUDE.md (Args/Returns/Raises/Thread safety/Async safety/Edge cases/Example).
3. [x] `tests/test_profiles.py` — failing tests for the marker: `@Profile("prod")` returns the same
   class object; the marker is readable via `_get_profile_expressions`; `@Profile("A", "b")`
   normalises to `("a", "b")`; `@Profile()` (no args) raises `ValueError`; `@Profile("")`,
   `@Profile("!")`, `@Profile("  ")` raise `ValueError`; a **subclass** of a `@Profile`d class does
   **not** inherit the expressions (`__dict__` lookup, matching `_is_alternative` at
   `metadata.py:413-415`); `@Profile` stacked on a `@Provider` function preserves
   `ProviderMetadata`; `@Profile` applied twice merges (union, order-preserving, deduped).
4. [x] `providify/metadata.py` — after the `@Alternative` marker block (`metadata.py:78-79`,
   `:413-420`) add:
   - `_PROFILE_ATTR = "__di_profile__"` beside `_ALTERNATIVE_ATTR` (line 79);
   - `@dataclass(frozen=True) class ProfileMetadata: expressions: tuple[str, ...]` beside
     `StereotypeMetadata` (line 106);
   - `_get_profile_metadata(obj) -> ProfileMetadata | None` and
     `_get_profile_expressions(obj) -> tuple[str, ...]` using
     `getattr(obj, "__dict__", {}).get(_PROFILE_ATTR)` + `isinstance` — one helper that works for
     classes (mappingproxy), plain functions, and bound methods (which proxy `__func__.__dict__`);
   - `_set_profile_marker(obj, expressions)`.
5. [x] `providify/decorator/scope.py` — add `Profile(*expressions: str)` immediately after
   `Alternative` (`scope.py:684`), returning a decorator usable on classes **and** functions.
   Validates/normalises via `providify.profiles._normalise`, raises `ValueError` on an empty
   argument list or an empty/`"!"`-only literal, merges with any existing marker, calls
   `_set_profile_marker`. Docstring must state the OR semantics, the `!` negation, the
   composition rule with `@Alternative`, and carry a Jakarta/Spring-parity note like
   `Alternative`'s (`scope.py:667-681`).
6. [x] `tests/test_profiles.py` — failing tests that every binding exposes `.profiles`:
   `ClassBinding(X, X).profiles == ()`; a `@Profile`d class bound via `container.bind()` and via
   `container.register()` both carry the expressions; `ProviderBinding(fn).profiles` reads the
   marker off the function; a `@Configuration` module's `@Provider` method registered as a **bound
   method** (`container.py:4697`) carries the marker; a `@Provider @property`
   (`container.py:4688-4693`) carries it too (the `__dict__.update` at `:4693` copies it).
7. [x] `providify/binding.py` — set `self.profiles: tuple[str, ...] = _get_profile_expressions(...)`
   in `ClassBinding.__init__` (after `binding.py:183`, source = `implementation`) and in
   `ProviderBinding.__init__` (after `binding.py:654`, source = `fn`). One-line DESIGN comment:
   *"resolved once at construction — `_filter()` runs per resolution and must not re-read markers."*
8. [x] `tests/test_profiles.py` — failing tests for container profile state: `DIContainer()` →
   `active_profiles == frozenset()`; `DIContainer(profiles=["prod"])` → `{"prod"}`;
   `DIContainer()` with `PROVIDIFY_PROFILES=prod,eu` → `{"prod", "eu"}`;
   `DIContainer(profiles=(), ...)` with the env var **set** → `frozenset()`;
   `activate_profile("Dev")` → `{"dev"}` and `container._validated is False`;
   `deactivate_profile("dev")` on an inactive name is a silent no-op (mirrors `discard` at
   `container.py:1116`); `active_profiles` is a `frozenset` and mutating the returned object is
   impossible.
9. [x] `providify/container.py:338-343` — add `profiles: Iterable[str] | None = None` to
   `__init__`; document it in the existing docstring (`:349-379`) with the precedence table and the
   `PROVIDIFY_PROFILES` name. In the "Jakarta CDI parity state" block (`:463-471`), immediately
   after `self._enabled_alternatives` (`:465`), add
   `self._active_profiles: frozenset[str] = resolve_active_profiles(profiles)` with a DESIGN
   comment mirroring the neighbouring one.
10. [x] `providify/container.py:1093-1116` — rename the section header to
    "`@Alternative` / `@Profile` — deployment-time bean activation" and add, after
    `disable_alternative`: the `active_profiles` property, `activate_profile()`, and
    `deactivate_profile()`. Each mutator sets `self._validated = False` and **does not** call
    `_invalidate_type_caches()` (docstring must say why: no binding was added or removed).
11. [x] `tests/test_profiles.py` — failing resolution tests through `_filter()`/`get()`:
    - unprofiled binding resolves under any active set, including the empty one;
    - `@Profile("prod")` class → `LookupError` under `{"dev"}`, resolves under `{"prod"}`;
    - two impls of one interface, `@Profile("prod")` / `@Profile("dev")` → `get(I)` returns
      exactly the one matching the active set, with **no** ambiguity, under each profile;
    - `get_all(I)` returns only active candidates;
    - `is_resolvable(I)` (`container.py:1046-1091`) tracks the active set;
    - `@Profile("!prod")` on a `@Provider` function is filtered out under `{"prod"}`;
    - `activate_profile()` **after** `get()` changes the next `get()`'s answer (no stale cache) —
      and a singleton already cached under the old profile is *not* re-created (documented, see
      §Edge cases).
12. [x] `providify/container.py:995-1044` — replace the `@Alternative` clause (`:1039-1043`) with
    `and self._binding_is_active(b)`. Add the private predicate `_binding_is_active(b) -> bool`
    directly above `_filter()`, implementing the three-line rule from §Design, reading the source
    object as `b.implementation` for `ClassBinding` / `b.fn` for `ProviderBinding`. Keep
    `_filter()`'s existing docstring structure and extend it with the activation rules table.
    Short-circuit order: `not b.profiles` first (the overwhelmingly common case).
13. [x] `tests/test_profiles.py` + `tests/test_alternative.py` — failing tests for the composition
    rule: `@Alternative` **without** `@Profile` still requires `enable_alternative()` (all 6
    existing tests in `tests/test_alternative.py:1-72` must pass unchanged);
    `@Alternative` **with** `@Profile("test")` resolves under `{"test"}` with **no**
    `enable_alternative()` call, and is filtered out under `{"prod"}`;
    `enable_alternative()` on a `@Profile`d alternative whose profile does *not* match still leaves
    it inactive (profile is a hard gate — the AND in `_binding_is_active`);
    an `@Alternative`-marked `@Provider` function is now inactive by default (**behaviour change**).
14. [x] `providify/container.py:5058-5062` (`copy()`) — add
    `new._active_profiles = self._active_profiles` beside the `_enabled_alternatives` copy
    (`:5059`) and extend the surrounding "Copy runtime state" comment.
15. [x] `tests/test_profiles.py` — end-to-end env-driven test with `monkeypatch.setenv`: a module
    containing a `@Profile("prod")` and a `@Profile("dev")` implementation of one interface,
    registered via `container.scan(...)`, resolves differently for two containers constructed under
    two different `PROVIDIFY_PROFILES` values — with **no** imperative activation call anywhere.
    Plus: `container.copy()` inherits the profile set; mutating the copy's set does not affect the
    original.
16. [x] `tests/test_profiles.py` — interaction with Plan 003 validation: a graph whose only
    provider of `I` is `@Profile("prod")` reports a `MISSING_BINDING` **error** from
    `container.validate()` when `"prod"` is inactive, and validates clean when it is active. This
    is intended behaviour and must be locked in by a test, because it is the most surprising
    consequence of filtering inside `_filter()`.
17. [x] `providify/__init__.py` — export `Profile` (add `"Profile"` to `__all__` in the
    "Jakarta CDI qualifier system" group next to `"Alternative"`, `__init__.py:20`) and add it to
    the `from .decorator.scope import (...)` block (`__init__.py:101-119`). Export
    `ProfileMetadata` next to `StereotypeMetadata` (`__init__.py:22`, `:134`).
18. [x] `README.md`, `docs/agents/usage-rules.md`, `CHANGELOG.md` — one section / one rule / one
    `### Added` entry under `[Unreleased]`. The CHANGELOG must additionally carry a
    `### Changed` note for the `@Alternative`-on-a-`@Provider` fix (step 13) — previously a silent
    no-op, now genuinely disabled by default.

## Edge cases

- No profiles active anywhere (`frozenset()`) and no `@Profile` used → behaviour is byte-for-byte
  today's: `_binding_is_active` short-circuits on `not b.profiles` and falls through to the
  unchanged `@Alternative` rule.
- `@Profile("prod")` with an empty active set → inactive.
- `@Profile("!prod")` with an empty active set → **active** (nothing is `prod`).
- `@Profile("dev", "!prod")` under `{"prod"}` → inactive (OR of two false terms).
- `@Profile("Prod")` + `PROVIDIFY_PROFILES=prod` → match (both sides normalised).
- `PROVIDIFY_PROFILES=""` → `frozenset()`, same as unset.
- `PROVIDIFY_PROFILES="a,,b"` / `"a, b"` → `{"a", "b"}`; empty segments dropped.
- `DIContainer(profiles=())` with the env var set → `frozenset()`; an explicit argument always wins,
  including the empty one.
- Subclass of a `@Profile`d class → **no** inherited expressions (`__dict__` lookup); the subclass
  must re-declare. Consistent with `@Alternative` (`metadata.py:413-415`).
- `@Profile` on a `@Configuration` *module class* → the module still installs and its providers are
  still registered (`install()` at `container.py:4606` is unconditional); the **providers** are
  filtered only if they carry their own `@Profile`. Documented limitation — module-level gating
  belongs with F5's module DAG.
- `@Profile` on a `@Provider @property` → carried, because `_register_module_providers` copies the
  function `__dict__` wholesale (`container.py:4693`).
- `activate_profile()` after a `@Profile`d singleton was already resolved → the cached instance is
  **not** evicted; `_singleton_cache` is untouched. Profiles are a startup-time concern. Documented
  in the method docstring; `override()`/`reset_binding()` remain the eviction tools.
- Two impls with mutually exclusive profiles bound to the same interface → not ambiguous, because
  only one survives `_filter()`. Under a profile that activates **both**, ordinary `@Priority`
  rules apply and `validate()` reports `AMBIGUOUS_BINDING` on a tie (Plan 003).
- `@Profile` + `@Alternative` + matching profile + `disable_alternative()` → still active; the
  profile is the activator, and `disable_alternative()` only removes membership in
  `_enabled_alternatives`, which the rule no longer consults for profiled alternatives. Documented
  on `disable_alternative()`.
- `container.copy()` → inherits the active set by value; the two sets are independent afterwards.

## Verification

```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_profiles.py -q            # new suite
uv run pytest tests/test_alternative.py -v         # 6 existing tests must pass unchanged
uv run pytest -q                                    # full suite — no regressions
uv run ruff check providify tests
uv run ruff format --check providify tests
```

Manual smoke check (must print `ConsoleMailer` then `RealMailer`):

```bash
cd /home/edoardo/projects/providify
PROVIDIFY_PROFILES=dev uv run python -c "
from abc import ABC
from providify import DIContainer, Singleton, Profile
class Mailer(ABC): ...
@Profile('dev','test')
@Singleton
class ConsoleMailer(Mailer): ...
@Profile('prod')
@Singleton
class RealMailer(Mailer): ...
c = DIContainer(); c.bind(Mailer, ConsoleMailer); c.bind(Mailer, RealMailer)
print(type(c.get(Mailer)).__name__)
c2 = DIContainer(profiles=['prod']); c2.bind(Mailer, ConsoleMailer); c2.bind(Mailer, RealMailer)
print(type(c2.get(Mailer)).__name__)
"
```

## Risks

- ⚠️ **ASSUMPTION — the env var is named `PROVIDIFY_PROFILES`, comma-separated.** Modelled on
  Spring's `SPRING_PROFILES_ACTIVE` (research 001 §Differentiators #7, via `BACKLOG.md:37`), but no
  source in this repo fixes the name. It is exported as `providify.profiles.ENV_VAR`, so renaming
  later is a one-line change plus a CHANGELOG note — but it **is** public API the moment it ships.
  Confirm the name with the primary internal consumer (`varco_core`, per `BACKLOG.md:3`) before
  merging.
- ⚠️ **ASSUMPTION — `@Profile` composes with, rather than replaces, `enable_alternative()`.**
  The rule chosen (profile matches ⇒ alternative is enabled, AND-ed with the profile gate) is the
  reading that makes `BACKLOG.md:37`'s "env-driven `@Alternative`" true without breaking the 6
  existing tests in `tests/test_alternative.py:1-72`. The alternative reading — profiles feed
  `_enabled_alternatives` and nothing else — was rejected because it cannot express "active in dev
  **or** test" and cannot gate `@Provider` functions. Invariant that must hold: *a container with no
  `@Profile` anywhere behaves exactly as it does today.* Step 13 locks that in.
- ⚠️ **BEHAVIOUR CHANGE — `@Alternative` on a `@Provider` function now actually disables it.**
  Today `container.py:1041` guards on `isinstance(b, _ClassBinding)`, so the marker is silently
  ignored on providers. Anything relying on that no-op will start seeing `LookupError`. It is a bug
  fix (the decorator's own docstring at `scope.py:667-670` promises "disabled by default"), but it
  must be called out in `CHANGELOG.md` under `### Changed` (step 18). ⚠️ ASSUMPTION — no internal
  consumer currently stamps `@Alternative` on a provider function; grep the consuming repos before
  merging, and if one does, split this fix into its own change.
- **Filtering inside `_filter()` makes profiles visible to `validate()`.** A `@Profile("prod")`-only
  provider makes the graph *invalid* under a dev profile (Plan 003 reports `MISSING_BINDING`). This
  is correct and is the point of validation, but it will surprise users who expect profiles to be
  "just a runtime switch". Must be documented in `validate()`'s docstring and in
  `docs/agents/usage-rules.md`; step 16 locks the behaviour in with a test.
- **Hot-path cost.** `_filter()` is O(N) over `_bindings` and is called per injection point;
  `_binding_is_active` adds one attribute read + one truthiness test per binding in the common
  (unprofiled) case, and a small `frozenset` scan otherwise. Precomputing `binding.profiles` at
  construction (step 7) is what keeps this negligible. ⚠️ ASSUMPTION — no benchmark exists in this
  repo (same gap Plan 003 §Risks records); do not add per-resolution marker lookups later without
  measuring.
- **Cached singletons ignore profile changes.** `activate_profile()` sets `_validated = False` but
  does not evict `_singleton_cache`, so an instance created under the old profile survives. This is
  intentional (profiles are startup configuration) and mirrors how `enable_alternative()` behaves
  today (`container.py:1095-1106` also only touches `_validated`). Invariant: *profiles affect
  which binding is **selected**, never which instance is **cached**.*
- **`ProfileMetadata` and `Profile` become permanent public API** once in `__all__`. Keeping
  `ProfileMetadata` a frozen dataclass with a single `expressions: tuple[str, ...]` field and
  `Profile(*expressions: str)` variadic leaves room for a future `Profile("a & b")` parser or a
  keyword-only option without a breaking change.
