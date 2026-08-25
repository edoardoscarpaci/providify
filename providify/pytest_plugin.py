"""providify's ``pytest11`` entry-point plugin: opt-in fixtures only.

Plan: `plans/007-pytest-integration.md`, step 9.

DESIGN — no autouse fixtures, no ``pytest_configure`` side effects:

    This module is imported by pytest at session start in **every** project
    that has providify installed (that is what a `pytest11` entry point
    means — see `pyproject.toml`'s ``[project.entry-points.pytest11]``
    table), including projects that never request any of these fixtures.
    The invariant that bounds the blast radius of that: this module defines
    fixtures and *nothing else* — no autouse fixtures, no hooks, no
    `pytest_configure`, no import-time work beyond `import providify`. A
    project that never asks for `di_container`/`di_overrides`/`di_global`/
    `di_acontainer` must see zero behavioural difference from providify not
    being installed at all (`tests/test_pytest_plugin.py`'s
    "requesting no providify fixture" test locks this in).

All four fixtures are function-scoped (pytest's default — stated explicitly
below) and yield-based (cleanup still runs when the test raises). Research
001 §4 documents concrete failure modes for session-scoped override
fixtures (`python-dependency-injector` issue #421, xdist "once per worker"
semantics) — function scope sidesteps both.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

import pytest

from .container import DIContainer
from .testing import ContainerOverrides

__all__: list[str] = []  # Deliberately nothing exported — see class docstring


@pytest.fixture
def di_container() -> Generator[DIContainer, None, None]:
    """Yield a fresh, empty :class:`DIContainer` and shut it down after the test.

    Function-scoped (pytest's default) — a new container per test, matching
    research 001 §4's guidance against session-scoped override fixtures.

    A consumer with an existing app container should override this fixture
    in their own conftest, e.g.::

        @pytest.fixture
        def di_container(app_container):
            return app_container.copy()  # isolated per test, no re-scan

    Yields:
        A new ``DIContainer()``.

    Edge cases:
        - A cached singleton has an **async** `@PreDestroy` → `shutdown()`
          raises `RuntimeError` telling you to use `ashutdown()` instead —
          use `di_acontainer` for that case.
        - A `@PreDestroy` raises → the exception surfaces as a fixture
          teardown error, which is correct: a leaking teardown is a real
          defect the test suite should not silently swallow.
    """
    container = DIContainer()
    yield container
    container.shutdown()


@pytest.fixture
async def di_acontainer() -> AsyncGenerator[DIContainer, None]:
    """Async mirror of :func:`di_container` — yields and awaits ``ashutdown()``.

    Requires an async-test runner such as pytest-asyncio
    (``asyncio_mode = "auto"``) or anyio to actually drive this coroutine;
    with neither installed and the fixture unrequested, nothing happens (it
    is simply never evaluated) — consistent with this module's "zero effect
    until requested" invariant.

    Function-scoped (pytest's default), yield-based.

    Yields:
        A new ``DIContainer()``.
    """
    container = DIContainer()
    yield container
    await container.ashutdown()


@pytest.fixture
def di_overrides(
    di_container: DIContainer,
) -> Generator[ContainerOverrides, None, None]:
    """Yield a :class:`~providify.testing.ContainerOverrides` bound to ``di_container``.

    Function-scoped (pytest's default) — overrides made in one test never
    leak into the next, since both the ``ContainerOverrides`` snapshot and
    the underlying container are recreated per test.

    Yields:
        A ``ContainerOverrides(di_container)``, already entered as a context
        manager — overrides are undone automatically at teardown even if
        the test raises.
    """
    with ContainerOverrides(di_container) as overrides:
        yield overrides


@pytest.fixture
def di_global(di_container: DIContainer) -> Generator[DIContainer, None, None]:
    """Install ``di_container`` as ``DIContainer.current()`` for the test, then restore.

    Opt-in only — **not** autouse (see module docstring's §Non-goals). Tests
    for code that resolves via ``DIContainer.current()`` (rather than
    receiving a container by dependency injection) need this fixture to
    point that global at the per-test container.

    Delegates to ``DIContainer.scoped(container)`` (plan 007), which adopts
    ``di_container`` as the global for the block and restores the previous
    global on exit — including when the test raises. Adopting does **not**
    shut the container down; `di_container`'s own teardown (`shutdown()`)
    still does that.

    Function-scoped (pytest's default), yield-based.

    Yields:
        ``di_container`` itself, for convenience (it is also reachable via
        ``DIContainer.current()`` for the fixture's duration).
    """
    with DIContainer.scoped(di_container) as container:
        yield container
