# Plan 014 — `@Disposes` wiring scoped to the installing module (+ `DISPOSER_OVERWRITTEN` / `UNMATCHED_DISPOSER` warnings)

Upstream gap: **P24-DISPOSES-FIRSTMATCH**
(`/home/edoardo/projects/varco/design/upstream-gaps/providify-disposes-first-match.md`;
index row `/home/edoardo/projects/providify/plans/000-index-upstream-gaps-p24-p27.md:14`).

Research basis: **none** — this is a pure internal bug fix in the
`@Disposes` wiring loop; no external research brief exists or is needed.
The only external reference is the Jakarta CDI disposer-resolution rule
cited in §Design, which is stated from memory and flagged in §Risks.

Coding standards: every step must follow
`/home/edoardo/.claude/skills/coding-practice/SKILL.md` — comments explain
*why*, docstrings carry Args / Returns / Raises / Edge cases, design choices
carry a `# DESIGN:` block with ✅/❌ tradeoffs, `from __future__ import
annotations` at the top of every new file, and error/warning messages say
what / why / how-to-fix.

Target release: **2.1.0** (minor — see §Versioning). Sibling plans in this
release: 015 (`@Requires`), 016 (open generics), 017 (`@Fallback`). This plan
does **not** touch `_filter()`, `_binding_is_active()` or
`_interface_matches()` — those belong to the siblings.

---

## Goal

After this plan:

1. A `@Disposes(X)` method on a `@Configuration` is attached **only** to a
   `ProviderBinding` that the *same* `install()` / `ainstall()` call created
   for that module. Two modules each providing `X` with their own
   `@Disposes(X)` both get their own disposer, and both instances are torn
   down at `shutdown()` / `ashutdown()`. varco's guard test flips from
   `xfail(strict=True)` to passing.
2. `container.validate()` reports two new **WARNING** kinds:
   - `IssueKind.DISPOSER_OVERWRITTEN` — a module declared two `@Disposes`
     that resolved to the *same* own binding; the later one silently replaced
     the earlier one.
   - `IssueKind.UNMATCHED_DISPOSER` — a module declared a `@Disposes(X)` but
     none of the bindings *it* registered matches `X`, so the disposer is
     attached to nothing (this is exactly the case that the pre-fix loop used
     to "solve" by grabbing a foreign binding).
3. `DIContainer.provide()` returns the `ProviderBinding` it created (was
   `None`) — additive, non-breaking.

## Non-goals

- ❌ Do not remove the `break` after the first match inside the (now scoped)
  wiring loop. One `@Disposes(X)` with **two** own providers of `X` (e.g. two
  qualifiers) still wires only the first — that is a separate,
  qualifier-blindness defect already noted as follow-up 2 in
  `plans/012-unreachable-pre-destroy-validation.md:623-626`. See §Follow-ups.
- ❌ Do not make the wiring qualifier-aware (`@Disposes(X, qualifier=...)`).
- ❌ Do not change `_dispose_sync()` (`container.py:4297-4335`) or
  `_adispose()` (`:4523-4555`) — how a disposer is *invoked* is untouched.
- ❌ Do not change `@PreDestroy`/producer semantics (P22 — closed).
- ❌ Do not add `Severity.INFO` (plan 015 owns that) — both new kinds are
  `WARNING`.
- ❌ Do not touch varco.

---

## Design

### The defect, precisely

