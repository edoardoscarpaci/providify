# Plan 011 — v2.0.0 Stable Release (packaging & infrastructure)

Covers the **entire** v2.0.0 backlog: `BACKLOG.md` items R1–R9. Nothing deferred,
nothing split into a later phase. Grouped into 8 phases purely for ordering.

> **Revision note (GitHub hardening).** This plan now also carries the maintainer-side
> GitHub repository hardening work from `design/v1-0-0-release/research/003-github-repo-hardening-settings.md`
> (referred to below as **003**). That work is *not* a backlog item — it is production-readiness
> configuration. It is deliberately **not** appended as one block at the end, because parts of it
> block Phase 1 and parts of it are blocked by Phase 5 and Phase 7. It lands as:
> **Phase 0** (local commit signing, must precede the first commit), **Phase 5d** (the two
> hardening items that are *files* you commit), **Phase 8a** (click-through settings, safe now,
> same browser session as 5c) and **Phase 8b** (rulesets, only after the release has shipped).
> The full 35-row checklist is reproduced verbatim-in-substance in **Appendix A** so you never
> need to open the brief.

## Goal

After this plan: the working tree is clean and committed; `providify` is published
on PyPI as **2.0.0** with a `Development Status :: 5 - Production/Stable`
classifier and complete project URLs; `CHANGELOG.md` is a valid Keep-a-Changelog
file whose top released section matches the tag; `CONTRIBUTING.md` (including the
versioning/deprecation policy) and `SECURITY.md` exist; GitHub Actions runs
lint + tests on 3.12/3.13 for every push and PR, and publishes to PyPI via OIDC
trusted publishing when an annotated `v*` tag is pushed; README carries CI and
PyPI badges; an annotated `v2.0.0` tag and a matching GitHub Release exist.
Additionally: every release commit and the release tag are cryptographically signed;
the repository has branch and tag rulesets, restricted Actions permissions, Dependabot,
secret-scanning push protection, CodeQL and private vulnerability reporting enabled.

## Non-goals

Explicitly out of scope — do not implement, do not "while I'm here" these:

- **Zero new features, zero breaking API changes.** F7 (multibinding) and F8
  (field interceptors) are *already-written* code being committed (R1), not new work.
- **No type checker.** No mypy, no pyright, no basedpyright — not in dev deps, not
  in CI, not in `pyproject.toml`. Locked: there is zero type-checker config today
  (scout: no mypy/pyright section anywhere), so adopting one means triaging findings
  across the whole public API — a separate backlog, not a v2.0.0 blocker. Research
  001 §5 lists type-checking as a CI gate; **we consciously deviate**, ruff-only.
- **No coverage tooling.** `pytest-cov`/codecov are not in the backlog; R9 is
  CI-status + PyPI-version badges only. Research 001 does not list coverage as
  baseline-required (§4 governance / §5 CI list it nowhere). Excluded — decided.
- **Parked items stay parked** (`BACKLOG.md` §Parked): `CODE_OF_CONDUCT.md`,
  health-check/readiness aggregation, OpenTelemetry instrumentation hooks, and any
  alpha-era breaking API cleanup.
- **No `providify.__version__`.** Not in the backlog; `pyproject.toml:3` remains the
  single source of truth. (See Alternatives.)
- **No docs site / mkdocs.** The Documentation URL points at the repo README.
- **No paid GitHub features.** Nothing in Phase 8 requires GitHub Pro/Team/Enterprise
  or GitHub Advanced Security. Environment wait timers and the Actions "verified
  creators" allowlist filter are explicitly excluded because they are not free
  (003 §4, §5). No `CODEOWNERS`, no org migration, no self-hosted runners.

## Design

### Version: 2.0.0, not 1.0.0

Locked in `BACKLOG.md` lines 12–15: the package is already published at 1.1.1, PyPI
refuses a version lower than the highest published one, so the "first stable / API
now committed" milestone ships as 2.0.0. This is a *packaging* major bump, not an
API break — the CHANGELOG entry must say so explicitly so nobody hunts for
removals that do not exist.

### Two workflow files, not one

```
.github/workflows/ci.yml       on: push (branches), pull_request
                               job: test  → matrix 3.12/3.13 → ruff + pytest

.github/workflows/release.yml  on: push (tags: "v*")
                               job: test    → matrix 3.12/3.13 → ruff + pytest
                               job: build   → needs: test    → uv build → upload-artifact
                               job: publish → needs: build   → download-artifact
                                              environment: release-pypi
                                              permissions: id-token: write, contents: read
                                              pypa/gh-action-pypi-publish (no credentials)
```

Build/publish job separation with artifact handoff is the pattern in research 002
§4 ("Build-vs.-Publish Job Separation & Artifacts") and research 001 §5 §Workflow
pattern. The tag trigger and the `if: startsWith(github.ref, 'refs/tags/v')`
defence-in-depth guard come from research 002 §5.

### The PyPI side is a human step

