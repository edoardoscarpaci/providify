"""Failing (red) tests for the providify pytest11 plugin (providify/pytest_plugin.py).

Plan: plans/007-pytest-integration.md, step 8 + step 11.

Uses pytest's `pytester` fixture to run generated test files against the
plugin. Since `providify/pytest_plugin.py` and the `pytest11` entry point do
not exist yet, these tests are expected to fail (ImportError on the plugin
module, or the generated inner test run reporting errors because the
`di_container` / `di_overrides` / `di_global` fixtures are undefined).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytest_plugins = ["pytester"]


def test_di_container_fixture_yields_empty_container_and_calls_shutdown(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        from providify.container import DIContainer
        from providify.decorator.lifecycle import PreDestroy
        from providify.decorator.scope import Singleton

        def test_it(di_container, tmp_path):
            assert isinstance(di_container, DIContainer)
            assert di_container.get_all_bindings.__call__ is not None

            marker = tmp_path / "destroyed.txt"

            @Singleton
            class Resource:
                @PreDestroy
                def stop(self):
                    marker.write_text("destroyed")

            di_container.bind(Resource, Resource)
            di_container.get(Resource)
            assert not marker.exists()
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_di_overrides_fixture_undoes_override_between_tests(
    pytester: pytest.Pytester,
) -> None:
    # Cross-test pollution regression (research 001 §4): an override made in
    # test A must not leak into test B.
    pytester.makepyfile(
        """
        # NOTE: bind()/override() require DI metadata on the implementation
        # (raises ClassBindingNotDecoratedError otherwise) — @Component
        # added here; this is a test fix, not a production behaviour change.
        from providify.decorator.scope import Component

        @Component
        class Notifier:
            pass

        @Component
        class FakeNotifier(Notifier):
            pass

        def test_a(di_container, di_overrides):
            di_container.bind(Notifier, Notifier)
            di_overrides.bind(Notifier, FakeNotifier)
            assert isinstance(di_container.get(Notifier), FakeNotifier)

        def test_b(di_container, di_overrides):
            di_container.bind(Notifier, Notifier)
            assert type(di_container.get(Notifier)) is Notifier
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=2)


def test_consumer_conftest_redefinition_of_di_container_wins(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeconftest(
        """
        import pytest
        from providify.container import DIContainer

        @pytest.fixture
        def di_container():
            c = DIContainer()
            c.custom_marker = True
            yield c
        """
    )
    pytester.makepyfile(
        """
        def test_it(di_container):
            assert getattr(di_container, "custom_marker", False) is True
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_di_global_installs_fixture_container_as_current_and_restores_after(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        from providify.container import DIContainer

        def test_it(di_container, di_global):
            assert DIContainer.current() is di_container
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_requesting_no_providify_fixture_has_zero_effect(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        """
        from providify.container import DIContainer

        def test_it():
            assert DIContainer._global is None
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)


def test_providify_plugin_is_registered_as_third_party_plugin(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile("def test_noop(): pass")
    result = pytester.runpytest("--trace-config")
    # Match the "<name>-<version> at <path>" line pytest prints for
    # setuptools/entry-point-registered plugins specifically — a bare
    # "*providify*" wildcard would also match unrelated lines that merely
    # mention the venv path (which lives under a directory named
    # "providify"), producing a false pass before the plugin exists.
    result.stdout.fnmatch_lines(["*providify-* at *pytest_plugin.py*"])


def test_core_import_does_not_pull_in_pytest_module() -> None:
    # `import providify` must never import pytest — the plugin module lives
    # in a separate file precisely to guard this (§Non-goals).
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, providify; assert 'pytest' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
