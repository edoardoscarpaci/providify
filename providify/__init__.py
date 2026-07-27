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
    "AnnotationResolutionError",
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
import logging

from .binding import AnyBinding
from .container import DIContainer, ScopeContext
from .decorator.interceptor import AroundInvoke, Interceptor, InterceptorBinding
from .decorator.lifecycle import (
    Disposes,
    DisposesMarker,
    Observes,
    ObservesMarker,
    PostConstruct,
    PreDestroy,
)
from .decorator.module import Configuration
from .decorator.scope import (
    Alternative,
    ApplicationScoped,
    Component,
    Decorator,
    Default,
    Inheritable,
    Named,
    # Priority is a field-update decorator (same module as Named) — it was
    # missing from the public surface despite being documented in the README.
    Priority,
    Provider,
    # Jakarta CDI parity decorators
    Qualifier,
    RequestScoped,
    SessionScoped,
    Singleton,
    Stereotype,
)
from .descriptor import BindingDescriptor, DIContainerDescriptor
from .exceptions import (
    AnnotationResolutionError,
    BindingError,
    CircularDependencyError,
    ClassBindingNotDecoratedError,
    LiveInjectionRequiredError,
    ProviderBindingNotDecoratedError,
    ScopeViolationDetectedError,
    providifyError,
)
from .metadata import Scope, StereotypeMetadata
from .type import (
    Delegate,
    DelegateMeta,
    Event,
    EventMeta,
    EventProxy,
    Inject,
    InjectInstances,
    InjectionPoint,
    InjectMeta,
    Instance,
    InstanceMeta,
    InstanceProxy,
    InvocationContext,
    Lazy,
    LazyMeta,
    LazyProxy,
    Live,
    LiveMeta,
    LiveProxy,
    NamedMeta,
)

logging.getLogger(__name__).addHandler(logging.NullHandler())
