# Plan 013 — Release `X.Y.Z` (requested: 2.0.1) — version bump, changelog cut, tag, publish

> **Every step below is written against a single parameter `X.Y.Z`.** Fix it once in
> §Decision Required, then substitute mechanically. Switching 2.0.1 → 2.1.0 changes
> five literals and nothing else. Do not hardcode a version anywhere this plan does
> not explicitly list.

---

## Decision Required (maintainer's call — resolve before Step 1)

The request is for **2.0.1**. The research brief
[`design/v2-0-1-release/research/001-patch-vs-minor-versioning.md`](../design/v2-0-1-release/research/001-patch-vs-minor-versioning.md)
concludes the evidence favours **2.1.0**. Presented neutrally; this is the
maintainer's decision, not the plan's.

**The change being released** (already merged in `82bcd94 "Predestroy (#13)"`):
`IssueKind.UNREACHABLE_PRE_DESTROY` — a new member on an enum that *is* public API
(`providify/__init__.py:71` in `__all__`), plus a new `WARNING` emitted by
`container.validate()`.

| | **2.0.1 (patch — as requested)** | **2.1.0 (minor — brief's recommendation)** |
|---|---|---|
| SemVer 2.0.0 | ❌ Clause 6 allows patch for "backward compatible **bug fixes**" only; a new exported enum member is new API, not a fix (brief §A) | ✅ Clause 7: "MUST be incremented if new, backward compatible functionality is introduced to the public API" (brief §A) |
| Project's own declared policy | ❌ `CONTRIBUTING.md:68-74` declares SemVer 2.0.0 compliance and commits the `__all__` surface from 2.0.0; only **non**-exported internals may change in a patch | ✅ consistent with `CONTRIBUTING.md:66-88` |
| Python tooling precedent | ❌ flake8 is explicit that new error codes are not patch releases; pylint/mypy treat new diagnostics as features (brief §B) | ✅ matches flake8 / pylint / mypy practice (brief §B) |
| Type-safe consumers | ❌ hidden: exhaustive `match` / `assert_never()` over `IssueKind` starts failing type-check with no version signal (brief §C) | ✅ signalled by the minor bump (brief §C) |
| PEP 440 / PyPI | ✅ valid, monotonically increasing (brief §E) | ✅ valid, monotonically increasing (brief §E) |
| `~=2.0.0` pins auto-upgrade | ✅ yes | ❌ no — those users stay on 2.0.x (brief §E table) |
| Perception | ✅ reads as a small, low-risk update | ❌ reads as a larger bump than it feels |

**Strength of evidence** (brief §Librarian's Note): HIGH for 2.1.0, LOW for 2.0.1
("only 'smaller bump perception'; no spec backing").

**Blast radius of choosing either — exactly five literals:**

1. `pyproject.toml:3` — `version = "X.Y.Z"`
2. `SKILL.md:11` — `**Version:** X.Y.Z (see ...)`
3. `CHANGELOG.md:10` — the released heading `## [X.Y.Z] — 2026-09-01`
4. `CHANGELOG.md:654-655` — the two link-reference lines
5. the git tag `vX.Y.Z` (Step 14) and the GitHub Release title (Step 17)

`uv.lock` follows automatically from (1) via `uv lock`. Nothing else in the repo
hardcodes a version (scout-verified; README badges at `README.md:6-8` are dynamic).

**➡️ Default for this plan: `X.Y.Z = 2.0.1`** (the maintainer's stated request).
If the maintainer instead picks `2.1.0`, substitute the string in the five places
above; **no step is rewritten, no step is added or removed**, with one exception
noted inline at Step 8 (CHANGELOG `### Added` framing already reads correctly for
either, so in practice there is no exception — see Step 8's note).

---

## Goal

Ship providify `X.Y.Z` to PyPI: version bumped in `pyproject.toml` + `uv.lock` +
`SKILL.md`, `CHANGELOG.md` cut from `[Unreleased]` to a dated release section per
Keep a Changelog 1.1.0, feature docs confirmed (not rewritten), branch merged to
`main`, signed annotated `vX.Y.Z` tag pushed, `release.yml` publish job green, and
a GitHub Release created from the changelog section.

## Non-goals

- **No code changes.** The release content is already merged (`82bcd94`):
  `providify/validation.py:96`, `providify/container.py:447,5271,5327,5408`,
  `providify/decorator/lifecycle.py:174`, `tests/test_unreachable_pre_destroy.py`.
- **No doc rewrites.** The feature docs are already written; Step 10 is a
  *confirmation* pass only.
- **No classifier change.** `pyproject.toml:22` stays
  `"Development Status :: 5 - Production/Stable"`.
- **No `__version__` attribute** added to `providify/__init__.py` — deliberately
  absent, per `plans/011-v2-0-0-stable-release.md:48-49`.
- **No deletion of `poetry.lock`** in this PR (see Step 9 — it is vestigial but
  removing it is a separate, unrelated change).
- **No new GitHub rulesets / security settings.** `plans/011` Phase 8 owns those.

---

## Design

```
prepare-2.0.1 (current branch)
   │
   ├─ Step 1-3   version literals: pyproject.toml:3, SKILL.md:11
   ├─ Step 4-8   CHANGELOG.md cut (Keep a Changelog 1.1.0 §D of brief)
   ├─ Step 9     uv lock  (uv.lock root entry follows pyproject.toml:3)
   ├─ Step 10    docs confirmation pass (read-only)
   ├─ Step 11    verification gate: lint, test, build, publish-dry, version triple
   ├─ Step 12    single release commit
   │
   ▼
  PR → main ──► CI green ──► Step 14: git tag -a vX.Y.Z (ANNOTATED + signed) on main
                                          │
                                          ▼ push tag
                              .github/workflows/release.yml
                              test (3.12,3.13) → build (uv build) → publish
                                          │      environment: release-pypi
                                          ▼      OIDC trusted publishing
                                       PyPI X.Y.Z
                                          │
                                          ▼ Step 17
                              gh release create vX.Y.Z (notes = changelog section)
```

**Load-bearing ordering fact:** the tag push is what publishes. Nothing reaches
PyPI before Step 15. Therefore every irreversible check belongs in Step 11, and
the tag must be created on the exact commit CI went green on
(`plans/011-v2-0-0-stable-release.md:812-848`, steps 30-35).

### Alternatives considered

- **Bump the version in a `__version__` attribute too (single-source via
  `importlib.metadata`)**: rejected because ❌ it adds a second source of truth to
  keep in sync and contradicts the deliberate decision recorded at
  `plans/011-v2-0-0-stable-release.md:48-49`; ✅ `pyproject.toml:3` staying the sole
  source of truth is what makes the blast radius five literals.
- **Automate the bump with `uv version` / `bump-my-version` / `hatch version`**:
  rejected because ❌ it would not touch `SKILL.md:11` or `CHANGELOG.md` anyway, so
  it replaces two manual edits with one tool plus two manual edits, and introduces
  a new dev dependency for a release that happens a few times a year; ✅ revisit if
  release cadence increases.
- **Tag directly on `prepare-2.0.1` without merging to `main` first**: rejected
  because ❌ `release.yml` would publish a version whose commit is not on the
  default branch, so the PyPI artefact would not correspond to `main`'s history and
  the GitHub Release would point at a commit that could still be rebased; ✅ merge
  first matches the 2.0.0 precedent (`plans/011:818-819`, step 30).
- **Squash the changelog `[Unreleased]` content into the 2.0.0 section** (i.e. treat
  it as a 2.0.0 amendment): rejected because ❌ 2.0.0 is already published to PyPI
  and tagged; rewriting a released section is a lie about what 2.0.0 contained.
- **Release as 2.1.0** — see §Decision Required; this is a live option, not a
  rejected one. It is *not* rejected on the merits; it is deferred to the
  maintainer.

---

## Steps

Each step is independently checkable. `X.Y.Z` = the string fixed in
§Decision Required (default `2.0.1`).

### A. Version literals

1. [ ] **Confirm the starting state is clean.** `git status --porcelain` is empty
       and `git rev-parse --abbrev-ref HEAD` prints `prepare-2.0.1`. If the branch
       name no longer matches the chosen version (e.g. maintainer picked 2.1.0),
       renaming the branch is optional and cosmetic — **do not** let it block; the
       branch name is not published anywhere.

2. [ ] `pyproject.toml:3` — bump the sole source of truth.
       - Before: `version = "2.0.0"`
       - After:  `version = "X.Y.Z"`
       Leave `pyproject.toml:22` (`"Development Status :: 5 - Production/Stable"`)
       untouched — it is already correct for a post-2.0.0 release.

3. [ ] `SKILL.md:11` — bump the documented version line.
       - Before: `- **Version:** 2.0.0 (see \`pyproject.toml\` for the current version)`
       - After:  `- **Version:** X.Y.Z (see \`pyproject.toml\` for the current version)`

### B. CHANGELOG cut

Mechanics per Keep a Changelog 1.1.0 as summarised in the brief §D
(`design/v2-0-1-release/research/001-patch-vs-minor-versioning.md:99-124`):
rename `[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`, create a new blank
`[Unreleased]` above it, update the link-reference definitions at EOF.
**House style deviation to preserve:** this repo uses an em-dash (`—`), not a
hyphen, between version and date (see `CHANGELOG.md:34`). Match the repo, not the
spec's ASCII example.

4. [ ] `CHANGELOG.md:10` — rename the unreleased heading to the release heading.
       - Before: `## [Unreleased]`
       - After:  `## [X.Y.Z] — 2026-09-01`
       (ISO 8601 date, brief §D. `2026-09-01` = today; if the release actually
       lands on a later day, use the day the tag is pushed, not the day this plan
       was written.)

5. [ ] `CHANGELOG.md` — insert a fresh empty `[Unreleased]` section **above** the
       heading edited in Step 4, so the file reads (lines 8-12 after the edit):
       ```markdown
       ---

       ## [Unreleased]

       ## [X.Y.Z] — 2026-09-01
       ```
       Do not add empty `### Added` / `### Changed` subheadings under the new
       `[Unreleased]`; the pre-existing convention is a bare heading.

6. [ ] `CHANGELOG.md` — leave the existing `### Added` (:12-23) and `### Changed`
       (:25-30) bodies **byte-for-byte unchanged**. They are already written,
       already correct, and the ⚠️ note at :20-23 (that `report.ok` can newly become
       `False` while `validate(raise_on_error=True)` still does not raise) is the
       single most important line in this release — do not reword it.

7. [ ] `CHANGELOG.md:654` — repoint the `[Unreleased]` compare link.
       - Before: `[Unreleased]: https://github.com/edoardoscarpaci/providify/compare/v2.0.0...HEAD`
       - After:  `[Unreleased]: https://github.com/edoardoscarpaci/providify/compare/vX.Y.Z...HEAD`

8. [ ] `CHANGELOG.md` — insert the new version link-ref immediately below
       `[Unreleased]` and above `[2.0.0]` (descending order is the file's existing
       invariant):
       ```
       [X.Y.Z]: https://github.com/edoardoscarpaci/providify/compare/v2.0.0...vX.Y.Z
       ```
       Note the *compare* form, matching `[0.1.7]`-and-below at `:656-660`.
       `[2.0.0]` at `:655` uses the `releases/tag/` form because it was the first
       tagged release with no predecessor to compare against; leave it alone.
       ⚠️ **Version-choice note:** the `### Added` body needs no rewording for
       either 2.0.1 or 2.1.0 — it already describes the enum member as an addition,
       which is accurate under both. There is nothing version-specific in the prose.

### C. Lockfiles

9. [ ] **`uv.lock`** — regenerate so the root package entry tracks
       `pyproject.toml:3`.
       ```bash
       cd /home/edoardo/projects/providify && uv lock
       ```
       Expected diff: exactly the root entry, `uv.lock:52`,
       `version = "2.0.0"` → `version = "X.Y.Z"` (the entry is at `:51-53`,
       `name = "providify"` / `version` / `source = { editable = "." }`).
       If `uv lock` produces a larger diff (transitive dev-dependency churn),
       that is acceptable but must be *reviewed*, not waved through — `release.yml`
       runs `uv sync --locked`, so a bad lock breaks the publish.

10. [ ] **`poetry.lock`** — **no action; do not edit, do not delete.**
        Verified during planning: `poetry.lock` contains **no `providify` root
        entry** (no `name = "providify"` anywhere in it) and `pyproject.toml:57-58`
        declares `requires = ["hatchling"]` / `build-backend = "hatchling.build"`
        with **no `[tool.poetry]` section**. It is a vestigial artefact of an
        earlier toolchain; CI (`.github/workflows/release.yml:24,40`) and the
        `Makefile` use `uv` exclusively. It therefore carries no version string to
        bump and cannot block this release. Removing it is a legitimate cleanup but
        belongs in its own PR (see §Risks).

### D. Documentation confirmation pass (read-only — confirm, do not rewrite)

11. [ ] Read each location below and confirm it already describes
        `UNREACHABLE_PRE_DESTROY` / the `@PreDestroy`-vs-`@Disposes` rule
        correctly. **If a location is already correct, tick it and move on — the
        deliverable of this step is a tick, not a diff.** Only if a location is
        *wrong* does it become an edit (and then say so in the PR description).

        | File:line | What must be true |
        |---|---|
        | `README.md:945-950` | blockquote: `@PreDestroy` not called for `@Provider`-produced instances, use `@Disposes`, `validate()` reports `UNREACHABLE_PRE_DESTROY` (a `WARNING`) — ✅ spot-checked during planning, present and correct |
        | `PROVIDERS.md:134-138` | CDI teardown framing: `@Disposes` is the teardown path for producer-made instances |
        | `docs/agents/usage-rules.md:183-188`, `:243-246` | agent-facing rule states the same, names the `IssueKind` |
        | `docs/agents/injection-cheatsheet.md:190-191` | bullet: "`@PreDestroy` ... never fires for `@Provider`-produced instances — use `@Disposes`." ✅ spot-checked; note it deliberately does **not** name the `IssueKind` — that is fine for a cheatsheet, do not add it |
        | `SKILL.md:287` | gotcha entry present |
        | `providify/decorator/lifecycle.py:174` | docstring states class-bindings-only |

12. [ ] Confirm no *other* file hardcodes `2.0.0` as the project version:
        ```bash
        cd /home/edoardo/projects/providify && \
          grep -rn --exclude-dir=.git --exclude-dir=.venv --exclude-dir=dist \
            '2\.0\.0' -- . | grep -v -e '^./CHANGELOG.md' -e '^./plans/' \
            -e '^./design/' -e 'semver.org' -e 'Semantic Versioning'
        ```
        Expected survivors after filtering: `pyproject.toml` (now `X.Y.Z`),
        `uv.lock`, and unrelated third-party pins. Any *prose* file still claiming
        the project is at 2.0.0 is a miss — fix it and note it here.

### E. Verification gate (all must pass before Step 14)

13. [ ] Run the full gate from the repo root, in this order, stopping at the first
        failure:
        ```bash
        cd /home/edoardo/projects/providify
        make lint          # uv run ruff check .
        make format-check  # uv run ruff format --check . && ruff check .
        make test          # uv run pytest   -> includes tests/test_unreachable_pre_destroy.py
        make clean && make build   # uv build -> dist/providify-X.Y.Z{-py3-none-any.whl,.tar.gz}
        make publish-dry   # uv publish --dry-run
        ```
        **Artefact filename check:** `ls dist/` must show `X.Y.Z` in both filenames.
        A stale `2.0.0` artefact here means `make clean` was skipped.

14. [ ] **Version-triple consistency check** — the non-negotiable pre-tag gate
        (`plans/011-v2-0-0-stable-release.md:820-823`, step 31). All three must
        print `X.Y.Z`:
        ```bash
        cd /home/edoardo/projects/providify
        sed -n '3p' pyproject.toml                       # version = "X.Y.Z"
        grep -A1 '^name = "providify"$' uv.lock          # version = "X.Y.Z"
        grep -m1 '^## \[' CHANGELOG.md                   # ## [Unreleased]   <- expected
        grep -m2 '^## \[' CHANGELOG.md | tail -1         # ## [X.Y.Z] — 2026-09-01
        ```
        Note the top section is now `[Unreleased]`, so the *released* section is the
        **second** `## [` match — that is why there are two changelog greps.

### F. Git, merge, tag, publish

15. [ ] Commit everything as one release commit on `prepare-2.0.1`:
        ```bash
        git add pyproject.toml uv.lock SKILL.md CHANGELOG.md
        git commit -S -m "chore(release): X.Y.Z

        Bump version, cut CHANGELOG [Unreleased] -> [X.Y.Z], regenerate uv.lock.
        Release content: IssueKind.UNREACHABLE_PRE_DESTROY validation warning (#13)."
        git push origin prepare-2.0.1
        ```
        ⚠️ If the repo's branch ruleset requires signed commits (set up in
        `plans/011` Phase 8b), `-S` is mandatory, not optional.

16. [ ] Open the PR `prepare-2.0.1` → `main`, titled `chore(release): X.Y.Z`.
        Body: paste the new `## [X.Y.Z]` changelog section verbatim, plus one line
        recording the §Decision Required outcome ("released as patch/minor because
        …"), so the reasoning is reviewable and survives this plan file.
        ⚠️ `CONTRIBUTING.md:61` says "Branch off `dev`" — if `dev` is still the
        integration branch, the PR target may need to be `dev` first, then `dev` →
        `main`. Confirm the current branch model with `git branch -r` before
        opening; see §Risks.
        **Wait for CI green on the merge commit.** Do not proceed on a green
        *branch* build if the merge commit differs.

17. [ ] After merge, on `main` at the exact green commit, create the **annotated,
        signed** tag (`plans/011:824-831`, step 32 — annotated, not lightweight, so
        tagger/date/message are stored):
        ```bash
        git checkout main && git pull --ff-only
        git log -1 --oneline           # confirm this is the merge commit CI passed on
        git tag -a vX.Y.Z -m "Release X.Y.Z — UNREACHABLE_PRE_DESTROY validation warning"
        git tag -v vX.Y.Z              # confirm signature + annotation
        git push origin vX.Y.Z
        ```

18. [ ] Watch the `Release` workflow (`.github/workflows/release.yml`):
        `test` (3.12 + 3.13) → `build` → `publish`. The `publish` job uses
        environment `release-pypi` and OIDC trusted publishing.
        **If `publish` fails the OIDC exchange, do NOT re-cut the tag** — fix the
        PyPI-side publisher registration (owner/repo/workflow filename/environment
        must match) and re-run the failed job (`plans/011:832-835`, step 33).

19. [ ] Confirm https://pypi.org/project/providify/ shows `X.Y.Z`.

20. [ ] Create the GitHub Release from the changelog section
        (`plans/011:836-841`, step 34):
        ```bash
        awk '/^## \[X\.Y\.Z\]/{f=1} /^## \[2\.0\.0\]/{f=0} f' CHANGELOG.md > /tmp/notes.md
        gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/notes.md --latest
        ```
        (`awk` rather than `sed -n '/a/,/b/p'` because `sed`'s range is inclusive of
        the terminating line and would drag the `## [2.0.0]` heading into the notes.)
        Escape the dots in the awk pattern as shown.

21. [ ] Post-release sanity in a clean environment (`plans/011:842-843`, step 35):
        ```bash
        uv run --with providify==X.Y.Z --no-project python -c \
          "from providify import IssueKind; print(IssueKind.UNREACHABLE_PRE_DESTROY)"
        ```
        This simultaneously proves the artefact installs and that the release's
        headline symbol is actually exported.

---

## Edge cases

- **`make build` run without `make clean`** → `dist/` holds both `2.0.0` and
  `X.Y.Z` artefacts; `uv publish` would attempt to upload the already-published
  2.0.0 and fail (or, worse, succeed partially). Step 13 always cleans first.
- **`uv lock` produces unrelated dependency churn** → acceptable, but `release.yml`
  runs `uv sync --locked`; review the diff rather than blind-committing (Step 9).
- **Changelog has content added to `[Unreleased]` between Step 5 and the tag push**
  → that content would ship silently inside `X.Y.Z`'s section boundary in the
  GitHub Release notes only if it landed *above* the release heading. Re-run
  Step 14's greps immediately before Step 17.
- **Tag pushed before the PR merged** → `release.yml` publishes a commit not on
  `main`. Irreversible (PyPI forbids re-uploading a version). Step 17 explicitly
  checks out `main` first.
- **Maintainer switches 2.0.1 → 2.1.0 after Step 9** → redo Steps 2, 3, 4, 7, 8, 9
  (six edits, all mechanical, none structural) and re-run Steps 13-14. No step is
  invalidated. This is the entire point of parameterising `X.Y.Z`.
- **Date slips past 2026-09-01** → Step 4's date must be the tag-push date, not the
  plan-authoring date; ISO 8601, brief §D.
- **A downstream user pins `~=2.0.0`** → picks up 2.0.1 automatically, does **not**
  pick up 2.1.0 (brief §E table). This is the single user-visible consequence of
  the §Decision Required choice.

---

## Verification

Run from `/home/edoardo/projects/providify`:

```bash
# Static + tests
make lint
make format-check
make test

# Build + publish rehearsal
make clean && make build && ls dist/    # both filenames must contain X.Y.Z
make publish-dry

# Version triple (must all print X.Y.Z)
sed -n '3p' pyproject.toml
grep -A1 '^name = "providify"$' uv.lock
grep -m2 '^## \[' CHANGELOG.md | tail -1

# Changelog link-refs resolve and are in descending order
tail -12 CHANGELOG.md

# Post-publish
uv run --with providify==X.Y.Z --no-project python -c \
  "from providify import IssueKind; print(IssueKind.UNREACHABLE_PRE_DESTROY)"
git tag -v vX.Y.Z
```

There is no separate type-check target in the `Makefile` (targets are
`install test lint format format-check build publish publish-dry clean`);
`ruff check` via `make lint` is the static gate, matching what
`release.yml:25` runs in CI.

---

## Risks

- **⚠️ ASSUMPTION — a `v*` tag ruleset may block Step 17's tag push.**
  `plans/011-v2-0-0-stable-release.md:163-164` warns: "The `v*` tag ruleset (rows
  10-14) **can lock you out of P7**. Ruleset rules bind repository admins unless
  admins are added as bypass actors." Phase 8b was scheduled *after* the 2.0.0
  release, so such a ruleset plausibly exists now and did not exist for 2.0.0.
  **Not verified** (requires repo settings access). *Invariant that must hold:* the
  maintainer can push `vX.Y.Z`. Check GitHub → Settings → Rules → Rulesets for a
  tag ruleset **before** Step 15, not after; discovering this at Step 17 wastes a
  merged PR's worth of time but is not destructive.

- **⚠️ ASSUMPTION — the `release-pypi` GitHub environment may require manual
  approval.** `release.yml:51-53` declares `environment: name: release-pypi`.
  Environments support required reviewers / wait timers, which would pause the
  `publish` job silently after `build` succeeds. **Not verified** from the repo
  (environment protection rules are not in version control). *Consequence if true:*
  Step 18 shows `publish` as "Waiting", not "Failed" — approve it in the Actions UI.
  Not a failure mode, just a surprise. Confirm in GitHub → Settings → Environments.

- **⚠️ ASSUMPTION — branch model: is `main` the correct PR target?**
  `CONTRIBUTING.md:61` instructs "Branch off `dev`", and `plans/011:818-819` hedges
  ("`dev` → `main` if that is the release branch"), yet the task states the PR is
  `prepare-2.0.1` → `main` and recent history shows `fb59e48 "Dev: sync dev OpenSSF
  fix to main"` — i.e. `dev` exists and is periodically synced to `main`.
  *Invariant:* the tagged commit must be reachable from `main`. Run `git branch -r`
  at Step 16; if `dev` is live, the safe route is `prepare-2.0.1` → `dev` → `main`,
  and `dev` must be re-synced from `main` after the release commit lands, or the
  next feature branch will silently revert `pyproject.toml:3`.

- **⚠️ ASSUMPTION — `report.ok` flipping to `False` may break a downstream CI gate.**
  The changelog note (`CHANGELOG.md:20-23`) states this explicitly:
  `validate(raise_on_error=True)` still does not raise (it is a `WARNING`), and a
  gate inspecting `report.errors` only will not see it — but a gate asserting
  `report.ok is True` **will** newly fail. The brief flags exactly this as an
  evidence gap (§Evidence Gaps: "what about CI pipelines configured to
  `fail_on_warning: True`?"). **Not verified** — providify's downstream consumers
  are unknown. *Mitigation:* the changelog note is the mitigation; do not remove or
  soften it (Step 6). This risk is materially larger under 2.0.1 than 2.1.0, since
  patch-pinned users receive it without opting in (brief §E).

- **⚠️ ASSUMPTION — no consumer relies on exhaustive `match` / `assert_never()` over
  `IssueKind`.** Per brief §C this is a static-analysis breaking change for such
  consumers (type-check fails; runtime is unaffected). Unverifiable from inside the
  repo; called out in brief §Evidence Gaps as worth its own audit.

- **`poetry.lock` is vestigial — verified, but its *presence* is a live hazard.**
  It has no `providify` entry and `pyproject.toml` has no `[tool.poetry]` section
  (hatchling backend, `pyproject.toml:57-58`), so it cannot affect this release.
  The risk is human: a future contributor may run `poetry install` and get a
  divergent environment from `uv sync --locked`. *Follow-up, not this PR:* delete
  `poetry.lock` in a standalone commit.

- **PyPI uploads are irreversible.** A version number, once published, can never be
  reused — even after deletion. This makes §Decision Required a one-way door: if
  2.0.1 ships and is later judged to have been a SemVer violation, the remedy is a
  *subsequent* 2.1.0, not a correction. Steps 13-14 exist entirely to make Step 17
  safe.

- **Two version literals live outside `pyproject.toml`** (`SKILL.md:11`,
  `CHANGELOG.md` heading) with no automated consistency check. *Invariant:*
  Step 14's version-triple grep is the only thing preventing drift. It is cheap;
  do not skip it because "the diff looked right".
