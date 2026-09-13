from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import (
    Any,
    TypeVar,
    overload,
)

from ..exceptions import NotDecoratedError
from ..metadata import (
    DIMetadata,
    ProviderMetadata,
    RequiresMarker,
    Scope,
    StereotypeMetadata,
    _get_own_metadata,
    _get_profile_expressions,
    _get_provider_metadata,
    _get_requires_markers,
    _is_decorated,
    _set_alternative_marker,
    _set_decorator_marker,
    _set_fallback_marker,
    _set_metadata,
    _set_profile_marker,
    _set_provider_metadata,
    _set_qualifier_marker,
    _set_requires_markers,
    _set_stereotype,
)
from ..profiles import _normalise

T = TypeVar("T")
R = TypeVar("R")


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────
def _is_function_provider(obj: Any) -> bool:
    """
    Returns True if obj is a callable but not a class.
    Distinguishes @Provider functions from @Component classes.
    """
    return callable(obj) and not isinstance(obj, type)


# ─────────────────────────────────────────────────────────────────
#  _make_decorator — factory for all scope decorators
#  Eliminates duplication across @Component, @Singleton,
#  @RequestScoped, @SessionScoped — only the Scope value differs
# ─────────────────────────────────────────────────────────────────
def _make_decorator(scope: Scope) -> Any:
    @overload
    def decorator(__cls: type[T]) -> type[T]: ...

    @overload
    def decorator(
        __cls: None = ...,
        *,
        qualifier: str | type | None = None,
        priority: int = 0,
        inherited: bool = False,
        track: bool = False,
    ) -> Callable[[type[T]], type[T]]: ...

    def decorator(
        __cls: Any = None,
        *,
        qualifier: str | type | None = None,
        priority: int = 0,
        inherited: bool = False,
        track: bool = False,
    ) -> Any:
        def stamp(c: type[T]) -> type[T]:
            existing = _get_own_metadata(c)

            _set_metadata(
                c,
                (
                    existing.merge(  # merge if already decorated
                        scope=scope,
                        qualifier=qualifier,
                        priority=priority,
                        inherited=inherited,
                        track=track,
                    )
                    if existing is not None
                    else DIMetadata(  # fresh if first decorator
                        scope=scope,
                        qualifier=qualifier,
                        priority=priority,
                        inherited=inherited,
                        track=track,
                    )
                ),
            )
            return c

        if __cls is not None:
            return stamp(__cls)
        return stamp

    return decorator


# ─────────────────────────────────────────────────────────────────
#  Public scope decorators — explicit @overload wrappers around
#  _make_decorator so that linters (pyright / mypy) can see the
#  kwargs (qualifier, priority, inherited) instead of just `Any`.
#
#  DESIGN: We intentionally keep _make_decorator as the single
#  source of truth for the runtime logic, but expose public names
#  as thin wrapper functions that carry the typed @overload stubs.
#
#  Without this, `Singleton = _make_decorator(Scope.SINGLETON)`
#  produces a name of type `Any` — overloads defined inside the
#  closure are invisible to the type checker.
#
#  Tradeoffs:
#    ✅ Linters see qualifier / priority / inherited kwargs
#    ✅ Type narrowing works: @Singleton(cls) → type[T]
#    ✅ Runtime behaviour is identical — delegates to _make_decorator
#    ❌ Four thin wrappers to maintain if the signature ever changes
#    ❌ Slightly more boilerplate — acceptable given the clear upside
#
#  Alternative considered: Protocol with overloaded __call__.
#  Rejected because pyright's support for @overload inside Protocol
#  bodies is inconsistent across versions, making it unreliable.
# ─────────────────────────────────────────────────────────────────

# Private implementations — carry the actual runtime logic.
_component_impl = _make_decorator(Scope.DEPENDENT)
_singleton_impl = _make_decorator(Scope.SINGLETON)
_request_impl = _make_decorator(Scope.REQUEST)
_session_impl = _make_decorator(Scope.SESSION)


# ── Component ──────────────────────────────────────────────────────


@overload
def Component(__cls: type[T]) -> type[T]: ...


