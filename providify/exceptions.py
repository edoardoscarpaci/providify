from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .metadata import LiveInjectionViolation, ScopeLeak

if TYPE_CHECKING:
    # Only for the type annotation on ContainerValidationError.__init__ —
    # `from __future__ import annotations` makes this a string at runtime,
    # so importing under TYPE_CHECKING avoids a real circular import:
    # validation.py never imports exceptions.py, but guard anyway so this
    # module's import graph stays acyclic even if that ever changes.
    from .validation import ValidationReport


class providifyError(Exception):
    """Base class for all errors in the providify framework."""

    pass


class BindingError(providifyError):
    """Base class for all binding-related errors."""

    pass


class ProviderBindingNotDecoratedError(BindingError):
    """Raised when a provider function is missing the required @Provider decorator."""

    def __init__(self, provider_fn: Callable[..., Any]):
        super().__init__(
            f"Provider function {provider_fn.__name__} is missing required @Provider decorator."
        )


class ClassBindingNotDecoratedError(BindingError):
    """Raised when a class provider is not decorated with @Component or @Singleton."""

    def __init__(self, cls: type):
        super().__init__(
            f"Class {cls.__name__} is not a DI component. Missing @Component or @Singleton decorator?"
        )


class NotDecoratedError(BindingError):
    """Raised when a class or function is not decorated with the required DI decorator."""

    def __init__(self, obj: Any):
        super().__init__(f"{obj} is not decorated with the required DI decorator.")


class ProviderAlreadyDecorated(providifyError):
    """Raised when a provider function is decorated more than once."""

    def __init__(self, fn: Callable[..., Any]):
        super().__init__(f"'{fn.__name__}' is already decorated with @Provider.")


class ClassAlreadyDecorated(providifyError):
    """Raised when a class is decorated with more than one scope decorator."""

    def __init__(self, cls: type):
        super().__init__(
            f"'{cls.__name__}' is already decorated with a scope decorator(@Component,@Singleton...)."
        )


class CircularDependencyError(providifyError):
    """
    Raised when a circular dependency is detected during resolution.

    Example:
        A → B → C → A

    This means A depends on B, B depends on C, and C depends back on A —
    the container cannot resolve this without infinite recursion.
    """

    def __init__(self, cycle: str) -> None:
        self.cycle = cycle
        super().__init__(
            f"Circular dependency detected: {cycle}\n"
            f"Break the cycle by:\n"
            f"  1. Introducing an interface between the dependent classes\n"
            f"  2. Using lazy injection — Lazy[T]\n"
            f"  3. Restructuring to remove the mutual dependency"
        )


class ValidationError(providifyError):
    pass


class LiveInjectionRequiredError(ValidationError):
    """Raised when a REQUEST or SESSION scoped dep is not wrapped in Live[T].

    A longer-lived component (SINGLETON or SESSION) that injects a scoped dep
    via ``Inject[T]``, ``Lazy[T]``, or a plain type annotation will capture a
    single instance at construction time. That instance becomes stale the moment
    the scope rotates to a new request or session — wrong and often a security
    issue (e.g. one user's JWT leaking into another user's request).

    ``Live[T]`` is the correct fix: it wraps the dep in a proxy that re-resolves
    from the container on every access, always returning the instance that belongs
    to the *currently active* scope context.

    Attributes:
        violations: Structured list of all violating parameters — one entry per
            constructor parameter that should be changed to Live[T].
    """

    def __init__(self, violations: list[LiveInjectionViolation]) -> None:
        self.violations = violations
        lines = [
            f"  - '{v.param_name}' in {v.binding[0].__name__} "
            f"(scope={v.binding[1].name}) injects {v.dep[0].__name__} "
            f"(scope={v.dep[1].name}) without Live[T].\n"
            f"    Fix: change `{v.param_name}: Inject[{v.dep[0].__name__}]` "
            f"→ `{v.param_name}: Live[{v.dep[0].__name__}]`\n"
            f"    Reason: Inject[T] and Lazy[T] capture one instance at "
            f"construction time — that instance becomes stale across "
            f"{v.dep[1].name.lower()} boundaries."
            for v in violations
        ]
        super().__init__(
            "Live[T] required for REQUEST/SESSION scoped dependencies:\n" + "\n".join(lines)
        )


