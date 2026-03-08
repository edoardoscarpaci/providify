from __future__ import annotations
from typing import Any,Callable,List
from .metadata import ScopeLeak

class InjectableError(Exception):
    """Base class for all errors in the injectable framework."""
    pass

class BindingError(InjectableError):
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
        super().__init__(
            f"{obj} is not decorated with the required DI decorator."
        )


class ProviderAlreadyDecorated(InjectableError):
    """Raised when a provider function is decorated more than once."""
    def __init__(self, fn: Callable[..., Any]):
        super().__init__(
            f"'{fn.__name__}' is already decorated with @Provider."
        )

class ClassAlreadyDecorated(InjectableError):
    """Raised when a class is decorated with more than one scope decorator."""
    def __init__(self, cls: type):
        super().__init__(
            f"'{cls.__name__}' is already decorated with a scope decorator(@Component,@Singleton...)."
        )

class CircularDependencyError(InjectableError):
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

class ValidationError(InjectableError):
    pass

class ScopeViolationDetectedError(ValidationError):
    def __init__(self,scope_violations : list[ScopeLeak]):
        self.scope_violations = scope_violations
        message = "\n".join([
            f"Scope leak detected from binding {violation.binding[0].__name__} with scope {violation.binding[1].name} to reference {violation.reference[0].__name__} with scope {violation.reference[1].name}"
            for violation in self.scope_violations
        ])
        super().__init__(message)