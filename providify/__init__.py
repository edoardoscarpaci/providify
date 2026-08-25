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
    "Profile",
    "ProfileMetadata",
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
    # Configuration binding (env / YAML / JSON / TOML → typed objects)
    "ConfigProperties",
    "ConfigSource",
    "DictSource",
    "EnvSource",
    "JsonSource",
    "TomlSource",
    "YamlSource",
    "ConfigPropertiesMetadata",
    # Testing helpers
    "ContainerOverrides",
    "ContainerSnapshot",
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
    "ContainerValidationError",
    "ShutdownError",
    "ShutdownFailure",
    "ConfigBindingError",
    "ConfigIssue",
    "ModuleCycleError",
    # Binding and descriptor types — for type annotations and introspection
    "AnyBinding",
    "BindingDescriptor",
    "DIContainerDescriptor",
    # Startup-time full-graph validation (container.validate())
    "ValidationReport",
    "ValidationIssue",
    "IssueKind",
    "Severity",
    # Observability (container telemetry)
    "InstanceCreated",
    "InstanceDisposed",
    "ScopeEntered",
    "ScopeExited",
    "ContainerEvent",
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
    # Field-level interceptors (plan 010, F8) — AspectJ get/set pointcut
    # analogues via the descriptor protocol; NOT a Jakarta CDI feature.
    "AroundGet",
    "AroundSet",
    "Advised",
    "FieldAccessContext",
    # Multibinding collection injection (plan 010, F7)
    "Multibound",
]
import logging

from .binding import AnyBinding
from .config import (
    ConfigSource,
    DictSource,
    EnvSource,
    JsonSource,
    TomlSource,
    YamlSource,
)
from .container import ContainerSnapshot, DIContainer, ScopeContext
from .decorator.config import ConfigProperties
from .decorator.interceptor import (
    AroundGet,
    AroundInvoke,
    AroundSet,
    Interceptor,
    InterceptorBinding,
)
from .decorator.lifecycle import (
    Disposes,
    DisposesMarker,
    Observes,
    ObservesMarker,
    PostConstruct,
    PreDestroy,
)
from .decorator.module import Configuration
from .decorator.multibinding import Multibound
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
    Profile,
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
    ConfigBindingError,
    ConfigIssue,
    ContainerValidationError,
    LiveInjectionRequiredError,
    ModuleCycleError,
    ProviderBindingNotDecoratedError,
    ScopeViolationDetectedError,
    ShutdownError,
    ShutdownFailure,
    providifyError,
)
from .field import Advised, FieldAccessContext
from .metadata import (
    ConfigPropertiesMetadata,
    ProfileMetadata,
    Scope,
    StereotypeMetadata,
)
from .observability import (
    ContainerEvent,
    InstanceCreated,
    InstanceDisposed,
    ScopeEntered,
    ScopeExited,
)
from .testing import ContainerOverrides
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
from .validation import IssueKind, Severity, ValidationIssue, ValidationReport

logging.getLogger(__name__).addHandler(logging.NullHandler())
