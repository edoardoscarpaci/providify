# Contributing to providify

Thanks for considering a contribution. providify is a solo-maintained project;
this document exists so contributions land smoothly on the first try.

## Getting set up

```bash
uv sync
make install
```

Requires Python 3.12+. `uv sync` installs the project plus the `dev`
dependency group (pytest, pytest-asyncio, ruff, PyYAML, pydantic).

## Running the checks

```bash
make test           # uv run pytest
make lint            # uv run ruff check .
make format-check    # uv run ruff format --check .
```

**CI gates on ruff (lint + format) and pytest only — there is no type-check
gate.** No mypy, pyright, or basedpyright is configured today. That is a
deliberate, documented decision for this release, not an oversight; adopting
a type checker means triaging findings across the whole public API and is
tracked as separate future work, not a contribution requirement.

## Coding standards

- Ruff configuration lives in `pyproject.toml` (`[tool.ruff]` /
  `[tool.ruff.lint]`): line-length 100, target `py312`, rule sets `E`, `F`,
  `I`, `UP`, double-quote strings.
- Public API (anything exported from `providify/__init__.py`) needs a
  complete docstring: `Args` / `Returns` / `Raises`, plus an `Example` where
  it helps.
- Prefer minimal abstraction. Comments explain *why* a design choice was
  made, not what the code already says.

## Tests

- Every behaviour change needs a test under `tests/`.
- pytest runs with `asyncio_mode = "auto"` (`pyproject.toml`), so async tests
  need no `@pytest.mark.asyncio` marker — just write `async def test_...`.

## Pull requests

- Branch off `dev`.
- One logical change per PR.
- Update `CHANGELOG.md` under `## [Unreleased]` for any user-visible change.
- All CI jobs must be green before merge.

## Versioning and deprecation policy

providify follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
and [PEP 440](https://peps.python.org/pep-0440/).

From **2.0.0**, the public API — everything exported from
`providify/__init__.py` (i.e. everything in its `__all__`) — is committed.
Anything not exported from `providify/__init__.py` is internal and may
change without notice, including in a patch release.

When a piece of public API needs to be deprecated:

1. It keeps working but emits a `DeprecationWarning` naming its replacement.
2. It is listed under a `### Deprecated` heading in `CHANGELOG.md`, with the
   replacement to migrate to.
3. It stays in that deprecated state for **at least two minor releases or
   six months, whichever is longer**, before removal.
4. It is only ever removed in a subsequent **major** release.

The exact window (two minors / six months) is a project judgement call —
Python packaging has no single universal standard for this — and is
documented here so it reads as policy, not surprise.

## Trusted GitHub Actions

This repository's workflows only use a small, deliberately short list of
third-party actions, kept short so every dependency is easy to reason about:
`actions/checkout`, `astral-sh/setup-uv`, `pypa/gh-action-pypi-publish`,
`actions/upload-artifact`, `actions/download-artifact`,
`ossf/scorecard-action`, `github/codeql-action`. Adding a new action to any
workflow should be treated as adding a new trusted party to this list, not
just a YAML edit.