class ScopeViolationDetectedError(ValidationError):
    def __init__(self, scope_violations: list[ScopeLeak]):
        self.scope_violations = scope_violations
        message = "\n".join(
            [
                f"Scope leak detected from binding {violation.binding[0].__name__} with scope {violation.binding[1].name} to reference {violation.reference[0].__name__} with scope {violation.reference[1].name}"
                for violation in self.scope_violations
            ]
        )
        super().__init__(message)


class AnnotationResolutionError(ValidationError):
    """Raised when a validator cannot even READ a binding's annotations.

    A validator's whole job is to prove a binding is safe (no scope leaks).
    If ``get_type_hints()`` itself fails — e.g. a ``NameError`` from a
    locally-defined type absent from both ``__globals__`` and the container's
    ``localns`` — the validator has no evidence either way. Silently treating
    "I don't know" as "no leaks found" (returning an empty list) reports a
    clean bill of health the validator cannot actually prove, which is worse
    than raising: it is a false negative that looks identical to genuine
    safety. Raising instead forces the container to refuse to start rather
    than lie about having validated something it never could evaluate.

    Subclassing ``ValidationError`` (rather than, say, ``LookupError``)
    matters operationally: ``validate_all()`` catches ``ValidationError``-
    family exceptions and appends ``str(e)`` to its violations list, so an
    unresolvable annotation surfaces as a *violation message* — never as a
    crash that bypasses the normal "collect every violation" flow, and never
    as a false "you're fine".

    Phase 7 (per-parameter resolution) narrows the failure to a single
    parameter or class attribute rather than an entire signature — ``param_name``
    carries that extra precision when known, so the message can say exactly
    *which* injection point could not be resolved instead of naming only the
    owning class/function.

    Attributes:
        owner_name: Human-readable name of the binding/class/function whose
            annotations could not be resolved (e.g. ``"Impl.__init__"`` or
            ``"@Provider(make_impl)"``).
        cause: The original exception raised by ``get_type_hints()`` (e.g.
            ``NameError``, ``AttributeError``) — chained via ``__cause__``.
        param_name: The specific parameter or class attribute that could not
            be resolved, when resolution is per-parameter (``None`` when the
            failure is whole-signature, e.g. release A's validators).
    """

    def __init__(
        self,
        owner_name: str,
        cause: Exception,
        param_name: str | None = None,
    ) -> None:
        self.owner_name = owner_name
        self.cause = cause
        self.param_name = param_name
        # WHY a conditional clause instead of always interpolating param_name:
        # release A call sites (whole-signature validators) never pass it, and
        # "for parameter 'None'" in every message would be actively misleading.
        located_at = f" parameter '{param_name}'" if param_name is not None else ""
        super().__init__(
            f"Cannot resolve type hints for '{owner_name}'{located_at} "
            f"({type(cause).__name__}: {cause}). Scope-leak validation cannot "
            f"run for it, so the container refuses to start rather than "
            f"report a clean bill of health it cannot prove.\n"
            f"Fix: import the annotated type at runtime instead of under "
            f"TYPE_CHECKING, or move locally-defined types to module level."
        )


