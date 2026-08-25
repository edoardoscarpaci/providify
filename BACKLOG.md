# Providify v2.0.0 Stable Release Backlog

Compiled 2026-08-25 via `/discover`. Scope: take providify from ALPHA to its
first official stable release. This backlog **supersedes the 2026-08-24
feature backlog** (F1–F9 below) for planning purposes — that backlog's scope
was feature parity/differentiation and is now effectively resolved (F1
startup validation, F2–F6, F9 confirmed shipped by scout; F7 multibinding and
F8 field interceptors are implemented and tested but sitting uncommitted).
This pass is deliberately **packaging/infrastructure only**: no new features,
no breaking API changes — locked with the user during the interview below.

Key decision from interview: the package is already published at v1.1.1
(under an Alpha classifier). Since PyPI won't let a lower version supersede a
higher one, the stable release ships as **v2.0.0** rather than v1.0.0 — same
"first stable, API now committed" milestone, monotonic version history.

Sources:
- Scout report (2026-08-25): confirmed current state — v1.1.1 in pyproject.toml,
  classifier still "3 - Alpha", CHANGELOG.md has a stale "[Unreleased] — v0.2.0"
  section mismatched against the shipped version, no `.github/workflows/`, no
  CONTRIBUTING/SECURITY/CODE_OF_CONDUCT, F7/F8 code+tests present but uncommitted
  (`git status`), no README badges.
- Research brief 001: `design/v1-0-0-release/research/001-v1-release-best-practices.md`
  — Python OSS v1.0-equivalent release conventions: semver + deprecation policy,
  PyPI "5 - Production/Stable" classifier, Keep a Changelog format, CONTRIBUTING.md
  + SECURITY.md as OpenSSF-2025-baseline non-optional, CI test matrix + lint/type
  gating, GitHub Actions trusted publishing (OIDC) over API tokens, annotated git
  tags linked to GitHub Releases.
- Research (reused from prior session): `design/di-features-taxonomy/research/`
  001–003 — confirms feature surface is at/above parity with comparable Python DI
  libraries (dishka, python-dependency-injector, wireup, svcs, etc.); flagged
  health-check aggregation and OpenTelemetry instrumentation hooks as ecosystem-wide
  gaps/differentiators — considered for this release and explicitly parked (see
  below) to keep v2.0.0 additive-free.

## Backlog

| ID | Item | Severity | Complexity | Rationale | Evidence |
|----|------|----------|------------|-----------|----------|
| R1 | Commit F7 (multibinding) + F8 (field interceptors) work | 🔴 must | S | Code, tests, and design docs are already complete (`providify/decorator/multibinding.py`, `providify/field.py`, `tests/test_multibinding.py`, `tests/test_field_interceptor.py`) — just uncommitted. Nothing else in this backlog can be sequenced until the working tree is clean. | Scout: `git status` shows `??` on all F7/F8 files; plan 010 marked complete |
| R4 | Fix CHANGELOG.md — resolve `[Unreleased] — v0.2.0` vs shipped v1.1.1 mismatch, add `[2.0.0]` entry | 🔴 must | S | Current changelog structure would confuse anyone diffing versions; Keep a Changelog format expects each released version section to match what's actually tagged. | Scout: CHANGELOG.md:10 mismatch; research 001 §Changelog format |
| R5 | Bump version 1.1.1 → 2.0.0; upgrade classifier `3 - Alpha` → `5 - Production/Stable` | 🔴 must | S | Locked with user: monotonic version bump (can't re-publish under a lower number), classifier is the standard PyPI signal consumers filter on for "safe to depend on". | Scout: pyproject.toml:3,22; research 001 §PyPI classifiers |
| R7 | Add CONTRIBUTING.md + SECURITY.md | 🔴 must | S | Research treats these as non-optional per OpenSSF's 2025 baseline for a credible stable OSS release — not just nice-to-have. CODE_OF_CONDUCT.md explicitly parked (see below) — locked with user as skippable for a solo maintainer. | Research 001 §Governance/community files |
| R2 | GitHub Actions CI: test matrix (Python versions per `python_requires`) + lint/type-check gating | 🔴 must | M | No CI at all today — a stable release with zero automated verification on PRs/pushes is the single biggest credibility gap. Ruff is already configured locally, just not wired into CI. | Scout: no `.github/workflows/`; research 001 §CI/CD expectations |
| R8 | Cut annotated git tag `v2.0.0` + matching GitHub Release once R1/R4/R5/R7/R2 land | 🔴 must | S | Final step — deliberately sequenced last regardless of table position: tagging before the version/changelog/CI work lands would tag an inconsistent state. | Research 001 §Git tagging conventions |
| R3 | Automated PyPI publishing via GitHub Actions trusted publishing (OIDC), triggered on tag push | 🟡 should | S | Locked with user over API-token secrets — current recommended approach, no long-lived credential to rotate/leak. Requires a one-time manual step: link this repo/workflow in the PyPI project's trusted-publisher settings before the workflow can publish. | Research 001 §Trusted publishing (OIDC) |
| R6 | pyproject.toml metadata hygiene — keywords, project URLs (repo/docs/changelog links) | 🟡 should | S | Cheap polish that improves PyPI project-page discoverability and trust signals; bundled with R5 since both touch the same file. | Scout: metadata gaps noted; research 001 §PyPI metadata |
| R9 | README badges (CI status, PyPI version) | 🟢 nice | S | Purely cosmetic trust signal, no functional impact — lowest priority, do last if time allows. | Scout: README has no badges currently |

## Parked

- **CODE_OF_CONDUCT.md** — locked with user as skippable: research flags it as
  optional (not OpenSSF-baseline-required) for a solo-maintainer project.
  Revisit if the contributor base grows beyond the maintainer.
- **Health-check / readiness aggregation hook** (Kubernetes-style
  liveness/readiness) — locked with user to scope v2.0.0 as packaging-only,
  zero new features. Research (di-features-taxonomy briefs) flags this as a
  genuine gap across the whole Python DI ecosystem (dishka,
  python-dependency-injector, FastAPI DI all lack it) — good candidate for the
  *next* feature-focused backlog, not this stabilization pass.
- **OpenTelemetry instrumentation hooks** on instantiation/scope-transition
  events — same reasoning as above: real differentiator opportunity (F6
  observability hooks already exist and could feed this), explicitly deferred
  past v2.0.0 to keep this release additive-free and zero-breaking-change.
- **Known alpha-era API rough edges / breaking cleanup** — user explicitly
  chose zero breaking changes for this release even though a major version
  bump would have made it a natural window. No specific items were raised;
  revisit only if concrete pain surfaces post-release.

(Carried forward from the 2026-08-24 backlog: F1–F6 and F9 are confirmed
shipped by the 2026-08-25 scout pass. F7/F8 are addressed here as R1. No
feature work remains open from that backlog.)
