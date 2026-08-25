# Research 001 — Pytest Plugin Patterns & Fixture-Based Container Overrides

Date: 2026-08-25 · Freshness matters: **yes** — pytest versions, plugin patterns, and fixture scoping best practices evolve; recommend revisit if pytest 9.x released or major dependency-injector versions appear.

## Question

For F4 (pytest-integration fixture-based container overrides in providify):

1. What is the current (2026) recommended way to ship a pytest plugin — `pytest11` entry point in pyproject.toml vs. just exposing fixtures for users to import? What do comparable libraries (FastAPI TestClient, python-dependency-injector, pytest-asyncio, pytest-mock) actually do?
2. What's the current best-practice pattern for cleanup fixtures — `yield` vs `addfinalizer`? Any pytest version constraints/deprecations?
3. How do similar DI/mocking libraries handle "override for testing" fixtures? Does python-dependency-injector expose a public pytest fixture for container overrides?
4. Any pitfalls with fixture scoping (function vs session) when overriding singletons/session-scoped bindings?

## Findings

### 1. Plugin Distribution: Entry Points vs. Direct Fixture Import

- **Entry Point (pytest11) for external distribution**: [Writing plugins — pytest documentation](https://docs.pytest.org/en/stable/how-to/writing_plugins.html) recommends entry points for plugins "intended for external use across multiple projects." Format: `[project.entry-points.pytest11] plugin_name = "module.to.plugin"` in pyproject.toml (pytest 8.x+, hatchling backend). — [pytest docs](https://docs.pytest.org/en/stable/how-to/writing_plugins.html) (current stable, pytest 8.x)

- **Real-world adoption**: pytest-asyncio registers `entry-points.pytest11.asyncio = "pytest_asyncio.plugin"` in pyproject.toml; pytest-mock similarly uses pytest11 entry point. — [pytest-asyncio GitHub](https://github.com/pytest-dev/pytest-asyncio/blob/main/pyproject.toml) (as of 2026-08)

- **Fixture-only approach for internal sharing**: The pytest docs also state `pytest_plugins` variable or direct conftest.py import are acceptable for "sharing fixtures within applications or even external applications without the need to create external plugins." — [pytest writing plugins guide](https://docs.pytest.org/en/stable/how-to/writing_plugins.html) (current)

- **FastAPI pattern**: FastAPI does NOT ship a pytest plugin; it exposes `TestClient` as a class users import directly and wrap in their own conftest fixtures. Most FastAPI testing docs show fixture patterns (not a plugin). — [FastAPI testing docs via Teclado & CodeSignal](https://www.teclado.com/fastapi-101/getting-started-with-fastapi-tests/) and [Testing FastAPI Applications (2026)](https://www.pyinns.com/python/web-development/fastapi-testing-pytest-testclient-python-2026) (2026 content)

- **Decision criteria**: Ship pytest11 entry point if providify users are external/PyPI consumers and want zero-config fixture availability. Use pytest_plugins variable (or docs-guided imports) if primarily for internal/framework use or if keeping test helpers separate from core library.

### 2. Fixture Cleanup: Yield vs. addfinalizer

- **Yield is the modern best practice (default recommendation)**: pytest documentation states `yield` fixtures should be the primary choice. Setup code goes before `yield`, teardown after. Cleanup runs in reverse order, and is **guaranteed even if the test raises an exception**. — [How to use fixtures - pytest documentation](https://docs.pytest.org/en/stable/how-to/fixtures.html) (pytest 8.x stable)

- **Why yield is preferred over addfinalizer**:
  - Simpler, more readable lexical scoping
  - Automatic reverse-order cleanup
  - Exception propagation is deterministic
  - Modern pytest best practices strongly prefer yield + context managers for "deterministic resource management" — [Ruff PT021 linting rule](https://github.com/m-burst/flake8-pytest-style/blob/master/docs/rules/PT021.md) (pytest style guide, 2025+)

- **addfinalizer use cases** (rare):
  - Factory-as-fixture pattern: when one fixture spawns multiple test-scoped resources needing independent cleanup
  - Multiple conditional cleanups where order matters, but this is an edge case — [Ruff pytest-fixture-finalizer-callback](https://astral.sh/ruff/rules/pytest-fixture-finalizer-callback/) (2026)

- **Pytest version constraint**: No deprecation of yield in pytest 8.x or planned for 9.x. Yield has been the standard since pytest 3.0+. Use yield without concern for version compatibility (providify requires Python 3.12+ and pytest >=8.0.0 per pyproject.toml).

### 3. Similar DI Libraries' Testing Fixtures

- **python-dependency-injector (dependency-injector 4.49+)**:
  - Exposes `Provider.override(provider_or_value)` and `Provider.reset_override()` methods
  - Does **NOT** publish an official public pytest fixture; users must implement their own fixture wrapping these methods
  - Common pattern: create a `@pytest.fixture` that calls `container.provider.override(mock)` in setup and `reset_override()` in teardown (yield-based)
  - Known issue: overrides may not reset if container is created in session-scoped fixture, causing test isolation bugs — [Issue #421 - ets-labs/python-dependency-injector](https://github.com/ets-labs/python-dependency-injector/issues/421) (unresolved, 2025+)

- **FastAPI dependency_overrides pattern**:
  - No official pytest plugin; relies on `app.dependency_overrides` dict (a plain dict mapping function → override)
  - Documentation shows fixture setup/teardown pattern: set overrides in fixture, clear in teardown via `yield`
  - **This is the closest "prior art"** to providify's planned fixture API — [FastAPI Testing (StackLesson 2026)](https://www.stacklesson.com/react-fastapi/fastapi-dependencies/ch28-lesson-05-dependency-overrides-and-testing/)

- **pytest-mock** (pytest-mock 3.6+):
  - Exposes `mocker` fixture (function-scoped)
  - Automatically handles cleanup after each test; no explicit reset needed
  - No session-scoped override fixture (by design, to avoid cross-test pollution)

- **Implication for providify F4**: No widely-adopted precedent for a pytest fixture wrapping DI container overrides. This is a greenfield design opportunity; closest pattern is FastAPI's dict-based overrides with yield fixtures.

### 4. Fixture Scoping Pitfalls & Container Overrides

- **ScopeMismatch error**: Occurs when a narrower-scoped fixture is used in a wider-scoped fixture. Example: `mocker` (function-scoped) cannot be used in a session-scoped fixture. — [Issue #638 - tortoise/tortoise-orm](https://github.com/tortoise/tortoise-orm/issues/638) and [pytest ScopeMismatch documentation](https://github.com/pytest-dev/pytest/issues/9235) (pytest 8.x)

- **Mutable fixture trap (session scope)**: A classic pitfall where a list/dict fixture modified by test A corrupts test B. Session-scoped mutable state is a common source of heisenbugs (pass locally, fail in CI). — [Unit Testing with pytest: Session Scope Causes Flaky CI](https://thecodeforge.io/python/unit-testing-pytest/) (2025+)

- **xdist + session-scoped fixtures**: If using pytest-xdist for parallel test runs, session-scoped fixtures run **once per worker**, not once globally. This breaks test isolation assumptions. — [Pytest Fixtures Scope Guide (QASkills.sh 2026)](https://qaskills.sh/blog/pytest-fixtures-scope-complete-guide)

- **Best practice for container overrides**:
  - Use **function-scoped** fixtures for binding overrides. Each test gets a fresh override state, preventing cross-test pollution.
  - Keep session-scoped fixtures only for truly expensive, stateless resources (e.g., database connection pool).
  - For providify: override fixture should be function-scoped by default; optional module/session scope only if explicitly requested by user.

- **Session-scoped singletons + function-scoped overrides**: A session-scoped binding (e.g., singleton) can be overridden by a function-scoped fixture **without ScopeMismatch**, because the override is a narrower concern. However, ensure cleanup via `yield` to reset the binding before the next test. — [Pytest scoping documentation](https://docs.pytest.org/en/stable/how-to/fixtures.html) (pytest 8.x)

## Options Compared

| Option | ✅ Strengths | ❌ Weaknesses | Evidence |
|---|---|---|---|
| **pytest11 entry point** | Zero-config for external users; auto-discovered by pytest; standard practice for distributed plugins | Adds maintenance burden if only internal use; slightly more complex shipping | [pytest writing plugins](https://docs.pytest.org/en/stable/how-to/writing_plugins.html); real-world: pytest-asyncio, pytest-mock |
| **Fixture-only (pytest_plugins or conftest)** | Simpler, no entry-point config; user controls import; easier to iterate/test | Requires end-user boilerplate or docs reading; not discoverable by pytest automatically | [pytest writing plugins](https://docs.pytest.org/en/stable/how-to/writing_plugins.html) (pytest_plugins section) |
| **yield fixtures** | Guaranteed cleanup even on exception; modern best practice; lexically clear; automatic reverse-order cleanup | Slightly less flexible for complex conditional cleanups (rare) | [pytest fixtures documentation](https://docs.pytest.org/en/stable/how-to/fixtures.html); Ruff PT021 linting rule |
| **addfinalizer** | Explicit control; suitable for factory patterns; multiple independent cleanup steps | Verbose; harder to read; exception propagation less predictable | [pytest fixtures documentation](https://docs.pytest.org/en/stable/how-to/fixtures.html) (addfinalizer section) |
| **Function-scoped override fixture** | Isolates overrides per test; prevents mutable-fixture trap; no xdist conflicts | Slightly more teardown overhead than session scope (negligible) | [pytest scoping guide](https://qaskills.sh/blog/pytest-fixtures-scope-complete-guide); python-dependency-injector issue #421 |

## Version/Compatibility Notes

- **pytest 8.x (current stable, required by providify)**: Full support for yield fixtures, pytest11 entry points, and `@pytest.mark.asyncio` (via pytest-asyncio).
- **pytest-asyncio 0.24.0+** (in providify dev deps): Officially registered pytest plugin via pytest11 entry point. No breaking changes for yield fixtures in 0.24.x.
- **python-dependency-injector 4.49.1** (comparable library, not a dep): Provider.override() API is stable; no deprecation notices as of 2025.
- **Hatchling build backend** (providify uses this): Correctly handles `[project.entry-points.pytest11]` in pyproject.toml as of hatchling 1.20+ (2024+).

No known deprecations or breaking changes planned for yield fixtures, pytest11 entry points, or function-scoped fixtures in pytest 9.x (not yet released; likely 2026-2027).

## Evidence Gaps

- **Specific pytest11 entry-point registration for pytest-mock**: Search results mention pytest-mock uses pytest11 but did not return pytest-mock's actual pyproject.toml. However, precedent from pytest-asyncio, pytest-env, pytest-html all confirm the `[project.entry-points.pytest11]` syntax is standard practice.
- **pytest 9.x planned changes**: Recommendation assumes pytest 9.x (if released by late 2026) does not introduce breaking changes to yield fixtures or entry points. This should be re-verified when pytest 9.0-beta is available.
- **Async fixture scope interactions**: Brief covers sync fixtures; async override fixtures (e.g., for async container setup) have less published guidance — worth a separate brief if F4 includes async fixture support.

## Librarian's Note

**What the sources indicate**: 

For providify F4, evidence strongly favours a **pytest11 entry-point plugin that exposes a yield-based fixture** (function-scoped by default). This mirrors FastAPI's dependency_overrides pattern + pytest best practices, and follows the proven pattern used by pytest-asyncio and pytest-mock. No prior art for a polished public DI-testing fixture means providify can establish a new convention. Function scoping is critical to prevent test isolation bugs seen in python-dependency-injector and session-scoped fixtures.

Users will expect a simple, discoverable fixture (not manual conftest imports), and the entry-point approach is the modern pytest standard for this. Yield is non-negotiable for guaranteed cleanup.

---

**Sources Cited:**
- [Writing plugins — pytest documentation](https://docs.pytest.org/en/stable/how-to/writing_plugins.html) (pytest 8.x stable)
- [How to use fixtures - pytest documentation](https://docs.pytest.org/en/stable/how-to/fixtures.html) (pytest 8.x stable)
- [pytest-asyncio GitHub pyproject.toml](https://github.com/pytest-dev/pytest-asyncio/blob/main/pyproject.toml) (2026-08)
- [FastAPI Testing with pytest (StackLesson 2026)](https://www.stacklesson.com/react-fastapi/fastapi-dependencies/ch28-lesson-05-dependency-overrides-and-testing/)
- [Provider overriding — Dependency Injector 4.48.3 documentation](https://python-dependency-injector.ets-labs.org/providers/overriding.html)
- [Issue #421 — python-dependency-injector](https://github.com/ets-labs/python-dependency-injector/issues/421) (session-scoped fixture override pitfall)
- [Pytest Fixtures Scope: The Complete Guide (2026)](https://qaskills.sh/blog/pytest-fixtures-scope-complete-guide)
- [Unit Testing with pytest: Session Scope Causes Flaky CI](https://thecodeforge.io/python/unit-testing-pytest/)
- [Ruff PT021 — pytest-fixture-finalizer-callback](https://astral.sh/ruff/rules/pytest-fixture-finalizer-callback/)