class ContainerValidationError(ValidationError):
    """Raised by ``DIContainer.validate()`` when the full-graph report contains
    at least one ``ERROR``-severity issue and ``raise_on_error=True`` (the
    default).

    Aggregates EVERY error found in one shot — missing bindings, ambiguous
    bindings, circular dependencies, scope leaks, ``Live[T]`` violations, and
    unresolvable annotations — rather than stopping at the first failure, so
    a misconfigured app can be fixed in one pass instead of one error at a
    time across repeated boot attempts.

    Subclasses ``ValidationError`` (not ``LookupError``) deliberately:
    startup validation is an explicit, opt-in call — aliasing its failure
    mode to the lazy-resolution error type (``LookupError``, raised by
    ``get()``) would blur two genuinely different situations for a caller
    with a bare ``except LookupError`` around request handling. Subclassing
    ``ValidationError`` still keeps ``except ValidationError`` (the existing
    ``validate_all()``/``validate_bindings()`` catch-all) working unchanged.

    Attributes:
        report: The full ``ValidationReport`` the container computed —
            every issue, not just the first, so callers can inspect
            ``exc.report.errors`` / ``exc.report.warnings`` directly instead
            of re-parsing ``str(exc)``.

    Example:
        try:
            container.validate()
        except ContainerValidationError as exc:
            for issue in exc.report.errors:
                log.error("%s: %s", issue.owner, issue.message)
            raise SystemExit(1)
    """

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        # One line per ERROR issue — warnings are intentionally omitted from
        # the raised message (they never gate startup; report.warnings is
        # still reachable via exc.report for callers who want them anyway).
        lines = [
            f"  - [{issue.severity.value.upper()}] {issue.owner}: {issue.message}"
            for issue in report.errors
        ]
        super().__init__(
            f"Container validation failed with {len(report.errors)} error(s):\n" + "\n".join(lines)
        )


@dataclass(frozen=True)
class ShutdownFailure:
    """One teardown hook or disposer that raised during ``shutdown()``/``ashutdown()``.

    Frozen because a failure record is a fact about what already happened —
    there is no legitimate reason to mutate it after capture, and immutability
    lets ``ShutdownError`` hand out ``self.failures`` without defensive copying.

    Attributes:
        owner: Human-readable identifier of what raised — e.g. ``"Db.close"``
            for an ``@PreDestroy`` method or ``"@Disposes(close_pool)"`` for a
            provider disposer — so a caller can pinpoint the failing component
            without re-deriving it from the traceback.
        exception: The original exception instance, preserved as-is (not
            re-wrapped) so ``type(f.exception)`` and ``f.exception.args`` stay
            usable for callers that branch on the underlying error type.
    """

    owner: str
    exception: BaseException


class ShutdownError(providifyError):
    """Raised by ``DIContainer.shutdown()`` / ``ashutdown()`` when one or more
    teardown hooks (``@PreDestroy`` methods or ``@Disposes`` disposers) raise.

    Aggregates EVERY failure in one shot — mirroring ``ContainerValidationError``
    (exceptions.py above) — rather than stopping at the first failing hook.
    This is a deliberate behaviour change from the old raise-on-first
    ``shutdown()``: every remaining teardown still runs, every cache is still
    cleared (``finally: self._clear_caches()`` in ``shutdown()``/``ashutdown()``),
    and every failure is reported together so a bad hook cannot mask, or be
    masked by, another one.

    ``__cause__`` is chained to the exception of the **earliest-created**
    failing component (``raise ShutdownError(failures) from failures[-1].exception``
    — ``failures`` is appended in teardown order, i.e. reverse-creation
    order, so its last entry is the earliest-created one). That is
    deliberately not "the first hook that happened to fail during the
    teardown walk": the earliest-created component is typically the most
    foundational one (e.g. a database connection pool), and its teardown
    failure is often the actual root cause behind failures in components
    created after it, which may just be downstream symptoms of the same
    unavailable resource.

    Attributes:
        failures: Every ``ShutdownFailure`` captured during the teardown pass,
            in teardown (reverse-dependency) order — not just the first one —
            so callers can inspect ``exc.failures`` directly instead of
            re-parsing ``str(exc)``.

    Example:
        try:
            container.shutdown()
        except ShutdownError as exc:
            for failure in exc.failures:
                log.error("teardown failed: %s", failure.owner, exc_info=failure.exception)
            raise SystemExit(1)
    """

    def __init__(self, failures: list[ShutdownFailure]) -> None:
        self.failures = failures
        # One line per failure — mirrors ContainerValidationError's message
        # shape so both aggregation errors read consistently in logs.
        lines = [f"  - {f.owner}: {type(f.exception).__name__}: {f.exception}" for f in failures]
        super().__init__(
            f"Shutdown completed with {len(failures)} teardown failure(s); "
            f"all caches were cleared:\n" + "\n".join(lines)
        )


