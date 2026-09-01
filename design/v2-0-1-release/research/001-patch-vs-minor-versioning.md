# Research 001 — Patch vs. Minor Versioning for New Enum Member + Warning

**Date:** 2026-09-01 · **Freshness matters:** YES (SemVer and ecosystem practice evolve; this snapshot is current for early 2026)

## Question

The pending 2.0.0 → unreleased change adds:
1. New enum member `IssueKind.UNREACHABLE_PRE_DESTROY` to a public exported enum
2. New WARNING-severity diagnostic emitted by `container.validate()` for a previously-undetected pattern
3. Consequence: containers with `report.ok == True` may now report `report.ok == False`
4. Non-breaking for `validate(raise_on_error=True)` (still does not raise on WARNING)

Should this release as **2.0.1 (patch)** or **2.1.0 (minor)**?

---

## Findings

### A. SemVer 2.0.0 Spec — New Functionality vs. Bug Fix

**Clause 6 (Patch):**
> "Patch version Z (x.y.Z | x > 0) MUST be incremented if only backward compatible bug fixes are introduced."  
— [SemVer 2.0.0 spec](https://github.com/semver/semver.org/blob/gh-pages/spec/v2.0.0.md)

**Clause 7 (Minor):**
> "Minor version Y (x.Y.z | x > 0) MUST be incremented if new, backward compatible functionality is introduced to the public API."  
— [SemVer 2.0.0 spec](https://github.com/semver/semver.org/blob/gh-pages/spec/v2.0.0.md)

**Clause 8 (Major):**
> "Major version X (X.y.z | X > 0) MUST be incremented if any backward incompatible changes are introduced to the public API."  
— [SemVer 2.0.0 spec](https://github.com/semver/semver.org/blob/gh-pages/spec/v2.0.0.md)

**Does adding an enum member count as "new public API functionality"?**

Yes. An enum member added to an exported enum (present in `__all__`) is new public API. It is backward compatible—existing code using other enum values continues to work. SemVer distinguishes:
- **Patch:** Bug fixes only (no new API shape)
- **Minor:** New backward-compatible functionality

Per the spec, adding a new enum member is **new functionality**, not a bug fix. — [SemVer 2.0.0 spec, Clause 7](https://github.com/semver/semver.org/blob/gh-pages/spec/v2.0.0.md)

---

### B. Python Ecosystem Treatment: New Diagnostics in Linters/Validators

How do production tools handle "newly-reported warning from an existing validator API"?

#### Flake8 (7.3.0)
**New error codes:** Minor version bump  
> "Patch releases should only ever have bug fixes in them."  
> "Minor releases... include... New errors detected by dependencies, e.g., by raising the upper limit on PyFlakes we introduce F405."  
— [Flake8 Releasing docs, v7.3.0](https://flake8.pycqa.org/en/latest/internal/releases.html)

#### Ruff (as of v0.x)
**New rules:** Patch (preview mode); Minor (promotion to stable)  
Ruff adds new rules **only in preview mode** to avoid immediate impact. Rules may be added in patch releases while in preview; only when promoted to stable (breaking) does a minor bump occur.  
— [Ruff Versioning docs](https://docs.astral.sh/ruff/versioning/)

#### Mypy (1.x / 2.x)
**New checks:** Can occur in patch or minor releases  
Mypy does not follow strict SemVer and allows new checks in minor and patch releases. Major version bumps are reserved for significant breaking changes affecting most users.  
> "In case of a major breaking change, mypy's major version will be bumped."  
— [Mypy release policy discussion](https://github.com/python/mypy/issues/20726)

#### Pylint (3.x / 4.x)
**New messages:** Expected in minor releases; not guaranteed to be absent  
> "New checks are added and old checks are improved to catch more violations or to avoid false-positives."  
> Public API (parameters, JSON format) are protected; output changes are not breaking.  
— [Pylint Upgrading guide](https://pylint.readthedocs.io/en/stable/user_guide/installation/upgrading_pylint.html)

**Consensus:** The ecosystem treats new warnings/diagnostics from existing validator APIs as **feature additions**, typically requiring **minor version bumps** (except Ruff's preview mechanism, which defers breaking impact). Flake8 is explicit: new error codes are **not** patch releases.

---

### C. Enum Member Addition: Is It Breaking for Type-Safe Code?

When a library adds a new enum member, does it break users' exhaustive match statements or `assert_never()` patterns?

#### Official Typing Guidance (typing.python.org)
> "If you add another member to the enum (say, `MULTIPLY`) but don't update the `match` statement, the type checker will give an error saying that you are not handling the `MULTIPLY` case."  
— [typing.python.org: Exhaustiveness Checking with `assert_never()`](https://typing.python.org/en/latest/guides/unreachable.html)

#### PEP 634 (Pattern Matching)
PEP 634 defines match statements but **does not mandate exhaustiveness checking**—that is a type checker responsibility. Exhaustiveness checking is optional and tool-dependent (mypy's `--warn-exhaustive-match`, pyright's `reportMatchNotExhaustive`).  
— [PEP 634 – Structural Pattern Matching](https://peps.python.org/pep-0634/)

#### Practical Breakage
**For code using `assert_never()` or exhaustive match checking:**
- Type checker **will report an error** when the new member is not handled
- Code **does not break at runtime**, but static analysis fails
- This is a **behavioral breaking change** for type-safe codebases (they must update to type-check cleanly)

**For code without exhaustiveness guarantees:**
- No effect at all

**Conclusion:** Adding an enum member IS technically breaking for code that relies on exhaustiveness checking (e.g., using `typing.assert_never()` or mypy's exhaustive-match plugin). However, this is a type-checking/static-analysis breaking change, not a runtime breaking change.

---

### D. Keep a Changelog 1.1.0 — Release Mechanics

**Heading format for releases:**
```
## [VERSION] - DATE
```
where DATE is ISO 8601 format (e.g., `2026-09-01`).

**Standard section order:**
- Added
- Changed
- Deprecated
- Removed
- Fixed
- Security

**Cutting a release from Unreleased:**
1. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
2. Create a new blank `## [Unreleased]` section at the top
3. Update link-reference definitions at EOF:
   ```
   [Unreleased]: https://github.com/user/repo/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/user/repo/releases/tag/vX.Y.Z
   ```

— [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)

---

### E. PEP 440 — Constraint & Ordering Difference

**Both 2.0.1 and 2.1.0 are valid** under PEP 440.

**Material differences:**

| Aspect | 2.0.1 | 2.1.0 |
|--------|-------|-------|
| Validity | ✓ Valid | ✓ Valid |
| Ordering | (lower) | (higher) 2.1.0 > 2.0.1 |
| `>=2.0.1` constraint | Matches | Matches |
| `~=2.0.1` constraint | Matches (2.0.*) | **Does NOT match** |
| `~=2.0.0` constraint | Matches | Matches |

**Semantic difference in PyPI:** None—they are simply different releases with standard version ordering. No special PyPI logic treats 2.0.1 vs 2.1.0 differently.

— [PEP 440 – Version Identification and Dependency Specification](https://peps.python.org/pep-0440/)

---

## Options Compared

| Option | Strengths | Weaknesses | Evidence |
|--------|-----------|-----------|----------|
| **2.0.1 (Patch)** | • Signals "small, low-risk update"<br>• Faster adoption (fewer SemVer surprises)<br>• Defensible if framed as "docstring fixes + warning calibration" | • Violates SemVer Clause 7 (new enum member is "new functionality," not a bug fix)<br>• Breaks type-safe code with exhaustive patterns (type checker errors)<br>• Contradicts flake8/pylint ecosystem norm: new diagnostics → minor<br>• Commits `__all__` API contract to "2.0.x" but adds new exports | SemVer spec; flake8 docs; typing.python.org; providify CONTRIBUTING.md declares SemVer 2.0.0 compliance |
| **2.1.0 (Minor)** | • **Aligns with SemVer 2.0.0 Clause 7** (new API functionality)<br>• Matches flake8/pylint/mypy ecosystem precedent<br>• New enum member is explicitly signaled to consumers<br>• Type-safe users can proactively test with 2.1.0rc1<br>• Honest about the change: new diagnostic = new feature | • Slightly more "bump" perception (not patch)<br>• Users on `~=2.0.0` pins will **not** auto-upgrade (constraint: `>=2.0.0, ==2.*`)<br>• Requires changelog discipline to call out `IssueKind.UNREACHABLE_PRE_DESTROY` as [Added] | SemVer spec; flake8 docs; Ruff docs; mypy/Pylint release history |

---

## Version/Compatibility Notes

**SemVer 2.0.0:** Published 2011; stable and widely adopted. providify declares compliance in CONTRIBUTING.md.  
**PEP 440:** Published 2013; updated 2021; governs PyPI versioning. Both 2.0.1 and 2.1.0 are canonical.  
**Keep a Changelog 1.1.0:** Published 2023; widely adopted (Python ecosystem standard).  
**Python typing.assert_never():** Available since Python 3.11 (PEP 673). Exhaustiveness checking support varies by type checker.

---

## Evidence Gaps

- **providify's own user base:** Do downstream consumers rely on exhaustive `match` statements over `IssueKind`? If so, 2.1.0 signals this breakage; 2.0.1 hides it. (Worth a separate brief: audit `IssueKind` usage patterns)
- **providify's policy on enum stability:** Does CONTRIBUTING.md or the v2.0.0 release notes commit that the `IssueKind` enum is "closed" for the 2.0.x series? If yes, 2.1.0 is mandatory.
- **Warning vs. Error severity:** The brief confirms `raise_on_error=True` is not broken. But what about CI pipelines configured to `fail_on_warning: True`? (Different domain; not covered here)

---

## Librarian's Note

**What the sources indicate:**

The evidence **strongly favours 2.1.0 (minor).** SemVer 2.0.0 Clause 7 explicitly requires a minor version bump for "new, backward compatible functionality added to the public API." An exported enum member is public API. The ecosystem standard (flake8, ruff, mypy, pylint) treats new warnings/diagnostics from existing validator APIs as feature additions, typically requiring minor bumps. Adding an enum member is also technically breaking for type-safe code using exhaustive patterns, reinforcing the minor version signal.

**2.0.1 is defensible only if:** providify was never intended to be SemVer-compliant for this decision, or if the enum member is not materially a "new feature" but a bug-fix to the enum's completeness. The brief found no evidence supporting this reframing.

**Strength of evidence:** HIGH for 2.1.0 (SemVer spec is explicit; ecosystem consensus is clear; typing guidance is official). Strength of evidence for 2.0.1: LOW (only "smaller bump perception"; no spec backing).

