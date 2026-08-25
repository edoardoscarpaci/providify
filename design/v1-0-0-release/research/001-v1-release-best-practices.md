# Research 001 — Python OSS v1.0.0 Release Best Practices

Date: 2026-08-25 · Freshness matters: **yes** (PyPI packaging standards evolving, GitHub Actions trusted publishing 2025+, OSPS baseline updated 2025)

## Question

What are the current (2025/2026) best practices for taking a Python OSS library from alpha to a credible v1.0.0 stable release? Specifically: semantic versioning conventions, PyPI metadata/classifiers, changelog format, governance files, CI/CD automation, and git tagging.

## Findings

### 1. Semantic Versioning & API Stability Commitment
- **v1.0.0 signals "stable" API**: Major version bump (1.x.x) indicates commitment to minimize breaking changes in subsequent releases. Follow [PEP 440](https://peps.python.org/pep-0440/) and [Semantic Versioning 2.0.0](https://semver.org/); major releases break compatibility, minor add backward-compatible features, patch fixes bugs — [Python Packages: Versioning](https://py-pkgs.org/07-releasing-versioning.html) (2024/2025)
- **Deprecation policy required**: Document how many release cycles (e.g., 6 months, 2 minor versions) deprecated APIs remain available before removal — [PyOpenSci Python Package Guide](https://www.pyopensci.org/python-package-guide/package-structure-code/python-package-versions.html)
- **Immutable release**: Once v1.0.0 is uploaded to PyPI, it cannot be re-uploaded; name reservation is permanent

### 2. PyPI Metadata & Classifiers
- **Development Status classifier**: Use `"Development Status :: 5 - Production/Stable"` for v1.0.0 — [PyPI Classifiers](https://pypi.org/classifiers/) (canonical list via trove-classifiers)
- **Essential metadata fields** in `pyproject.toml`:
  - `description`: One-line headline (shown on PyPI)
  - `readme`, `license`, `authors`, `maintainers`, `requires-python` (e.g., `">=3.8"`)
  - `[project.urls]`: Repository, documentation, bug tracker links
  - `keywords`: Improve discoverability
  — [Python Packaging Guide: Writing pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) (2025)

### 3. Changelog Format (Keep a Changelog)
- **Standard format**: Header `## [1.0.0] - 2026-08-25` with ISO 8601 date; organize changes by type (Added, Changed, Deprecated, Removed, Fixed, Security) — [Keep a Changelog 1.0.0](https://keepachangelog.com/en/1.0.0/)
- **v1.0.0 entry best practice**: List all breaking changes from pre-1.0 versions under `Removed`; summarize major features under `Added`; link to GitHub Release for full details

### 4. Governance Files (OSPS 2025 Baseline)
- **CONTRIBUTING.md**: Explain contribution process, coding standards, testing requirements — essential, not optional — [OpenSSF Project Security Baseline 2025-10-10](https://baseline.openssf.org/versions/2025-10-10.html)
- **SECURITY.md**: Mandatory for production packages; include security contact(s), coordinated disclosure policy with response timeframe, private reporting mechanism
- **CODE_OF_CONDUCT.md**: Recommended (not table-stakes for solo maintainer, but increasingly expected); links to Contributor Covenant widely adopted — [OSPS Baseline 2025](https://baseline.openssf.org/versions/2025-10-10.html)
- **Collectively**: Governance files demonstrate maturity and clarify expectations; Python community consensus treats CONTRIBUTING + SECURITY as table-stakes for v1.0.0

### 5. CI/CD: Automated Testing & Publishing
- **Test matrix**: GitHub Actions with Python versions matching `requires-python` range; run linting (ruff, pylint), type-checking (mypy), tests (pytest) on all pull requests — [Python Packaging Guide: GitHub Actions CI/CD](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/) (2025)
- **Trusted Publishing (OIDC) is current standard**: Replaces API tokens; configure "pending publishers" at PyPI, use `pypa/gh-action-pypi-publish@release/v1` with `id-token: write` permission — [Python Packaging Guide: Trusted Publishing](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/); [PyDeployer: Trusted Publishing Guide](https://www.pydeployer.com/guides/from-code-to-pypi-automate-python-package-publishing-with-github-actions/)
- **Workflow pattern**: Build distributions on every push (separate job); publish only on git tags using artifacts from build job; PublishTestPyPI on all releases for validation

### 6. Git Tagging Conventions
- **Use annotated tags** with semver format: `git tag -a v1.0.0 -m "Release 1.0.0 stable"` — stores tagger, date, message; advantages over lightweight tags — [Recommended Git Tag Naming Conventions](https://zenn.dev/tonbi_attack/articles/8ec1fe75dce874?locale=en)
- **Match pyproject.toml version exactly**: Version tag `v1.0.0` must match `version = "1.0.0"` in pyproject.toml; use setuptools-scm to derive version from git tags automatically
- **Pre-release tags** follow semver: `v1.0.0-rc.1`, `v1.0.0-beta.1` for release candidates
- **Create GitHub Release** from each tag with changelog entry as body; enables release notes on PyPI/GitHub — standard workflow via `actions/create-release` or GitHub CLI

## Options Compared

| Aspect | Standard Practice | Notes |
|--------|-------------------|-------|
| **Versioning** | Semantic Versioning (PEP 440) | MAJOR.MINOR.PATCH; v1.0.0 = "API stable" |
| **Deprecation Policy** | Documented (N release cycles) | Critical for post-v1.0 user trust |
| **PyPI Classifier** | "5 - Production/Stable" | Signals production-ready to PyPI searchers |
| **Changelog** | Keep a Changelog (Markdown) | Six change types; ISO 8601 dates |
| **CONTRIBUTING.md** | Required | Explains contribution workflow |
| **SECURITY.md** | Required | Coordinated disclosure policy + contact |
| **CODE_OF_CONDUCT** | Recommended | Contributor Covenant standard |
| **CI/CD Gating** | Test matrix + linting + type-check | All on PRs; publish on tags only |
| **PyPI Publishing** | Trusted Publishing (OIDC) | Replaces API tokens (security improvement) |
| **Git Tags** | Annotated, semver, match version | `v1.0.0` + GitHub Release |

## Version/Compatibility Notes

- **PEP 440** (Python versioning): Current standard; allows pre-release, post-release, dev versions — [PEP 440](https://peps.python.org/pep-0440/) (2021 stable, no breaking changes planned)
- **Semantic Versioning 2.0.0**: Widely adopted; `v1.0.0-rc.1`, `v1.0.0-alpha.1` supported — [semver.org](https://semver.org/)
- **Trove Classifiers**: Stable list maintained on PyPI; "5 - Production/Stable" well-established
- **Keep a Changelog 1.0.0**: Stable format; no breaking changes expected
- **GitHub Actions trusted publishing**: Launched 2021, matured by 2024–2025; now recommended over API tokens
- **OSPS Baseline 2025-10-10**: Current authoritative OSS governance framework; evolving annually

## Evidence Gaps

1. **Exact deprecation timeline expectations**: Python community has no universal standard (unlike Java's 2-3 major-version rule). Depends on library maturity and user base; major projects (Django, FastAPI) document explicitly but patterns vary.
2. **CODE_OF_CONDUCT adoption rates**: OSPS marks as "recommended" not "required," and enforcement/value for small maintainers underdocumented.
3. **Post-v1.0.0 SemVer compliance tracking**: No centralized audit; unclear what % of Python packages violate SemVer after declaring v1.0.0.

## Librarian's Note

**What the sources indicate:**

A credible Python v1.0.0 release requires:
- ✅ Semantic versioning + documented deprecation policy
- ✅ PyPI metadata (requires-python, description, license, authors, urls) + "5 - Production/Stable" classifier
- ✅ Keep a Changelog format with v1.0.0 entry summarizing breaking changes
- ✅ CONTRIBUTING.md + SECURITY.md (table-stakes per OSPS 2025 baseline)
- ✅ CI/CD test matrix + linting/type-check gates + trusted publishing via OIDC
- ✅ Annotated git tags matching version, GitHub Releases linked

**For providify**: Current repo has CHANGELOG.md and governance files in flight (per git status). Migrate to Keep a Changelog format, enable trusted publishing, document deprecation policy for post-1.0.0, and enforce test matrix on all Python versions in `requires-python` range.