class ModuleCycleError(providifyError):
    """Raised when ``@Configuration`` module ``depends_on=`` edges form a cycle.

    Detected by ``providify.modules.resolve_install_order`` (a DFS post-order
    walk with a ``path`` list) *before* any module is instantiated — a cycle
    therefore leaves the container completely untouched (see plan 008
    §Design "Install pipeline": cycle detection happens strictly before
    ``_resolve_constructor`` is ever called for any class in the batch).

    Mirrors ``CircularDependencyError`` above (same "name every class in the
    cycle, in order" shape) but is deliberately a *separate* exception type
    rather than reused: a singleton `get()` cycle and a module install-order
    cycle are different failure surfaces raised by different code paths
    (`container.py`'s resolution stack vs. `modules.py`'s pure DFS) — merging
    them would force a caller with a narrow `except CircularDependencyError`
    around `get()` to also start catching module-install failures it never
    asked about.

    Attributes:
        cycle: The classes forming the cycle, in traversal order — e.g.
            ``(A, B, C, A)`` for ``A -> B -> C -> A``. The first and last
            elements are the same class, matching the arrow-chain rendering
            in the message.

    Example:
        @Configuration(depends_on=[C])
        class A: ...
        @Configuration(depends_on=[A])
        class B: ...
        @Configuration(depends_on=[B])
        class C: ...

        container.install(A)  # raises ModuleCycleError: A -> B -> C -> A
    """

    def __init__(self, cycle: Sequence[type]) -> None:
        self.cycle: tuple[type, ...] = tuple(cycle)
        rendered = " → ".join(cls.__name__ for cls in self.cycle)
        super().__init__(
            f"Circular @Configuration depends_on detected: {rendered}\n"
            f"Break the cycle by removing one of the depends_on= edges above, "
            f"or merging the mutually-dependent modules into one."
        )


class ConditionEvaluationError(providifyError):
    """Raised when an `@Requires` predicate raises instead of returning a bool.

    Surfaced from `get()`/`aget()`/`get_all()`/`aget_all()`/`is_resolvable()`
    (via `DIContainer._binding_is_active()` -> the module-level
    `_conditions_hold()` helper) and from `DIContainer.validate()`'s pass 1c
    — both call sites wrap the SAME predicate call, so a broken condition
    fails identically everywhere it is evaluated (plan 015 §Design).

    Subclasses `providifyError` directly rather than `BindingError`
    (`exceptions.py:24-45`):
    DESIGN: not `BindingError`
        `BindingError` subclasses all mean "registration was malformed" —
        raised once, at `bind()`/`provide()` time, before anything is
        resolved. This error is the opposite shape: registration succeeded;
        the predicate itself, user code evaluated lazily at resolution
        time, misbehaved. Reusing `BindingError` would make a caller with a
        narrow `except BindingError` around registration start silently
        swallowing a live resolution-time bug — the same "narrow except
        clause starts catching unrelated failures" rationale `ModuleCycleError`
        gives (`:340-345`) for not reusing `CircularDependencyError`.
        ✅ Callers narrowly catching `BindingError` are unaffected by a
           broken predicate.
        ❌ One more top-level exception type to document — accepted, the
           alternative (silently misclassifying a resolution bug as a
           registration bug) is worse.

    Attributes:
        owner:  Human-readable owner of the binding whose condition raised —
            same `"ClassName"` / `"@Provider(fn_name)"` vocabulary as
            `validate()`'s `owner_of()` closure, produced by the
            module-level `_owner_label()` helper.
        marker: The `RequiresMarker` whose `is_satisfied()` raised. Typed
            `Any` here (not `RequiresMarker`) deliberately — importing
            `metadata.py` into `exceptions.py` would give this leaf module a
            dependency the rest of the file avoids; every other attribute in
            this module is either a builtin or already imported from
            `metadata.py` for an unrelated reason (`LiveInjectionViolation`,
            `ScopeLeak`), so adding one just for a type hint here is not
            worth it.

    The original exception is preserved via `__cause__` (`raise ... from
    exc`), so `exc.__cause__` still carries the real type and traceback —
    only the *message* is providify's, not the underlying failure.

    DESIGN: alternatives considered
        - "Re-raise the original exception unchanged" — rejected: the
          original has no way to carry the binding's name, which is the one
          thing a user needs to find the broken predicate.
        - "Swallow and treat as inactive" — rejected: silently hides a
          programming error; a broken predicate is a bug, not a legitimate
          "off" state.

    Example:
        @Requires(condition=lambda: 1 / 0)
        @Component
        class Broken(Base): ...

        container.bind(Base, Broken)
        try:
            container.get(Base)
        except ConditionEvaluationError as exc:
            assert isinstance(exc.__cause__, ZeroDivisionError)
    """

    def __init__(self, owner: str, marker: Any, exc: BaseException) -> None:
        self.owner = owner
        self.marker = marker
        super().__init__(
            f"@Requires condition on {owner} raised {type(exc).__name__}: {exc}. "
            f"Conditions must be cheap, pure, and must not raise — fix the "
            f"predicate ({marker.describe()}) rather than catching this error."
        )


