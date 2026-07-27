"""Regression tests for quoted (forward-reference) @Provider return annotations.

Bug report (downstream consumer): a single ``@Provider`` whose return
annotation is a quoted forward reference — ``def f() -> "Foo"`` — silently
poisoned dependency injection for the ENTIRE container.  Every provider and
constructor was then invoked with zero injected kwargs, surfacing as a
nonsensical ``TypeError: ... missing N required positional arguments`` on a
completely unrelated binding.

The failure chain (all three links covered here):

1. ``ProviderBinding.__init__`` — under PEP-563 the annotation ``-> "Foo"``
   stringifies to the *literal* ``"'Foo'"``.  The ``eval`` fallback therefore
   returns the plain ``str`` ``'Foo'``, which was registered as the binding
   interface without complaint.
2. ``DIContainer._build_localns`` — ``localns[b.interface.__name__]`` raised
   ``AttributeError: 'str' object has no attribute '__name__'`` for that one
   binding, aborting construction of the CONTAINER-WIDE namespace.
3. ``_collect_kwargs_sync`` / ``_collect_kwargs_async`` — the bare
   ``except Exception: hints = {}`` swallowed that AttributeError, so every
   resolution in the container silently lost all of its type hints.

Correct behaviour: a quoted return annotation resolves to the real type when
resolution is possible, and fails loudly at registration time when it is not.
Injection for all OTHER bindings must be unaffected either way.
"""

from __future__ import annotations

import logging

import pytest

from providify.binding import ProviderBinding
from providify.container import DIContainer
from providify.decorator.scope import Provider

# ─────────────────────────────────────────────────────────────────
#  Module-level domain types
#
#  These live at module level so they ARE present in a provider
#  function's __globals__ — that is what makes a quoted forward
#  reference genuinely resolvable.
# ─────────────────────────────────────────────────────────────────


class ProfilingSettings:
    """Stand-in for the downstream type that was quoted in its provider."""


class TracerProvider:
    """Unrelated dependency — the injection victim in the original report."""


class OtelConfiguration:
    """Unrelated consumer whose provider needs an injected TracerProvider."""

    def __init__(self, tracer: TracerProvider) -> None:
        self.tracer = tracer


class Repository[T]:
    """Generic interface — a parameterised alias is a legal binding interface."""


class User:
    """Type argument used in the generic-alias annotation test."""


NOT_A_TYPE = 42
"""Module-level non-type: resolves fine, but is unusable as an interface."""


class ModuleLevelOptionalDep:
    """A defaulted, optional provider parameter type.

    Deliberately at MODULE level (not function-local) — release A's Tier-2
    validators (``_check_provider_scope_violation``) now run on every
    provider binding on the container's first ``get()``/``aget()`` call
    (via the implicit ``validate_bindings()``), and raise
    ``AnnotationResolutionError`` if they cannot resolve a parameter's
    annotation, even for a param that would end up unbound and defaulted.
    A function-local class here would be unresolvable via ``__globals__``
    or ``localns`` and would make an unrelated end-to-end test fail on
    validation before ever reaching the behaviour under test. Bucket (b)
    per Plan 001 Step 18: move the offending type to module level.
    """


# ─────────────────────────────────────────────────────────────────
#  Link 1 — ProviderBinding must not register a str as an interface
# ─────────────────────────────────────────────────────────────────


