# providify/__init__.py

__all__ = [
    # Container
    "DIContainer",
    "ScopeContext",
    # Scope decorators
    "Component",
    "Singleton",
    "ApplicationScoped",
    "RequestScoped",
    "SessionScoped",
    "Provider",
    "Named",
    "Priority",
    "Inheritable",
    # Jakarta CDI qualifier system
    "Qualifier",
    "Default",
    "Alternative",
    "Stereotype",
    "StereotypeMetadata",
    "Decorator",
    # Lifecycle decorators
    "PostConstruct",
    "PreDestroy",
    "Disposes",
    "DisposesMarker",
    "Observes",
    "ObservesMarker",
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
    "Event",
    "Delegate",
    "LiveProxy",
    "LazyProxy",
    "InstanceProxy",
    "EventProxy",
    # Metadata — use with Annotated[T, XxxMeta(...)] for fully type-safe
    # annotations when qualifier / priority / optional options are needed.
    # e.g.  store: Annotated[Storage, InjectMeta(qualifier="cloud")]
    # This avoids the # type: ignore[valid-type] that call-form requires.
    "InjectMeta",
    "LazyMeta",
    "LiveMeta",
    "InstanceMeta",
    "NamedMeta",
    "DelegateMeta",
    "EventMeta",
    # Context / AOP types
    "InjectionPoint",
    "InvocationContext",
    # Interceptor decorators
    "Interceptor",
    "InterceptorBinding",
    "AroundInvoke",
]
from .container import DIContainer, ScopeContext
from .decorator.scope import (
    Component,
    Singleton,
    ApplicationScoped,
    RequestScoped,
    SessionScoped,
    Provider,
    Named,
    # Priority is a field-update decorator (same module as Named) — it was
    # missing from the public surface despite being documented in the README.
    Priority,
    Inheritable,
    # Jakarta CDI parity decorators
    Qualifier,
    Default,
    Alternative,
    Stereotype,
    Decorator,
)
from .decorator.lifecycle import (
    PostConstruct,
    PreDestroy,
    Disposes,
    DisposesMarker,
    Observes,
    ObservesMarker,
)
from .decorator.module import Configuration
from .metadata import Scope, StereotypeMetadata
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
    Event,
    Delegate,
    LiveProxy,
    LazyProxy,
    InstanceProxy,
    EventProxy,
    InjectMeta,
    LazyMeta,
    LiveMeta,
    InstanceMeta,
    NamedMeta,
    DelegateMeta,
    EventMeta,
    InjectionPoint,
    InvocationContext,
)
from .decorator.interceptor import Interceptor, InterceptorBinding, AroundInvoke

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
