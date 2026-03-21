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
    "Inheritable",
    # Lifecycle decorators
    "PostConstruct",
    "PreDestroy",
    # Module
    "Configuration",
    # Types
    "Inject",
    "InjectInstances",
    "Lazy",
    "Live",
    "LiveProxy",
    "LazyProxy",
    # Metadata — use with Annotated[T, XxxMeta(...)] for fully type-safe
    # annotations when qualifier / priority / optional options are needed.
    # e.g.  store: Annotated[Storage, InjectMeta(qualifier="cloud")]
    # This avoids the # type: ignore[valid-type] that call-form requires.
    "InjectMeta",
    "LazyMeta",
    "LiveMeta",
]
from .container import DIContainer, ScopeContext
from .decorator.scope import (
    Component,
    Singleton,
    RequestScoped,
    SessionScoped,
    Provider,
    Named,
    Inheritable,
)
from .decorator.lifecycle import PostConstruct, PreDestroy
from .decorator.module import Configuration
from .type import (
    Inject,
    InjectInstances,
    Lazy,
    Live,
    LiveProxy,
    LazyProxy,
    InjectMeta,
    LazyMeta,
    LiveMeta,
)

import logging

logging.getLogger(__name__).addHandler(logging.NullHandler())