class TestQuotedReturnAnnotation:
    """The return annotation of a @Provider must always resolve to a type."""

    def test_regression_quoted_return_annotation_resolves_to_type(self) -> None:
        """User reports: `-> "Foo"` registers the *string* 'Foo' as the interface.

        Correct behaviour: the interface must be the ``ProfilingSettings``
        class, because a binding interface is a type — a ``str`` can never be
        matched by ``container.get()`` and corrupts every code path that
        expects ``interface.__name__``.

        The provider below declares a LOCALLY defined parameter type, which is
        absent from ``fn.__globals__``.  That makes ``get_type_hints(fn)`` raise
        and forces ``ProviderBinding`` down its ``eval`` fallback — the exact
        path that produced the ``str`` interface.
        """

        class LocalDep:
            """Locally defined → absent from fn.__globals__ by construction."""

        @Provider(singleton=True)
        def profiling_settings(dep: LocalDep) -> "ProfilingSettings":  # noqa: UP037
            assert dep is not None
            return ProfilingSettings()

        binding = ProviderBinding(profiling_settings)

        assert binding.interface is ProfilingSettings

    def test_regression_unresolvable_quoted_return_raises_at_registration(
        self,
    ) -> None:
        """An unresolvable forward reference must fail fast and name the culprit.

        Correct behaviour: raise at registration time.  Registering the
        unresolved ``str`` defers the failure to an unrelated binding at
        resolution time, which is exactly the debugging nightmare reported.
        """

        class LocalProduct:
            """Not present in fn.__globals__ → genuinely unresolvable."""

        @Provider(singleton=True)
        def local_product() -> "LocalProduct":  # noqa: UP037
            return LocalProduct()

        with pytest.raises(TypeError) as exc_info:
            ProviderBinding(local_product)

        message = str(exc_info.value)
        assert "local_product" in message
        assert "LocalProduct" in message

    def test_quoted_return_annotation_end_to_end(self, container: DIContainer) -> None:
        """A resolvable quoted return annotation is usable through the container."""

        @Provider(singleton=True)
        def profiling_settings(
            dep: ModuleLevelOptionalDep | None = None,
        ) -> "ProfilingSettings":  # noqa: UP037
            return ProfilingSettings()

        container.provide(profiling_settings)

        assert isinstance(container.get(ProfilingSettings), ProfilingSettings)

    async def test_quoted_return_annotation_async_provider(
        self, container: DIContainer
    ) -> None:
        """Async providers take the same registration path — cover it explicitly."""

        @Provider(singleton=True)
        async def profiling_settings(
            dep: ModuleLevelOptionalDep | None = None,
        ) -> "ProfilingSettings":  # noqa: UP037
            return ProfilingSettings()

        container.provide(profiling_settings)

        assert isinstance(await container.aget(ProfilingSettings), ProfilingSettings)

    def test_string_generic_alias_return_annotation_is_accepted(self) -> None:
        """A parameterised alias is a legal interface — do not reject it.

        Guards the fix itself: the new "must be a type" check has to keep
        accepting ``Repository[User]``, whose ``isinstance(_, type)`` is False.
        """

        class LocalDep:
            pass

        @Provider(singleton=True)
        def user_repo(dep: LocalDep | None = None) -> Repository[User]:
            return Repository[User]()

        binding = ProviderBinding(user_repo)

        assert binding.interface == Repository[User]

    def test_return_annotation_resolving_to_non_type_raises(self) -> None:
        """A resolvable-but-not-a-type annotation must be rejected, not registered."""

        class LocalDep:
            pass

        @Provider(singleton=True)
        def bogus(dep: LocalDep) -> NOT_A_TYPE:  # type: ignore[valid-type]
            return object()

        with pytest.raises(TypeError, match="not a type"):
            ProviderBinding(bogus)

    def test_unresolvable_quoted_return_raises_through_container_provide(
        self, container: DIContainer
    ) -> None:
        """The failure must surface from the public API, not only from Binding()."""

        class LocalProduct:
            pass

        @Provider(singleton=True)
        def local_product() -> "LocalProduct":  # noqa: UP037
            return LocalProduct()

        with pytest.raises(TypeError, match="local_product"):
            container.provide(local_product)

        # And the container is left clean — no half-registered poison binding.
        assert not [b for b in container._bindings if isinstance(b.interface, str)]


# ─────────────────────────────────────────────────────────────────
#  Link 2 — one malformed binding must never break the whole localns
# ─────────────────────────────────────────────────────────────────


def _add_malformed_binding(container: DIContainer) -> None:
    """Append a binding whose interface is a ``str`` instead of a type.

    Reproduces the corrupted state the original bug produced, without relying
    on the (now fixed) registration path that created it.  Registration is
    bypassed deliberately: this test asserts that the container *tolerates*
    such a binding, which is the second line of defence.
    """

    @Provider(singleton=True)
    def profiling_settings() -> ProfilingSettings:
        return ProfilingSettings()

    bad = ProviderBinding(profiling_settings)
    bad.interface = "ProfilingSettings"  # type: ignore[assignment]
    container._bindings.append(bad)
    container._localns_cache = None


class TestLocalnsIsolation:
    """``_build_localns`` is container-wide — it must tolerate one bad binding."""

    def test_regression_malformed_interface_does_not_break_localns(
        self, container: DIContainer
    ) -> None:
        """User reports: one str interface raises AttributeError inside _build_localns.

        Correct behaviour: the malformed binding is skipped, every other
        binding still contributes its name, because the namespace is shared by
        EVERY resolution in the container.
        """

        @Provider(singleton=True)
        def tracer_provider() -> TracerProvider:
            return TracerProvider()

        container.provide(tracer_provider)

        _add_malformed_binding(container)

        localns = container._build_localns()

        assert localns["TracerProvider"] is TracerProvider

    def test_regression_malformed_binding_does_not_break_other_injection(
        self, container: DIContainer
    ) -> None:
        """The original symptom: an unrelated provider loses its injected kwargs."""

        @Provider(singleton=True)
        def tracer_provider() -> TracerProvider:
            return TracerProvider()

        @Provider(singleton=True)
        def otel_configuration(tracer: TracerProvider) -> OtelConfiguration:
            return OtelConfiguration(tracer)

        container.provide(tracer_provider)
        container.provide(otel_configuration)

        _add_malformed_binding(container)

        config = container.get(OtelConfiguration)

        assert isinstance(config.tracer, TracerProvider)

    async def test_regression_malformed_binding_does_not_break_async_injection(
        self, container: DIContainer
    ) -> None:
        """Async mirror — ``_collect_kwargs_async`` shares the same namespace."""

        @Provider(singleton=True)
        def tracer_provider() -> TracerProvider:
            return TracerProvider()

        @Provider(singleton=True)
        async def otel_configuration(tracer: TracerProvider) -> OtelConfiguration:
            return OtelConfiguration(tracer)

        container.provide(tracer_provider)
        container.provide(otel_configuration)

        _add_malformed_binding(container)

        config = await container.aget(OtelConfiguration)

        assert isinstance(config.tracer, TracerProvider)


