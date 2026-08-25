"""Shared pytest fixtures for the providify test suite.

Every test module imports from this file automatically (pytest discovers it).
The two fixtures here handle the two isolation concerns:

1. ``container`` — a fresh DIContainer *instance* per test (no global state).
   Most tests use this to avoid touching the global singleton at all.
   Dogfooding (plan 007 step 12): this is a thin alias of the shipped
   ``di_container`` fixture (`providify/pytest_plugin.py`) so providify's own
   37+ test modules exercise the exact fixture consumers get — including its
   ``shutdown()`` teardown call.

2. ``reset_global`` — autouse fixture that wipes DIContainer._global before
   and after every test. Needed because a handful of tests exercise
   DIContainer.current() / DIContainer.scoped(), which write to the global.
   Without this, test ordering could affect outcomes.

   Kept local to providify's own suite, deliberately **not** promoted into
   `providify/pytest_plugin.py` — the plugin must have zero effect on
   consumer projects unless they explicitly request a fixture (plan 007
   §Non-goals), and an autouse fixture would violate that for every project
   that installs providify.

Thread safety:  ✅ Each test gets its own container instance.
                The global reset uses DIContainer.reset() which is lock-protected.
Async safety:   ✅ pytest-asyncio runs each async test in its own event loop.
"""

from __future__ import annotations

import pytest

from providify.container import DIContainer


@pytest.fixture
def container(di_container: DIContainer) -> DIContainer:
    """Return a fresh, empty DIContainer instance.

    DESIGN: thin alias of the shipped ``di_container`` fixture — not a
    separately-maintained ``DIContainer()`` construction — so the providify
    suite dogfoods its own pytest plugin (plan 007 step 12). ``di_container``
    already calls ``shutdown()`` in its teardown; any test that leaves a
    failing ``@PreDestroy`` behind now surfaces that at teardown instead of
    silently leaking, which is the intended, correct behaviour.

    Returns:
        An empty DIContainer with no bindings and no cached instances.
    """
    return di_container


@pytest.fixture(autouse=True)
def reset_global_container() -> None:
    """Reset DIContainer._global before and after every test.

    autouse=True — runs for every test without needing an explicit parameter.

    DESIGN: yield-based so both setup (before test) and teardown (after test)
    are guaranteed to run, even if the test raises an exception.

    Edge cases:
        - If a test calls DIContainer.current(), this fixture ensures the next
          test starts with a fresh global.
        - reset() is idempotent — safe to call even when _global is already None.
    """
    DIContainer.reset()
    yield
    DIContainer.reset()
