from __future__ import annotations
from typing import Any, Callable
from .metadata import LiveInjectionViolation, ScopeLeak


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
            "Live[T] required for REQUEST/SESSION scoped dependencies:\n"
            + "\n".join(lines)
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