`_register_module_providers(module_cls, instance)` (`container.py:6160-6214`)
registers the module's providers (`:6176-6199`, calling `self.provide()` at
`:6196` and `:6199`) and then wires disposers (`:6201-6214`) by scanning
**`self._bindings`** — the whole container — and attaching to the **first**
`ProviderBinding` whose interface matches, then `break`ing (`:6209-6214`).
With module A installed first, module B's `@Disposes(X)` lands on A's
binding (overwriting A's own disposer) and B's binding keeps
`disposer = None` (`binding.py:683` default). At shutdown, `_adispose()`
only calls `binding.disposer(instance)` when it is non-`None`
(`:4541-4546`), so B's instance is never torn down. Both `_install_one()`
(`:6135`) and `_ainstall_one()` (`:6157`) funnel through the same body, so
sync and async are equally affected.

### Fix (a): scope the search to the bindings this call created

```
_register_module_providers(module_cls, instance)
  │
  ├─ provider loop  ──► own: list[ProviderBinding]   ◄── NEW: collect what
  │      self.provide(...) now RETURNS the binding         this call registered
  │
  └─ disposer loop  ──► for binding in own:           ◄── was: self._bindings
         if _interface_matches(binding.interface, X):
             record DISPOSER_OVERWRITTEN if binding.disposer is not None
             binding.disposer = bound method; break
         else (no match): record UNMATCHED_DISPOSER
```

**Chosen: `provide()` returns the `ProviderBinding`; the wiring loop iterates
the list of bindings it collected.** The gap report's "Preferred, minimal"
option is the positional slice `self._bindings[start:]`; the report itself
says the slice is only "the cheapest correct fix available *today*" because
`provide()` returns `None` (`container.py:1048`). Making `provide()` return
the binding removes that constraint for one extra line.

| | slice `self._bindings[start:]` | `provide()` returns the binding |
|---|---|---|
| lines changed in the wiring | ~3 | ~5 (+1 `return`, +1 annotation, +Returns docstring) |
| ownership is | **implicit** — "whatever got appended after index `start`" | **explicit** — the exact objects this loop registered |
| robust to `provide()` growing a dedupe / insert-not-append / conditional-skip path (plan 015/016 territory) | ❌ silently wrong the day `provide()` stops appending exactly one at the tail | ✅ still correct; a `None`/skip return would just not be collected |
| public-API change | none | additive: `-> ProviderBinding` where callers today ignore `None`. Only one `def provide(` exists in the package (`container.py:1048`, verified by grep) — no subclass override to break |
| serves the report's DI-13 wish ("hold direct references rather than re-deriving a slice") | ❌ | ✅ |

The only real cost of the return-value approach is a one-line public
signature change, which `CONTRIBUTING.md` treats as additive (a caller who
discards the result is unaffected). Recommended: **return the binding**.

The `_prop_provider` path (`:6182-6196`) calls the same `provide()`, so
property providers are collected identically — this matters, see the
rejected "derive ownership from `fn.__self__`" alternative below.

### Fix (b): detect and report, not just fix

**Where detection happens.** The wiring loop is the only place that knows
(i) which bindings are the module's own, (ii) whether a match already had a
disposer, and (iii) whether a `@Disposes` matched nothing. None of that is
re-derivable later from `self._bindings` alone (see rejected alternative
"derive at validate() time"). So the loop **records** facts; `validate()`
**reports** them — mirroring how `UNREACHABLE_PRE_DESTROY` is a `validate()`
issue built from state set at install time (`container.py:5399-5424`,
`:5487-5490`; kind at `validation.py:92-96`).

**Where the record lives.** On the existing `_ModuleRecord`
(`container.py:232-270`), whose docstring already defines it as "a fact
about one install event". New field with a default so every existing
constructor call (`:6141`, `:6158`) and the `replace(rec, owned=False)` in
`copy()` (`:6706-6708`) keep working unchanged:

```python
@dataclass(frozen=True, slots=True)
class _DisposerWiringIssue:
    """One defect found while wiring a module's @Disposes methods (plan 014)."""
    kind: Literal["overwritten", "unmatched"]
    module_name: str            # e.g. "RedisLayeredCacheConfiguration"
    disposer_name: str          # the @Disposes method that was being wired
    disposed_type: str          # _type_name(marker.disposed_type)
    binding_owner: str | None   # "@Provider(fn_name)" for overwritten; None for unmatched
    replaced_disposer: str | None  # the method name that lost, for overwritten; else None

@dataclass(frozen=True, slots=True)
class _ModuleRecord:
    instance: object
    owned: bool
    disposed: bool
    disposer_issues: tuple[_DisposerWiringIssue, ...] = ()   # NEW
```

Fields are **strings only** — no binding references. Rationale (write it as
the `# DESIGN:` block on the dataclass):
- ✅ `reset_binding()` (`:6515-6573`), `override()` (`:6438-6488`) and
  `restore()` (`:6809`) all replace `self._bindings`; a stored binding
  reference would go stale and `owner_of(binding)` would describe a binding
  no longer in the container. Strings computed at wiring time cannot go
  stale.
- ✅ `copy()` inherits records via `replace()` — the copy holds the same
  (shallow-copied) bindings with the same disposers, so inheriting the
  warnings is correct.
- ❌ Duplicates `owner_of()`'s `f"@Provider({b.fn.__name__})"` format
  (`:5388-5389`). Accepted: it is one f-string; add a comment on both sites
  pointing at each other so they stay in sync.

`_register_module_providers()` changes signature to
`-> tuple[_DisposerWiringIssue, ...]`; `_install_one()` and
`_ainstall_one()` pass the result into `_ModuleRecord(...)`. `snapshot()` /
`restore()` (`:6713-6809`) do not touch `_installed_modules` today (verified
by grep: no reference between `:6713` and `:6809`) — nothing to add there.

**Where reporting happens.** A new **pass 3 — module teardown tier** in
`validate()`, after the per-binding loop ends (`:5683`) and **before** cycle
detection (`:5685`), so `ValidationReport`'s documented ordering
(`validation.py:199-202`: "…cycles appended last") stays true with one added
clause. It walks `self._installed_modules.values()` in install order and
emits one `ValidationIssue` per record entry.

| field | `DISPOSER_OVERWRITTEN` | `UNMATCHED_DISPOSER` |
|---|---|---|
| `severity` | `WARNING` | `WARNING` |
| `owner` | `rec.binding_owner` → `"@Provider(make_cache)"` (same vocabulary as `owner_of`, `:5388`) | `f"{module_name}.{disposer_name}"` → `"CacheModule.close_cache"` (same shape as `"OrderService.__init__"`, `validation.py:118-119`) |
| `param_name` | `disposer_name` — the disposer that **won** | `disposer_name` |
| `requested` | `disposed_type` | `disposed_type` |
| `qualifier` | `None` (wiring is qualifier-blind; do not pretend otherwise) | `None` |
| `candidates` | `()` | `()` |

Exact messages (what / why / fix):

```python
# overwritten
f"@Disposes '{winner}' on {module} replaced @Disposes '{loser}' on the same "
f"binding {owner} (interface {disposed}): both methods match it, and only "
f"one disposer can be attached, so '{loser}' will never run. Fix: keep a "
f"single @Disposes per produced interface in {module}, or split the "
f"providers into separate @Configuration classes."

# unmatched
f"@Disposes '{name}' on {module} matches no @Provider declared by {module} "
f"(interface {disposed}), so it is attached to nothing and will never run. "
f"A @Disposes only tears down instances produced by its own "
f"@Configuration. Fix: add a @Provider returning {disposed} to {module}, "
f"or move the @Disposes to the @Configuration that provides {disposed}."
```

**Why `WARNING`, not `ERROR`** — identical reasoning to plan 012 §"Why
WARNING": `validate(raise_on_error=True)` is the documented default
(`container.py:5299-5302`) and R12 tells users to call it at startup
(`docs/agents/usage-rules.md:227`); an `ERROR` would turn a working boot
into a `ContainerValidationError` on a minor upgrade. Nothing here fails at
resolution time — it is a latent teardown defect, the same class as
`UNREACHABLE_PRE_DESTROY`.

**Why `UNMATCHED_DISPOSER` is in scope (decision: yes).** Fix (a) removes a
behaviour: pre-fix, a module with `@Disposes(X)` and no own `X` provider
*did* attach to some other module's `X` binding — by accident, but
observably. Post-fix that `@Disposes` silently does nothing. A silent no-op
is the worst outcome for a teardown hook, and this warning is the **only**
signal of the one behavioural change this plan makes. It is also the
principled position: Jakarta CDI treats a disposer with no matching producer
on the same bean class as a *definition error* (§Risks, ⚠️ from memory).
Cost: one enum member, one extra branch in the same loop (a `for … else:`),
~15 lines + tests. The detection mechanism is shared with
`DISPOSER_OVERWRITTEN`, so the marginal cost is small; keeping it out would
leave the migration hazard undetectable.

**Overwrite semantics: last-wins is kept.** The current loop already
assigns unconditionally (`:6213`); `vars()` yields definition order, so the
later `@Disposes` wins today and continues to. Changing to first-wins would
be a second, unrelated behaviour change with no user asking for it; the
warning names both methods so the user can choose.

### Alternatives considered

- **Positional slice `self._bindings[start:]`** (gap report "Preferred,
  minimal") — rejected in favour of the return value: ✅ 3 lines, no
  signature change; ❌ implicit ownership, breaks silently if `provide()`
  ever stops appending exactly one binding at the tail (plausible under
  plans 015/016); ❌ does nothing for the report's DI-13 wish.
- **Derive ownership at `validate()` time from `binding.fn.__self__ is
  record.instance`** (stateless, no record field) — rejected: ❌ property
  providers are wrapped in `_prop_provider` (`:6190-6196`), a plain function
  with no `__self__`, so every `@Disposes` for a property-provided type
  would be falsely reported `UNMATCHED_DISPOSER`; ❌ `bind_config()`
  (`:1195`) and `scanner.py:259` also call `provide()` with non-method
  callables. ✅ would have needed no new state.
- **Store the owning module on `ProviderBinding` (e.g. `binding.module`)**
  — rejected: ❌ new public-ish attribute on a binding type for a purely
  internal need; ❌ still needs a place to record "overwritten"/"unmatched"
  facts. ✅ would make `describe()` richer (possible future follow-up, not
  this plan).
- **`warnings.warn` / `logging` at wiring time instead of a `validate()`
  issue** — rejected for the same reasons as plan 012: ❌ unstructured,
  unfilterable, fires at import/install in test suites; ✅ reaches users who
  never call `validate()`. The gap report asks for the `validate()` surface
  ("precisely the class of finding DI-7's wiring report should surface").
- **Raise `TypeError` at install for overwritten/unmatched (CDI "definition
  error")** — rejected: ❌ a hard failure on a minor upgrade for code that
  installs today; `install()` raising is a 3.0.0-class change. Severity can
  be raised later; never lowered.
- **Drop the `break` so one `@Disposes(X)` serves every own `X` binding
  (CDI one-disposer-many-producers)** — deferred to §Follow-ups: ✅ 1-line,
  fixes a real leak (plan 012 E12); ❌ out of the scope the index assigns to
  this plan and changes how many times a disposer runs for existing users.
- **A single combined kind (`DISPOSER_UNWIRED`) for both cases** —
  rejected: ❌ the two have different owners, different fixes, and different
  `param_name` meaning; a filter on one kind should not have to parse the
  message to tell them apart.

---

## Steps

TDD-ordered. Steps 1-3 are RED (must fail before Steps 4-9 land).
File paths are absolute-from-repo; line numbers are against
`1402f0a` and will drift once Steps 4+ are applied.

1. [x] `tests/test_disposes_scoped_wiring.py` — **new file**. Module
       docstring names gap `P24-DISPOSES-FIRSTMATCH`, this plan, and the
       varco guard it unblocks. `from __future__ import annotations`. Uses the
       `container` fixture (`tests/conftest.py:37`) and `asyncio_mode=auto`.
       Shared fixtures at module level: `class CacheBackend` (ABC with
       `stop()`), `class RedisCache(CacheBackend)`, `class
       LayeredCache(CacheBackend)`, each appending its tag to a `stopped:
       list[str]` passed in; two `@Configuration` classes
       `RedisCacheConfiguration` / `RedisLayeredCacheConfiguration`, each with
       `@Provider(qualifier="redis" | "layered", scope=Scope.SINGLETON) ->
       CacheBackend` and its own `@Disposes(CacheBackend)`. (Qualifiers are
       needed so *both* singletons can be resolved and cached —
       `container.get(CacheBackend, qualifier=...)` is the established
       pattern, `tests/test_configuration.py:170-181`.) Build the modules
       inside a factory function or per-test so the `stopped` list is
       per-test, mirroring `tests/test_shutdown_order.py:364-372`.
       Class `TestDisposesScopedToInstallingModule` — the fix:
       - `test_sync_install_two_modules_same_interface_both_disposed_by_own_disposer`
         — `install(A); install(B)`; resolve both; `shutdown()`; assert
         `sorted(stopped) == ["layered", "redis"]` **and** that each stop was
         invoked through its own module's disposer (record the disposer
         method's `__qualname__` or module tag alongside the instance tag).
         **This is the port of the varco guard** and must fail on `1402f0a`
         with only `"redis"` present.
       - `test_async_install_two_modules_same_interface_both_disposed_by_own_disposer`
         — same with `ainstall()` / `aget()` / `ashutdown()` and `async def`
         disposers.
       - `test_install_order_reversed_still_wires_each_to_own` — `install(B);
         install(A)`; symmetric result.
       - `test_first_module_disposer_is_not_replaced_by_second_module` —
         after both installs, A's instance is stopped by A's disposer, not
         B's (asserts identity of the *method*, not only that a stop
         happened — this is what the gap report §2 calls "coincidentally
         torn down by the wrong method").
       - `test_shutdown_order_reverse_creation_preserved_across_modules` —
         resolve redis then layered; `stopped == ["layered", "redis"]` (the
         reverse-creation-order guarantee in `docs/agents/usage-rules.md:190`
         must survive the change).
       - `test_disposes_with_no_own_provider_is_not_attached_to_foreign_binding`
         — module A provides `X` with **no** `@Disposes`; module B has only
         `@Disposes(X)`. After both installs and `shutdown()`, A's instance is
         **not** stopped (B's disposer must no longer hijack A's binding).
         This is the behavioural change; it pins it deliberately.

2. [x] `tests/test_disposes_scoped_wiring.py` — class
       `TestDisposesScopedWiringRegressions` (must pass before **and** after):
       - `test_single_module_single_provider_still_wired` — the
         `tests/test_disposes.py:23-31` shape.
       - `test_single_module_two_interfaces_two_disposers_still_wired` — the
         `tests/test_disposes.py:69-95` shape.
       - `test_property_provider_disposer_still_wired` — `@Provider
         @property def conn(self) -> Connection` + `@Disposes(Connection)`;
         proves the `_prop_provider` path (`container.py:6182-6196`) is
         collected by the return-value approach.
       - `test_disposes_base_type_matches_own_subclass_provider` — provider
         `-> Sub`, `@Disposes(Base)`; wired (issubclass, `utils.py:138`).
       - `test_provide_returns_the_created_binding` — `b =
         container.provide(fn)`; `isinstance(b, ProviderBinding)` and `b is
         container._bindings[-1]`. (Private access is acceptable here; it is
         the one direct check of the new return contract.)

3. [x] `tests/test_disposes_scoped_wiring.py` — class
       `TestDisposerWiringValidation` — the two new kinds. Every test calls
       `container.validate(raise_on_error=False)` and filters by kind, the
       pattern at `tests/test_validation.py:329`:
       - `test_two_disposes_on_same_own_binding_reports_disposer_overwritten`
         — one module, one `-> Connection` provider, `@Disposes(Connection)
         close_a` **and** `close_b`. Exactly one `DISPOSER_OVERWRITTEN`,
         `severity is WARNING`, `param_name == "close_b"`, `owner ==
         "@Provider(make_conn)"`, message contains both `close_a` and
         `close_b`. At runtime, `shutdown()` runs **only** `close_b`
         (last-wins, unchanged).
       - `test_disposes_base_and_sub_on_same_binding_reports_disposer_overwritten`
         — `@Disposes(Base)` + `@Disposes(Sub)` with one `-> Sub` provider.
       - `test_disposes_matching_no_own_binding_reports_unmatched_disposer` —
         module with `@Disposes(Connection)` and no provider; one
         `UNMATCHED_DISPOSER`, `owner == "LonelyModule.close_conn"`,
         `requested == "Connection"`.
       - `test_unmatched_disposer_reported_even_when_another_module_provides_the_type`
         — A provides `X`, B has only `@Disposes(X)`: still one
         `UNMATCHED_DISPOSER` owned by B (the point of the kind).
       - `test_ainstall_records_same_wiring_issues_as_install` — the
         overwritten fixture via `ainstall()`; identical issue.
       - `test_matched_disposer_reports_no_wiring_issue` — control: the
         `test_disposes.py:23-31` shape yields zero issues of either kind.
       - `test_wiring_warnings_do_not_raise_by_default` — with the
         overwritten fixture, `container.validate()` (default
         `raise_on_error=True`) returns normally and `report.ok is False`.
       - `test_copy_inherits_wiring_issues` — `container.copy().validate(...)`
         reports the same `DISPOSER_OVERWRITTEN`.
       - `test_wiring_issues_appear_before_cycle_issues` — **optional**; only
         if a cycle fixture is cheap to build from `tests/test_validation.py`.
         Otherwise skip — ordering is covered by the docstring contract.
       - `test_empty_container_report_is_unchanged` — `issues == ()`,
         `ok is True` (regression on `tests/test_validation.py:57`).

4. [x] `providify/validation.py:58-96` — add two members **after**
       `UNREACHABLE_PRE_DESTROY` (`:96`), in this order, using the `#:`
       comment style of the other nine:
       ```python
       #: Two `@Disposes` methods on ONE @Configuration resolved to the same
       #: ProviderBinding it registered; the later one (definition order)
       #: replaced the earlier, which will therefore never run.
       DISPOSER_OVERWRITTEN = "disposer_overwritten"
       #: A `@Disposes(X)` on a @Configuration matches none of the bindings
       #: that @Configuration itself registered — it is attached to nothing.
       #: A disposer only ever tears down its own module's providers.
       UNMATCHED_DISPOSER = "unmatched_disposer"
       ```
       ⚠️ Merge point with plans 015/017, which also append members here —
       keep alphabetical-by-arrival order irrelevant; just ensure no
       duplicate values.

5. [x] `providify/validation.py:115-134` — extend the `ValidationIssue`
       Attributes docstring: `owner` gains "…or, for `UNMATCHED_DISPOSER`,
       `"ModuleName.method_name"`"; `param_name` gains "for
       `DISPOSER_OVERWRITTEN` / `UNMATCHED_DISPOSER`, the `@Disposes` method
       name (the winning one, for overwritten)"; `requested` gains "for the
       two disposer kinds, the `@Disposes(...)` argument". Docstring only.
       Also `validation.py:199-202` (`ValidationReport.issues`): insert
       "then module-level disposer-wiring issues in install order;" before
       "cycles appended last".

6. [x] `providify/container.py:225-270` — add `_DisposerWiringIssue`
       (frozen, slots) **above** `_ModuleRecord`, and add
       `disposer_issues: tuple[_DisposerWiringIssue, ...] = ()` as the
       **last** field of `_ModuleRecord` (default keeps `:6141`, `:6158`,
       `:6706-6708` valid). Both get full docstrings (Attributes; Thread/Async
       safety: frozen — safe) and the strings-only `# DESIGN:` block from
       §Design. `Literal` import: check `typing` imports at the top of
       `container.py` and add if missing.

7. [x] `providify/container.py:1048-1091` — `provide()`:
       `-> ProviderBinding`; build the binding into a local, append it,
       `return` it. Docstring `Returns:` becomes "The `ProviderBinding` just
       registered — useful for callers that need to attach post-registration
       state (the `@Disposes` wiring in `_register_module_providers` is the
       in-package consumer). Callers may ignore it." Add one sentence to the
       description noting the return is new in 2.1.0 and was `None` before.
       Do **not** change `bind_config()` (`:1195`) — it may keep discarding
       the value.

8. [x] `providify/container.py:6160-6214` — `_register_module_providers()`:
       - signature `-> tuple[_DisposerWiringIssue, ...]`;
       - `own: list[ProviderBinding] = []`; both `self.provide(...)` calls
         (`:6196`, `:6199`) become `own.append(self.provide(...))`;
       - wiring loop iterates `own` instead of `self._bindings`; before the
         assignment at `:6213`, if `binding.disposer is not None` append an
         `"overwritten"` record (`binding_owner=f"@Provider({binding.fn.__name__})"`,
         `replaced_disposer=binding.disposer.__name__`); use `for … else:` on
         the inner loop to append an `"unmatched"` record when no match;
       - keep the `break`;
       - `return tuple(issues)`;
       - rewrite the docstring: Args / Returns (the records) / Edge cases
         (property providers are collected via the same `provide()` return;
         a module with zero providers and a `@Disposes` yields one
         `"unmatched"` record; two `@Disposes` hitting one binding yields one
         `"overwritten"` record and last-wins) / and a `# DESIGN:` block
         stating **why the search is scoped to `own`** with the gap ID and
         the ✅/❌ of return-value vs slice from §Design.

9. [x] `providify/container.py:6114-6158` — `_install_one()` and
       `_ainstall_one()`: `issues = self._register_module_providers(cls,
       instance)` and pass `disposer_issues=issues` into `_ModuleRecord(...)`
       at `:6141` / `:6158`. Update both docstrings' Returns/description to
       mention the record now carries wiring diagnostics.

10. [x] `providify/container.py:5683-5685` — `validate()`: insert **pass 3**
        between the end of the per-binding loop and the cycle DFS:
        ```python
        # ── Pass 3: module teardown tier — @Disposes wiring defects ────
        # Recorded by _register_module_providers() at install time (plan
        # 014); validate() is the reporting surface, same as pass 1b.
        for module_cls, rec in self._installed_modules.items():
            for w in rec.disposer_issues:
                issues.append(disposer_wiring_issue(w))
        ```
        with a nested closure `disposer_wiring_issue(w: _DisposerWiringIssue)
        -> ValidationIssue` beside `unreachable_pre_destroy_issue`
        (`:5399`), building the two issue shapes and messages from §Design.
        Update `validate()`'s docstring pass list (`:5260-5281`): add "3.
        **Module teardown tier** (plan 014) — …" and an `Edge cases` bullet:
        the pass reads `_installed_modules`, so it reports nothing for
        providers registered via bare `provide()`; it requires `install()` to
        have run (same caveat as the `UNREACHABLE_PRE_DESTROY` bullet at
        `:5327-5333`); `copy()` inherits the records.

11. [x] `providify/decorator/lifecycle.py:246-267` — `Disposes` docstring:
        add one paragraph — "A `@Disposes` is matched only against the
        `@Provider` methods of the **same** `@Configuration` (the ones
        registered by the same `install()`), never against bindings from
        other modules. `container.validate()` reports `UNMATCHED_DISPOSER`
        when nothing in the module matches, and `DISPOSER_OVERWRITTEN` when
        two `@Disposes` in one module resolve to the same provider." Add the
        Args / Returns sections the skill requires (currently absent).

12. [x] `README.md:992-1021` (`### @Disposes — provider teardown`) — after
        `:1021` add a short paragraph: scoping rule (own module only), the
        two-module example in one sentence ("two `@Configuration`s can each
        provide `CacheBackend` with their own `@Disposes(CacheBackend)`; each
        tears down its own instance"), and the two new `validate()` kinds.
        `README.md:1531` area (`validate()` prose): extend the check list
        with "and `@Disposes` wiring defects (`DISPOSER_OVERWRITTEN`,
        `UNMATCHED_DISPOSER`)". `README.md:2152` (test-file table): add a row
        for `test_disposes_scoped_wiring.py`.

13. [x] `docs/agents/usage-rules.md:182-188` — after the `@Disposes` sentence
        add: "`@Disposes` only attaches to providers declared in the **same**
        `@Configuration`." `docs/agents/usage-rules.md:243-248` (R12) — extend
        the warning sentence to name the two new kinds alongside
        `UNREACHABLE_PRE_DESTROY`.
        `docs/agents/injection-cheatsheet.md:191` — append "(matched within
        the same `@Configuration` only)".
        `docs/agents/choosing-decorators.md:99-102` — no change needed unless
        the paragraph implies cross-module matching (read it; if it is
        neutral, leave it).

14. [x] `CHANGELOG.md:10-11` — under `## [Unreleased]`:
        `### Fixed` — "`@Disposes` wiring attached to the first matching
        `ProviderBinding` in the whole container instead of the installing
        module's own — with two `@Configuration`s providing the same
        interface, the second module's disposer overwrote the first's and the
        second module's instance was never torn down (silent leak on
        `shutdown()`/`ashutdown()`). Wiring is now scoped to the bindings the
        same `install()`/`ainstall()` registered. **Behaviour change:** a
        `@Disposes(X)` on a module that declares no `X` provider no longer
        attaches to another module's binding; `validate()` now reports it as
        `UNMATCHED_DISPOSER`. (P24-DISPOSES-FIRSTMATCH, plan 014.)"
        `### Added` — `IssueKind.DISPOSER_OVERWRITTEN`,
        `IssueKind.UNMATCHED_DISPOSER` (both `WARNING`; can newly make
        `report.ok` `False`); `DIContainer.provide()` now returns the
        `ProviderBinding` it registered (was `None`).
        ⚠️ Merge point with plans 015/016/017 — keep one `[Unreleased]`
        block; do not bump `pyproject.toml`.

15. [x] Exhaustiveness audit for the two new `IssueKind` members (same table
        as plan 012 §"Exhaustiveness audit"): `providify/__init__.py:71,222`
        — verify only, `IssueKind` already exported and members ride on the
        type; re-run `rg -n 'match .*\.kind'` — must stay empty;
        `CHANGELOG.md:111` historical 2.0.0 list — do not edit.

16. [x] Verification pass (§Verification). Confirm RED→GREEN for Steps 1
        and 3, GREEN throughout for Step 2, and that `tests/test_disposes.py`,
        `tests/test_shutdown_order.py`, `tests/test_unreachable_pre_destroy.py`,
        `tests/test_configuration.py`, `tests/test_validation.py` are
        unchanged in outcome.

---

## Edge cases

| # | input / state | expected |
|---|---|---|
| E1 | modules A and B both provide `X` (different qualifiers), each with `@Disposes(X)`; `install(A); install(B)` | A's binding → A's disposer, B's → B's; both instances torn down; zero validation issues of the new kinds |
| E2 | E1 with `ainstall()` | identical — shared `_register_module_providers` body |
| E3 | E1 with install order reversed | symmetric |
| E4 | A provides `X` with a disposer; B has only `@Disposes(X)` | A keeps its own disposer (not hijacked); B gets one `UNMATCHED_DISPOSER` |
| E5 | A provides `X` with **no** disposer; B has only `@Disposes(X)` | A's instance is **not** stopped (behaviour change vs 2.0.1, documented in CHANGELOG); B gets `UNMATCHED_DISPOSER`; A may additionally get `UNREACHABLE_PRE_DESTROY` if `X` has a `@PreDestroy` — both are correct and independent |
| E6 | one module, one `-> X` provider, two `@Disposes(X)` | last in definition order wins (unchanged); one `DISPOSER_OVERWRITTEN`, `param_name` = winner, message names loser |
| E7 | one module, `-> Sub` provider, `@Disposes(Base)` and `@Disposes(Sub)` | both match (`issubclass`, `utils.py:138`) → `DISPOSER_OVERWRITTEN` |
| E8 | one module, `-> Base` provider, `@Disposes(Sub)` | `issubclass(Base, Sub)` is False → `UNMATCHED_DISPOSER` |
| E9 | one module, **two** `-> X` providers (two qualifiers), one `@Disposes(X)` | first own match wins, second own binding unwired — **unchanged**, no new issue (non-goal; `UNREACHABLE_PRE_DESTROY` fires only if `X` has `@PreDestroy`). See §Follow-ups |
| E10 | provider declared as `@Provider @property` + `@Disposes` | wired — the `_prop_provider` wrapper goes through the same `provide()` return |
| E11 | provider `-> Repo[User]`, `@Disposes(Repo)` | matches (`utils.py:144`, origin issubclass) — unchanged |
| E12 | provider gated by an inactive `@Profile` | binding is still registered (profiles filter at lookup, not registration) → still wired to its own module's disposer; no issue |
| E13 | module installed twice (dedup at `:6074`/`:6110`) | second install skipped; records unchanged; no duplicate issues |
| E14 | `copy()` | records inherited via `replace(rec, owned=False)`; copy's `validate()` reports the same warnings; copy's `shutdown()` still skips module hooks (`owned=False`) — untouched |
| E15 | `reset_binding()` / `override()` / `restore()` after install | records are strings → still reported (possibly for a binding no longer present). Accepted: the diagnostic describes the install event, not the current graph; documented in the `_DisposerWiringIssue` docstring |
| E16 | `validate()` before any `install()` | pass 3 iterates an empty dict → no new issues; empty-container report unchanged (`issues == ()`) |
| E17 | bare `container.provide(fn)` outside any module | never has a disposer, never appears in pass 3 — unchanged |
| E18 | `@Disposes` declared on a non-callable attribute (e.g. a `property`) | `if not callable(fn): continue` (`:6203`) skips it — unchanged |
| E19 | `shutdown()` then `validate()` | `_installed_modules` survives shutdown (`:812-816`) → warnings still reported; consistent with `UNREACHABLE_PRE_DESTROY` |

---

## Verification

```bash
cd /home/edoardo/projects/providify

# RED first: after Steps 1-3, before Steps 4-10
uv run pytest tests/test_disposes_scoped_wiring.py -x -q      # must FAIL
#   expected first failure: only "redis" in `stopped` (the gap), and
#   AttributeError on IssueKind.DISPOSER_OVERWRITTEN for the validation class

# GREEN: after Step 10
uv run pytest tests/test_disposes_scoped_wiring.py -q

# no regression in the lifecycle / validation / module surfaces
uv run pytest tests/test_disposes.py tests/test_shutdown_order.py \
              tests/test_unreachable_pre_destroy.py tests/test_configuration.py \
              tests/test_validation.py tests/test_field_provider.py \
              tests/test_profiles.py tests/test_observability.py -q

# full suite (≈1043 at 2.0.1 + this plan's additions)
make test

# lint + format (repo has no type-check target — ruff only, Makefile:9-18)
make format-check

# exhaustiveness re-check for the new IssueKind members
rg -n 'IssueKind' --glob '!plans/*' .
rg -n 'match .*\.kind' .
```

Manual message check: paste the E6 and E4 fixtures into a scratch script
under the scratchpad dir and print `issue.message` — each must name the
module, the method(s), the interface, and a concrete fix.

---

## Risks

- ⚠️ **ASSUMPTION** — the Jakarta CDI statement "a disposer method with no
  matching producer on the same bean class is a definition error" is quoted
  from memory (CDI 2.0 §3.3.7 *Disposer method resolution*), not re-read for
  this plan. It is supporting rationale for `UNMATCHED_DISPOSER`, not
  load-bearing; the load-bearing argument is the behavioural change in E5.
  If the implementer wants to cite it in a docstring, verify the section
  number first.
- ⚠️ **ASSUMPTION** — no downstream consumer subclasses `DIContainer` and
  overrides `provide()` with `-> None` (a type-checker-only mismatch, not a
  runtime break). In-repo verified: exactly one `def provide(`
  (`container.py:1048`); `providify/testing.py:149,191` and
  `providify/scanner.py:259` call it and discard the result.
- ⚠️ **ASSUMPTION** — no user relied on the accidental cross-module attach
  (E5). Unverifiable; mitigated by the `UNMATCHED_DISPOSER` warning and an
  explicit CHANGELOG "Behaviour change" line. Invariant: every `@Disposes`
  that matched its *own* module's provider in 2.0.1 still does.
- **Merge-conflict risk with siblings** — `providify/validation.py`
  (`IssueKind` members, plans 015/017 add theirs; 015 adds `Severity.INFO`),
  `CHANGELOG.md` `[Unreleased]`, `README.md` `validate()` prose,
  `docs/agents/usage-rules.md` R12, and `providify/__init__.py` (this plan
  needs **no** `__init__.py` change — verify only, Step 15). Build 014
  first per the index (`000-index…md:21-25`); siblings rebase onto it.
- **`_ModuleRecord` field addition** — `slots=True` + frozen dataclass; the
  new field **must** have a default and be last, or `:6141`, `:6158` and
  `copy()`'s `replace()` break. Invariant: `_ModuleRecord(instance=…,
  owned=…, disposed=…)` remains a valid call.
- **Stale records after binding surgery** (E15) — a `reset_binding()`
  followed by `validate()` may report an overwrite for a binding that is
  gone. Accepted and documented; the alternative (binding references) is
  strictly worse (stale *objects*). If this proves noisy, a follow-up can
  prune records whose `binding_owner` no longer matches any
  `ProviderBinding` — do not do it here.
- **Ordering contract** — `ValidationReport.issues` docstring
  (`validation.py:199-202`) is amended, not contradicted: per-binding
  issues, then module-level wiring issues, then cycles. Any existing test
  asserting exact global issue order across kinds would need review; grep
  `tests/test_validation.py` for `report.issues[` index assertions before
  landing Step 10.
- **New-noise risk** — an existing user with a deliberate "teardown-only"
  module (E5 shape) now sees a warning and loses the accidental teardown.
  That is the bug being fixed; `WARNING` (not `ERROR`) keeps
  `validate(raise_on_error=True)` from raising for them.

---

## Downstream — varco

After this lands (and varco pins providify ≥ 2.1.0):

- `varco_redis/tests/test_redis_cache_disposes.py::test_both_cache_configurations_installed_together_both_get_stopped`
  — currently `xfail(strict=True)`; with `strict=True` an unexpected pass
  **fails** the varco suite, so varco must remove the `xfail` marker in the
  same change that bumps the pin. Expected observation post-fix: both
  `"redis"` and `"layered"` in the stop-order list, each stopped through its
  own configuration's `close_cache`.
- varco's `assert_no_structural_di_issues()` gate: if it inspects
  `report.issues` / `report.ok` (not only `report.errors`), any varco module
  whose `@Disposes` matches no own provider will now surface as
  `UNMATCHED_DISPOSER`. That would be a true positive worth fixing in varco.
- BACKLOG `DI-9` closes; `DI-13`'s "every `@Disposes` reachable" test can be
  written against `provide()`'s returned binding plus
  `validate()`'s `UNMATCHED_DISPOSER`.

---

## Versioning

**Minor — 2.1.0.** Not a patch, even though the core change is a bug fix:

- `provide()`'s return type changes (`None` → `ProviderBinding`) — public
  API per `CONTRIBUTING.md` (everything in `__init__.py`'s `__all__`, and
  `DIContainer` is in it). Additive, but observable.
- Two new `IssueKind` members — the public enum's value set grows; a
  previously-clean `report.ok` can become `False` (same argument as plan
  012 §Versioning).
- One behavioural change at runtime (E5) — a `@Disposes` that used to
  attach to a foreign binding no longer does. Documented; warned; not
  breaking for any correctly-authored module.
- Not a major: `validate(raise_on_error=True)` still does not raise (both
  kinds are `WARNING`); no existing correctly-scoped `@Disposes` changes
  behaviour; no signature becomes stricter.
- `pyproject.toml` bump and CHANGELOG heading move happen in the 2.1.0
  release cut (mirror `plans/013`), not here.

---

## Follow-ups (not this plan)

1. **Drop the `break` in the scoped wiring loop** so one `@Disposes(X)`
   serves every own `X` binding (E9; CDI one-disposer-many-producers). One
   line, but it changes how many times a disposer runs — needs its own
   CHANGELOG line and tests; do after 016 lands so generic aliases are
   settled.
2. **Qualifier-aware `@Disposes(X, qualifier=...)`** — would make E9 fully
   expressible without the attach-all semantics.
3. **Prune stale `_DisposerWiringIssue` records** on `reset_binding()` /
   `override()` if E15 proves noisy in practice.
4. **`describe()` could expose the disposer per `ProviderBinding`** now that
   ownership is explicit — useful for DI-7's wiring report.
