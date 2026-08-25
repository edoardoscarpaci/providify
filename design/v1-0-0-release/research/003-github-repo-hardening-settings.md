# Research 003 — GitHub Repository Hardening for Production OSS

Date: 2026-08-25 · Freshness matters: **yes** (GitHub feature rollout ongoing 2025–2026, rulesets deprecating branch protection, Actions security model changed 2023–2024, Scorecard checks evolving)

## Question

What are all the important GitHub repository settings, UI toggles, and CLI commands a solo maintainer must manually configure for a production open-source Python project to be credibly hardened? Specifically: branch/tag protection, commit signing, Actions security, environment controls, supply-chain scanning, repo hygiene, and OpenSSF baseline. Exclude CI/CD workflow YAML (covered separately) and governance files (CONTRIBUTING, SECURITY, etc.—also separate). Deliverable: a prioritized checklist of human actions with exact UI paths or `gh` commands, evidence citations, and free-tier notes.

## Findings

### 1. Rulesets vs. Classic Branch Protection: Current Status & Solo-Maintainer Configuration

**Status quo (2026):**
- **Rulesets are the current standard** and the recommended path forward for all new repositories — [GitHub Docs: Creating rulesets for a repository](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository) (current)
- **Classic branch protection rules are deprecated** in GitHub Enterprise Server 3.16+; GitHub automatically migrates existing branch protection rules to rulesets — [GitHub Changelog: Automatically migrate branch protection rules (Aug 2026)](https://github.blog/changelog/2026-08-11-automatically-migrate-branch-protection-rules-to-repository-rulesets/)
- **Rulesets advantages over classic rules:** Multiple rulesets can apply simultaneously (classic rules only allow one), broader targeting (branches & tags), finer-grained bypass control per actor type, and future GitHub feature parity
- **Branch protection is not dead, but absorbed into rulesets** — [Medium: Branch Protection is Dead, Long Live Rulesets (Jun 2026)](https://wafaa-t.medium.com/branch-protection-is-dead-long-live-rulesets-1de78c35efca)

**Solo-maintainer ruleset configuration for default branch (`main`):**
- **Access path:** Settings → Rules → Rulesets → New ruleset → New branch ruleset
- **Target:** `main` (or current default branch)
- **Enforcement:** Active
- **Core rules to enable** (from [Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)):
  1. **Require pull request before merging** — All changes must go through a PR; enforces code review pattern even for solo maintainer (documents intent, creates audit trail)
  2. **Require status checks to pass** — Tie to CI workflow job names (e.g., `test`, `lint`, `type-check`); blocks merge until GitHub Actions CI passes
  3. **Require linear history** — Prevents merge commits; enforces squash-merge or rebase workflow; keeps history clean
  4. **Block force pushes** — Prevents accidental history destruction (enabled by default in rulesets)
  5. **Restrict deletions** — Prevents branch deletion (enabled by default in rulesets)

**Bypass actors for solo maintainer:**
- Rulesets support "bypass actors" — users/roles that can override rules — [GitHub Changelog: Repository rulesets user bypass (May 2026)](https://github.blog/changelog/2026-05-07-repository-rulesets-user-bypass-and-branch-renaming/)
- **Recommended:** Add "Repository admins" as bypass actor with "Always allow" mode; this permits the maintainer to force-push to fix CI issues or revert dangerous commits without creating a second account
- **Solo-maintainer trade-off:** "Require PR" is theatre when the same person approves their own PR; **pragmatic approach:** keep the rule for documentation/automation benefit (third-party tooling may check it), but acknowledge the bypass negates enforcement. Alternative: enable "Require PR" without PR-review requirements; admins can bypass to emergency-push if needed, but this is rare.
- **Bypass for bots:** Dependabot (when enabled) can bypass PR creation rules if configured; optional, depends on auto-merge preferences

**No classic branch protection UI should be used going forward** — if inherited, convert to ruleset via Settings → Branches → [Old rule] → Convert to ruleset.

### 2. Commit Signing: Setup & Enforcement

**Supported methods (2026) and recommendations:**
- **GPG (GNU Privacy Guard):** Supported, but setup is friction-heavy (key generation, management, expiration) — [GitHub Docs: About commit signature verification](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification)
- **SSH (ED25519 recommended):** **Lowest friction in 2026** — reuse existing GitHub SSH auth key, simple setup, widely supported — [tobywf: Ditching GPG (Jan 2026)](https://tobywf.com/2026/01/ditch-gnupg-signing-commits-with-ssh/); [Khimananda: Sign commits with GPG or SSH (2026)](https://khimananda.com/blog/sign-your-git-commits-with-gpg-or-ssh)
- **Gitsign + Sigstore:** Keyless signing via OIDC; no key management, short-lived certificates — [Sigstore Gitsign repo](https://github.com/sigstore/gitsign); **caveat:** GitHub web UI does not show green "Verified" badge for gitsign-signed commits (yet), reducing social signal
- **S/MIME:** Organizational context only; out of scope for solo maintainer

**Setup path for SSH signing (recommended):**
```bash
git config --global gpg.format ssh
git config --global user.signingKey ~/.ssh/id_ed25519.pub  # Path to your SSH public key
git config --global commit.gpgSign true  # Auto-sign all commits
git config --global tag.gpgSign true     # Auto-sign all tags
```
— [GitHub Docs: Telling Git about your signing key](https://docs.github.com/en/authentication/managing-commit-signature-verification/telling-git-about-your-signing-key)

**Enforcing signed commits via ruleset:**
- **Access path:** Settings → Rules → Rulesets → [your main-branch ruleset] → Edit
- **Add rule:** "Require signed commits" — blocks any unsigned commits from being pushed to the branch
- **Note:** Web UI edits (e.g., editing a file directly in GitHub.com) automatically sign with GitHub's key; no friction for GUI users
- **Merge commits:** When merging a PR, GitHub creates a merge commit signed by GitHub if the base branch requires signed commits (seamless)
- **Squash merges:** Also automatically signed by GitHub; no additional setup
- **Known issue:** If a contributor pushes unsigned commits, they must rebase, sign locally, and force-push; can be friction in a public repo

**Practical trade-off for solo maintainer:** Signed commits are table-stakes for large or security-sensitive projects, but add friction for a solo maintainer working alone; compromise: enable the ruleset rule but configure permissive bypass to allow self-override in emergencies (see bypass actors above).

### 3. Tag Protection: Release Tags & Security

**Current status (2026):**
- **Classic tag protection rules** (Settings → Tags → [pattern]) are deprecated in favor of rulesets — [GitHub Enterprise Docs: Tag protection rules](https://docs.github.com/en/enterprise-server@3.15/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/configuring-tag-protection-rules); [GitHub Changelog: Tag protection (Mar 2022, but still relevant)](https://github.blog/changelog/2022-03-08-tag-protection-rules/)
- **Recommendation:** Use rulesets for tag protection instead of the deprecated classic UI
- **Auto-migration:** Existing tag protection rules will be migrated to rulesets in future GitHub versions

**Tag protection via ruleset (best practice):**
- **Access path:** Settings → Rules → Rulesets → New ruleset → New tag ruleset
- **Target:** `v*` (fnmatch pattern for semantic version tags `v1.0.0`, `v2.0.0-beta.1`, etc.)
- **Rules to enable:**
  1. **Restrict creations** — Only admins/maintainers can push version tags (prevents accidental tag pushes)
  2. **Restrict updates** — Tags cannot be force-pushed (immutable releases)
  3. **Restrict deletions** — Tags cannot be deleted (audit trail preservation)
- **Bypass actors:** Same as branch ruleset (repository admins), allowing emergency re-tagging if needed
- **Bypass for CI:** Tag-triggered release workflows (CI pushing artifacts) do NOT require bypass if the triggering push event comes from an admin; the workflow itself runs with `id-token` permission (not touching the tag creation)

**Interaction with release workflow:**
- Release workflows (e.g., `release.yml`) are triggered by `push` event on tags matching `refs/tags/v*` — no additional tag protection needed in the workflow YAML
- The release workflow does **not** create the tag; the maintainer does (`git tag -a v1.0.0 && git push origin v1.0.0`), and the tag ruleset protects that step
- Release job permissions are controlled by environment protection rules (see section 5), not tag rules

### 4. GitHub Actions Security Settings (Repository)

**Default GITHUB_TOKEN permissions:**
- **Status quo:** New repositories default to **read-only** (`contents: read` + `packages: read`) for the GITHUB_TOKEN — [GitHub Changelog: Default token permissions read-only (Feb 2023)](https://github.blog/changelog/2023-02-02-github-actions-updating-the-default-github_token-permissions-to-read-only/) (now widely adopted by 2026)
- **Importance:** Prevents compromised actions or exfiltrated tokens from sabotaging CI/CD (no ability to approve PRs, delete artifacts, or modify repo settings by default)
- **Verification:** No action required if repo was created after Feb 2023; if pre-2023, check Settings → Actions → General → Workflow permissions

**Workflow permissions setting:**
- **Access path:** Settings → Actions → General → Workflow permissions
- **Recommended default:** "Read repository contents and packages permissions" (the restrictive option)
- **Override in workflow:** Individual jobs can escalate via `permissions:` block if needed (e.g., `id-token: write` for OIDC publishing)
- No need to change the repository default if workflows explicitly declare their permissions — [GitHub Docs: Controlling permissions for GITHUB_TOKEN](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/controlling-permissions-for-github_token)

**Actions permission policy:**
- **Access path:** Settings → Actions → General → Actions permissions
- **Options:**
  1. **Allow all actions and reusable workflows** — Least secure, but practical for projects using many third-party actions
  2. **Allow OWNER and select non-OWNER actions** — Whitelist specific creators/actions; safer for security-conscious teams (requires maintainence)
  3. **Disable GitHub Actions** — Only if not using CI/CD
- **Recommendation:** Start with "Allow all" (most practical for a solo project with curated action list), but if considering "allow list," the syntax is `ACTION_OWNER/*`, `ACTION_OWNER/ACTION_NAME`, or `ACTION_OWNER/ACTION_NAME@ref`
- **Action pinning best practice:** Pin all actions to **exact commit SHA** (not branch or major version tag) in workflows — [GitHub Docs: Security hardening](https://docs.github.com/en/actions/security-for-github-actions/security-hardening-for-github-actions) (separate from repo setting; covered in workflow YAML)

**Fork PR workflow approval:**
- **Access path:** Settings → Actions → General → Fork pull request workflows from outside collaborators
- **Options:**
  1. "Require approval for first-time contributors who are new to GitHub" — Balances usability and security
  2. "Require approval for first-time contributors" — Requires approval on first PR from any account
  3. "Require approval for all external contributors" — Every PR from non-org member needs approval
  4. "Approve all pull requests" — Unsafe; auto-approves all fork PRs
- **Recommendation:** Option 1 (first-time new GitHub accounts) is safest without being overly burdensome; prevents bot-account spam while allowing established contributors to contribute without friction
- **Automation:** Maintainer must manually approve fork PRs via GitHub UI or `gh pr review` CLI before workflows run

**Allowed actions and verified-creator filtering:**
- The "Allow OWNER and select non-OWNER actions" mode supports filtering to "verified creators" only (GitHub-verified publishers) in enterprise/team plans; **free tier does not support this filter**
- **Free-tier approach:** Use "Allow all" or maintain an explicit allowlist of actions used in CI workflows (document in CONTRIBUTING.md which actions are trusted)

### 5. Environment Protection Rules (for `release-pypi` environment)

**Availability on free tier:**
- Environments and environment protection rules are **free for public repositories** (any plan) — [GitHub Docs: Managing environments for deployment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
- **Note:** Private repositories require GitHub Pro, Team, or Enterprise for environment access; solo maintainers on public repos are fine

**Setup path:**
1. **Create environment:** Settings → Environments → New environment → Name: `release-pypi`
2. **Set environment URL (optional but recommended):** `https://pypi.org/project/providify/` (helps identify environment in workflow logs)

**Protection rules for `release-pypi`:**

**Deployment branches and tags restriction:**
- **Access path:** Settings → Environments → release-pypi → Deployment branches and tags
- **Setting:** "Allow specific branches and tags" → Pattern: `ref:refs/tags/v*` (restricts deployments to tags matching `v*` only)
- **Effect:** Only workflows triggered by tags matching `v*` can deploy to this environment; prevents accidental PyPI publishes from `main` branch pushes
- **Syntax:** `ref:` prefix for tag/branch refs in fnmatch syntax — [GitHub Docs: Deployments and environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)

**Required reviewers (optional for solo maintainer):**
- **Access path:** Settings → Environments → release-pypi → Required reviewers
- **Caveat:** Required reviewers block workflow deployment until a specified person approves; for solo maintainer, this creates a manual approval gate for every release
- **Pragmatic approach:** Do NOT enable for solo maintainer (adds friction without security benefit; you are the reviewer)
- **Use case:** For multi-maintainer teams, this is valuable; for solo, it's ceremony

**Wait timer (enterprise/team only):**
- Delays deployment by N minutes; **not available on free tier**

**Secrets specific to environment:**
- Can store PyPI token as environment secret (though trusted publishing via OIDC is preferred and requires no secrets — covered in earlier brief)

### 6. Supply-Chain Security & Scanning Features

**A. Dependency Graph & Dependabot Alerts**
- **Dependency graph:** Automatically enabled for all public repositories; displays in "Insights" tab — [GitHub Docs: About the dependency graph](https://docs.github.com/en/code-security/reference/supply-chain-security/understanding-dependencies)
- **Action:** No manual setup required; GitHub automatically scans `pyproject.toml`, `requirements.txt`, `poetry.lock`, etc.
- **Status:** Free for all; no configuration needed

**B. Dependabot Security Updates**
- **Cost:** Free for all repositories (public and private) — [AppSec Santa: Is Dependabot Free (2026)](https://appsecsanta.com/dependabot)
- **Activation path:** Settings → Code security & analysis → Dependabot alerts → Enable (usually pre-enabled)
- **Effect:** GitHub scans dependencies for known vulnerabilities and creates alerts; maintainer is notified
- **Auto-PR creation:** Can enable "Dependabot security updates" → "Enable" to auto-create PRs for vulnerability fixes — same settings area
- **Note:** Requires Dependabot alerts to be enabled first; no separate toggle

**C. Dependabot Version Updates**
- **Cost:** Free
- **Activation:** Requires `dependabot.yml` file in `.github/` directory; no UI toggle (YAML-only config)
- **Example config for Python + GitHub Actions:**
  ```yaml
  version: 2
  updates:
    - package-ecosystem: "pip"
      directory: "/"
      schedule:
        interval: "weekly"
    - package-ecosystem: "github-actions"
      directory: "/"
      schedule:
        interval: "weekly"
  ```
  — [GitHub Docs: Keeping your actions up to date with Dependabot](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/keeping-your-actions-up-to-date-with-dependabot); [Supported ecosystems](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories) confirms `github-actions` is a supported ecosystem
- **Rationale:** `dependabot.yml` is recommended (not required) because it enables granular control over which package managers to monitor, update frequency, and PR grouping

**D. Secret Scanning & Push Protection**
- **Secret scanning:** Automatically enabled for all public repositories; no UI toggle — [GitHub Docs: About secret scanning](https://docs.github.com/code-security/secret-scanning/about-secret-scanning)
- **Push protection:** **Free for public repositories** — blocks pushes containing secrets before they hit the repo — [AppSec Santa: GitHub Secret Scanning (2026)](https://appsecsanta.com/github-secret-scanning); [GitHub Changelog: Secret scanning coverage (Aug 2026)](https://github.blog/changelog/2026-08-07-secret-scanning-coverage-updates/)
- **Coverage:** 39+ token types (AWS, GitHub tokens, Datadog, Heroku, etc.) automatically blocked — [GitHub Changelog: Pattern updates (April 2026)](https://github.blog/changelog/2026-04-14-secret-scanning-pattern-updates-and-product-improvements/)
- **Activation:** Settings → Code security & analysis → Secret scanning → Enable (usually pre-enabled); toggle "Push protection" → On
- **Private repo note:** Private repos need GitHub Advanced Security license; public repos are free

**E. CodeQL / Code Scanning**
- **Cost:** Free for public repositories; requires GitHub Advanced Security for private repos
- **Setup method (recommended):** Use "Default setup" (2-click, no YAML) instead of advanced setup
- **Access path:** Settings → Code security & analysis → Code scanning → Enable (or "Set up default setup")
- **Effect:** GitHub automatically configures CodeQL to scan Python, JavaScript, TypeScript, etc. on every PR and scheduled weekly; no workflow file needed
- **Alternative:** Can use `gh code-scanning enable` CLI command for bulk enablement — [GitHub CLI: gh code-scanning](https://github.com/advanced-security/gh-code-scanning) (community extension)
- **Configuration:** Language and query suite chosen automatically; can be customized later if needed

**F. Private Vulnerability Reporting (aka "security advisories")**
- **Cost:** Free for all public repositories — [GitHub Changelog: Private vulnerability reporting generally available](https://github.blog/security/supply-chain-security/private-vulnerability-reporting-now-generally-available/)
- **Purpose:** Allows researchers to privately report vulnerabilities without public disclosure; maintainer creates advisory, publishes when ready
- **Activation:** Settings → Code security & analysis → Private vulnerability reporting → Enable
- **Effect:** Adds "Report a vulnerability" link to repository README/about section; researchers can use the form to submit sensitive security issues

**Summary table (supply-chain features):**
| Feature | Free for Public? | Requires Config? | Activation |
|---------|---|---|---|
| Dependency graph | ✓ Yes | No | Automatic |
| Dependabot alerts | ✓ Yes | No | Settings toggle |
| Dependabot security updates | ✓ Yes | No | Settings toggle |
| Dependabot version updates | ✓ Yes | Yes (`.github/dependabot.yml`) | YAML file |
| Secret scanning | ✓ Yes | No | Automatic + toggle for push protection |
| CodeQL default setup | ✓ Yes | No | Settings → "Set up default setup" |
| Private vulnerability reporting | ✓ Yes | No | Settings toggle |

### 7. Repository Hygiene Settings

**A. Auto-delete head branches on merge**
- **Access path:** Settings → General → Pull Requests → "Automatically delete head branches" → Enable
- **Effect:** When a PR is merged, the source branch is automatically deleted; reduces clutter in branch list
- **Recommendation:** Enable (harmless, clean)
- **Note:** Users can still restore deleted branches from the branch list for 90 days

**B. Merge strategy restrictions**
- **Access path:** Settings → General → Pull Requests → "Allow [X] merge"
- **Options:** "Allow merge commits", "Allow squash merging", "Allow rebase merging"
- **Recommendation for linear history:** Disable "Allow merge commits"; allow only "Squash" or "Rebase". With ruleset rule "Require linear history" enabled, squash/rebase are forced anyway. Recommendation: **Allow squash only** (cleanest history for a solo project) or "Allow rebase only" (if preserving individual commits matters)
- **Rationale:** For a library with a `main` branch ruleset enforcing "Require linear history", merge commits are blocked at the branch level anyway; the UI checkbox is redundant but provides a signal to contributors

**C. Require conversation resolution before merging**
- **Not a separate repo setting**, but a branch protection rule (now in rulesets)
- **Can be added to main-branch ruleset:** Settings → Rules → Rulesets → [main ruleset] → Add rule → "Require conversation resolution" (if available in your GitHub version)
- **Effect:** All discussion threads on a PR must be marked "resolved" before merge
- **Recommendation:** Optional; useful for larger teams to ensure feedback is addressed; low value for solo maintainer

**D. Issues, Discussions, Wiki toggles**
- **Access path:** Settings → General → "Features"
- **Recommendation:**
  - **Issues:** Enable (default; allows bug reports and feature requests)
  - **Discussions:** Enable (optional; provides a forum; helpful if maintainer wants to foster community; can be disabled for small projects)
  - **Wiki:** Disable (unless actively maintained; stale wiki is worse than no wiki; use README + docs/ folder instead)
- **Project:** Enable if using GitHub Projects for roadmap; optional but not critical for v2.0.0

**E. About / README / topics / description**
- **Not a security setting, but affects discoverability**
- **Recommendation:**
  - Add repository description (one line, visible on GitHub search and PyPI)
  - Add 3–5 topics: e.g., "python", "dependency-injection", "di", "library", "open-source"
  - Ensure README is comprehensive and includes:
    - Brief description + use case
    - Installation instructions (`pip install providify`)
    - Quick example
    - Links to docs, CONTRIBUTING, LICENSE
    - Badges: build status, PyPI version, license, OpenSSF Scorecard (once enabled; see section 8)

### 8. OpenSSF Scorecard: Running & Interpreting

**What is it?**
- Free, open-source automated security assessment tool run by the OpenSSF — [OpenSSF Scorecard](https://scorecard.dev/)
- Evaluates 20 checks across your repository and provides a 0–10 score; can be run weekly via GitHub Action, CI, or public REST API
- Results include a badge (e.g., "Scorecard 9/10") suitable for README

**Checks relevant to Python projects** (from [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md)):
1. **Branch-Protection** — Checks if default branch has protection rules (rulesets); will pass if ruleset with "require PR" is enabled
2. **CI-Tests** — Verifies that CI tests run on PRs and are required for merge (status checks in ruleset)
3. **Code-Review** — Checks if PRs require code review (ruleset "require PR" counts)
4. **Contributors** — Ensures repository has a diverse contributor base (low score for solo-only repos; acknowledged limitation)
5. **Dependency-Update-Tool** — Checks if Dependabot or similar is enabled (will pass if Dependabot alerts/updates are on)
6. **License** — Checks if repository has a LICENSE file (must be present)
7. **Maintained** — Checks if repository is actively maintained (based on recent commits/issues; v2.0.0 passes this)
8. **Packaging** — Checks if package is published to PyPI or similar (will pass for providify on PyPI)
9. **Pinned-Dependencies** — Checks if dependencies are pinned to specific versions in requirements.txt / pyproject.toml (best practice; pass if using lock files or pinned versions)
10. **SAST** — Checks if static analysis tools (CodeQL, etc.) are enabled (will pass if CodeQL default setup is on)
11. **Security-Policy** — Checks if SECURITY.md exists (mandatory per OSPS baseline; required for pass)
12. **Signed-Releases** — Checks if GitHub releases are created and/or commits are signed (will pass if tags are annotated + GitHub Releases created)
13. **Token-Permissions** — Checks if GITHUB_TOKEN permissions are restricted in workflows (pass if default is read-only)
14. **Vulnerabilities** — Checks for known CVEs in dependencies (pass if no vulnerabilities are found)

**Setup (recommended):**
1. **Add GitHub Action to CI:**
   ```yaml
   - name: Run OpenSSF Scorecard
     uses: ossf/scorecard-action@v2.3.0
     with:
       results_file: results.sarif
       publish_results: true
   ```
   — Runs on every push and publishes results to GitHub Security tab; [ossf/scorecard-action](https://github.com/ossf/scorecard-action)

2. **Alternative: Run in GitHub Action once or add to cron:**
   ```yaml
   on:
     schedule:
       - cron: '0 0 * * 0'  # Weekly on Sundays
   ```

3. **Alternative: Run manually or via REST API:**
   - `docker run gcr.io/openssf/scorecard --repo=https://github.com/edoardo-scarpaci/providify`
   - Results viewable at [scorecard.dev](https://scorecard.dev/viewer)

**Badge (optional):**
- Add to README:
  ```markdown
  [![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/edoardo-scarpaci/providify/badge)](https://securityscorecards.dev/viewer?uri=github.com/edoardo-scarpaci/providify)
  ```

**Expected score for providify v2.0.0 (given planned hardening):**
- **Likely 8–9/10** if:
  - ✅ Branch-Protection ruleset on main: enabled
  - ✅ CI-Tests: required status checks in ruleset
  - ✅ CODE-REVIEW: require PR enabled
  - ✅ Dependency-Update-Tool: Dependabot alerts on
  - ✅ License: LICENSE file present
  - ✅ Maintained: active project
  - ✅ Packaging: on PyPI
  - ✅ Pinned-Dependencies: `uv.lock` or pinned in `pyproject.toml`
  - ✅ SAST: CodeQL enabled
  - ✅ Security-Policy: SECURITY.md exists
  - ✅ Signed-Releases: tags + GitHub Releases
  - ✅ Token-Permissions: default read-only
  - ✅ Vulnerabilities: no known CVEs
  - ⚠️ **Contributors:** Solo maintainer; Scorecard flags this but not a pass/fail (acknowledged limitation for small/new projects)

**Worth it?**
- **For a production v2.0.0 release:** Yes, minimal effort (add 1 GitHub Action) and provides credibility signal to users
- **Badge adds social proof** that the project takes security seriously
- **Checks align with OSPS 2025 baseline** (mentioned in earlier brief); running Scorecard confirms compliance

## Version/Compatibility Notes

- **GitHub Rulesets:** Stable since ~2023; feature-complete for branch/tag protection as of 2026. No breaking changes expected.
- **Commit signing:** SSH signing stable in Git 2.34+; gitsign/Sigstore still evolving (no verified badge yet).
- **Tag protection:** Auto-migration to rulesets planned for Enterprise 3.16+; classic rules remain functional but deprecated.
- **GitHub Actions:** Default read-only GITHUB_TOKEN became default Feb 2023; now standard across all 2026 repos.
- **Dependabot:** Stable; supports all major package ecosystems including python (`pip`) and `github-actions` since ~2022.
- **Secret scanning push protection:** GA in 2022; 39+ token types covered as of Aug 2026.
- **CodeQL default setup:** GA in 2024; now preferred over manual workflow setup.
- **Environments:** Stable since GitHub Actions launch (2019); no breaking changes.
- **OpenSSF Scorecard:** 20 checks stable; released 2020, actively maintained; check definitions may change quarterly, but no major breaking changes.

## Evidence Gaps

1. **Exact behavior when solo maintainer approves own PR with branch protection enabled:** GitHub documentation doesn't explicitly state whether self-approval counts as "code review" or if admin bypass is required. Practical answer: Solo maintainer can approve own PR (they are a reviewer); the rule checks *that* a review happened, not who gave it. However, best practice is to use ruleset bypass-for-admins so the maintainer can merge without PR if emergency is needed.

2. **Gitsign verified badge status:** Search results indicate GitHub web UI does not yet show "Verified" for gitsign commits, but this may change; no official announcement found. Worth re-checking in 2027.

3. **Real-world Scorecard score distribution:** No large-scale data on what score solo Python maintainers typically achieve; no published benchmark, only the tool itself.

4. **Deprecation timeline for classic branch protection rules:** GitHub has not announced a hard EOL date for classic rules; they are "deprecated in favor of rulesets" but may coexist indefinitely. No forced migration timeline visible as of Aug 2026.

5. **`gh` CLI commands for enabling repo settings:** GitHub CLI (`gh`) has limited coverage for repository setting toggles; most settings still require web UI or REST API. No centralized "enable hardening" command found.

## Librarian's Note

**What the sources indicate:**

A production Python OSS v2.0.0 release on GitHub should:

✅ **Rulesets (branch + tag):** Use the ruleset UI (not deprecated classic branch protection) to enforce:
- Require pull request before merge
- Require status checks (CI) to pass
- Require linear history (squash/rebase only)
- Require signed commits (if acceptable friction)
- Block force-push and deletions

✅ **Commit signing:** SSH key signing is lowest-friction for 2026; enforce via ruleset rule. Solo maintainer should configure admin bypass to avoid lockout.

✅ **Tag protection:** Use tag ruleset targeting `v*` pattern to restrict tag creation/deletion to admins; pairs with annotated-tag + GitHub Release workflow.

✅ **Actions security:** Keep default read-only GITHUB_TOKEN (now default); explicitly grant `id-token: write` only for OIDC publishing jobs. Fork PR approval: "first-time new account" is safe default.

✅ **Environment (`release-pypi`):** Use free environment protection with tag-pattern restriction (`v*` only) to gate PyPI deployments.

✅ **Supply-chain scanning:** Enable all free features:
- Dependabot alerts + security updates (toggle in Settings)
- Dependabot version updates (add `.github/dependabot.yml`)
- Secret scanning with push protection (toggle in Settings)
- CodeQL default setup (one-click in Settings)
- Private vulnerability reporting (toggle in Settings)

✅ **Repo hygiene:** Auto-delete head branches, restrict merge to squash-only (or rebase), enable Issues, disable unused Discussions/Wiki, add topics for discoverability.

✅ **OpenSSF Scorecard:** Run weekly via GitHub Action; minimal effort, high credibility signal; expect 8–9/10 score given above hardening.

**Trade-off for solo maintainer:** Rules like "require signed commits" and "require PR + code review" are theatre when one person both writes and approves the code. However, they serve as:
1. **Audit trail** for automated tooling and third-party audits (OSPS baseline, Scorecard checks)
2. **Muscle memory** for multi-person workflow (when team grows)
3. **Emergency-escape-hatch availability** via admin bypass (if rules become blocking)

**Bottom line:** The evidence **strongly favours** configuring these settings as a checklist, not selectively. The marginal friction (admin can bypass) is low; the credibility and compliance signal is high.

---

## Checklist: GitHub Repository Hardening Settings for providify v2.0.0

**Instructions:** Work through the table in order (grouped by GitHub UI section for efficiency). Priority levels:
- **MUST:** Required for production open-source credibility (OSPS baseline, Scorecard pass)
- **SHOULD:** Strongly recommended; minor friction with high security/hygiene benefit
- **NICE:** Optional; adds marginal value; skip if time-constrained

### Checklist Table

| # | Setting | Where (exact UI path or `gh` CLI command) | Value to set | Priority | Free tier? | Source |
|---|---------|-------|------|----------|-----------|--------|
| 1 | Create branch ruleset | Settings → Rules → Rulesets → New ruleset → New branch ruleset | Name: `default-branch-protection`; Target: `main` (or current default); Enforcement: Active | MUST | Yes | [GitHub Docs: Creating rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository) |
| 2 | Require PR before merge | Settings → Rules → Rulesets → [ruleset name] → Add rule → "Require pull request before merging" | Require: 0 approval required (solo), no dismissal of stale PRs needed | MUST | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 3 | Require status checks | Settings → Rules → Rulesets → [ruleset name] → Add rule → "Require status checks to pass" | Required checks: (tie to CI workflow job names, e.g., `test`, `lint`, `type-check`) | MUST | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 4 | Require linear history | Settings → Rules → Rulesets → [ruleset name] → Add rule → "Require linear history" | Enabled | MUST | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 5 | Block force-push | Settings → Rules → Rulesets → [ruleset name] → Add rule → "Block force pushes" | Enabled (default) | MUST | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 6 | Restrict deletions | Settings → Rules → Rulesets → [ruleset name] → Add rule → "Restrict deletions" | Enabled (default) | MUST | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 7 | Bypass actors (admin) | Settings → Rules → Rulesets → [ruleset name] → Bypass actors → Add actors | Actor: Repository admins; Bypass type: Always allow | MUST | Yes | [GitHub Changelog: Ruleset bypass (May 2026)](https://github.blog/changelog/2026-05-07-repository-rulesets-user-bypass-and-branch-renaming/) |
| 8 | Require signed commits | Settings → Rules → Rulesets → [ruleset name] → Add rule → "Require signed commits" | Enabled | SHOULD | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 9 | Configure SSH signing | Local git config: `git config --global gpg.format ssh && git config --global user.signingKey ~/.ssh/id_ed25519.pub && git config --global commit.gpgSign true && git config --global tag.gpgSign true` | SSH key signing enabled for all commits/tags | SHOULD | Yes | [GitHub Docs: Commit signature verification](https://docs.github.com/en/authentication/managing-commit-signature-verification/about-commit-signature-verification) |
| 10 | Create tag ruleset | Settings → Rules → Rulesets → New ruleset → New tag ruleset | Name: `release-tags`; Target: `v*`; Enforcement: Active | MUST | Yes | [GitHub Docs: Creating rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository) |
| 11 | Restrict tag creation | Settings → Rules → Rulesets → [tag ruleset] → Add rule → "Restrict creations" | Enabled | SHOULD | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 12 | Restrict tag updates | Settings → Rules → Rulesets → [tag ruleset] → Add rule → "Restrict updates" | Enabled | SHOULD | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 13 | Restrict tag deletion | Settings → Rules → Rulesets → [tag ruleset] → Add rule → "Restrict deletions" | Enabled | SHOULD | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 14 | Tag ruleset bypass | Settings → Rules → Rulesets → [tag ruleset] → Bypass actors → Add actors | Actor: Repository admins; Bypass type: Always allow | SHOULD | Yes | [GitHub Changelog: Ruleset bypass (May 2026)](https://github.blog/changelog/2026-05-07-repository-rulesets-user-bypass-and-branch-renaming/) |
| 15 | Verify GITHUB_TOKEN default | Settings → Actions → General → Workflow permissions | "Read repository contents and packages permissions" (restrictive) | MUST | Yes | [GitHub Changelog: Default token permissions (Feb 2023)](https://github.blog/changelog/2023-02-02-github-actions-updating-the-default-github_token-permissions-to-read-only/) |
| 16 | Set Actions policy | Settings → Actions → General → Actions permissions | "Allow all actions and reusable workflows" OR "Allow OWNER and select non-OWNER actions" with allowlist if policy needed | SHOULD | Yes | [GitHub Docs: Managing Actions settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository) |
| 17 | Configure fork PR approval | Settings → Actions → General → Fork pull request workflows from outside collaborators | "Require approval for first-time contributors who are new to GitHub" | SHOULD | Yes | [GitHub Docs: Managing Actions settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository) |
| 18 | Create `release-pypi` environment | Settings → Environments → New environment | Name: `release-pypi`; URL (optional): `https://pypi.org/project/providify/` | MUST | Yes | [GitHub Docs: Managing environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment) |
| 19 | Restrict deployment to tags | Settings → Environments → `release-pypi` → Deployment branches and tags | "Allow specific branches and tags"; Pattern: `ref:refs/tags/v*` | MUST | Yes | [GitHub Docs: Managing environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment) |
| 20 | Dependabot alerts | Settings → Code security & analysis → Dependabot alerts → Enable | Enabled | MUST | Yes | [GitHub Docs: Dependabot security updates](https://docs.github.com/en/code-security/concepts/supply-chain-security/about-dependabot-security-updates) |
| 21 | Dependabot security updates | Settings → Code security & analysis → Dependabot security updates → Enable | Enabled | MUST | Yes | [GitHub Docs: Dependabot security updates](https://docs.github.com/en/code-security/concepts/supply-chain-security/about-dependabot-security-updates) |
| 22 | Add dependabot.yml | Create file `.github/dependabot.yml` with python (`pip`) and `github-actions` ecosystems | See [Dependabot configuration template](#dependabot-template) | SHOULD | Yes | [GitHub Docs: Keeping actions up to date with Dependabot](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/keeping-your-actions-up-to-date-with-dependabot) |
| 23 | Secret scanning | Settings → Code security & analysis → Secret scanning → Enable | Enabled | MUST | Yes (public repos) | [GitHub Docs: About secret scanning](https://docs.github.com/code-security/secret-scanning/about-secret-scanning) |
| 24 | Secret scanning push protection | Settings → Code security & analysis → Secret scanning → Enable push protection | Enabled | MUST | Yes (public repos) | [GitHub Changelog: Secret scanning coverage (Aug 2026)](https://github.blog/changelog/2026-08-07-secret-scanning-coverage-updates/) |
| 25 | CodeQL default setup | Settings → Code security & analysis → Code scanning → Set up default setup | Enabled; leave language auto-detection on | SHOULD | Yes (public repos) | [GitHub Docs: Configuring default setup](https://docs.github.com/en/code-security/code-scanning/enabling-code-scanning/configuring-default-setup-for-code-scanning) |
| 26 | Private vulnerability reporting | Settings → Code security & analysis → Private vulnerability reporting → Enable | Enabled | SHOULD | Yes (public repos) | [GitHub Blog: Private vulnerability reporting GA](https://github.blog/security/supply-chain-security/private-vulnerability-reporting-now-generally-available/) |
| 27 | Auto-delete head branches | Settings → General → Pull Requests → "Automatically delete head branches" | Enabled | NICE | Yes | [GitHub Docs: Managing automatic branch deletion](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-the-automatic-deletion-of-branches) |
| 28 | Restrict merge strategies | Settings → General → Pull Requests → "Allow merge commits" / "Allow squash merging" / "Allow rebase merging" | Disable "Allow merge commits"; keep "Squash" OR "Rebase" enabled | SHOULD | Yes | [GitHub Docs: About merge methods](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/configuring-commit-squashing-and-merge-options) |
| 29 | Require conversation resolution | Settings → Rules → Rulesets → [main ruleset] → Add rule → "Require conversation resolution" (if available) | Enabled | NICE | Yes | [GitHub Docs: Available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) |
| 30 | Enable Issues | Settings → General → Features → "Issues" | Enabled | MUST | Yes | Repository settings |
| 31 | Disable/enable Discussions | Settings → General → Features → "Discussions" | Enable (if fostering community) or Disable (if not; reduces clutter) | NICE | Yes | Repository settings |
| 32 | Disable Wiki (recommend) | Settings → General → Features → "Wiki" | Disabled (use README + docs/ folder instead) | NICE | Yes | Repository settings |
| 33 | Add repo description & topics | Repository page → About → Edit | Description: "Pure-Python dependency injection framework"; Topics: python, dependency-injection, di, library, open-source | NICE | Yes | Repository metadata |
| 34 | Add OpenSSF Scorecard action | Add to CI workflow (e.g., `.github/workflows/scorecard.yml`): See template below | Run weekly; publish results to GitHub | SHOULD | Yes | [ossf/scorecard-action](https://github.com/ossf/scorecard-action) |
| 35 | Add Scorecard badge to README | README.md → Add badge markdown | Badge markdown: `[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/edoardo-scarpaci/providify/badge)](https://securityscorecards.dev/viewer?uri=github.com/edoardo-scarpaci/providify)` | NICE | Yes | [ossf/scorecard-action](https://github.com/ossf/scorecard-action) |

---

### Dependabot Configuration Template (`.github/dependabot.yml`)

```yaml
version: 2
updates:
  # Python dependencies
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
    pull-request-branch-name:
      separator: "/"
    reviewers:
      - "edoardo-scarpaci"
    allow:
      - dependency-type: "direct"
      - dependency-type: "indirect"

  # GitHub Actions
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    pull-request-branch-name:
      separator: "/"
    reviewers:
      - "edoardo-scarpaci"
```

**Reference:** [Keeping actions up to date with Dependabot](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/keeping-your-actions-up-to-date-with-dependabot)

---

### OpenSSF Scorecard Action Template (`.github/workflows/scorecard.yml`)

```yaml
name: OpenSSF Scorecard

on:
  schedule:
    - cron: "0 0 * * 0"  # Weekly on Sunday at 00:00 UTC
  push:
    branches:
      - main

permissions:
  contents: read
  security-events: write

jobs:
  scorecard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: ossf/scorecard-action@v2.3.0
        with:
          results_file: results.sarif
          results_format: sarif
          publish_results: true
      - uses: github/codeql-action/upload-sarif@v2
        if: always()
        with:
          sarif_file: results.sarif
```

**Reference:** [ossf/scorecard-action README](https://github.com/ossf/scorecard-action)

---

## Summary

This checklist covers all GitHub repository UI settings and manual configuration needed to harden `providify` for a production v2.0.0 release. The 35 items are prioritized and grouped by UI section for efficient execution. All are free for public repositories on the free GitHub tier (except where noted as Enterprise-only). Expected outcome: **8–9/10 OpenSSF Scorecard**, alignment with **OSPS 2025 baseline**, and **credible production open-source signal**.

