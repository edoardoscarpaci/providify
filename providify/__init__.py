# providify/__init__.py

__all__ = [
    # Container
    "DIContainer",
    "ScopeContext",
    # Scope decorators
    "Component",
    "Singleton",
    "RequestScoped",
    "SessionScoped",
    "Provider",
    "Named",
    "Priority",
    "Inheritable",
    # Lifecycle decorators
    "PostConstruct",
    "PreDestroy",
    # Module
    "Configuration",
    # Scope enum — exported so users never need to import from metadata
    "Scope",
    # Exception hierarchy — exported for except clauses without internal imports
    "providifyError",
    "BindingError",
    "ClassBindingNotDecoratedError",
    "ProviderBindingNotDecoratedError",
    "CircularDependencyError",
    "ScopeViolationDetectedError",
    "LiveInjectionRequiredError",
    # Binding and descriptor types — for type annotations and introspection
    "AnyBinding",
    "BindingDescriptor",
    "DIContainerDescriptor",
    # Types
    "Inject",
    "InjectInstances",
    "Lazy",
    "Live",
    "Instance",
    "LiveProxy",
    "LazyProxy",
    "InstanceProxy",
    # Metadata — use with Annotated[T, XxxMeta(...)] for fully type-safe
    # annotations when qualifier / priority / optional options are needed.
    # e.g.  store: Annotated[Storage, InjectMeta(qualifier="cloud")]
    # This avoids the # type: ignore[valid-type] that call-form requires.
    "InjectMeta",
    "LazyMeta",
    "LiveMeta",
    "InstanceMeta",
]
from .container import DIContainer, ScopeContext
from .decorator.scope import (
    Component,
    Singleton,
    RequestScoped,
    SessionScoped,
    Provider,
    Named,
    # Priority is a field-update decorator (same module as Named) — it was
    # missing from the public surface despite being documented in the README.
    Priority,
    Inheritable,
)
from .decorator.lifecycle import PostConstruct, PreDestroy
from .decorator.module import Configuration
from .metadata import Scope
from .exceptions import (
    providifyError,
    BindingError,
    ClassBindingNotDecoratedError,
    ProviderBindingNotDecoratedError,
    CircularDependencyError,
    ScopeViolationDetectedError,
    LiveInjectionRequiredError,
)
from .binding import AnyBinding
from .descriptor import BindingDescriptor, DIContainerDescriptor
from .type import (
    Inject,
    InjectInstances,
    Lazy,
    Live,
    Instance,
    LiveProxy,
    LazyProxy,
    InstanceProxy,
    InjectMeta,
    LazyMeta,
    LiveMeta,
    InstanceMeta,
)

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