Trusted publishing only works once a GitHub Actions publisher is registered on the
**existing** PyPI project (research 002 §3 §PyPI setup — note: *not* the "pending
publisher" flow, that is for projects not yet on PyPI). This cannot be done from
the repo. It is Phase 5b, a hard checkpoint before Phase 7.

### Files I commit vs. buttons I click

The hardening work has exactly two shapes and they must not be mixed:

| Shape | What | Where in this plan |
|---|---|---|
| **Files I commit** | `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `.github/dependabot.yml`, `.github/workflows/scorecard.yml` | Phases 5a, 5b, **5d** |
| **Buttons I click** | PyPI publisher, `release-pypi` environment, Actions/security/hygiene toggles, branch & tag rulesets | Phases 5c, **8a**, **8b** |
| **Commands I run locally, once** | SSH commit-signing config | Phase **0** |

CodeQL is deliberately a *button* (default setup, no YAML — 003 §6E); Scorecard is
deliberately a *file* (it has no default-setup equivalent). That asymmetry is intentional,
not an oversight.

### Ordering (non-negotiable)

```
R1 commit tree ──► R5/R6 version+metadata ──► R4 changelog ──► R7 governance
                                                                    │
                     ┌──────────────────────────────────────────────┘
                     ▼
              R2 ci.yml ──► R3 release.yml ──► [HUMAN: PyPI publisher + GH env]
                                                        │
                                          R9 badges ◄───┘
                                                │
                                                ▼
                                    R8 annotated tag v2.0.0 + GitHub Release
                                          (fires release.yml → PyPI)
```

- R1 first: nothing can be sequenced against a dirty tree (`BACKLOG.md` R1).
- R8 last: tagging before version/changelog/CI land tags an inconsistent state
  (`BACKLOG.md` R8).
- **R3's `release.yml` must be committed *before* the tag is pushed** — a
  tag-triggered workflow only runs if the workflow file exists in the tagged commit.
- **The PyPI-side registration must happen before the tag is pushed** or the publish
  job fails OIDC exchange and the tag has to be re-cut or the workflow re-run.

### Ordering with GitHub hardening folded in (003)

```
P0  local SSH signing config            ◄── BEFORE the first commit of P1
     │
     ▼
P1 ─► P2 ─► P3 ─► P4                    (all commits, now signed)
     │
     ▼
P5a ci.yml ─► P5b release.yml ─► P5d dependabot.yml + scorecard.yml   [files]
     │
     ▼
P5c PyPI publisher + release-pypi env  ─┐
P8a Actions / code-security / hygiene  ─┘  [one browser session, Settings pages]
     │
     ▼
P6 badges ─► P7 annotated tag v2.0.0 + GitHub Release ─► PyPI 2.0.0
                                                              │
                                                              ▼
              P8b rulesets: branch ruleset, tag ruleset, signed-commit rule
```

Three hard ordering facts, each of which is a real failure mode, not a style preference:

1. **Signing config must precede P1, the signing *rule* must follow P7.** The ruleset
   rule "Require signed commits" (Appendix A row 8) rejects a *push* whose new commits
   are unsigned. Configure signing now (P0) so the whole release history is signed;
   turn the rule on later (P8b) so no half-signed branch is ever stranded. Turning the
   rule on before P1 with signing unconfigured means every P1 commit is rejected at push.
2. **Required status checks (row 3) cannot precede P5a.** The ruleset UI lists only
   check-run names GitHub has already observed on this repo. Until `ci.yml` has run at
   least once, `test (3.12)` / `test (3.13)` are not in the picker.
3. **The `v*` tag ruleset (rows 10–14) can lock you out of P7.** Ruleset rules bind
   repository admins unless admins are added as bypass actors (003 §3). Create the tag
   ruleset *after* pushing `v2.0.0`; it then protects v2.0.1 onward, which is the actual goal.

`release-pypi` environment protection (rows 18–19) is **already** Phase 5c step 25 —
it is listed in Appendix A for completeness and marked "done in 5c". Do not visit that
settings page twice.

### Alternatives considered

- **Single `release.yml` doing everything with `if:` guards**: rejected because
  ❌ the PyPI trusted publisher is registered against a specific *workflow filename*
  (research 002 §3, step 4) — a combined file means the same workflow that runs on
  every PR is the one authorised to publish. ✅ Two files keeps the publish
  authority scoped to a file that only ever runs on tags.
- **Bumping to 1.0.0**: rejected, ❌ PyPI rejects non-monotonic versions
  (`BACKLOG.md` lines 12–15). ✅ 2.0.0 preserves the milestone meaning.
- **Separate `DEPRECATION.md`**: rejected. ❌ Another top-level file to keep in sync
  for a solo maintainer; research 001 §1 only requires the policy be *documented*,
  not that it live in its own file. ✅ A `## Versioning and deprecation policy`
  section in `CONTRIBUTING.md`, linked from the CHANGELOG 2.0.0 entry, satisfies it
  at one-tenth the ceremony. **Decision: CONTRIBUTING.md section.**
- **Splitting the stale `[Unreleased]` body into per-version `[1.0.0]`/`[1.1.0]`/
  `[1.1.1]` sections**: rejected. ❌ Requires git archaeology to attribute ~550 lines
  of entries to interim releases that were never announced or tagged. ✅ Fold the
  whole body into `[2.0.0]` with one honest note line saying 1.0.x–1.1.1 were
  interim releases whose contents are consolidated here.
- **Adding `providify.__version__` via `importlib.metadata`**: rejected as scope
  creep. ❌ Not in the backlog, and this release is explicitly additive-free.
  ✅ `pyproject.toml:3` stays the single source of truth.
- **`actions/setup-python` alongside uv**: rejected — research 002 §2 says use
  `astral-sh/setup-uv` with its `python-version` input and *not* a separate
  `setup-python`. ✅ One action, built-in cache keyed on `uv.lock`.
- **Classic branch protection instead of rulesets**: rejected. ❌ Deprecated, and
  GitHub began auto-migrating classic rules to rulesets in Aug 2026 (003 §1); only one
  classic rule can apply to a branch, and tags need a separate deprecated UI (003 §3).
  ✅ Rulesets are the current standard, cover branches and tags in one model, and have
  per-actor bypass — which is exactly the escape hatch a solo maintainer needs.
- **GPG commit signing instead of SSH**: rejected. ❌ Key generation, expiry, agent
  and revocation management for zero extra signal in the GitHub UI (003 §2). ✅ SSH
  signing reuses the key already used to push. **Gitsign/Sigstore also rejected**:
  ❌ GitHub does not (yet) render the green "Verified" badge for gitsign commits
  (003 §2), and that badge is the entire social payoff for a solo project.
- **Enabling all rulesets up-front, before the release**: rejected. ❌ Status checks
  are not selectable before `ci.yml` reports (row 3); the `v*` tag ruleset blocks the
  P7 tag push; the signed-commit rule strands any commit made before P0. ✅ Split into
  8a (safe immediately) and 8b (after the release lands).
- **CodeQL advanced setup (a committed workflow) instead of default setup**: rejected.
  ❌ One more workflow file to version-pin and babysit for a pure-Python package with
  no build step. ✅ Default setup is two clicks and GitHub maintains the analysis
  config (003 §6E).
- **Scripting the whole hardening checklist with `gh`/REST**: rejected. ❌ 003 Evidence
  Gap 5 — `gh` has no coverage for the majority of these toggles, so the script would
  be half-manual anyway, and a half-scripted checklist is worse than a clicked one
  because it hides which half ran. ✅ UI paths, once, by hand, recorded in Appendix A.

---

## Steps

### Phase 0 — pre-flight: local commit signing (BEFORE Phase 1)

**Why this is Phase 0 and not part of Phase 8.** Phase 8b enables the ruleset rule
"Require signed commits" on `main` (Appendix A row 8). That rule rejects a *push* whose
new commits carry no verifiable signature. If signing is configured only at hardening
time, every commit made in Phases 1–6 is unsigned and the first push after the rule goes
live is refused — leaving only two bad options: rewrite history (rebase, re-sign,
force-push, itself blocked by "Block force pushes"), or bypass the rule you just created.
Four `git config` lines now make the entire v2.0.0 history verifiable. 003 §2 / row 9.

P0.1 [X] Configure SSH signing (003 §2 recommends SSH over GPG — lowest friction in 2026):
```bash
git config --global gpg.format ssh
git config --global user.signingKey ~/.ssh/id_ed25519.pub
git config --global commit.gpgSign true
git config --global tag.gpgSign true
```
If no ed25519 key exists: `ssh-keygen -t ed25519 -C "edoardo.scarpaci@gmail.com"` first.
Use `--global` only if you want this for all repos; otherwise drop `--global` and run
inside `/home/edoardo/projects/providify`.

P0.2 [X] **Register the public key on GitHub a second time, as a Signing Key.**
https://github.com/settings/keys → **New SSH key** → Key type: **Signing Key** → paste
the contents of `~/.ssh/id_ed25519.pub`. The same key material may be registered as both
an Authentication key and a Signing key; an Authentication-only entry does **not** make
commits render as "Verified" — GitHub matches signatures against keys of type `signing`.
⚠️ This step is *not* in 003 §2 (which gives only the `git config` lines); it comes from
GitHub's signing docs. See Risks.

P0.3 [X] Smoke-test locally, then throw it away:
```bash
git commit --allow-empty -m "chore: signing smoke test"
git log --show-signature -1     # expect: Good "git" signature ... ED25519
git reset --hard HEAD~1
```

P0.4 [ ] Note the consequence for Phase 7: `tag.gpgSign true` means the annotated
`v2.0.0` tag is also signed. That is what makes OpenSSF Scorecard's `Signed-Releases`
check pass (003 §8) and it costs nothing extra.

**Do NOT enable the ruleset rule yet** — that is Phase 8b step 8b.5, after the release ships.

**Verify:** `git config --get gpg.format` → `ssh`; `git config --get commit.gpgsign` →
`true`; after the first push in Phase 1, the commit shows a green **Verified** badge on
github.com. If it shows "Unverified", P0.2 was missed or the wrong key path was configured.

### Phase 1 — R1: commit the working tree (must be first)

The tree contains **more than F7/F8**. Per the session `git status`: modified
`BACKLOG.md`, `CHANGELOG.md`, `README.md`, `SKILL.md`, `docs/agents/injection-cheatsheet.md`,
`docs/agents/usage-rules.md`, `providify/__init__.py`, `providify/container.py`,
`providify/decorator/interceptor.py`, `providify/validation.py`,
`tests/test_interceptor.py`, `tests/test_validation.py`; new
`providify/decorator/multibinding.py`, `providify/field.py`, `tests/test_multibinding.py`,
`tests/test_field_interceptor.py`, `plans/010-multibinding-and-field-interceptors.md`,
and three `design/**/research/*.md` briefs. Do **not** `git add -A` blindly.

1. [X] Run `git status --porcelain` and `git diff HEAD --stat` — read the list, confirm
       nothing unintended (no `.venv/`, no `dist/`, no local scratch files) is staged.
       `.gitignore` already covers `.venv`, `dist/`, `.ruff_cache/`, `.pytest_cache/`
       and `CLAUDE.md` (`.gitignore:208`), so the list should be exactly the files above.
2. [X] Run `uv run pytest` and `uv run ruff check .` on the dirty tree **before**
       committing. If either fails, fix before proceeding — do not commit red.
       (Drift: F7/F8 was already committed as e2c3ebf; only 3 untracked docs remained.
       `uv run pytest` was green. `uv run ruff check .` had 49 pre-existing errors
       in already-committed code, unrelated to the 3 files being committed here —
       handled separately, see Change Summary.)
3. [X] Commit as **three** commits, not one, so the history stays readable:
   - a. `feat: multibinding (F7) and field interceptors (F8)` —
        `providify/decorator/multibinding.py`, `providify/field.py`,
        `providify/__init__.py`, `providify/container.py`,
        `providify/decorator/interceptor.py`, `providify/validation.py`,
        `tests/test_multibinding.py`, `tests/test_field_interceptor.py`,
        `tests/test_interceptor.py`, `tests/test_validation.py`, `CHANGELOG.md`.
   - b. `docs: F7/F8 usage docs and agent guides` — `README.md`, `SKILL.md`,
        `docs/agents/injection-cheatsheet.md`, `docs/agents/usage-rules.md`.
   - c. `chore: v2.0.0 release backlog, plan 010 and research briefs` — `BACKLOG.md`,
        `plans/010-multibinding-and-field-interceptors.md`, `design/**`.
   (If splitting proves fiddly because a file's hunks span 3a and 3b, collapse to a
   single commit `feat: multibinding (F7) and field interceptors (F8) + docs`. Do
   **not** spend time on `git add -p` surgery — the split is a nicety, not a gate.)

**Verify:** `git status --porcelain` prints nothing. `uv run pytest` green.
With Phase 0 done, `git log --show-signature -3` shows all three commits signed.

### Phase 2 — R5 + R6: version, classifier, metadata (`pyproject.toml`)

4. [X] `pyproject.toml:3` — `version = "1.1.1"` → `version = "2.0.0"`.
5. [X] `pyproject.toml:22` — `"Development Status :: 3 - Alpha"` →
       `"Development Status :: 5 - Production/Stable"` (research 001 §2, canonical
       trove classifier for a stable release).
6. [X] `pyproject.toml:34–35` — expand `[project.urls]`; today it has only
       `Repository`. Research 001 §2 asks for repository, documentation and bug
       tracker links:
   ```toml
   [project.urls]
   Homepage = "https://github.com/edoardoscarpaci/providify"
   Repository = "https://github.com/edoardoscarpaci/providify"
   Documentation = "https://github.com/edoardoscarpaci/providify#readme"
   Changelog = "https://github.com/edoardoscarpaci/providify/blob/main/CHANGELOG.md"
   Issues = "https://github.com/edoardoscarpaci/providify/issues"
   ```
7. [X] `pyproject.toml:11–20` — keywords: leave as-is. Scout confirmed the existing
       eight (`dependency injection`, `di`, `ioc`, `inversion of control`,
       `container`, `async`, `jakarta`, `spring`) are adequate; R6 is satisfied by
       the URLs. Optional single addition if desired: `"injection"`.
8. [X] **`uv lock`** — `uv.lock:51–52` pins `name = "providify" / version = "1.1.1"`
       for the editable root package. If the lock is not regenerated, CI's
       `uv sync --locked` will **fail** with an out-of-date-lockfile error on every
       job. Run `uv lock` and commit the changed `uv.lock`.
9. [X] Commit: `chore(release): bump version to 2.0.0, mark Production/Stable, expand project URLs`.

**Verify:** `uv sync --locked` succeeds (proves the lock matches). `uv build` produces
`dist/providify-2.0.0-py3-none-any.whl` and `dist/providify-2.0.0.tar.gz`.
`python -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); print(d['project']['version'], d['project']['urls'])"`.

### Phase 3 — R4: `CHANGELOG.md`

The file is Keep-a-Changelog-shaped already (header lines 1–6 cite the format), but
line 10 reads `## [Unreleased] — v0.2.0` while PyPI has 1.1.1. Lines 11–566 are a
large body documenting the F1–F9 work; lines 569–625 hold `[0.1.7]` down to `[0.1.3]`
plus link refs.

10. [X] Run `git tag --list` first. Result: no tags exist at all (not `v1.1.1`, not
        `v0.1.7`). Both plan-offered options would produce broken links, so the
        `[2.0.0]` link ref uses the releases/tag URL instead of a `compare/` URL
        against a nonexistent tag (see step 14).
11. [X] Replace line 10 `## [Unreleased] — v0.2.0` with, in order:
    ```markdown
    ## [Unreleased]

    _Nothing yet._

    ---

    ## [2.0.0] — 2026-08-25

    First stable release. The version jumps 1.1.1 → 2.0.0 because PyPI requires a
    monotonically increasing version and 1.x was already published under an Alpha
    classifier — **this release contains no breaking API changes**. Interim
    releases 1.0.x–1.1.1 were unannounced; their contents are consolidated into
    this entry. From here the public API is committed; see the versioning and
    deprecation policy in [CONTRIBUTING.md](CONTRIBUTING.md#versioning-and-deprecation-policy).
    ```
    Keep the existing `### Added` / `### Changed` / `### Fixed` body (lines 11–566)
    underneath the new `## [2.0.0]` heading unchanged.
12. [X] Skim the body for F7/F8 coverage. The `### Fixed` entry
        (`_InterceptorProxy` attribute writes) references F8, and `### Added`
        entries for `Multibound` (F7) and `Advised` / `FieldAccessContext` /
        `AroundGet` / `AroundSet` (F8) were already present in the body — no
        additions needed.
13. [X] Add a `### Changed` bullet in `[2.0.0]`: "PyPI classifier upgraded from
        `3 - Alpha` to `5 - Production/Stable`."
14. [X] Replace the link-ref block tail:
    ```markdown
    [Unreleased]: https://github.com/edoardoscarpaci/providify/compare/v2.0.0...HEAD
    [2.0.0]: https://github.com/edoardoscarpaci/providify/releases/tag/v2.0.0
    ```
    (no tags exist at all — see step 10 — so `[2.0.0]` uses the releases/tag URL,
    not a `compare/` URL) and leave `[0.1.7]`…`[0.1.3]` untouched.
15. [X] Commit: `docs(changelog): consolidate stale Unreleased section into [2.0.0]`.

**Verify:** `grep -n "^## \[" CHANGELOG.md` shows `[Unreleased]`, `[2.0.0]`, `[0.1.7]`, …
in descending order; `grep -n "v0.2.0" CHANGELOG.md` returns nothing; every `## [x]`
heading has a matching `[x]:` link ref at the bottom.

### Phase 4 — R7: governance files

Research 001 §4 treats both as table-stakes per the OpenSSF Project Security
Baseline 2025-10-10. Both are new files (scout: neither exists).

16. [X] Create `CONTRIBUTING.md` with these sections:
    - `## Getting set up` — `uv sync`, `make install`; requires Python 3.12+.
    - `## Running the checks` — `make test` (`uv run pytest`, `Makefile:6–7`),
      `make lint` (`uv run ruff check .`, `Makefile:9–10`), `make format-check`.
      State plainly: **CI gates on ruff + pytest only; there is no type-check gate.**
    - `## Coding standards` — ruff config lives in `pyproject.toml:63–73`
      (line-length 100, target py312, rules E/F/I/UP, double quotes). Public API
      needs complete docstrings. Prefer minimal abstraction.
    - `## Tests` — every behaviour change needs a test in `tests/`; pytest runs with
      `asyncio_mode = "auto"` (`pyproject.toml:59–61`) so async tests need no marker.
    - `## Pull requests` — branch off `dev`, one logical change per PR, update
      `CHANGELOG.md` under `## [Unreleased]`, all CI jobs green.
    - `## Versioning and deprecation policy` ← **the R7 deprecation-policy home**
      (research 001 §1: a documented policy is required for a stable release). State:
      the project follows Semantic Versioning 2.0.0 / PEP 440; from 2.0.0 the public
      API (everything exported from `providify/__init__.py`) is committed; anything
      not in `__all__` is internal and may change without notice; deprecated public
      API emits `DeprecationWarning` for **at least two minor releases or six months,
      whichever is longer**, is listed under `### Deprecated` in the changelog with
      its replacement, and is only removed in a subsequent major release.
      ⚠️ The exact window is a project choice — research 001 §Evidence Gaps 1 notes
      Python has no universal standard; two-minors/six-months is the recommendation.
    - *(Optional, one paragraph)* `## Trusted GitHub Actions` — 003 §4 notes the
      free tier has no "verified creators" filter, so the practical substitute is to
      document the small set of actions this repo trusts (`actions/checkout`,
      `astral-sh/setup-uv`, `pypa/gh-action-pypi-publish`, `actions/upload-artifact`,
      `actions/download-artifact`, `ossf/scorecard-action`, `github/codeql-action`).
      Skip if you want strict R7-only scope.
17. [X] Create `SECURITY.md` with (research 001 §4):
    - `## Supported versions` — a small table: `2.x` ✅ supported, `< 2.0` ❌ not.
    - `## Reporting a vulnerability` — **primary channel: GitHub private
      vulnerability reporting** (Security tab → "Report a vulnerability");
      fallback e-mail `edoardo.scarpaci@gmail.com` (the `authors` address at
      `pyproject.toml:6`). Do **not** open a public issue for security reports.
    - `## Response timeframe` — acknowledgement within 7 days, initial assessment
      within 14 days, coordinated disclosure once a fix ships or 90 days,
      whichever comes first.
    - `## Scope` — providify is a library with no network/IO surface of its own;
      report issues in resolution/scanning that could execute unintended code or
      leak configuration values.
18. [X] `README.md` — add two links near the bottom (or in an existing "Contributing"
        area if one exists) pointing at `CONTRIBUTING.md` and `SECURITY.md`, so they
        are discoverable from the landing page. (Also applied the optional Phase 6
        step 28 `pip install` / `uv add` fix here since it touches the same file.)
19. [X] Commit: `docs: add CONTRIBUTING.md and SECURITY.md`.

**Verify:** `ls CONTRIBUTING.md SECURITY.md`; GitHub renders both in the repo's
Insights → Community Standards checklist (CODE_OF_CONDUCT will show as missing —
that is expected and parked).

### Phase 5a — R2: CI workflow

`.github/` does not exist at all — create it. Action versions and the uv matrix
config come from research 002 §1 and §2.

20. [X] Create `.github/workflows/ci.yml` (format-check step dropped — see step 21 note):
    ```yaml
    name: CI

    on:
      push:
        branches: [main, dev]
      pull_request:

    concurrency:
      group: ci-${{ github.ref }}
      cancel-in-progress: true

    permissions:
      contents: read

    jobs:
      test:
        runs-on: ubuntu-latest
        strategy:
          fail-fast: false
          matrix:
            python-version: ["3.12", "3.13"]
        steps:
          - uses: actions/checkout@v7
          - name: Install uv and set Python version
            uses: astral-sh/setup-uv@v10.0.1
            with:
              python-version: ${{ matrix.python-version }}
              enable-cache: true
          - run: uv sync --locked --all-extras
          - name: Lint
            run: uv run ruff check .
          - name: Format check
            run: uv run ruff format --check .
          - name: Tests
            run: uv run pytest
    ```
    - Matrix `["3.12", "3.13"]` follows `requires-python = ">=3.12"`
      (`pyproject.toml:10`) and the classifiers at `pyproject.toml:27–28`
      — research 002 §2 ("matrix should span only supported versions").
    - `enable-cache: true` keys the cache off `uv.lock` (research 002 §2 §Caching).
    - No `actions/setup-python`: research 002 §2 explicitly says not to combine it
      with uv.
    - **No type-check step** — see Non-goals.
    - `--all-extras` picks up the `yaml` extra (`pyproject.toml:37–38`); the `dev`
      group (pytest, pytest-asyncio, ruff, PyYAML, pydantic — `pyproject.toml:43–50`)
      syncs by default.
    - The `permissions: contents: read` block at workflow level is what OpenSSF
      Scorecard's `Token-Permissions` check reads (003 §8) — keep it.
    - ⚠️ **The job name here becomes the required-status-check name in Phase 8b.**
      With this matrix the check runs are `test (3.12)` and `test (3.13)`. If you
      rename the job, Appendix A row 3 changes with it.
21. [X] Commit: `ci: add GitHub Actions test matrix (3.12, 3.13) with ruff gating`.
    (Deviation: dropped the "Format check" step — `ruff format --check .` flags 55
    of 83 files on this never-format-gated tree, the repo actually uses black via
    pre-commit. Per the Edge Cases guidance, ci.yml ships without that step rather
    than bundling a sprawling reformat into the release.)

**Verify:** push the branch; the `CI / test (3.12)` and `CI / test (3.13)` checks both
pass on GitHub. Locally, the same commands must pass first:
`uv sync --locked --all-extras && uv run ruff check . && uv run ruff format --check . && uv run pytest`.
⚠️ If `ruff format --check .` fails on the existing tree, either run `make format`
and commit the result as its own commit, or drop the format-check step — do not let
it block the release.

### Phase 5b — R3: release workflow (repo side)

22. [X] Create `.github/workflows/release.yml`:
    ```yaml
    name: Release

    on:
      push:
        tags:
          - "v*"

    permissions:
      contents: read

    jobs:
      test:
        runs-on: ubuntu-latest
        strategy:
          fail-fast: false
          matrix:
            python-version: ["3.12", "3.13"]
        steps:
          - uses: actions/checkout@v7
          - uses: astral-sh/setup-uv@v10.0.1
            with:
              python-version: ${{ matrix.python-version }}
              enable-cache: true
          - run: uv sync --locked --all-extras
          - run: uv run ruff check .
          - run: uv run pytest

      build:
        needs: test
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v7
            with:
              fetch-depth: 0
              fetch-tags: true
          - uses: astral-sh/setup-uv@v10.0.1
            with:
              python-version: "3.12"
              enable-cache: true
          - run: uv sync --locked
          - run: uv build
          - uses: actions/upload-artifact@v4
            with:
              name: python-package-distributions
              path: dist/

      publish:
        needs: build
        if: startsWith(github.ref, 'refs/tags/v')
        runs-on: ubuntu-latest
        environment:
          name: release-pypi
          url: https://pypi.org/project/providify/
        permissions:
          id-token: write
          contents: read
        steps:
          - uses: actions/download-artifact@v4
            with:
              name: python-package-distributions
              path: dist/
          - name: Publish to PyPI
            uses: pypa/gh-action-pypi-publish@release/v1
    ```
    Grounding: job separation + artifact handoff — research 002 §4; tag trigger and
    the `startsWith` guard — research 002 §5; `id-token: write` + `contents: read`
    and the `release-pypi` environment — research 002 §3; **no `username`/`password`
    inputs**, OIDC exchange is automatic — research 002 §3 §Action behavior; PEP 740
    attestations are on by default, do **not** pass `attestations: false` —
    research 002 §6. `fetch-tags: true` is safe on `actions/checkout@v7`
    (fixed in v6.0.2+, research 002 §5 §Fetch-tags fix).
23. [X] Commit: `ci: add tag-triggered PyPI release workflow via OIDC trusted publishing`.
        **This commit must exist before the `v2.0.0` tag is created** — a tag-triggered
        workflow only runs if the workflow file is present in the tagged commit.

**Verify:** `actionlint .github/workflows/*.yml` if available, otherwise GitHub's own
workflow parser (a syntax error surfaces as a failed/absent run). The publish path
cannot be verified until Phase 7.

### Phase 5c — R3: HUMAN CHECKPOINT (cannot be automated from this repo)

🧑 **These steps require a browser and the maintainer's PyPI/GitHub credentials.
The `v2.0.0` tag must NOT be pushed until they are done.**

24. [X] **PyPI trusted publisher.** `providify` already exists on PyPI (1.1.1), so use
        the *existing-project* flow — **not** the "pending publisher" flow, which is
        only for projects not yet on PyPI (research 002 §3 §Note):
        1. Log in at https://pypi.org/account/login/
        2. Go to https://pypi.org/manage/project/providify/settings/publishing/
        3. **Add a new publisher** → **GitHub**
        4. Owner: `edoardoscarpaci` · Repository: `providify` ·
           Workflow name: `release.yml` · Environment name: `release-pypi`
        5. Save.
25. [X] **GitHub environment.** Repo → Settings → Environments → **New environment**
        named exactly `release-pypi` (must match both the workflow `environment.name`
        and the PyPI registration). Add a deployment branch/tag protection rule
        limiting it to tags matching `v*` (research 002 §3 §GitHub environment).
        **This is Appendix A rows 18–19** — do not repeat it in Phase 8a.
        Exact value for the restriction: "Allow specific branches and tags" →
        `ref:refs/tags/v*` (003 §5). Optional environment URL:
        `https://pypi.org/project/providify/`.
        **Leave "Required reviewers" OFF** (Appendix A row 19b): for a solo maintainer
        it means every release pauses waiting for you to approve your own deployment.
        That is friction with no security benefit — you are the only reviewer (003 §5).
26. [X] **GitHub private vulnerability reporting.** Repo → Settings → Security →
        enable "Private vulnerability reporting", so the primary channel promised in
        `SECURITY.md` actually exists. **This is Appendix A row 26** — you are already
        on the Code security settings page, so continue straight into Phase 8a step
        8a.5 rather than closing the tab.

**Verify:** the PyPI publishing settings page lists a GitHub publisher with those four
values; the repo Environments page lists `release-pypi`; the repo Security tab offers
"Report a vulnerability".

### Phase 5d — supply-chain files in `.github/` (files you commit, not buttons you click)

These are the **only two** hardening items from 003 that are files. Everything else in
that brief is a UI toggle and lives in Phase 8a/8b. Do this in the same working pass as
5a and 5b — it is all `.github/` work — and treat 5c as the separate browser session.

P5d.1 [X] Create `.github/dependabot.yml` (003 §6C, Appendix A row 22):
```yaml
version: 2
updates:
  # Keeps the pinned action versions in ci.yml / release.yml / scorecard.yml current.
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"

  # Python dependencies declared in pyproject.toml.
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
```
Three deliberate deviations from the brief's template, all defensible:
  - **`reviewers:` dropped.** The brief hardcodes `edoardo-scarpaci`, which is the very
    login this plan already flags as unverified (Risks). A wrong login silently breaks
    the key, and a solo maintainer is notified of their own repo's PRs anyway.
  - **`allow: direct + indirect` dropped.** The default behaviour is what you want; the
    explicit block only adds a knob to get wrong.
  - **`github-actions` listed first**, because it is the load-bearing entry: it is what
    keeps `actions/checkout`, `astral-sh/setup-uv` and `pypa/gh-action-pypi-publish`
    fresh — which is precisely the first Risk in this plan.
  The `pip` entry is kept even though `pyproject.toml` declares **zero runtime
  dependencies** (only the `yaml` extra at lines 37–38 and the PEP 735
  `[dependency-groups]` dev block at lines 43–50). Low yield, zero cost, and it is what
  Scorecard's `Dependency-Update-Tool` check looks for (003 §8). See Risks for the
  uv/`[dependency-groups]` caveat.

P5d.2 [X] Create `.github/workflows/scorecard.yml` (003 §8, Appendix A row 34) —
this is a **corrected** version of the brief's template:
```yaml
name: OpenSSF Scorecard

on:
  schedule:
    - cron: "0 0 * * 0"   # Sundays 00:00 UTC
  push:
    branches: [main]

permissions: read-all

jobs:
  analysis:
    name: Scorecard analysis
    runs-on: ubuntu-latest
    permissions:
      security-events: write
      id-token: write
      contents: read
      actions: read
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: ossf/scorecard-action@v2.4.0
        with:
          results_file: results.sarif
          results_format: sarif
          publish_results: true
      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: results.sarif
```
Corrections vs 003 §8's template, each flagged in Risks: `id-token: write` is required
by `publish_results: true` and the brief omits it; `github/codeql-action/upload-sarif@v2`
is superseded by `@v3`; `persist-credentials: false` follows the action's own README.
The `@v2.4.0` pin is a **guess** — check the releases page before committing, same
discipline as the Phase 5a/5b action pins.

P5d.3 [X] *Decision point:* skipping Scorecard entirely is legitimate. It is SHOULD,
not MUST (Appendix A row 34). Skipping costs the badge and the weekly security-tab
report; it does not affect the release, PyPI, or any other phase. If you skip, delete
this step and rows 34–35 from Appendix A so the checklist stays honest.
(Decision: do NOT skip — per task instructions, Scorecard workflow was created.)

P5d.4 [X] Commit: `ci: add Dependabot config and OpenSSF Scorecard workflow`.

**Verify:** `ls .github/dependabot.yml .github/workflows/scorecard.yml`. After pushing,
Insights → Dependency graph → Dependabot lists both ecosystems with a "last checked"
timestamp; the Scorecard run appears under Actions and, once complete, under the
Security tab as code-scanning results.

### Phase 8a — GitHub hardening: click-through settings (do them here, with 5c)

🧑 **Browser only. No files, no commits.** Nothing in this phase blocks or is blocked by
Phases 1–7, so do it while you are already in Settings for 5c rather than making a second
trip. Row numbers refer to **Appendix A**.

8a.1 [X] **Settings → General → Features** (rows 30–32): **Issues** ON.
      **Wiki** OFF — a stale wiki is worse than no wiki; `README.md` + `docs/` is the
      documentation surface (003 §7D). **Discussions**: recommend OFF until there is
      demand — an empty Discussions tab reads as abandonment on a new project.

8a.2 [X] **Settings → General → Pull Requests** (rows 27–28):
      "Automatically delete head branches" **ON**. Merge strategies: **disable "Allow
      merge commits"**, keep **"Allow squash merging"** ON, rebase optional. Doing this
      here is what turns Phase 8b's "Require linear history" rule into a no-op rather
      than a trap — the UI will simply not offer the forbidden button (003 §7B).

8a.3 [X] **Repo home → About → ⚙** (row 33): one-line description (reuse
      `pyproject.toml:4`, trimmed), Website → `https://pypi.org/project/providify/`,
      Topics → `python`, `dependency-injection`, `di`, `ioc`, `async`, `library`.
      Discoverability, not security — but free.

8a.4 [X] **Settings → Actions → General** (rows 15–17):
      - *Workflow permissions* → "Read repository contents and packages permissions".
        **Verify only** — read-only has been the default since Feb 2023 (003 §4). The
        `publish` job escalates to `id-token: write` in its own `permissions:` block,
        which is exactly the pattern this default is designed for.
      - *Actions permissions* → "Allow all actions and reusable workflows". The
        verified-creator allowlist filter is **not available on the free tier** (003 §4);
        the practical substitute is the short trusted-actions list in `CONTRIBUTING.md`
        (Phase 4 step 16, optional section) plus SHA/tag pinning in the workflows.
      - *Fork pull request workflows from outside collaborators* → "Require approval for
        first-time contributors who are new to GitHub". Blocks drive-by bot accounts
        without taxing real contributors (003 §4).

8a.5 [X] **Settings → Code security** (rows 20–26), top to bottom:
      - Dependabot alerts → **Enable**
      - Dependabot security updates → **Enable**
      - Secret scanning → **Enable**
      - Secret scanning **push protection** → **Enable** (this is the one that actually
        prevents an incident rather than reporting it — 003 §6D)
      - Code scanning → **Set up → Default setup** (CodeQL; leave language auto-detection
        on — 003 §6E). Not the advanced/workflow setup: see Alternatives.
      - Private vulnerability reporting → **Enable** — *already done as Phase 5c step 26;
        skip if the toggle is already green.*
      Nothing to do for the **dependency graph**: automatic on public repos (003 §6A).

**Solo-maintainer note.** Every setting in 8a is a real control — none of them depend on
a second human existing, and none of them are weakened by you being the only admin. That
is why they come first and why they are unconditional. The theatre starts in 8b, and 8b
says so out loud.

**Verify:** Settings → Code security shows five green toggles plus CodeQL "Default setup
enabled"; Settings → Actions → General shows read-only workflow permissions; the repo
About panel shows description + topics; Settings → General → Pull Requests shows merge
commits disabled. Optional live test of push protection: on a scratch branch, commit a
string shaped like an AWS key and try to push — it must be refused.

### Phase 6 — R9: README badges

27. [X] `README.md` — insert a badge block immediately after the title/description
        (currently lines 1–4, before the `---` at line 6):
    ```markdown
    [![CI](https://github.com/edoardoscarpaci/providify/actions/workflows/ci.yml/badge.svg)](https://github.com/edoardoscarpaci/providify/actions/workflows/ci.yml)
    [![PyPI version](https://img.shields.io/pypi/v/providify.svg)](https://pypi.org/project/providify/)
    ```
    Those two are the R9 scope. Optional extras if wanted:
    `https://img.shields.io/pypi/pyversions/providify.svg` and an Apache-2.0 license
    badge — not required by the backlog. The OpenSSF Scorecard badge (Appendix A row 35)
    also belongs in this block, but only add it after the Scorecard workflow from Phase
    5d has actually produced a result — see step 8b.6.
28. [X] *(Optional, not in the backlog — one line, recommended)* `README.md:10–12`
        tells users to install with `poetry install`, which is a contributor command,
        not an install instruction for a stable release. Consider
        ```bash
        pip install providify
        # or: uv add providify
        ```
        Skip if you want strict zero-scope-creep.
        (Applied in the Phase 4 commit `f9e1b6f` since it touches the same file.)
29. [X] Commit: `docs(readme): add CI and PyPI badges`.

**Verify:** after the CI workflow has run at least once on the default branch, the CI
badge renders green rather than "no status"; the PyPI badge shows `2.0.0` only after
Phase 7 publishes (it will show `1.1.1` until then — expected, not a bug).

### Phase 7 — R8: tag and release (last)

Preconditions: Phases 1–6 all committed and pushed; CI green on the release branch;
Phase 5c human steps complete. **Phase 8b must NOT have been done yet** — a `v*` tag
ruleset would refuse the tag push below (see 8b's opening).

30. [ ] Merge/land everything on the release branch (`dev` → `main` if that is the
        release branch) and confirm CI is green on the exact commit to be tagged.
31. [ ] Re-check the version triple matches before tagging: `pyproject.toml:3` says
        `2.0.0`, `uv.lock` root entry says `2.0.0`, `CHANGELOG.md` top released
        section is `## [2.0.0]`. Research 001 §6: "Version tag `v2.0.0` must match
        `version = "2.0.0"` in pyproject.toml".
32. [ ] Create the **annotated** tag (research 001 §6, research 002 §5 — annotated,
        not lightweight, so tagger/date/message are stored):
    ```bash
    git tag -a v2.0.0 -m "Release 2.0.0 — first stable release"
    git push origin v2.0.0
    ```
    With Phase 0's `tag.gpgSign true` this tag is also signed; confirm with
    `git tag -v v2.0.0`.
33. [ ] Watch the `Release` workflow: `test` → `build` → `publish`. If `publish` fails
        OIDC exchange, the cause is almost always a mismatch between the PyPI
        registration (owner/repo/workflow filename/environment) and the workflow —
        fix the PyPI side and re-run the failed job; **do not** re-cut the tag.
34. [ ] Create the GitHub Release from the tag (research 001 §6 §Create GitHub Release):
    ```bash
    gh release create v2.0.0 --title "v2.0.0 — first stable release" \
      --notes-file <(sed -n '/^## \[2.0.0\]/,/^## \[0.1.7\]/p' CHANGELOG.md)
    ```
    or paste the `[2.0.0]` changelog section into the web UI. Mark it "Latest release".
35. [ ] Post-release sanity: `pip install providify==2.0.0` in a clean venv (or
        `uv run --with providify==2.0.0 --no-project python -c "import providify"`).

**Verify:** https://pypi.org/project/providify/ shows 2.0.0, the "Development Status ::
5 - Production/Stable" classifier, and the five project links from Phase 2; the release
page shows an attestation/provenance section (PEP 740 default, research 002 §6);
`git show v2.0.0` displays the tagger and message (proves it is annotated).

### Phase 8b — GitHub hardening: rulesets (ONLY after Phase 7 has shipped)

🧑 **Browser only.** **Do not start this until `v2.0.0` is pushed and PyPI shows 2.0.0.**
Three independent reasons, each a real failure mode:

1. **Required status checks (row 3) are not selectable until the check has reported.**
   The ruleset picker lists only check-run names GitHub has already observed on this
   repo. Before `ci.yml` (Phase 5a) has run, `test (3.12)` / `test (3.13)` are simply
   absent from the list. Note the names you need are the **matrix job** names, not the
   workflow name `CI`.
2. **"Require signed commits" (row 8) rejects the push, not the commit.** Phase 0 makes
   this safe — but only for commits created *after* Phase 0. Enabling the rule after
   everything is pushed removes the question entirely.
3. **The `v*` tag ruleset (rows 10–14) binds you too.** Ruleset rules apply to repository
   admins unless admins are explicitly added as bypass actors (003 §3). Creating the tag
   ruleset after pushing `v2.0.0` sidesteps the whole problem; it then protects v2.0.1
   onward, which is all it was ever for. Be aware it also disables this plan's own
   recovery path ("delete and re-cut the tag", Edge cases) — after 8b.4 that route runs
   through the bypass actor or a temporarily disabled ruleset.

8b.1 [ ] **Branch ruleset** (rows 1–6). Settings → Rules → Rulesets → New ruleset →
      **New branch ruleset**. Name `default-branch-protection`; Target: **`main` only**
      — ⚠️ do *not* also target `dev`; a ruleset on `dev` turns day-to-day solo work into
      PR ceremony for zero benefit. Enforcement: **Active**. Rules:
      - Require a pull request before merging — **0 required approvals** (see 8b.2)
      - Require status checks to pass — select `test (3.12)` and `test (3.13)`
      - Require linear history
      - Block force pushes
      - Restrict deletions

8b.2 [ ] **Bypass actors** (row 7): add **Repository admin → Always allow**.
      **Say it plainly, and do not let this line get sanded off:** with admin bypass on,
      and you as the only admin, this ruleset enforces **nothing** against you. What it
      actually buys is (a) an audit trail, (b) the config that OpenSSF Scorecard's
      `Branch-Protection` and `Code-Review` checks read, and (c) muscle memory for the
      day a second contributor arrives (003 §1, §Librarian's Note). Keep it — but never
      describe the repo as "protected" in a security sense on that basis.
      The alternative — no bypass — means the day a GitHub incident wedges a status check
      you cannot land the fix. For one maintainer, **bypass-on is the correct call**.
      Equally: **required approvals stay at 0.** GitHub lets you approve your own PR
      (003 Evidence Gap 1: the rule checks that a review exists, not who wrote it), so
      setting it to 1 buys a self-approval click, not a review.

8b.3 [ ] (row 29, NICE) "Require conversation resolution" — **skip**. Zero value with
      zero reviewers; it only creates a merge blocker you must clear yourself.

8b.4 [ ] **Tag ruleset** (rows 10–14). Settings → Rules → Rulesets → New ruleset →
      **New tag ruleset**. Name `release-tags`; Target `v*`; Enforcement Active. Rules:
      Restrict creations, Restrict updates, Restrict deletions. **Bypass actors:
      Repository admin → Always allow** (row 14) — without this you cannot cut v2.0.1
      at all, since you are the one who pushes tags.

8b.5 [ ] (row 8) **Now** add "Require signed commits" to the branch ruleset from 8b.1.
      Before enabling it, push one signed commit to `main` and confirm github.com shows
      **Verified**. If it shows "Unverified", the Phase 0 key was registered as an
      authentication key only (P0.2) — fix that first, or this rule locks you out of
      your own default branch.

8b.6 [ ] (row 35, optional, **file edit not a click**) If Phase 5d's Scorecard workflow
      is in place and has produced a result, add the badge to the Phase 6 block in
      `README.md` and commit it normally:
      ```markdown
      [![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/edoardoscarpaci/providify/badge)](https://securityscorecards.dev/viewer?uri=github.com/edoardoscarpaci/providify)
      ```
      ⚠️ The owner segment must match the real GitHub login (see Risks). Expect **8–9/10**,
      not 10 — the `Contributors` check penalises single-maintainer projects by design
      (003 §8) and is not worth chasing.

**Verify:** Settings → Rules → Rulesets lists two **Active** rulesets. Open a throwaway
PR against `main`: the merge button must stay blocked until both `test` checks report.
`git push --force origin main` must be rejected (unless you deliberately bypass).
`git push origin refs/tags/test-tag` (a non-`v*` tag) succeeds; a `v*` tag push is
gated by the ruleset and only lands via the admin bypass. Scorecard's `Branch-Protection`
check moves off zero on the next weekly run.

---

## Appendix A — GitHub repository hardening checklist (all 35 settings)

Lifted from `design/v1-0-0-release/research/003-github-repo-hardening-settings.md`
§Checklist, reordered so you can work **page by page** through the GitHub settings UI
without bouncing around, and annotated with the phase that owns each row.

Priority tiers (unchanged from 003): **MUST** = required for production OSS credibility
(OSPS baseline / Scorecard pass) · **SHOULD** = strongly recommended, low friction ·
**NICE** = optional, skip if time-constrained. Where this plan *disagrees* with the
brief's priority for a solo repo, the disagreement is stated in the Value column.

| # | Setting | Where (UI path / command) | Value to set | Prio | Free? | Phase |
|---|---|---|---|---|---|---|
| **— Local machine —** |
| 9 | Configure SSH commit signing | `git config --global gpg.format ssh` · `user.signingKey ~/.ssh/id_ed25519.pub` · `commit.gpgSign true` · `tag.gpgSign true` | SSH (ED25519) signing for all commits **and tags** | SHOULD | Yes | **P0.1** |
| 9b | Register the key as a **Signing Key** on GitHub | https://github.com/settings/keys → New SSH key → Key type: **Signing Key** | Same public key as the auth key | SHOULD | Yes | **P0.2** ⚠️ not in 003 |
| **— Settings → General → Features —** |
| 30 | Issues | Settings → General → Features | Enabled | MUST | Yes | 8a.1 |
| 32 | Wiki | Settings → General → Features | **Disabled** — README + `docs/` instead | NICE | Yes | 8a.1 |
| 31 | Discussions | Settings → General → Features | **Off** for now; enable when there is demand | NICE | Yes | 8a.1 |
| **— Settings → General → Pull Requests —** |
| 28 | Merge strategies | Settings → General → Pull Requests | **Disable "Allow merge commits"**; keep squash | SHOULD | Yes | 8a.2 |
| 27 | Auto-delete head branches | Settings → General → Pull Requests | Enabled | NICE | Yes | 8a.2 |
| **— Repo home → About —** |
| 33 | Description, website, topics | Repo page → About → ⚙ | Description from `pyproject.toml:4`; website = PyPI page; topics: python, dependency-injection, di, ioc, async, library | NICE | Yes | 8a.3 |
| **— Settings → Actions → General —** |
| 15 | Default `GITHUB_TOKEN` permissions | Settings → Actions → General → Workflow permissions | "Read repository contents and packages permissions" (**verify only** — default since Feb 2023) | MUST | Yes | 8a.4 |
| 16 | Actions permissions policy | Settings → Actions → General → Actions permissions | "Allow all actions and reusable workflows"; the verified-creator filter is **not on the free tier** | SHOULD | Yes | 8a.4 |
| 17 | Fork PR workflow approval | Settings → Actions → General → Fork pull request workflows | "Require approval for first-time contributors who are new to GitHub" | SHOULD | Yes | 8a.4 |
| **— Settings → Environments —** |
| 18 | Create `release-pypi` environment | Settings → Environments → New environment | Name `release-pypi`; URL `https://pypi.org/project/providify/` | MUST | Yes | **done in P5c step 25** |
| 19 | Restrict deployments to release tags | Settings → Environments → `release-pypi` → Deployment branches and tags | "Allow specific branches and tags" → `ref:refs/tags/v*` | MUST | Yes | **done in P5c step 25** |
| 19b | Required reviewers on the environment | Settings → Environments → `release-pypi` → Required reviewers | **Leave OFF** — solo maintainer approving their own deploy is pure friction (003 §5) | — | Yes | **P5c step 25 (non-action)** |
| **— Settings → Code security —** |
| 20 | Dependabot alerts | Settings → Code security → Dependabot alerts | Enabled | MUST | Yes | 8a.5 |
| 21 | Dependabot security updates | Settings → Code security → Dependabot security updates | Enabled | MUST | Yes | 8a.5 |
| 23 | Secret scanning | Settings → Code security → Secret scanning | Enabled | MUST | Yes (public) | 8a.5 |
| 24 | Secret scanning **push protection** | Settings → Code security → Secret scanning → Push protection | Enabled | MUST | Yes (public) | 8a.5 |
| 25 | CodeQL code scanning | Settings → Code security → Code scanning → Set up → **Default setup** | Enabled, language auto-detect | SHOULD | Yes (public) | 8a.5 |
| 26 | Private vulnerability reporting | Settings → Code security → Private vulnerability reporting | Enabled — backs the channel promised in `SECURITY.md` | SHOULD | Yes (public) | **done in P5c step 26** |
| — | Dependency graph | (none) | Automatic on public repos — no action | — | Yes | — |
| **— Files you commit —** |
| 22 | `.github/dependabot.yml` | File in repo | `github-actions` + `pip`, weekly; **no `reviewers:` key** | SHOULD | Yes | **P5d.1** |
| 34 | `.github/workflows/scorecard.yml` | File in repo | Weekly cron + push to `main`; `publish_results: true` | SHOULD | Yes | **P5d.2** |
| **— Settings → Rules → Rulesets (branch) — AFTER Phase 7 —** |
| 1 | Create branch ruleset | Settings → Rules → Rulesets → New branch ruleset | Name `default-branch-protection`; Target **`main` only**; Enforcement Active | MUST | Yes | 8b.1 |
| 2 | Require PR before merging | [ruleset] → Add rule | Enabled, **0 required approvals** (self-approval is not review) | MUST | Yes | 8b.1 |
| 3 | Require status checks | [ruleset] → Add rule | `test (3.12)`, `test (3.13)` — **only selectable after `ci.yml` has run (P5a)** | MUST | Yes | 8b.1 |
| 4 | Require linear history | [ruleset] → Add rule | Enabled (pairs with row 28) | MUST | Yes | 8b.1 |
| 5 | Block force pushes | [ruleset] → Add rule | Enabled (ruleset default) | MUST | Yes | 8b.1 |
| 6 | Restrict deletions | [ruleset] → Add rule | Enabled (ruleset default) | MUST | Yes | 8b.1 |
| 7 | Bypass actors | [ruleset] → Bypass actors | Repository admin → **Always allow** — see the honesty note in 8b.2 | MUST | Yes | 8b.2 |
| 29 | Require conversation resolution | [ruleset] → Add rule | **Skip** — no value with zero reviewers | NICE | Yes | 8b.3 (skip) |
| 8 | Require signed commits | [ruleset] → Add rule | Enabled **last**, after a Verified commit is confirmed | SHOULD | Yes | 8b.5 |
| **— Settings → Rules → Rulesets (tag) — AFTER the `v2.0.0` tag is pushed —** |
| 10 | Create tag ruleset | Settings → Rules → Rulesets → New **tag** ruleset | Name `release-tags`; Target `v*`; Enforcement Active | MUST | Yes | 8b.4 |
| 11 | Restrict tag creations | [tag ruleset] → Add rule | Enabled | SHOULD | Yes | 8b.4 |
| 12 | Restrict tag updates | [tag ruleset] → Add rule | Enabled (immutable releases) | SHOULD | Yes | 8b.4 |
| 13 | Restrict tag deletions | [tag ruleset] → Add rule | Enabled | SHOULD | Yes | 8b.4 |
| 14 | Tag ruleset bypass actors | [tag ruleset] → Bypass actors | Repository admin → **Always allow** — mandatory, or you cannot cut v2.0.1 | SHOULD | Yes | 8b.4 |
| **— README —** |
| 35 | OpenSSF Scorecard badge | `README.md` badge block (Phase 6) | Add only after Scorecard has produced a result; expect 8–9/10 | NICE | Yes | 8b.6 / P6 |

**Not free / not used** (003 §4, §5): environment **wait timers** and the Actions
**verified-creator allowlist filter** are Team/Enterprise features. They are deliberately
absent from this table's action rows — do not go hunting for them in the UI.

---

## Edge cases

- **`uv sync --locked` fails in CI right after the version bump** → `uv.lock:52` still
  says `1.1.1`; Phase 2 step 8 (`uv lock`) was skipped. Regenerate and commit.
- **`ruff format --check .` fails on the pre-existing tree** → the repo has never been
  format-gated. Either run `make format` and commit as a standalone "style: apply ruff
  format" commit, or delete that step from `ci.yml`. Do not mix formatting churn into
  the release commits.
- **Tag pushed before `release.yml` was committed** → no workflow runs at all (silent).
  Fix: commit the workflow, delete and re-cut the tag (`git tag -d v2.0.0 &&
  git push --delete origin v2.0.0`, then re-tag) — safe as long as nothing was published.
- **Tag pushed before PyPI trusted-publisher registration** → `test`/`build` pass, the
  `publish` job fails at OIDC token exchange. Fix on the PyPI side, then re-run the
  failed job from the Actions UI. No re-tag needed.
- **`publish` job hangs "waiting for approval"** → the `release-pypi` environment has a
  required-reviewer protection rule. Approve it, or remove the reviewer requirement and
  keep only the tag-pattern rule. (Appendix A row 19b says leave it off for this reason.)
- **PyPI rejects the upload as a duplicate** → 2.0.0 was already published; PyPI files
  are immutable (research 001 §1). Bump to 2.0.1 and re-run the whole tag flow.
- **Existing `v1.1.1` git tag found in step 10** → use it in the `[2.0.0]` compare link
  instead of `v0.1.7`.
- **CI badge shows "no status"** → the workflow has not yet run on the default branch;
  it resolves itself after the first push to `main`.
- **Commits show "Unverified" on GitHub despite `commit.gpgSign true`** → the public key
  was registered only as an Authentication key. Add it again with Key type **Signing Key**
  (P0.2). Nothing needs re-committing; GitHub re-evaluates existing signatures.
- **Push to `main` rejected with "commits must have verified signatures"** → either row 8
  was enabled while unsigned local commits were pending, or P0.2 was skipped. Fix P0.2
  first; if the commits genuinely are unsigned, re-sign with
  `git rebase --exec 'git commit --amend --no-edit -S' <base>` and push — which requires
  the admin bypass, since "Block force pushes" is on.
- **`test (3.12)` does not appear in the required-status-checks picker** → `ci.yml` has
  never reported on this repo, or you are searching for the workflow name `CI` instead of
  the job name. Push once so the check runs, then reopen the ruleset editor.
- **`git push origin v2.0.1` rejected: "tag creation is restricted"** → the tag ruleset
  from 8b.4 is Active without an admin bypass actor (row 14). Add the bypass, or set the
  ruleset to Disabled for the duration of the release and re-enable it after.
- **Cannot delete a mis-cut tag** (the recovery path in the third edge case above) →
  "Restrict deletions" on the tag ruleset. Same fix: bypass, or temporarily disable.
- **Dependabot PR fails CI immediately** → Dependabot bumps `pyproject.toml` but does not
  regenerate `uv.lock`, so `uv sync --locked` fails. Check out the branch, run `uv lock`,
  push. This will be the normal shape of every Dependabot Python PR in this repo.
- **CodeQL default setup reports "no languages detected"** → auto-detection found nothing;
  set the language to Python explicitly in the default-setup configuration dialog.
- **Scorecard's `Branch-Protection` check reports "unknown" / scores 0 after 8b** → the
  check may need a token with elevated read on repository settings, which `GITHUB_TOKEN`
  does not grant. This is a token-scope artefact, not a missing setting. Do not mint a PAT
  to chase a badge digit.

## Verification

Per phase, in order. Every command runs from the repo root.

```bash
# Phase 0 (signing)
git config --get gpg.format          # ssh
git config --get commit.gpgsign      # true
git config --get tag.gpgsign         # true

# Phase 1 (R1)
git status --porcelain            # must be empty afterwards
uv run pytest
uv run ruff check .
git log --show-signature -3       # every release commit signed

# Phase 2 (R5, R6)
uv sync --locked                  # proves uv.lock was regenerated
uv build                          # dist/providify-2.0.0*
grep -n 'version = "2.0.0"' pyproject.toml
grep -n "5 - Production/Stable" pyproject.toml

# Phase 3 (R4)
grep -n "^## \[" CHANGELOG.md     # [Unreleased], [2.0.0], [0.1.7] ...
! grep -q "v0.2.0" CHANGELOG.md

# Phase 4 (R7)
ls CONTRIBUTING.md SECURITY.md
grep -n "Versioning and deprecation policy" CONTRIBUTING.md

# Phase 5a/5b (R2, R3)
ls .github/workflows/ci.yml .github/workflows/release.yml
uv sync --locked --all-extras && uv run ruff check . && uv run pytest   # same as CI
# then: green "CI / test (3.12)" and "CI / test (3.13)" checks on GitHub

# Phase 5d (hardening files)
ls .github/dependabot.yml .github/workflows/scorecard.yml

# Phase 6 (R9)
grep -n "badge.svg" README.md

# Phase 7 (R8)
git show v2.0.0 | head -5         # must show "tag v2.0.0" + Tagger (annotated)
git tag -v v2.0.0                 # signed tag (Phase 0)
# then: green Release workflow, PyPI shows 2.0.0
```

Full-suite gate before tagging: `make test && make lint && make build`
(`Makefile:6–21`).

**Phase 8a / 8b are UI-verified, not command-verified** (003 Evidence Gap 5: `gh` has no
coverage for most of these toggles). Walk Appendix A top to bottom and confirm each row's
state on its settings page. The two behavioural smoke tests worth actually running:

1. On a scratch branch, commit a fake AWS-key-shaped string and push — **push protection
   must refuse it** (row 24).
2. Open a throwaway PR against `main` — **merge must stay blocked until both `test`
   checks report** (row 3), and `git push --force origin main` must be rejected (row 5).

## Risks

- ⚠️ **ASSUMPTION — action versions.** Research 002 §1 pins `actions/checkout@v7`,
  `astral-sh/setup-uv@v10.0.1`, `pypa/gh-action-pypi-publish@release/v1`, and the
  brief's own §Evidence Gaps 1–2 admit it could not confirm whether newer releases
  exist as of mid-2026. Before committing the workflows, check the releases pages and
  bump if a newer stable tag exists. **Invariant:** never use `@master`,
  `@unstable/v1`, or `@latest` (research 002 §6 — `setup-uv` publishes no floating
  major tags; `gh-action-pypi-publish@master` is sunset).
- ⚠️ **ASSUMPTION — `setup-uv` SHA.** Research 002 §2 quotes commit SHA
  `c771a70e6277c0a99b617c7a806ffedaca235ff9` for v10.0.1. That SHA is not
  independently verified here, so the plan pins the **tag** `@v10.0.1`. Swap to a SHA
  only after verifying it on the releases page.
- ⚠️ **ASSUMPTION — GitHub owner login is `edoardoscarpaci`**, derived from
  `pyproject.toml:35` and the changelog compare links (`CHANGELOG.md:619–625`).
  Research 002 §3 guessed `edoardo-scarpaci`. The PyPI trusted-publisher Owner field
  must match the real login **exactly** or OIDC fails. Confirm at registration time.
- ⚠️ **ASSUMPTION — security contact.** `SECURITY.md` uses
  `edoardo.scarpaci@gmail.com` (`pyproject.toml:6`). Confirm this is the address the
  maintainer wants publicly listed; GitHub private vulnerability reporting is the
  primary channel precisely so the address is a fallback.
- ⚠️ **ASSUMPTION — deprecation window.** "Two minor releases or six months" is a
  judgement call; research 001 §Evidence Gaps 1 confirms Python has no universal
  standard. Once written it is a public promise — adjust before publishing, not after.
- ⚠️ **ASSUMPTION — release branch.** `ci.yml` triggers on `[main, dev]`; current
  branch is `dev`, main branch is `main`. Adjust the branch list if the actual release
  branch differs.
- **Node 20 runtime deprecation** (research 002 §6): Node 20 removal is dated
  September 2026. Any action version predating that transition may fail with cryptic
  warnings. **Invariant:** if a workflow step fails with a Node-runtime error, bump
  that action to its newest major before debugging anything else.
- **Tagging an inconsistent state.** `pyproject.toml:3`, the `uv.lock` root entry, the
  top `## [2.0.0]` changelog heading, and the tag name must agree. Step 31 is the gate;
  skipping it is the single most likely way to ship a wrong release.
- **PyPI immutability.** 2.0.0 can be published exactly once (research 001 §1). Run
  `uv build` and inspect `dist/` before pushing the tag; there is no undo.
- **CI deviates from research 001 §5** by omitting a type-check gate. Accepted and
  documented in Non-goals and in `CONTRIBUTING.md` so it reads as a decision, not an
  oversight.

### Risks added by the GitHub hardening work (Phases 0, 5d, 8a, 8b)

- ⚠️ **ASSUMPTION — the repository is public.** Every "Yes" in Appendix A's Free? column
  assumes a public repo (003 §5, §6D, §6E): environments, secret scanning **and** push
  protection, CodeQL and private vulnerability reporting are free **only** for public
  repositories; private repos need Pro/Team or GitHub Advanced Security. **Invariant:**
  if this repo is ever made private, roughly half of Phase 8a silently stops working —
  re-audit before flipping visibility.
- ⚠️ **NOT FREE — deliberately excluded.** Environment **wait timers** (003 §5) and the
  Actions **"verified creators" allowlist filter** (003 §4) are Team/Enterprise only.
  Neither is used. Do not spend time looking for them in the UI and concluding the plan
  is wrong.
- ⚠️ **ASSUMPTION — the signing-key registration step (P0.2) is not in 003.** Brief §2
  gives the four `git config` lines but never states that the public key must *also* be
  registered on GitHub with key type **Signing Key**, without which commits sign locally
  yet render "Unverified". P0.2 comes from GitHub's own docs, not from the brief; verify
  the "Key type" selector exists as described when you get there. **Invariant:** confirm
  one commit shows **Verified** before enabling ruleset row 8 (step 8b.5).
- ⚠️ **ASSUMPTION — the Scorecard workflow template in 003 §8 is stale/incomplete.** It
  pins `ossf/scorecard-action@v2.3.0` and `github/codeql-action/upload-sarif@v2`, and
  omits `id-token: write` even though `publish_results: true` requires it. Phase 5d.2
  uses `@v2.4.0`, `upload-sarif@v3`, adds `id-token: write` and `persist-credentials: false`
  — **all four changes are unverified** against current releases pages. Check before
  committing, exactly as for the Phase 5a/5b pins.
- ⚠️ **ASSUMPTION — Scorecard's `Branch-Protection` check may need a PAT.** It reads
  ruleset/branch-protection configuration, which the default `GITHUB_TOKEN` may not
  expose. Unverified. If the check reports "unknown" after Phase 8b, that is a token-scope
  artefact, not a missing setting — and not worth minting a long-lived PAT to fix.
- ⚠️ **ASSUMPTION — Dependabot `pip` coverage of this project.** `pyproject.toml` declares
  **zero runtime dependencies** (only the `yaml` extra, lines 37–38, and a PEP 735
  `[dependency-groups]` dev block, lines 43–50) and the project locks with `uv.lock`.
  Whether Dependabot's `pip` ecosystem parses `[dependency-groups]` or `uv.lock` at all is
  **unverified** — 003 §6C does not say. Expect the `github-actions` entry to do all the
  real work. **Invariant:** any Dependabot PR that edits `pyproject.toml` without touching
  `uv.lock` will fail CI's `uv sync --locked`; re-lock on the branch before merging.
- ⚠️ **ASSUMPTION — ruleset rule labels and UI paths.** 003 Evidence Gap 4 notes GitHub
  has published no EOL for classic branch protection and the ruleset UI is still moving
  (auto-migration shipped Aug 2026). Exact rule names may differ from the table. Match by
  intent, not by string.
- ⚠️ **ASSUMPTION — self-approval semantics.** 003 Evidence Gap 1: GitHub's docs do not
  state whether a maintainer approving their own PR satisfies "required approvals". This
  plan sidesteps it by requiring **0** approvals. Do not raise it to 1 expecting it to
  work — verify on a throwaway PR first if you ever change it.
- ⚠️ **ASSUMPTION — `gh` CLI cannot script this.** 003 Evidence Gap 5: there is no `gh`
  command for the majority of these toggles, which is why Phases 8a/8b are written as UI
  paths. If a future `gh` release covers them, great — but do not lose an afternoon to
  the REST API for a one-time checklist.
- **Bypass actors defeat the rules they guard.** Stated in 8b.2 and repeated here because
  it is the single most misread line in this plan: a solo repo with `Repository admin →
  Always allow` is *documented*, not *enforced*. That is the right trade for one
  maintainer, but say so honestly rather than claiming protection the setup does not
  provide. **Invariant:** never cite the branch ruleset as a security control in
  `SECURITY.md` or release notes.
- **Lockout by tag ruleset.** If Appendix A rows 10–14 are enabled before Phase 7, or
  without a bypass actor, `git push origin v2.0.0` is rejected and the release stalls
  mid-flight. **Invariant:** the tag ruleset comes *after* the tag, and row 14 (admin
  bypass) is set in the same sitting as rows 10–13 — never rows 10–13 alone.
- **Signed-commit rule vs. unsigned history.** Row 8 applies to newly pushed commits only;
  existing unsigned history in `main` is not retroactively rejected. **Invariant:** never
  enable row 8 while unsigned commits are still unpushed locally.
- **`main`-only ruleset targeting.** Targeting `dev` as well as `main` converts every
  solo working commit into a PR. **Invariant:** the branch ruleset targets the default
  branch only; `dev` stays unrestricted.
