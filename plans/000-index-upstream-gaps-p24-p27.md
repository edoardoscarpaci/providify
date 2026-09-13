# Index — upstream gaps P24–P27 (varco → providify 2.1.0)

Source request: `/home/edoardo/projects/varco/UPSTREAM-GAPS.md` (Open gaps table,
2026-09-12). Each gap's full contract lives in
`/home/edoardo/projects/varco/design/upstream-gaps/providify-<slug>.md`; the plans
below cite those reports directly.

Target release: **2.1.0** (all four are additive or bug-fix; none is breaking).

## Slices

| # | Plan | Gap | Kind | Risk | Research basis |
|---|------|-----|------|------|----------------|
| 1 | [014-disposes-scoped-to-installing-module.md](014-disposes-scoped-to-installing-module.md) | P24-DISPOSES-FIRSTMATCH | bug fix + `IssueKind.DISPOSER_OVERWRITTEN` | low — ~3-line loop change | none (internal) |
| 2 | [015-requires-conditional-registration.md](015-requires-conditional-registration.md) | P25-CONDITIONAL-REGISTRATION | new `@Requires(condition=…, env=…, value=…)` marker + `IssueKind.CONDITION_INACTIVE` + `Severity.INFO` | medium — touches `_binding_is_active()` | varco brief 014 (`design/research/014-multi-binding-qualifiers-and-conditional-di.md`) |
| 3 | [017-fallback-binding.md](017-fallback-binding.md) | P27-FALLBACK-BINDING | new `@Fallback` marker + post-filter step in `_filter()` + `IssueKind.FALLBACK_SHADOWED` | medium — touches `_filter()`, every lookup path | varco briefs 017, 010 |
| 4 | [016-open-generic-binding.md](016-open-generic-binding.md) | P26-OPEN-GENERIC-BINDING | `returns=Repo[T]` open registration, `type[T]` param delivery, per-closed-alias singleton cache | **high** — rewrites `_interface_matches()` both-generic branch, adds specificity tie-break, new cache key | varco brief 016 + providify brief `design/upstream-gaps-2-1-0/research/001-typevar-introspection-py312.md` |

## Build order and dependency edges

Build in the order **014 → 015 → 017 → 016**.

- **014** is independent of everything else; it only touches
  `_register_module_providers` and adds one `IssueKind`. Land it first — it
  is the smallest and unblocks varco's `DI-9` immediately.
- **015 before 017**: both add an informational validation issue. 015 is the
  one that **adds `Severity.INFO`** to `providify/validation.py`; 017 reuses
  it. (If 017 is built first for some reason, it adds INFO itself and 015 must
  then not redefine it — both plans say so.)
- **017 before 016**: 017's "generic aliases don't shadow each other" edge row
  relies on today's `_interface_matches()` semantics; landing it before 016
  keeps its tests meaningful as a baseline, and 016's own tests then extend
  them (an open `@Fallback` `Repo[T]` vs a closed non-fallback `Repo[User]`).
- **016 last**: highest risk, largest diff, and the only one whose design
  needed fresh external research. Nothing else depends on it.

Cross-plan facts settled by the planners:

- **014** makes `provide()` return the `ProviderBinding` (additive; was `None`).
  015/016/017 may rely on that if built afterwards.
- **015** adds `Severity.INFO`, `ValidationReport.infos`, and fixes the
  `ValidationReport.__repr__` (`validation.py:275`) tier list; 017 reuses all
  three (its plan has a verify-or-add step).
- **016**'s closed-beats-open tie-break runs in `_get_best_candidate()`, i.e.
  structurally *after* 017's fallback-drop inside `_filter()` — that order is
  deliberate (a closed `@Fallback` must still yield to an open non-fallback).
- **Downstream caveat (015):** varco's P25 guard binds undecorated classes;
  `bind()` raises `ClassBindingNotDecoratedError` for those, so the guard needs
  `@Component` on `OnImpl`/`OffImpl` before it can flip.

All four edit `providify/__init__.py` (exports), `providify/validation.py`
(`IssueKind`), `CHANGELOG.md` `[Unreleased]`, and README/`docs/agents/` — each
plan's Risks section calls out the merge points. Build each on its own branch
off `2.1.0-plan`, merge sequentially, re-run `make test` after each merge.

## Definition of done for the set

- All four varco guards flip from `xfail(strict=True)` to passing against the
  new providify build (the `strict=True` makes an unexpected pass fail the
  varco suite, so the flip is loud, not silent):
  - `varco_redis/tests/test_redis_cache_disposes.py::test_both_cache_configurations_installed_together_both_get_stopped`
  - `varco_core/tests/test_providify_upstream_gaps.py::TestP25ConditionalRegistration::test_requires_decorator_is_exported`
  - `varco_core/tests/test_providify_upstream_gaps.py::TestP26OpenGenericBinding::test_open_alias_registration_resolves_closed_request` and `::test_open_alias_factory_receives_closed_type_argument`
  - `varco_core/tests/test_providify_upstream_gaps.py::TestP27FallbackBinding::test_fallback_decorator_is_exported` and `::test_validate_reports_shadowed_fallback`
- Every always-passing control test in those varco classes still passes.
- providify's own suite (`make test`, ~1043 tests at 2.0.1) passes with the
  new tests from each plan; `make lint` clean.
- `CHANGELOG.md` `[Unreleased]` has one entry per gap; `README.md` documents
  `@Requires`, `@Fallback` (contrasted with the `@Default` qualifier), and
  open-generic `returns=Repo[T]`.
- Release 2.1.0 is cut by a separate release plan (mirror `plans/013`), not by
  any of these four.