@dataclass(frozen=True)
class ConfigIssue:
    """One field-level problem discovered while binding a ``@ConfigProperties``
    target (``providify/config.py``'s stdlib coercion path, or a source-loading
    failure that leaves nothing else to attempt).

    Frozen for the same reason as ``ShutdownFailure`` above — an issue is a
    fact about what already happened during binding, never mutated after
    capture, so ``ConfigBindingError`` can hand out ``self.issues`` without
    defensive copying.

    Attributes:
        field: Dotted/indexed path to the offending field, e.g.
            ``"pool_size"`` or ``"db.replicas[1]"`` — precise enough to point
            a caller at the exact declaration without re-deriving it from the
            merged mapping.
        message: Human-readable description of the failure, e.g.
            ``"expected int, got 'twenty'"``.
        source: Human-readable identifier of the originating source, e.g.
            ``"EnvSource(DB__POOL_SIZE)"`` or ``"YamlSource(config.yaml)"`` —
            ``None`` when the issue cannot be attributed to a single source
            (e.g. a missing required field with no source at all).
    """

    field: str
    message: str
    source: str | None


class ConfigBindingError(providifyError):
    """Raised when binding a ``@ConfigProperties`` target fails — either a
    source could not be loaded (unreadable/malformed file, missing PyYAML) or
    one or more declared fields could not be coerced from the merged mapping.

    Aggregates EVERY field failure in one shot — mirroring
    ``ContainerValidationError``/``ShutdownError`` above — rather than
    stopping at the first bad field, so a config file with four typos reports
    four issues, not one. Source-loading failures are the one exception:
    there is nothing left to attempt, so they raise immediately with a single
    issue.

    Attributes:
        target: Human-readable name of the class being bound, e.g.
            ``"DbSettings"``.
        issues: Every ``ConfigIssue`` captured while attempting the bind —
            not just the first — so callers can inspect ``exc.issues``
            directly instead of re-parsing ``str(exc)``.

    Example:
        try:
            container.get(DbSettings)
        except ConfigBindingError as exc:
            for issue in exc.issues:
                log.error("%s: %s (%s)", issue.field, issue.message, issue.source)
            raise SystemExit(1)
    """

    def __init__(self, target: str, issues: Sequence[ConfigIssue]) -> None:
        self.target = target
        self.issues = list(issues)
        # One line per issue — mirrors ContainerValidationError's/ShutdownError's
        # message shape so all three aggregation errors read consistently in logs.
        lines = [
            f"  - {issue.field}: {issue.message}"
            + (f" (source: {issue.source})" if issue.source is not None else "")
            for issue in self.issues
        ]
        super().__init__(
            f"Failed to bind configuration for '{target}' "
            f"({len(self.issues)} issue(s)):\n" + "\n".join(lines)
        )