# ─────────────────────────────────────────────────────────────────
#  Link 3 — losing ALL hints must never be silent
# ─────────────────────────────────────────────────────────────────


def _pre_validate(container: DIContainer) -> None:
    """Run ``validate_bindings()`` to completion (unboomed) and mark it done.

    WHY this exists — do not delete as "redundant": ``get()``/``aget()`` call
    ``validate_bindings()`` themselves on their first invocation
    (``container.py:799-801``), which walks EVERY registered binding, not
    just the one under test. If a test installs a ``get_type_hints`` boom and
    THEN calls ``container.get(Target)`` on a fresh (never-validated)
    container, that implicit validation runs Tier 2 (the now-strict
    ``_check_provider_scope_violation``) against the SAME boomed target
    *before* the Tier-1 code the test actually wants to exercise ever runs —
    so the boom is intercepted by the wrong tier. Running validation to
    completion here, before the boom is installed, isolates the tier under
    test. ``validate_bindings()`` itself does not set ``_validated`` (only
    ``get()``/``aget()``/``get_all()``/``aget_all()`` do, after calling it)
    so it must be set explicitly afterwards.
    """
    container.validate_bindings()
    container._validated = True


def _boom_for(monkeypatch: pytest.MonkeyPatch, target: object, exc: Exception) -> None:
    """Make ``get_type_hints`` raise *exc* for *target* only, real behaviour otherwise.

    A blanket monkeypatch (patching every ``get_type_hints`` call
    unconditionally) is too broad here: it also fires inside Tier 2's
    validators for bindings that aren't the one under test, making the test
    observe validation failures instead of the injection-tier behaviour it
    means to exercise. Mirrors ``tests/test_annotation_resolution.py``'s
    helper of the same name.
    """
    import providify.container as container_module

    real = container_module.get_type_hints

    def fake(t: object, *args: object, **kwargs: object) -> dict[str, object]:
        if t is target:
            raise exc
        return real(t, *args, **kwargs)

    monkeypatch.setattr(container_module, "get_type_hints", fake)


class TestHintFailureIsNotSilent:
    """A container-wide loss of injection must be loud, never silent."""

    def test_regression_unexpected_hint_error_propagates_sync(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User reports: an AttributeError inside hint resolution vanished.

        Correct behaviour: anything other than an unresolvable-name failure is
        a genuine bug and must propagate — swallowing it turns a precise error
        into a bogus "missing required positional arguments" three layers away.
        """

        @Provider(singleton=True)
        def tracer_provider() -> TracerProvider:
            return TracerProvider()

        @Provider(singleton=True)
        def otel_configuration(tracer: TracerProvider) -> OtelConfiguration:
            return OtelConfiguration(tracer)

        container.provide(tracer_provider)
        container.provide(otel_configuration)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            otel_configuration,
            AttributeError("'str' object has no attribute '__name__'"),
        )

        with pytest.raises(AttributeError, match="__name__"):
            container.get(OtelConfiguration)

    async def test_regression_unexpected_hint_error_propagates_async(
        self, container: DIContainer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Async mirror of the propagation guarantee."""

        @Provider(singleton=True)
        def tracer_provider() -> TracerProvider:
            return TracerProvider()

        @Provider(singleton=True)
        async def otel_configuration(tracer: TracerProvider) -> OtelConfiguration:
            return OtelConfiguration(tracer)

        container.provide(tracer_provider)
        container.provide(otel_configuration)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            otel_configuration,
            AttributeError("'str' object has no attribute '__name__'"),
        )

        with pytest.raises(AttributeError, match="__name__"):
            await container.aget(OtelConfiguration)

    def test_unresolvable_name_is_tolerated_but_logged(
        self,
        container: DIContainer,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The legitimate case (unresolvable local annotation) stays non-fatal.

        It must still leave a trace: a silent loss of every injected kwarg is
        undebuggable from the resulting TypeError.
        """

        @Provider(singleton=True)
        def tracer_provider(unbound: object | None = None) -> TracerProvider:
            return TracerProvider()

        container.provide(tracer_provider)
        _pre_validate(container)  # isolate Tier 1 — see _pre_validate docstring

        _boom_for(
            monkeypatch,
            tracer_provider,
            NameError("name 'LocallyDefined' is not defined"),
        )

        with caplog.at_level(logging.WARNING, logger="providify.container"):
            assert isinstance(container.get(TracerProvider), TracerProvider)

        assert any("tracer_provider" in record.message for record in caplog.records)
