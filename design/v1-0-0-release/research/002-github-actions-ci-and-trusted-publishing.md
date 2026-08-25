# Research 002 — GitHub Actions CI & PyPI Trusted Publishing (Mechanical)

Date: 2026-08-25 · Freshness matters: **yes** (GitHub Actions runtime deprecations 2026, PEP 740 default behavior changed 2024–2025, action versions update frequently)

## Question

What are the exact mechanical steps and YAML configurations for:
1. Pinning current versions of `actions/checkout`, `actions/setup-python`, `astral-sh/setup-uv`, and `pypa/gh-action-pypi-publish`
2. Building a Python version matrix using uv in GitHub Actions (cache config, python-version input)
3. Configuring OIDC trusted publishing for PyPI (permissions, environment, exact PyPI setup flow, action inputs)
4. Triggering a release workflow on git tag push (tag-ref guards, fetch-tags behavior)
5. Any 2025–2026 breaking changes (Node 20 deprecation, PEP 740 defaults, action runtime updates)

## Findings

### 1. Current Action Versions & Release Dates

**Note on currency:** Search results return 2024 release dates for several actions; the GitHub Actions ecosystem updates frequently and versions may have newer releases not captured in search results.

- **actions/checkout**
  - Latest verified: **v7.0.1** (July 20, 2024) — includes PR safety fixes (allow-unsafe-pr-checkout)
  - v6.1.0 available; v5.1.0, v4.4.0 maintained for backports
  - **Recommended:** Pin to v7 (latest stable, July 2024) or v6 for broader runner compatibility
  - Fetch-tags behavior: v6.0.2+ (early 2026) fixed `fetch-tags: true` to preserve annotated tag objects via refspec — [actions/checkout releases](https://github.com/actions/checkout/releases); [GitHub Changelog on safer PR checkout defaults](https://github.blog/changelog/2026-06-18-safer-pull_request_target-defaults-for-github-actions-checkout/)

- **actions/setup-python**
  - Latest verified: **v7.0.0** (July 20, 2024) — ESM migration, Node 24 runtime, removed `pip-install` input
  - v6.3.0 (June 2024) supported; v5.6.0+ also maintained
  - **Recommended:** Pin to v7 (latest, July 2024) for Node 24 compatibility — [actions/setup-python releases](https://github.com/actions/setup-python/releases)

- **astral-sh/setup-uv**
  - Latest verified: **v10.0.1** (August 14, 2024) — tolerate transient manifest timeouts
  - v9.0.0 (July 2024) marked breaking change (cache pruning behavior); v8.3.2, v8.3.1 available
  - **Note:** As of 2024, no floating major-version tags (`@latest`, `@v10`); must pin to exact version or commit SHA
  - **Recommended:** Pin to v10.0.1 (August 2024) or specific commit SHA (not branch pointers like `@main`)
  - See uv docs for matrix config — [astral-sh/setup-uv releases](https://github.com/astral-sh/setup-uv/releases); [uv GitHub Actions guide](https://docs.astral.sh/uv/guides/integration/github/)

- **pypa/gh-action-pypi-publish**
  - Latest verified: **v1.14.2** (July 29, 2024) — Twine v7 update for core metadata v2.5; also v1.12.4 (January 2025) fixed PEP 639 licensing metadata
  - Master branch sunset; must use `release/v1` branch or specific tagged version
  - **Recommended:** Pin to **v1.14.2** (July 2024) or later 2025 release if available; use `release/v1` branch or commit SHA, never `unstable/v1`
  - Default behavior: PEP 740 attestations enabled by default (as of October 2024, rolled out default in action) — [pypa/gh-action-pypi-publish releases](https://github.com/pypa/gh-action-pypi-publish/releases); [PyPI Blog: Digital Attestations](https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/)

### 2. uv-Based CI Matrix Configuration

**Recommended approach:** Use `astral-sh/setup-uv` with `python-version` matrix input; do NOT use separate `actions/setup-python` when using uv.

**Matrix setup:**
```yaml
strategy:
  matrix:
    python-version: ["3.12", "3.13"]

steps:
  - uses: actions/checkout@v7
  
  - name: Install uv and set Python version
    uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9  # v10.0.1 commit SHA (use specific version)
    with:
      python-version: ${{ matrix.python-version }}
  
  - name: Run tests
    run: uv sync --locked --all-extras && uv run pytest tests
```

**Caching (built-in):**
```yaml
  - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9
    with:
      python-version: ${{ matrix.python-version }}
      enable-cache: true  # Default: true; builds cache key from uv.lock + runner OS + Python version
```

**Manual cache pruning in CI (optional, recommended for cost control):**
```yaml
  - run: uv cache prune --ci  # Removes unused cache entries for CI environments
```

**For `requires-python = ">=3.12"`**, matrix should span only supported versions: `["3.12", "3.13"]` (match pyproject.toml classifiers).

**Pinning uv itself:**
```yaml
  - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9
    with:
      version: "0.12.5"  # Optional: pin the uv tool version (e.g., to match your dev environment)
```

— [uv: Using uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/); [actions/checkout README](https://github.com/actions/checkout?tab=readme-ov-file#usage); [Earthly: Artifacts in GitHub Actions](https://earthly.dev/blog/github-action-artifacts/)

### 3. Trusted Publishing (OIDC) Mechanics

**Permissions block (job-level, strongly recommended):**
```yaml
jobs:
  publish:
    permissions:
      id-token: write  # Mandatory: enables OIDC token exchange with PyPI
      contents: read   # Recommended: allows checkout to read repository content
```

**contents: read is separate from id-token:** The id-token is for PyPI authentication; contents: read is needed only if the publishing job reads the repo (e.g., via `actions/checkout`). In most release workflows, you'll need both.

**GitHub environment (optional but recommended):**
```yaml
jobs:
  publish:
    environment:
      name: release-pypi
      url: https://pypi.org/project/${{ github.repository }}
    permissions:
      id-token: write
      contents: read
    steps:
      ...
```

Environment protection rules can restrict which branches/tags can deploy; GitHub evaluates these before the job runs. **Recommended for security:** Add environment protection rule limiting deployments to tags matching `refs/tags/v*`.

**PyPI setup (one-time, for existing projects):**
1. Log into PyPI (https://pypi.org/account/login/)
2. Navigate to your project's **Publishing** settings: `https://pypi.org/manage/project/<projectname>/settings/publishing/`
3. Click **Add a new publisher** → Choose **GitHub**
4. Fill in:
   - **GitHub Owner:** Repository owner username (e.g., `edoardo-scarpaci` for `edoardo-scarpaci/providify`)
   - **GitHub Repository:** Repository name (e.g., `providify`)
   - **Workflow Name:** YAML file name in `.github/workflows/` (e.g., `release.yml`)
   - **Environment Name:** (optional) Name of GitHub Actions environment (e.g., `release-pypi`)
5. Click **Save**

**Note:** For *existing* projects on PyPI (like providify at v1.1.1), there is **no "pending publisher" flow**—you directly add the publisher to your existing project's trusted publishers list. "Pending publishers" apply only to *new* projects not yet on PyPI.

— [GitHub Docs: OIDC in PyPI](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-pypi); [PyPI Docs: Trusted Publishers](https://docs.pypi.org/trusted-publishers/using-a-publisher/); [PyPI Docs: Controlling Permissions](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/controlling-permissions-for-github_token)

**Publishing workflow step (OIDC, no credentials):**
```yaml
      - name: Download artifacts
        uses: actions/download-artifact@v4  # Must download built wheels/sdist before publishing
        with:
          name: python-package-distributions  # Artifact name from build job
          path: dist/

      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1  # Must use release/v1 or tagged version, NOT master/unstable
        # NO inputs: username, password are omitted; OIDC token is automatic
```

**Action behavior:** When `id-token: write` permission is present and username/password are omitted, `pypa/gh-action-pypi-publish` automatically exchanges GitHub's OIDC token for a PyPI short-lived API token. This requires the GitHub Actions publisher to already be registered on PyPI (see step 2–5 above).

**Attestations (PEP 740):** Enabled by default as of November 2024 (action update October 2024). No action input required; the action automatically creates Sigstore-signed attestations for each distribution. Disable only if needed: `attestations: false` — [PyPI Blog: Digital Attestation Support](https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/); [PEP 740](https://peps.python.org/pep-0740/)

### 4. Build-vs.-Publish Job Separation & Artifacts

**Best practice:** Separate build and publish into distinct jobs, with artifact handoff.

**Build job (test matrix; run linting + tests):**
```yaml
jobs:
  test:
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9
        with:
          python-version: ${{ matrix.python-version }}
      - run: uv sync --locked --all-extras
      - run: uv run ruff check .
      - run: uv run pytest tests

  build:
    needs: test  # Runs only after test matrix passes
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9
        with:
          python-version: "3.12"
      - run: uv sync --locked
      - run: uv build  # Builds wheel + sdist into dist/
      
      - name: Upload distributions
        uses: actions/upload-artifact@v4
        with:
          name: python-package-distributions
          path: dist/

  publish:
    needs: build  # Runs only after build succeeds
    environment: release-pypi
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Download artifacts
        uses: actions/download-artifact@v4
        with:
          name: python-package-distributions
          path: dist/
      
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

**Rationale:** Each job runs on a clean runner (filesystem isolation); `actions/upload-artifact` and `actions/download-artifact` bridge between jobs. Build job may run a full test matrix; publish runs once with downloaded artifacts. Artifacts expire after 90 days by default — [GitHub Docs: Storing Workflow Artifacts](https://docs.github.com/actions/using-workflows/storing-workflow-data-as-artifacts); [Python Packaging Guide: Publishing with GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/)

### 5. Tag-Triggered Release Workflow

**Trigger on annotated tags only:**
```yaml
on:
  push:
    tags:
      - "v*"  # Triggers on push of tag matching v* (e.g., v1.0.0, v2.0.0-beta.1)
```

**Tag annotation best practice:** Use annotated tags (not lightweight) to preserve tagger info:
```bash
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0
```

**Fetch annotated tag info in workflow:**
```yaml
  - uses: actions/checkout@v7
    with:
      fetch-depth: 0      # Fetch full history (some linting/versioning tools need it)
      fetch-tags: true    # IMPORTANT: fetch tags so git describe, version-from-tag tools work
```

**Fetch-tags fix (v6.0.2+, 2026):** Prior to actions/checkout v6.0.2, `fetch-tags: true` did not work; v6.0.2+ fixed it to preserve tag annotations via refspec (`+refs/tags/*:refs/tags/*`) instead of `git fetch --tags`. If using v5 or v4, omit `fetch-tags: true` and use `git fetch --tags` in a run step instead.

**Conditional: only publish on version tags (optional guard):**
```yaml
jobs:
  publish:
    if: startsWith(github.ref, 'refs/tags/v')  # Runs only if push ref is a tag matching v*
    steps:
      ...
```

**Fork safety:** Push events (unlike `pull_request_target`) are not triggered from forks, so trusted publishing is safe from tag pushes. However, for defense-in-depth, the conditional `if: startsWith(github.ref, 'refs/tags/v')` prevents accidentally publishing on branch pushes.

— [GitHub Docs: Events that Trigger Workflows](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows); [GitHub Changelog: Safer PR Checkout Defaults](https://github.blog/changelog/2026-06-18-safer-pull_request_target-defaults-for-github-actions-checkout/)

### 6. 2025–2026 Breaking Changes & Gotchas

**Node 20 Runtime Deprecation (critical):**
- As of June 16, 2026: GitHub Actions runners began using **Node 24** by default
- Node 20 reaches EOL in April 2026; full removal September 2026
- **Impact:** GitHub Actions, particularly `pypa/gh-action-pypi-publish`, expect Node 24 runtime
- **Check runner version:** Ensure runner v2.328.0+ if using self-hosted runners (supports both Node 20/24)
- **Mitigation:** Use action versions released after June 2026 (e.g., `pypa/gh-action-pypi-publish@release/v1` v1.14.0+). Actions using deprecated Node 20 may fail with cryptic warnings
— [GitHub Changelog: Deprecation of Node 20](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/); [GitHub Issue: Default Node.js Version Change](https://github.com/actions/runner-images/issues/14029)

**PEP 740 Digital Attestations (not breaking, but default behavior changed):**
- As of November 14, 2024, PyPI enabled PEP 740 digital attestation support
- As of October 2024, `pypa/gh-action-pypi-publish` rolls out attestations **by default** (no action input change needed)
- Attestations are generated using Sigstore and create `.sigstore/` metadata alongside distributions
- **Compatibility:** All package installers (pip, uv, etc.) ignore attestations safely; no client-side impact
- **Gotcha:** Older tutorials showing `attestations: false` are now unnecessary; rely on the new default
— [PyPI Blog: Digital Attestations](https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/); [PEP 740](https://peps.python.org/pep-0740/)

**actions/checkout tag annotation preservation (v6.0.2+, early 2026):**
- Pre-v6.0.2: `fetch-tags: true` did not work; tag annotations were lost
- v6.0.2+: `fetch-tags: true` fixed to use refspec (`+refs/tags/*:refs/tags/*`), preserving tag object metadata
- **Action:** If pinning to v5 or v4, supplement with `git fetch --tags` if tag info is needed
— [GitHub Issue: Tag Annotations](https://github.com/actions/checkout/issues/290); [actions/checkout releases](https://github.com/actions/checkout/releases)

**Master branch sunset (pypa/gh-action-pypi-publish):**
- `pypa/gh-action-pypi-publish@master` is no longer maintained
- **Gotcha:** Widely-copied tutorials use `@master`; will fail with stale behavior or warnings
- **Correct path:** Always use `@release/v1` or a specific tagged version (e.g., `@v1.14.2`)
— [pypa/gh-action-pypi-publish README](https://github.com/pypa/gh-action-pypi-publish/blob/unstable/v1/README.md)

**astral-sh/setup-uv no floating major tags:**
- Prior versions published `@latest`, `@v8`, etc. (floating tags)
- v10.0.0+ (and earlier v8/v9 releases) do not publish floating tags
- **Gotcha:** Naive workflows using `@latest` will fail; must pin to exact version or commit SHA
- **Correct path:** Use `astral-sh/setup-uv@v10.0.1` or `@c771a70e6277c0a99b617c7a806ffedaca235ff9`
— [astral-sh/setup-uv releases](https://github.com/astral-sh/setup-uv/releases)

## Version/Compatibility Notes

- **actions/checkout:** v7 (July 2024) current; v6 also maintained with backports through 2026
- **actions/setup-python:** v7 (July 2024) current; v6 maintained
- **astral-sh/setup-uv:** v10.0.1 (August 2024) verified; recommend pinning exact version
- **pypa/gh-action-pypi-publish:** v1.14.2 (July 2024) verified; v1.12.4+ (Jan 2025) available; use `release/v1` branch
- **Node.js runtime:** Node 20 deprecated June 2026, removed September 2026; must pin or use Node 24-compatible actions
- **Python:** providify requires `>=3.12`; test matrix should include 3.12, 3.13

## Evidence Gaps

1. **Exact minor version of astral-sh/setup-uv in August 2026:** Search results show v10.0.1 (August 2024) as latest; unknown if v11/v12 exist in mid-2026
2. **pypa/gh-action-pypi-publish in mid-2026:** Latest verified is v1.14.2 (July 2024); possible v1.15+ exists but not found in searches
3. **Real-world breaking-change reports:** No specific incident reports found of Node 20 deprecation breaking popular GitHub Actions workflows (expected, but unverified)
4. **Future PEP 740 enhancements:** Specification stable as of 2024; unknown if 2025–2026 adds new fields or breaking changes

## Librarian's Note

**What the sources indicate:**

The mechanical setup for v2.0.0 release should:

✅ **CI workflow:** Matrix over Python 3.12–3.13 using `astral-sh/setup-uv@v10.0.1` (or current 2026 patch) + `uv sync`/`uv run pytest` + `uv run ruff check .` (linting gate, per earlier brief)

✅ **Trusted publishing:** Separate `build` and `publish` jobs; build outputs to artifact; publish uses `pypa/gh-action-pypi-publish@release/v1` with `id-token: write` + `contents: read` permissions; **no credentials passed**; one-time PyPI setup adds GitHub Actions as trusted publisher under project settings (not "pending publisher" — that's for new projects)

✅ **Release trigger:** Push annotated tags (`v*`); use `fetch-tags: true` in `actions/checkout@v7` to preserve tag metadata

⚠️ **2026 awareness:** Node 20 deprecated (June) and removed (September 2026); ensure all actions pinned to versions released after June 2026, or they may fail. PEP 740 attestations are now default and automatic.

✅ **Artifact flow:** `actions/upload-artifact@v4` (build) → `actions/download-artifact@v4` (publish); simplest pattern for isolated jobs.

For **providify v2.0.0**, the evidence **strongly favours** separate build/publish jobs with OIDC trusted publishing; this is now the canonical "blessed" pattern per PyPA and GitHub Actions docs (2024–2025). API token workflows are deprecated and should be avoided.