@overload
def Component(
    __cls: None = ...,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def Component(
    __cls: Any = None,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Any:
    """
    Marks a class as a DI component with DEPENDENT (prototype) scope.

    Each injection creates a fresh instance — no shared state.
    Equivalent to Jakarta CDI's default (dependent) scope.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @Component decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.
        track:      If True, instances are tracked for @PreDestroy on
                    flush_dependents() — useful for DEPENDENT-scoped beans
                    with teardown logic.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @Component
        class EmailService(NotificationService): ...

        @Component(qualifier="sms", priority=2)
        class SmsService(NotificationService): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _component_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited, track=track
    )


# ── Singleton ──────────────────────────────────────────────────────


@overload
def Singleton(__cls: type[T]) -> type[T]: ...


@overload
def Singleton(
    __cls: None = ...,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def Singleton(
    __cls: Any = None,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Any:
    """
    Marks a class as a DI component with SINGLETON scope.

    One shared instance is created per container and reused for every
    injection — equivalent to Jakarta CDI's @ApplicationScoped.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @Singleton decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.
        track:      Reserved for API consistency — no effect for SINGLETON.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @Singleton
        class DatabasePool: ...

        @Singleton(qualifier="primary", priority=10)
        class PrimaryDatabase(Database): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _singleton_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited, track=track
    )


# ── RequestScoped ──────────────────────────────────────────────────


@overload
def RequestScoped(__cls: type[T]) -> type[T]: ...


@overload
def RequestScoped(
    __cls: None = ...,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def RequestScoped(
    __cls: Any = None,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Any:
    """
    Marks a class as a DI component with REQUEST scope.

    One instance is created per active request context and shared across
    all injections within that request — equivalent to Jakarta's @RequestScoped.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @RequestScoped decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @RequestScoped
        class RequestContext: ...

        @RequestScoped(qualifier="audit")
        class AuditRequestContext(RequestContext): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _request_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited, track=track
    )


# ── SessionScoped ──────────────────────────────────────────────────


@overload
def SessionScoped(__cls: type[T]) -> type[T]: ...


@overload
def SessionScoped(
    __cls: None = ...,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Callable[[type[T]], type[T]]: ...


def SessionScoped(
    __cls: Any = None,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
    track: bool = False,
) -> Any:
    """
    Marks a class as a DI component with SESSION scope.

    One instance is created per active session context and shared across
    all injections within that session — equivalent to Jakarta's @SessionScoped.

    Args:
        __cls:      The class to decorate (positional-only, implicit when
                    used as a bare @SessionScoped decorator).
        qualifier:  Named qualifier to distinguish multiple bindings of
                    the same type — equivalent to Jakarta's @Named.
        priority:   Binding priority; higher wins when multiple bindings
                    match the same type.
        inherited:  If True, subclasses inherit this binding automatically.
        track:      Reserved for API consistency — no effect for SESSION.

    Returns:
        The decorated class unchanged (type preserved for the type checker),
        or a decorator when called with keyword arguments.

    Raises:
        TypeError: If __cls is not a class.

    Example:
        @SessionScoped
        class UserSession: ...

        @SessionScoped(qualifier="admin")
        class AdminSession(UserSession): ...

    Thread safety:  ✅ Safe — metadata stamped at decoration time, before
                    any concurrent access.
    Async safety:   ✅ Safe — pure metadata write, no async state involved.
    """
    return _session_impl(
        __cls, qualifier=qualifier, priority=priority, inherited=inherited, track=track
    )


# ─────────────────────────────────────────────────────────────────
#  _make_updater — factory for single/multi field update decorators
#  Named / Priority / and any future field-update decorators
#
#  Accepts a builder callable that receives the decorator's kwargs
#  and returns the dict of fields to update — keeping _make_updater
#  itself generic and field-agnostic.
# ─────────────────────────────────────────────────────────────────


def _make_updater(
    builder: Callable[..., dict[str, Any]],
    *,
    require_args: bool = False,
) -> Any:
    def updater(__cls: Any = None, **kwargs: Any) -> Any:
        def decorator(c: Any) -> Any:
            if not _is_decorated(c):
                raise NotDecoratedError(c)

            updates = builder(**kwargs)

            if _is_function_provider(c):
                existing = _get_provider_metadata(c)
                if existing is not None:
                    _set_provider_metadata(c, existing.merge(**updates))
            else:
                existing = _get_own_metadata(c)
                if existing is not None:
                    _set_metadata(c, existing.merge(**updates))

            return c

        if __cls is not None:
            if require_args:
                if isinstance(__cls, str):
                    # User wrote @Named("smtp") instead of @Named(name="smtp").
                    # Give a targeted message so the fix is obvious.
                    raise TypeError(
                        f"@Named requires a keyword argument: "
                        f"use @Named(name={__cls!r}) instead of @Named({__cls!r})."
                    )
                raise TypeError(
                    f"This decorator requires keyword arguments — "
                    f"use it with parens: @{updater.__name__}(...)"
                )
            return decorator(__cls)
        return decorator

    return updater


# ─────────────────────────────────────────────────────────────────
#  Public updater decorators
# ─────────────────────────────────────────────────────────────────

Priority = _make_updater(
    # Single field — only updates priority, never touches qualifier/scope/etc.
    lambda *, priority: {"priority": priority},
)

Named = _make_updater(
    # Single field — only updates qualifier
    # name= maps to qualifier internally, matching Jakarta's @Named
    lambda *, name: {"qualifier": name},
    require_args=True,  # @Named without name= is always a mistake
)

Inheritable = _make_updater(
    # Updates inherited flag only
    lambda: {"inherited": True}
)


# ─────────────────────────────────────────────────────────────────
#  @Provider — standalone, not a _make_updater candidate
#  It stamps __di_provider__ from scratch, not updating an existing
#  metadata dict, and has unique async detection logic.
# ─────────────────────────────────────────────────────────────────
@overload
def Provider(__fn: Callable[..., R]) -> Callable[..., R]: ...


@overload
def Provider(
    __fn: None = ...,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    singleton: bool = False,
    scope: Scope | None = None,
    returns: Any = None,
) -> Callable[[Callable[..., R]], Callable[..., R]]: ...


def Provider(
    __fn: Any = None,
    *,
    qualifier: str | type | None = None,
    priority: int = 0,
    singleton: bool = False,
    # scope — explicit Scope value, overrides singleton flag when set.
    # Enables @Provider to produce REQUEST or SESSION scoped values,
    # mirroring Jakarta CDI's @Produces @RequestScoped pattern.
    # Example: @Provider(scope=Scope.REQUEST)
    scope: Scope | None = None,
    # returns — explicit interface override, bypassing return-annotation
    # derivation entirely. See `binding._normalize_explicit_interface` for the
    # full accepted-shapes table: a type, a parameterised generic alias, an
    # `Annotated[...]` wrapper, or a zero-arg callable evaluated once at
    # `ProviderBinding` construction (i.e. at `provide()`/`scan()`/`install()`
    # time, never at decoration time).
    returns: Any = None,
) -> Any:
    """
    Marks a function as a DI provider.
    Return type hint determines the provided type.

    Supports both sync and async functions —
    is_async is detected once at decoration time via inspect,
    so ProviderBinding never needs to call inspect at resolution time.

    Equivalent to Jakarta's @Produces / @Bean.

    Scope resolution priority:
        1. ``scope=Scope.REQUEST`` / ``scope=Scope.SESSION`` — explicit scope
        2. ``singleton=True``                                — Scope.SINGLETON
        3. default                                           — Scope.DEPENDENT

    Interface resolution priority (highest wins):
        1. ``container.provide(fn, returns=...)`` — call-site override
        2. ``@Provider(returns=...)``              — decoration-time override
        3. ``fn``'s resolved return annotation

    When ``returns=`` is given, the return annotation is not read, not
    evaluated, and not validated — the factory may be annotated ``-> Any``,
    annotated with an unresolvable forward ref, or unannotated.

    Usage:
        @Provider
        def email_service() -> NotificationService:
            return EmailService(load_config())

        @Provider(qualifier="sms", priority=2, singleton=True)
        def sms_service() -> NotificationService:
            return SMSService(api_key="secret")

        @Provider(singleton=True)
        async def db_pool() -> DatabasePool:
            pool = DatabasePool()
            await pool.connect()    # async initialisation ✅
            return pool

        # Mimic Jakarta's @Produces @RequestScoped —
        # factory runs once per request, result cached for its duration.
        @Provider(scope=Scope.REQUEST)
        def jwt_token(header: Inject[AuthHeader]) -> JWTToken:
            return JWTToken.decode(header.value)

        # Interface known only via a generic alias built inside a loop —
        # no static return annotation could ever name it.
        @Provider(returns=Repository[User])
        def user_repository() -> Any:
            return InMemoryRepo()

        # Deferred form — evaluated once at registration, not at decoration.
        @Provider(returns=lambda: Repository[User])
        def user_repository() -> Any:
            return InMemoryRepo()

        # Open-generic form (plan 016) — ONE registration replaces a
        # per-type loop when every closed type shares one factory shape.
        # `entity: type[T]` receives the CLOSED type argument (matched by
        # TypeVar name), never resolved from the container. See
        # DIContainer.provide()'s docstring for the full eight-rule table.
        T = TypeVar("T")

        @Provider
        def repository(entity: type[T]) -> Repository[T]:
            return InMemoryRepo(entity)
        # container.get(Repository[User])  -> InMemoryRepo(User)
    """

    def decorator(fn: Callable[..., R]) -> Callable[..., R]:
        existing = _get_provider_metadata(fn)

        # `returns` is only included in the merge-kwargs dict when it is not
        # None. `ProviderMetadata.merge()` preserves a key that is *not
        # passed*; if we always passed `returns=returns`, stacking a plain
        # `@Provider(qualifier="a")` on top of `@Provider(returns=X)` would
        # clobber `returns` back to None. Every other field is intentionally
        # NOT given this treatment — their current clobbering behaviour is
        # relied on by existing tests.
        updates: dict[str, Any] = {
            "singleton": singleton,
            "qualifier": qualifier,
            "priority": priority,
            "scope": scope,
            "is_async": inspect.iscoroutinefunction(fn),  # detected once at decoration time
        }
        if returns is not None:
            updates["returns"] = returns

        _set_provider_metadata(
            fn,
            (
                existing.merge(**updates)  # merge if already decorated
                if existing is not None
                else ProviderMetadata(  # fresh if first decorator
                    singleton=singleton,
                    qualifier=qualifier,
                    priority=priority,
                    scope=scope,
                    is_async=inspect.iscoroutinefunction(fn),  # detected once at decoration
                    returns=returns,
                )
            ),
        )

        return fn

    if __fn is not None:
        return decorator(__fn)
    return decorator


# ─────────────────────────────────────────────────────────────────
#  @Qualifier — marks a class as a typed qualifier annotation
# ─────────────────────────────────────────────────────────────────


def Qualifier(cls: type) -> type:
    """Mark a class as a CDI-style qualifier annotation.

    Equivalent to Jakarta CDI's @Qualifier meta-annotation.
    Once marked, the class can be used as a typed qualifier in scope
    decorators and injection points instead of bare strings.

    Example:
        @Qualifier
        class Primary: ...

        @Singleton(qualifier=Primary)
        class PrimaryRepo(Repo): ...

        repo = container.get(Repo, qualifier=Primary)
    """
    _set_qualifier_marker(cls)
    return cls


# ─────────────────────────────────────────────────────────────────
#  @Default — explicit default qualifier (Jakarta CDI @Default)
# ─────────────────────────────────────────────────────────────────


@Qualifier
class Default:
    """Explicit default qualifier — semantically equivalent to qualifier=None.

    Equivalent to Jakarta CDI's @Default.
    Applying @Default(qualifier=Default) is identical to no qualifier.
    """

    pass


# ─────────────────────────────────────────────────────────────────
#  @ApplicationScoped — alias for @Singleton (Jakarta CDI terminology)
# ─────────────────────────────────────────────────────────────────

ApplicationScoped = Singleton


# ─────────────────────────────────────────────────────────────────
#  @Alternative — deployment-time bean replacement
# ─────────────────────────────────────────────────────────────────


def Alternative(cls: type) -> type:
    """Mark a class as a CDI alternative — disabled by default.

    Alternative beans are excluded from resolution unless explicitly
    activated on a container via ``container.enable_alternative(cls)``.
    This enables clean test/environment substitution without affecting
    production containers.

    Example:
        @Alternative
        @Singleton
        class MockMailer(Mailer): ...

        container.enable_alternative(MockMailer)
        container.get(Mailer)  # returns MockMailer
    """
    _set_alternative_marker(cls)
    return cls


# ─────────────────────────────────────────────────────────────────
#  @Profile — deployment-time profile activation (Spring @Profile parity)
# ─────────────────────────────────────────────────────────────────


def Profile(*expressions: str) -> Callable[[Any], Any]:
    """Gate a class or ``@Provider`` function on the container's active profiles.

    Equivalent to Spring's ``@Profile`` (research 001 §Differentiators #7,
    via ``BACKLOG.md:37``) restricted to OR-of-literals with a leading ``!``
    for negation — no boolean expression language (plan 005 §Non-goals).
    A binding whose profile expression does not match the container's
    ``active_profiles`` is invisible to ``get()``, ``get_all()``,
    ``is_resolvable()``, and ``validate()`` — the same visibility rule an
    un-enabled ``@Alternative`` already has (see ``container.py``'s
    ``_binding_is_active``).

    Semantics:
        - Multiple expressions are OR'd: ``@Profile("dev", "test")`` is
          active when *either* ``"dev"`` or ``"test"`` is active.
        - A leading ``"!"`` negates a single literal:
          ``@Profile("!prod")`` is active whenever ``"prod"`` is **not**
          active — including when no profile at all is active.
        - Profile names are normalised (stripped, lower-cased) here and
          again in ``resolve_active_profiles``, so ``"PROD"`` and ``"prod"``
          always compare equal.
        - Composition with ``@Alternative``: an ``@Alternative`` class that
          also carries ``@Profile`` is governed by its profile instead of
          requiring an imperative ``container.enable_alternative()`` call —
          the "env-driven ``@Alternative``" half of this feature. Profile
          and alternative rules are AND'd (a matching profile does not
          bypass a *non*-matching one; there is only one gate here, the
          profile, once ``@Profile`` is present).
        - Applying ``@Profile`` more than once merges the expression tuples
          (union, order-preserving, de-duplicated) rather than overwriting —
          consistent with how repeated qualifier-style decorators compose
          elsewhere in this module.
        - Usable on classes **and** ``@Provider`` functions (including
          ``@Configuration`` bound methods and ``@Provider @property``) —
          unlike ``@Alternative``, which (before this plan) only took effect
          on classes; see plan 005 §Design "asymmetry fix".

    Args:
        *expressions: One or more profile literals, e.g. ``"prod"``,
            ``"!prod"``. At least one is required.

    Returns:
        A decorator that stamps the normalised, merged expression tuple
        onto the target's own ``__dict__`` and returns the target
        unchanged (same object identity).

    Raises:
        ValueError: If called with no arguments, or if any literal is empty,
            all-whitespace, or is a bare ``"!"`` with nothing to negate.
            Raised at decoration time (import time), not at resolution time,
            so a malformed profile expression fails fast.

    Thread safety: Decoration happens at import time on a single thread in
        every documented usage — see ``_set_profile_marker``'s note.
    Async safety:  No await points; pure marker stamping.

    Edge cases:
        - ``@Profile()`` (no args) -> ``ValueError``.
        - ``@Profile("")`` / ``@Profile("!")`` / ``@Profile("  ")`` ->
          ``ValueError``.
        - A subclass of a ``@Profile``d class does **not** inherit the
          expressions (``__dict__``-only lookup, matching ``@Alternative``).

    Example:
        @Profile("prod")
        @Singleton
        class RealMailer(Mailer): ...

        @Profile("dev", "test")            # OR — active in either
        @Singleton
        class ConsoleMailer(Mailer): ...

        @Profile("!prod")                  # negation
        @Provider(singleton=True)
        def fake_clock() -> Clock: ...

        container = DIContainer(profiles=("prod",))
        container.get(Mailer)              # -> RealMailer
    """
    if not expressions:
        raise ValueError("@Profile requires at least one profile expression.")

    normalised: list[str] = []
    for raw in expressions:
        expr = _normalise(raw)
        # A bare "!" negates nothing; an empty/whitespace-only literal is
        # never a valid profile name either — both are decoration-time
        # mistakes, so fail fast rather than silently matching nothing.
        if expr in ("", "!"):
            raise ValueError(f"Invalid @Profile expression: {raw!r}")
        normalised.append(expr)

    def decorator(target: Any) -> Any:
        existing = _get_profile_expressions(target)
        # Union, order-preserving, de-duplicated — repeated application
        # composes rather than overwrites (mirrors the merge semantics
        # documented above).
        merged = list(existing)
        for expr in normalised:
            if expr not in merged:
                merged.append(expr)
        _set_profile_marker(target, tuple(merged))
        return target

    return decorator


# ─────────────────────────────────────────────────────────────────
#  @Requires — condition-gated binding (Micronaut @Requires parity)
# ─────────────────────────────────────────────────────────────────


def Requires(
    *,
    condition: Callable[[], bool] | None = None,
    env: str | None = None,
    value: str | None = None,
) -> Callable[[Any], Any]:
    """Gate a class or ``@Provider`` function on a predicate evaluated lazily.

    The third conjunct of ``DIContainer._binding_is_active()`` beside
    ``@Profile`` and ``@Alternative`` (plan 015). Spelled after Micronaut's
    ``@Requires(property=..., value=...)``/``env=`` forms (research 014
    §73-101) rather than Spring Boot's ``@ConditionalOn*`` family — one
    name, keyword-only, no taxonomy of variants to promise.

    ⚠️ Divergence from Micronaut: in Micronaut ``@Requires(env=...)`` names
    an *environment* (≈ providify's own ``@Profile``). In providify the
    profile role is already taken by ``@Profile``, so ``env=`` here is
    unambiguously ``os.environ[...]`` — a plain OS environment variable
    lookup, nothing more.

    Two spellings, combinable by AND on one marker, and stackable (a second
    ``@Requires`` appends a second marker; every marker in the stack must
    be satisfied — AND across the whole stack, plan 015 §Design):

    - ``condition=`` — the generic escape hatch: any zero-arg predicate.
    - ``env=``/``env=`` + ``value=`` — sugar over the primitive, reading
      ``os.environ`` at evaluation time. ``env="X"`` alone means "X is set
      to a non-empty string"; ``env="X", value="y"`` means "X is set to
      exactly ``'y'``" (exact, case-sensitive — env values are user data,
      unlike ``@Profile``'s lower-cased names).

    The condition is evaluated **lazily, at resolve time**, on every
    ``_filter()`` call — never cached, never evaluated at decoration or
    registration time (unlike an ``eager=`` scan-time option, which this
    decorator does not offer — plan 015 §Non-goals). This is what lets a
    test flip a flag or ``monkeypatch.setenv`` *after* import and see it
    take effect on the very next ``get()``. The predicate contract is
    therefore **cheap and pure, and must not raise** — a raising predicate
    propagates as :class:`~providify.exceptions.ConditionEvaluationError`
    from every public lookup path AND from :meth:`DIContainer.validate`.

    Composition with ``@Profile``/``@Alternative``: AND'd together, and
    ``@Requires`` is evaluated **last** — after profile/alternative
    activation has already excluded a binding, its (possibly expensive or
    environment-specific) predicate never runs at all. A
    ``@Profile("prod")``-gated predicate that only works in production
    cannot break a dev ``get()`` this way.

    Stacking = AND, not merge: each ``@Requires`` keeps its own marker, so
    a failure message can name exactly which clause is unsatisfied.
    ``@Requires`` reads its own ``__dict__`` only — a subclass of a
    ``@Requires``-decorated class does **not** inherit the marker, matching
    ``@Profile``'s non-inheriting lookup.

    No singleton eviction: a ``@Singleton`` resolved while its condition
    was ``True`` stays cached (and still gets disposed at shutdown) even
    after the condition flips ``False`` — the same documented caveat
    ``activate_profile()`` already carries; ``override()``/
    ``reset_binding()`` remain the eviction tools.

    Goes on the ``@Provider`` **method**, not the ``@Configuration``
    **class** — ``ProviderBinding.conditions`` is read off the provider
    function only, mirroring ``ProviderBinding.profiles``. A ``@Requires``
    stamped on a ``@Configuration`` class itself has no effect on the
    providers it declares (E15, documented as an edge case, not
    implemented).

    Args:
        condition: A zero-arg predicate returning (or coercing to) a
            ``bool``. ``None`` means "no predicate clause" — must be
            combined with ``env=`` or this decorator raises.
        env: Name of an OS environment variable to check at resolve time.
            ``None`` means "no env clause" — must be combined with
            ``condition=`` or this decorator raises.
        value: Exact string ``env`` must equal to satisfy this marker. Only
            meaningful together with ``env=``; passing it without ``env=``
            raises. ``value=""`` is allowed and distinct from omitting
            ``value=`` — it means "the variable is set and exactly empty".

    Returns:
        A decorator that appends a ``RequiresMarker`` to the target's own
        ``__dict__`` and returns the target unchanged (same object
        identity) — order-insensitive relative to ``@Provider``/
        ``@Singleton``/``@Component``/``@Profile`` since every scope
        decorator in this module returns the object it was given.

    Raises:
        ValueError: Neither ``condition`` nor ``env`` given; ``value=``
            given while ``env`` is ``None``; or ``env=""``/whitespace-only.
            Raised at decoration time (import time), fail-fast, mirroring
            ``@Profile``'s ``ValueError`` above.
        TypeError: ``condition`` given but not callable, or ``env`` given
            as a non-``str``.

    Thread safety: Decoration happens at import time on a single thread in
        every documented usage — see ``_set_requires_markers``'s note.
    Async safety:  No await points; pure marker stamping. The stamped
        predicate itself must be a plain sync callable — there is no async
        ``@Requires`` form.

    Edge cases:
        - ``@Requires()`` (no args) -> ``ValueError``.
        - ``@Requires(value="x")`` (no ``env``) -> ``ValueError``.
        - ``@Requires(env="")`` / ``@Requires(env="   ")`` -> ``ValueError``.
        - ``@Requires(condition="not-callable")`` -> ``TypeError``.
        - ``@Requires(condition=..., env=..., value=...)`` all given -> AND
          of both clauses on the one marker (E11).
        - A subclass of a ``@Requires``-decorated class does **not**
          inherit the marker (E21).
        - Applied to an undecorated class then ``bind()`` -> the existing
          ``ClassBindingNotDecoratedError`` fires as today — ``@Requires``
          never stamps ``DIMetadata`` (E22).

    Example:
        @Requires(env="FEATURE_REDIS_CACHE")
        @Singleton
        class RedisCache(Cache): ...

        @Requires(condition=lambda: settings.use_mock_mailer)
        @Provider(singleton=True)
        def fake_mailer() -> Mailer: ...

        @Requires(env="CACHE_BACKEND", value="redis")
        @Requires(condition=lambda: redis_is_reachable())
        @Singleton
        class RedisCache(Cache): ...   # AND of both clauses
    """
    if condition is None and env is None:
        raise ValueError("@Requires needs at least one of condition= or env=.")
    if value is not None and env is None:
        raise ValueError("@Requires(value=...) is only meaningful together with env=.")
    if condition is not None and not callable(condition):
        raise TypeError("@Requires(condition=...) must be callable.")
    if env is not None:
        if not isinstance(env, str):
            raise TypeError("@Requires(env=...) must be a str.")
        if not env.strip():
            raise ValueError("@Requires(env=...) must name a non-empty environment variable.")

    marker = RequiresMarker(condition=condition, env=env, value=value)

    def decorator(target: Any) -> Any:
        # Append, not merge — a second @Requires keeps its own marker so a
        # failure message can name exactly which clause is unsatisfied
        # (plan 015 §Design "Stacking -> tuple of markers, not a merge").
        existing = _get_requires_markers(target)
        _set_requires_markers(target, (*existing, marker))
        return target

    return decorator


# ─────────────────────────────────────────────────────────────────
#  @Fallback — resolve-time default, yields to any active non-fallback
#  binding (Quarkus @DefaultBean parity, plan 017)
# ─────────────────────────────────────────────────────────────────


def Fallback(target: Any) -> Any:
    """Mark a class or ``@Provider`` function/method as a default binding.

    Rule (one sentence, plan 017 §Design): for a request
    ``(interface, qualifier, priority)``, a ``@Fallback`` binding is a
    candidate **iff** the request's candidate set contains no active
    non-fallback binding. Composes with ``@Profile``/``@Alternative`` by
    AND — a ``@Fallback`` binding that is itself inactive (profile off,
    alternative not enabled) is simply invisible, same as any other
    inactive binding; it is not "shadowed", it never entered the candidate
    set to begin with.

    Evaluated **lazily, at resolve time** — the same moment ``@Profile``/
    ``@Alternative``/``@Requires`` are checked, inside ``_filter()`` — so a
    shadowing binding registered *after* the fallback still wins on the
    very next lookup. This mirrors the mutable-after-registration model
    ``enable_alternative()``/``activate_profile()`` already rely on, and
    deliberately rejects a registration-time check (ASP.NET ``TryAdd*``):
    a fallback ``scan()``ned before the real binding is ``install()``ed
    would otherwise "win" the race and never yield (plan 017 §Design
    "Alternatives considered").

    Usable on classes **and** ``@Provider`` functions, including
    ``@Configuration`` bound methods and ``@Provider @property`` getters —
    place ``@Fallback`` **beneath** ``@property``, the same placement rule
    ``@Profile`` has. Decorator order relative to
    ``@Singleton``/``@Component``/``@Provider``/``@Profile``/
    ``@Alternative`` is irrelevant: bindings are constructed at
    ``bind()``/``register()``/``provide()``/``scan()`` time, long after
    every decorator has already run.

    Contrast with ``@Default``: ``@Default`` is a **qualifier** meaning "no
    named qualifier" (selection by *name*); ``@Fallback`` is an
    **activation rule** (selection by *presence of a competitor*) — the two
    solve unrelated problems despite the similar-sounding names (plan 017
    §Non-goals, rejecting the ``@DefaultBean`` name for exactly this
    collision risk).

    ``container.validate()`` reports each shadowed, active ``@Fallback``
    binding as ``IssueKind.FALLBACK_SHADOWED`` at ``Severity.INFO`` — never
    an error or warning, since shadowing is the feature working as
    designed; the issue exists purely so a wiring report can explain *why*
    a default is not the live binding.

    Jakarta/Quarkus note: CDI itself has no fallback primitive; this
    mirrors Quarkus's ``@DefaultBean`` (research 010 §19-22) adapted to
    providify's resolve-time evaluation model instead of Quarkus's
    build-time one (research 017 §41).

    Args:
        target: The class or function/method to mark. Returned unchanged
            (same object identity) — this decorator carries no arguments,
            so it is always used bare (``@Fallback``, no parentheses).

    Returns:
        *target*, unmodified except for the marker stamped on its own
        ``__dict__``.

    Thread safety: Decoration happens at import time on a single thread in
        every documented usage — see ``_set_fallback_marker``'s note.
    Async safety:  No await points; pure marker stamping.

    Edge cases:
        - A subclass of a ``@Fallback`` class does **not** inherit the
          marker (``__dict__``-only lookup, matching ``@Profile``) — it
          must be re-decorated to also be a fallback.
        - Two ``@Fallback`` bindings tied at equal priority, with no active
          non-fallback competitor: neither is dropped by the post-filter,
          so ``get()`` falls back to today's "first registered wins" rule
          and ``validate()`` reports the tie as the pre-existing
          ``IssueKind.AMBIGUOUS_BINDING`` — never a second issue kind for
          this.
        - **Not evicted**: a ``@Fallback`` singleton resolved and cached
          *before* its shadowing binding is registered stays cached (and is
          still torn down at ``shutdown()``); a dependent constructed
          earlier keeps its reference to it. This mirrors
          ``activate_profile()``'s documented caveat exactly.
          ``override()``/``reset_binding()`` remain the eviction tools.

    Example:
        @Fallback
        @Singleton
        class InMemoryCache(Cache): ...

        @Singleton
        class RedisCache(Cache): ...

        container.bind(Cache, InMemoryCache)
        container.get(Cache)              # -> InMemoryCache (sole candidate)

        container.bind(Cache, RedisCache)
        container.get(Cache)              # -> RedisCache (fallback yields)
    """
    _set_fallback_marker(target)
    return target


# ─────────────────────────────────────────────────────────────────
#  @Stereotype — composed annotation bundles (Jakarta CDI @Stereotype)
# ─────────────────────────────────────────────────────────────────


def Stereotype(
    *,
    scope: Scope = Scope.DEPENDENT,
    qualifier: str | type | None = None,
    priority: int = 0,
    inherited: bool = False,
) -> Callable[[type], type]:
    """Create a reusable composed DI decorator (Jakarta CDI @Stereotype parity).

    Stamps a class as a stereotype annotation. When applied as a decorator
    to a target class, it provides default DIMetadata values. Explicit scope
    decorators on the target always win over stereotype defaults.

    Example:
        @Stereotype(scope=Scope.SINGLETON, qualifier="service")
        class ServiceLayer: ...

        @ServiceLayer          # applies scope=SINGLETON, qualifier="service"
        class MyService: ...

        @Singleton             # explicit scope overrides stereotype
        @ServiceLayer
        class OverriddenService: ...
    """
    smeta = StereotypeMetadata(
        scope=scope, qualifier=qualifier, priority=priority, inherited=inherited
    )

    def _apply(target: type) -> type:
        """Apply stereotype metadata to *target*, respecting any explicit decorators."""
        _set_stereotype(target, smeta)
        existing = _get_own_metadata(target)
        if existing is not None:
            # An explicit DI decorator (e.g. @Singleton) already stamped metadata.
            # Stereotype fills only gaps: qualifier if unset, priority if still 0,
            # inherited OR'd in. The explicit scope always wins.
            _set_metadata(
                target,
                DIMetadata(
                    scope=existing.scope,
                    qualifier=(
                        existing.qualifier if existing.qualifier is not None else smeta.qualifier
                    ),
                    priority=(existing.priority if existing.priority != 0 else smeta.priority),
                    inherited=existing.inherited or smeta.inherited,
                    track=existing.track,
                ),
            )
        else:
            _set_metadata(
                target,
                DIMetadata(
                    scope=smeta.resolved_scope(),
                    qualifier=smeta.qualifier,
                    priority=smeta.priority,
                    inherited=smeta.inherited,
                ),
            )
        return target

    return _apply


# ─────────────────────────────────────────────────────────────────
#  @Decorator — interface-level bean delegation
# ─────────────────────────────────────────────────────────────────


def Decorator(cls: type) -> type:
    """Mark a class as a CDI-style bean decorator.

    A @Decorator wraps another implementation of the same interface,
    using @Delegate injection to receive the wrapped bean. Multiple
    decorators stack by priority (highest priority wraps outermost).

    Example:
        @Decorator
        @Singleton
        class LoggingMailer(Mailer):
            def __init__(self, delegate: Annotated[Mailer, DelegateMeta()]) -> None:
                self._delegate = delegate

            def send(self, msg: str) -> None:
                print(f"[LOG] {msg}")
                self._delegate.send(msg)
    """
    _set_decorator_marker(cls)
    return cls
