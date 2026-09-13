"""Unit and integration tests for Instance[T] and container.is_resolvable().

Instance[T] is the Jakarta CDI-inspired programmatic injection handle.
Unlike Inject[T] (eager, single) or InjectInstances[T] (eager, all),
Instance[T] injects an InstanceProxy that defers ALL resolution to call time.
Qualifiers and priorities are NOT baked into the annotation — they are passed
as keyword arguments on each proxy method call, making one proxy usable with
multiple filters in the same component.

Covered:
    InstanceProxy unit (isolated from the real container):
    - Instance[T] in a constructor creates an InstanceProxy, not the dep itself
    - proxy.get()         calls container.get()            on every invocation
    - proxy.get_all()     calls container.get_all()        on every invocation
    - proxy.resolvable()  calls container.is_resolvable()  on every invocation
    - proxy.get()         forwards qualifier and priority correctly
    - proxy.get_all()     forwards qualifier correctly
    - proxy.resolvable()  forwards qualifier and priority correctly
    - proxy.__repr__      contains the wrapped type name
    - proxy.get_all()     returns [] when container.get_all() raises LookupError

    Integration — basic resolution:
    - Instance[T] proxy resolves the highest-priority binding via get()
    - Instance[T] proxy resolves all bindings sorted by priority via get_all()
    - resolvable() returns True when at least one binding is registered
    - resolvable() returns False when no binding is registered
    - get_all() returns [] (not raises) when no binding is registered

    Integration — qualifier filtering at call time:
    - get(qualifier=...) selects only the named binding
    - get_all(qualifier=...) filters the result list
    - resolvable(qualifier=...) is True only for registered qualifiers
    - resolvable(qualifier=...) is False for unknown qualifiers
    - same proxy used with different qualifiers in the same method → correct results
    - priority filtering forwarded correctly via get(priority=...)

    Integration — scope safety:
    - Instance[RequestScoped] in @Singleton passes validation (no LiveInjectionRequiredError)
    - Instance[SessionScoped] in @Singleton passes validation
    - Instance[Component/DEPENDENT] in @Singleton passes validation (no ScopeViolationDetectedError)
    - Instance[RequestScoped] re-resolves per request block via get()
    - Instance[RequestScoped] get_all() returns fresh list per request block

    Integration — async:
    - proxy.aget() resolves the single best instance asynchronously
    - proxy.aget_all() resolves all bindings asynchronously
    - proxy.aget_all() returns [] (not raises) when no bindings are registered

    Integration — class-level annotation:
    - Instance[T] as a class-level attribute annotation receives an InstanceProxy
    - proxy methods on class-level Instance[T] work identically to constructor form

    Type-alias expansion:
    - Instance[T] expands to Annotated[T, InstanceMeta()] at runtime
    - InstanceMeta has no qualifier or priority fields
    - get_type_hints() resolves Instance[T] correctly under from __future__ import annotations

    container.is_resolvable():
    - True when at least one binding matches
    - False when no binding is registered
    - True/False with qualifier filtering
    - True/False with priority filtering
    - Does NOT create any instances (no side effects)
    - Evaluated on every call — reflects dynamic binding registration
"""

from __future__ import annotations

from typing import ClassVar

from providify.container import DIContainer
from providify.decorator.scope import (
    Component,
    RequestScoped,
    SessionScoped,
    Singleton,
)
from providify.type import Instance, InstanceMeta, InstanceProxy

# ─────────────────────────────────────────────────────────────────
#  Domain types shared across all test classes
# ─────────────────────────────────────────────────────────────────


class Notifier:
    """Abstract-style interface for notification backends."""


@Singleton(qualifier="email")
class EmailNotifier(Notifier):
    """Concrete email backend — SINGLETON scope, qualifier='email'."""


@Component(qualifier="sms")
class SmsNotifier(Notifier):
    """Concrete SMS backend — DEPENDENT scope, qualifier='sms'."""


@Component(priority=1)
class LowPriorityNotifier(Notifier):
    """DEPENDENT, no qualifier, priority=1."""


@Component(priority=2)
class HighPriorityNotifier(Notifier):
    """DEPENDENT, no qualifier, priority=2."""


@RequestScoped
class RequestContext:
    """REQUEST scope — one instance per request block.

    instance_count lets tests verify re-resolution across request blocks.
    """

    instance_count: int = 0

    def __init__(self) -> None:
        RequestContext.instance_count += 1
        self.index = RequestContext.instance_count

    @classmethod
    def reset(cls) -> None:
        """Reset counter between tests so assertions stay independent."""
        cls.instance_count = 0


@SessionScoped
class SessionContext:
    """SESSION scope — one instance per active session ID."""

    instance_count: int = 0

    def __init__(self) -> None:
        SessionContext.instance_count += 1
        self.index = SessionContext.instance_count

    @classmethod
    def reset(cls) -> None:
        SessionContext.instance_count = 0


# ─────────────────────────────────────────────────────────────────
#  InstanceProxy unit tests (fake container — fully isolated)
# ─────────────────────────────────────────────────────────────────


class TestInstanceProxyUnit:
    """Unit tests for InstanceProxy backed by a call-recording fake container.

    DESIGN: Using a fake container (not the real DIContainer) isolates
    InstanceProxy's forwarding behaviour from the container's resolution logic.
    This lets us assert on exact call counts and exact argument values without
    worrying about what the container does with them.
    """

    # ── Fake container helpers ────────────────────────────────────

    def _make_fake_container(self, resolved_value: object) -> tuple[object, list]:
        """Return (fake_container, calls) where calls records every get/get_all/is_resolvable call.

        Each entry in calls is a dict with keys: method, tp, qualifier, priority.
        """
        calls: list[dict] = []

        class FakeContainer:
            def get(
                self_,  # noqa: N805
                tp: type,
                *,
                qualifier: str | None = None,
                priority: int | None = None,
            ) -> object:
                calls.append(
                    {
                        "method": "get",
                        "tp": tp,
                        "qualifier": qualifier,
                        "priority": priority,
                    }
                )
                return resolved_value

            def get_all(
                self_,  # noqa: N805
                tp: type,
                *,
                qualifier: str | None = None,
            ) -> list:
                calls.append({"method": "get_all", "tp": tp, "qualifier": qualifier})
                return [resolved_value]

            def is_resolvable(
                self_,  # noqa: N805
                tp: type,
                *,
                qualifier: str | None = None,
                priority: int | None = None,
            ) -> bool:
                calls.append(
                    {
                        "method": "is_resolvable",
                        "tp": tp,
                        "qualifier": qualifier,
                        "priority": priority,
                    }
                )
                return True

        return FakeContainer(), calls  # type: ignore[return-value]

    def _make_raising_container(self) -> object:
        """Return a fake container whose get_all() always raises LookupError.

        Used to verify that InstanceProxy.get_all() swallows the error and returns [].
        """

        class RaisingContainer:
            def get_all(self_, tp: type, *, qualifier: str | None = None) -> list:  # noqa: N805
                raise LookupError("no bindings")

            async def aget_all(self_, tp: type, *, qualifier: str | None = None) -> list:  # noqa: N805
                raise LookupError("no bindings")

        return RaisingContainer()  # type: ignore[return-value]

    # ── Constructor injects proxy, not dep ────────────────────────

    def test_injection_creates_proxy_not_dep(self, container: DIContainer) -> None:
        """Instance[T] in a constructor must produce an InstanceProxy, not the resolved dep.

        This is the core contract of Instance[T]: resolution is always deferred
        to the point where .get() / .get_all() is called, not at construction time.
        """

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        # Must be a proxy — not a resolved Notifier
        assert isinstance(svc.n, InstanceProxy)

    # ── get() delegation ─────────────────────────────────────────

    def test_get_delegates_to_container_get(self) -> None:
        """proxy.get() must call container.get() and return its result."""
        resolved = object()
        fake, calls = self._make_fake_container(resolved)
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        result = proxy.get()

        assert result is resolved
        assert len(calls) == 1
        assert calls[0]["method"] == "get"
        assert calls[0]["tp"] is Notifier

    def test_get_forwards_qualifier(self) -> None:
        """proxy.get(qualifier=...) must forward the qualifier to container.get()."""
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.get(qualifier="email")

        assert calls[0]["qualifier"] == "email"

    def test_get_forwards_priority(self) -> None:
        """proxy.get(priority=...) must forward the priority to container.get()."""
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.get(priority=5)

        assert calls[0]["priority"] == 5

    def test_get_called_multiple_times_delegates_each_time(self) -> None:
        """proxy.get() must call container.get() on every invocation — no caching.

        Unlike LazyProxy (which short-circuits after the first call), InstanceProxy
        has no _resolved guard — every call goes to the container.
        """
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.get()
        proxy.get()
        proxy.get()

        # Three proxy.get() calls → exactly three container.get() calls
        assert len(calls) == 3
        assert all(c["method"] == "get" for c in calls)

    # ── get_all() delegation ──────────────────────────────────────

    def test_get_all_delegates_to_container_get_all(self) -> None:
        """proxy.get_all() must call container.get_all() and return its result."""
        resolved = object()
        fake, calls = self._make_fake_container(resolved)
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        result = proxy.get_all()

        assert result == [resolved]
        assert calls[0]["method"] == "get_all"
        assert calls[0]["tp"] is Notifier

    def test_get_all_forwards_qualifier(self) -> None:
        """proxy.get_all(qualifier=...) must forward the qualifier to container.get_all()."""
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.get_all(qualifier="sms")

        assert calls[0]["qualifier"] == "sms"

    def test_get_all_returns_empty_list_when_lookup_error(self) -> None:
        """proxy.get_all() must return [] instead of propagating LookupError.

        DESIGN: The whole point of Instance[T].get_all() is to handle the
        'zero or more' case without requiring a try/except at the call site.
        This is the key behavioural difference from container.get_all().
        """
        proxy = InstanceProxy(self._make_raising_container(), Notifier)  # type: ignore[arg-type]

        result = proxy.get_all()

        assert result == []

    # ── resolvable() delegation ───────────────────────────────────

    def test_resolvable_delegates_to_container_is_resolvable(self) -> None:
        """proxy.resolvable() must call container.is_resolvable() — not container.get()."""
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        result = proxy.resolvable()

        assert result is True
        assert calls[0]["method"] == "is_resolvable"
        assert calls[0]["tp"] is Notifier

    def test_resolvable_forwards_qualifier(self) -> None:
        """proxy.resolvable(qualifier=...) must forward qualifier to is_resolvable()."""
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.resolvable(qualifier="email")

        assert calls[0]["qualifier"] == "email"

    def test_resolvable_forwards_priority(self) -> None:
        """proxy.resolvable(priority=...) must forward priority to is_resolvable()."""
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.resolvable(priority=3)

        assert calls[0]["priority"] == 3

    def test_resolvable_does_not_call_get(self) -> None:
        """proxy.resolvable() must use is_resolvable(), never container.get().

        DESIGN: container.get() would create and potentially cache instances as
        a side effect, which is wrong for a state-free predicate.
        """
        fake, calls = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        proxy.resolvable()

        # Only is_resolvable should have been called — no get() side effect
        assert all(c["method"] == "is_resolvable" for c in calls)

    # ── __repr__ ──────────────────────────────────────────────────

    def test_repr_contains_type_name(self) -> None:
        """proxy.__repr__ must include the wrapped type's name for debuggability."""
        fake, _ = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        r = repr(proxy)

        assert "InstanceProxy" in r
        assert "Notifier" in r

    def test_repr_contains_unresolved_marker(self) -> None:
        """proxy.__repr__ must say 'unresolved' — no qualifier/priority stored on proxy."""
        fake, _ = self._make_fake_container(object())
        proxy = InstanceProxy(fake, Notifier)  # type: ignore[arg-type]

        assert "unresolved" in repr(proxy)


# ─────────────────────────────────────────────────────────────────
#  Integration — basic resolution
# ─────────────────────────────────────────────────────────────────


class TestInstanceProxyIntegration:
    """Integration tests against the real DIContainer.

    These tests verify that Instance[T] works correctly end-to-end:
    construction-time proxy injection, call-time resolution, and the
    interaction with the container's binding registry.
    """

    def test_proxy_get_returns_single_best_priority_instance(self, container: DIContainer) -> None:
        """proxy.get() must return the highest-priority matching binding."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, LowPriorityNotifier)  # priority=1
        container.bind(Notifier, HighPriorityNotifier)  # priority=2
        container.register(AlertService)

        svc = container.get(AlertService)

        # container.get() selects the highest-priority number candidate —
        # priority=2 (HighPriorityNotifier) beats priority=1 (LowPriorityNotifier)
        assert isinstance(svc.n.get(), HighPriorityNotifier)

    def test_proxy_get_all_returns_all_sorted_by_priority(self, container: DIContainer) -> None:
        """proxy.get_all() must return all bindings sorted by ascending priority."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, LowPriorityNotifier)  # priority=1
        container.bind(Notifier, HighPriorityNotifier)  # priority=2
        container.register(AlertService)

        svc = container.get(AlertService)
        all_n = svc.n.get_all()

        assert len(all_n) == 2
        # Sorted ascending: priority=1 first, priority=2 second
        assert isinstance(all_n[0], LowPriorityNotifier)
        assert isinstance(all_n[1], HighPriorityNotifier)

    def test_resolvable_true_when_binding_registered(self, container: DIContainer) -> None:
        """proxy.resolvable() must return True when at least one binding exists."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert svc.n.resolvable() is True

    def test_resolvable_false_when_no_binding_registered(self, container: DIContainer) -> None:
        """proxy.resolvable() must return False when no bindings match."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        # Notifier NOT registered — proxy still injected (no eager resolution)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert svc.n.resolvable() is False

    def test_get_all_returns_empty_list_when_no_binding(self, container: DIContainer) -> None:
        """proxy.get_all() must return [] when no bindings are registered — not raise."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.register(AlertService)

        svc = container.get(AlertService)

        # Must not raise LookupError — empty list is the expected result
        result = svc.n.get_all()

        assert result == []

    def test_proxy_resolves_concrete_class_directly(self, container: DIContainer) -> None:
        """proxy.get() works when T is a concrete class, not just an interface."""

        @Singleton
        class Consumer:
            def __init__(self, n: Instance[EmailNotifier]) -> None:
                self.n = n

        container.register(EmailNotifier)
        container.register(Consumer)

        svc = container.get(Consumer)

        assert isinstance(svc.n.get(), EmailNotifier)


# ─────────────────────────────────────────────────────────────────
#  Integration — qualifier and priority filtering at call time
# ─────────────────────────────────────────────────────────────────


class TestInstanceProxyQualifierFiltering:
    """Verify that qualifiers and priorities passed at call time filter correctly.

    DESIGN: The key differentiator of Instance[T] over InjectInstances[T] is that
    the same proxy handle can be used with different qualifier filters in the same
    component body — qualifiers are NOT baked into the annotation.
    """

    def test_get_with_qualifier_selects_named_binding(self, container: DIContainer) -> None:
        """proxy.get(qualifier='sms') must return only the SMS-qualified binding."""

        @Singleton
        class NotificationRouter:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(NotificationRouter)

        svc = container.get(NotificationRouter)

        assert isinstance(svc.n.get(qualifier="email"), EmailNotifier)
        assert isinstance(svc.n.get(qualifier="sms"), SmsNotifier)

    def test_get_all_with_qualifier_filters_list(self, container: DIContainer) -> None:
        """proxy.get_all(qualifier='email') must return only email-qualified bindings."""

        @Singleton
        class NotificationRouter:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(NotificationRouter)

        svc = container.get(NotificationRouter)

        email_only = svc.n.get_all(qualifier="email")
        assert len(email_only) == 1
        assert isinstance(email_only[0], EmailNotifier)

        sms_only = svc.n.get_all(qualifier="sms")
        assert len(sms_only) == 1
        assert isinstance(sms_only[0], SmsNotifier)

    def test_resolvable_true_for_known_qualifier(self, container: DIContainer) -> None:
        """proxy.resolvable(qualifier='email') → True when that qualifier is registered."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert svc.n.resolvable(qualifier="email") is True

    def test_resolvable_false_for_unknown_qualifier(self, container: DIContainer) -> None:
        """proxy.resolvable(qualifier='fax') → False when that qualifier is not registered."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert svc.n.resolvable(qualifier="fax") is False

    def test_same_proxy_used_with_multiple_qualifiers(self, container: DIContainer) -> None:
        """The same Instance[T] proxy can serve different qualifiers in the same method.

        This is the core flexibility advantage over InjectInstances[T] with a
        baked-in qualifier: one proxy replaces the need for multiple injections.
        """

        @Singleton
        class RoutingService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self._n = n

            def route(self, channel: str) -> Notifier | None:
                """Dispatch to the right notifier based on channel name."""
                if self._n.resolvable(qualifier=channel):
                    return self._n.get(qualifier=channel)
                return None

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(RoutingService)

        svc = container.get(RoutingService)

        # Same proxy, different qualifier at call time
        assert isinstance(svc.route("email"), EmailNotifier)
        assert isinstance(svc.route("sms"), SmsNotifier)
        assert svc.route("fax") is None

    def test_get_with_priority_filter(self, container: DIContainer) -> None:
        """proxy.get(priority=2) must return only the binding with priority=2."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, LowPriorityNotifier)  # priority=1
        container.bind(Notifier, HighPriorityNotifier)  # priority=2
        container.register(AlertService)

        svc = container.get(AlertService)

        assert isinstance(svc.n.get(priority=2), HighPriorityNotifier)

    def test_get_all_with_unknown_qualifier_returns_empty_list(
        self, container: DIContainer
    ) -> None:
        """proxy.get_all(qualifier='fax') must return [] when qualifier is not registered."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = svc.n.get_all(qualifier="fax")

        assert result == []


# ─────────────────────────────────────────────────────────────────
#  Integration — scope safety
# ─────────────────────────────────────────────────────────────────


class TestInstanceProxyScopeSafety:
    """Verify that Instance[T] passes scope validation in all cases.

    Instance[T] is treated like Live[T] by the scope validator — it is always
    safe because the proxy stores no resolved instance. The SINGLETON stores the
    InstanceProxy itself (not a Notifier), so no scope boundary is crossed at
    construction time.
    """

    def test_instance_of_request_scoped_in_singleton_passes_validation(
        self, container: DIContainer
    ) -> None:
        """Instance[RequestScoped] in @Singleton must NOT raise LiveInjectionRequiredError.

        Inject[T] and Lazy[T] for a REQUEST-scoped dep raise that error because
        they capture one instance at construction time. InstanceProxy does not —
        it defers resolution, so the validator exempts it.
        """

        @Singleton
        class ProcessingService:
            def __init__(self, ctx: Instance[RequestContext]) -> None:
                self.ctx = ctx

        container.register(RequestContext)
        container.register(ProcessingService)

        # Must not raise — validation passes for Instance[RequestScoped]
        with container.scope_context.request():
            svc = container.get(ProcessingService)

        assert isinstance(svc.ctx, InstanceProxy)

    def test_instance_of_session_scoped_in_singleton_passes_validation(
        self, container: DIContainer
    ) -> None:
        """Instance[SessionScoped] in @Singleton must NOT raise LiveInjectionRequiredError."""

        @Singleton
        class UserService:
            def __init__(self, ctx: Instance[SessionContext]) -> None:
                self.ctx = ctx

        container.register(SessionContext)
        container.register(UserService)

        with container.scope_context.session("user-abc"):
            svc = container.get(UserService)

        assert isinstance(svc.ctx, InstanceProxy)

    def test_instance_of_dependent_in_singleton_passes_scope_validation(
        self, container: DIContainer
    ) -> None:
        """Instance[DEPENDENT] in @Singleton must NOT raise ScopeViolationDetectedError.

        Without Instance[T], Inject[Component] in a @Singleton raises
        ScopeViolationDetectedError because the singleton pins one DEPENDENT
        instance. With Instance[T], the proxy is what's stored — no pinning.
        """

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, SmsNotifier)  # DEPENDENT scope
        container.register(AlertService)

        # Must not raise ScopeViolationDetectedError
        svc = container.get(AlertService)

        assert isinstance(svc.n, InstanceProxy)

    def test_instance_request_scoped_re_resolves_per_request(self, container: DIContainer) -> None:
        """proxy.get() inside different request blocks must return different instances.

        This proves that Instance[RequestScoped] is scope-safe: each call to
        proxy.get() inside a request block returns that request's cached instance,
        not a stale one from a previous request.
        """
        RequestContext.reset()

        @Singleton
        class ProcessingService:
            def __init__(self, ctx: Instance[RequestContext]) -> None:
                self.ctx = ctx

        container.register(RequestContext)
        container.register(ProcessingService)

        svc = None

        with container.scope_context.request():
            svc = container.get(ProcessingService)
            first_ctx = svc.ctx.get()

        with container.scope_context.request():
            # Same singleton, same proxy, different request → different instance
            second_ctx = svc.ctx.get()

        assert first_ctx is not second_ctx
        assert first_ctx.index != second_ctx.index

    def test_instance_get_all_re_resolves_per_request(self, container: DIContainer) -> None:
        """proxy.get_all() must return fresh instances in each new request scope.

        Verifies that get_all() does not cache results between calls — each call
        goes back to the container which routes through the active ScopeContext.
        """
        RequestContext.reset()

        @Singleton
        class ProcessingService:
            def __init__(self, ctx: Instance[RequestContext]) -> None:
                self.ctx = ctx

        container.register(RequestContext)
        container.register(ProcessingService)

        svc = None

        with container.scope_context.request():
            svc = container.get(ProcessingService)
            first_result = svc.ctx.get_all()

        with container.scope_context.request():
            second_result = svc.ctx.get_all()

        # Both returned one item — but different RequestContext instances
        assert len(first_result) == 1
        assert len(second_result) == 1
        assert first_result[0] is not second_result[0]


# ─────────────────────────────────────────────────────────────────
#  Integration — async paths
# ─────────────────────────────────────────────────────────────────


class TestInstanceProxyAsync:
    """Async path: aget() and aget_all() must mirror their sync counterparts."""

    async def test_aget_resolves_single_instance(self, container: DIContainer) -> None:
        """proxy.aget() must resolve and return the single best-priority instance."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = await svc.n.aget()

        assert isinstance(result, EmailNotifier)

    async def test_aget_with_qualifier(self, container: DIContainer) -> None:
        """proxy.aget(qualifier=...) must forward the qualifier on the async path."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert isinstance(await svc.n.aget(qualifier="email"), EmailNotifier)
        assert isinstance(await svc.n.aget(qualifier="sms"), SmsNotifier)

    async def test_aget_all_returns_all_instances(self, container: DIContainer) -> None:
        """proxy.aget_all() must return all matching bindings, sorted by priority."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, LowPriorityNotifier)
        container.bind(Notifier, HighPriorityNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = await svc.n.aget_all()

        assert len(result) == 2
        assert isinstance(result[0], LowPriorityNotifier)
        assert isinstance(result[1], HighPriorityNotifier)

    async def test_aget_all_returns_empty_list_when_no_binding(
        self, container: DIContainer
    ) -> None:
        """proxy.aget_all() must return [] when no bindings are registered — not raise."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.register(AlertService)

        svc = container.get(AlertService)

        result = await svc.n.aget_all()

        assert result == []

    async def test_aget_all_with_qualifier(self, container: DIContainer) -> None:
        """proxy.aget_all(qualifier=...) must filter the result on the async path."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = await svc.n.aget_all(qualifier="sms")

        assert len(result) == 1
        assert isinstance(result[0], SmsNotifier)

    async def test_aget_all_unknown_qualifier_returns_empty_list(
        self, container: DIContainer
    ) -> None:
        """proxy.aget_all(qualifier='fax') → [] on the async path."""

        @Singleton
        class AlertService:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = await svc.n.aget_all(qualifier="fax")

        assert result == []


# ─────────────────────────────────────────────────────────────────
#  Integration — class-level attribute injection
# ─────────────────────────────────────────────────────────────────


class TestInstanceProxyClassLevel:
    """Instance[T] as a class-level annotation is resolved and set after construction.

    Class-level injection works identically to constructor-parameter injection —
    the container detects the InstanceMeta marker via _has_providify_metadata()
    and injects an InstanceProxy after __init__ returns.
    """

    def test_class_level_instance_annotation_injects_proxy(self, container: DIContainer) -> None:
        """A class-level Instance[T] annotation must be set to an InstanceProxy."""

        @Singleton
        class AlertService:
            # Class-level annotation — resolved after __init__
            notifiers: Instance[Notifier]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert isinstance(svc.notifiers, InstanceProxy)

    def test_class_level_proxy_get_works(self, container: DIContainer) -> None:
        """An InstanceProxy set via class-level injection must resolve correctly."""

        @Singleton
        class AlertService:
            notifiers: Instance[Notifier]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert isinstance(svc.notifiers.get(), EmailNotifier)

    def test_class_level_proxy_get_all_works(self, container: DIContainer) -> None:
        """proxy.get_all() on a class-level-injected proxy must return all bindings."""

        @Singleton
        class AlertService:
            notifiers: Instance[Notifier]

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = svc.notifiers.get_all()

        assert len(result) == 2

    def test_class_level_proxy_resolvable_works(self, container: DIContainer) -> None:
        """proxy.resolvable() on a class-level proxy must reflect actual registry state."""

        @Singleton
        class AlertService:
            notifiers: Instance[Notifier]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert svc.notifiers.resolvable() is True
        assert svc.notifiers.resolvable(qualifier="fax") is False


# ─────────────────────────────────────────────────────────────────
#  Type-alias expansion regression tests
# ─────────────────────────────────────────────────────────────────


class TestInstanceTypeAliasExpansion:
    """Regression tests for the runtime expansion of Instance[T].

    At runtime (when TYPE_CHECKING is False), Instance is _InstanceAlias().
    Its __getitem__ must return Annotated[T, InstanceMeta()] so the container's
    _has_providify_metadata() and _get_providify_metadata() helpers find the
    InstanceMeta marker and dispatch to InstanceProxy creation.
    """

    def test_instance_subscript_expands_to_annotated_with_instance_meta(
        self,
    ) -> None:
        """Instance[T] must expand to Annotated[T, InstanceMeta()] at runtime."""
        from typing import Annotated, get_args, get_origin

        result = Instance[Notifier]

        assert get_origin(result) is Annotated, (
            f"Instance[T] must expand to Annotated[T, InstanceMeta()]; got {result!r}"
        )
        args = get_args(result)
        assert args[0] is Notifier
        assert isinstance(args[1], InstanceMeta), (
            f"Second Annotated arg must be InstanceMeta; got {type(args[1])!r}"
        )

    def test_instance_meta_has_no_qualifier_or_priority_fields(self) -> None:
        """InstanceMeta must be an empty marker — no qualifier or priority attributes.

        DESIGN: Filtering is done at call time on the proxy methods, not baked
        into the annotation. Verifying the absence of those fields here guards
        against accidental re-introduction.
        """
        meta = InstanceMeta()

        assert not hasattr(meta, "qualifier"), (
            "InstanceMeta must not have a qualifier field — filtering is call-time"
        )
        assert not hasattr(meta, "priority"), (
            "InstanceMeta must not have a priority field — filtering is call-time"
        )

    def test_instance_meta_instances_are_equal(self) -> None:
        """Two InstanceMeta() instances must be equal (dataclass __eq__ with no fields)."""
        assert InstanceMeta() == InstanceMeta()

    def test_get_type_hints_resolves_instance_annotation(self) -> None:
        """get_type_hints() must resolve Instance[T] to Annotated[T, InstanceMeta()]
        even when from __future__ import annotations is active.

        Under PEP 563, all annotations are stored as strings. get_type_hints()
        evaluates them against the module globals. If Instance is not visible or
        its __getitem__ produces the wrong type, the InstanceMeta marker is lost
        and the container falls back to plain type resolution — a silent bug.
        """
        from typing import Annotated, get_args, get_origin, get_type_hints

        @Component
        class ServiceWithInstance:
            def __init__(self, n: Instance[Notifier]) -> None:
                self.n = n

        hints = get_type_hints(ServiceWithInstance.__init__, include_extras=True)

        assert "n" in hints, "parameter 'n' must appear in resolved type hints"
        n_hint = hints["n"]

        assert get_origin(n_hint) is Annotated, (
            f"get_type_hints must resolve Instance[Notifier] to Annotated; got {n_hint!r}"
        )
        inner_type, meta = get_args(n_hint)[:2]
        assert inner_type is Notifier
        assert isinstance(meta, InstanceMeta)


# ─────────────────────────────────────────────────────────────────
#  container.is_resolvable() tests
# ─────────────────────────────────────────────────────────────────


class TestContainerIsResolvable:
    """Tests for the public DIContainer.is_resolvable() method.

    is_resolvable() is a side-effect-free predicate that checks whether at least
    one registered binding matches the given type, qualifier, and priority.
    It is the public counterpart to the private _filter() helper, designed so
    InstanceProxy.resolvable() can stay on the public API surface.
    """

    def test_true_when_binding_is_registered(self, container: DIContainer) -> None:
        """is_resolvable(T) must return True when T has a registered binding."""
        container.bind(Notifier, EmailNotifier)

        assert container.is_resolvable(Notifier) is True

    def test_false_when_no_binding_registered(self, container: DIContainer) -> None:
        """is_resolvable(T) must return False when no binding exists for T."""
        # Notifier is NOT registered

        assert container.is_resolvable(Notifier) is False

    def test_true_with_matching_qualifier(self, container: DIContainer) -> None:
        """is_resolvable(T, qualifier='email') → True when that qualifier exists."""
        container.bind(Notifier, EmailNotifier)  # qualifier='email'

        assert container.is_resolvable(Notifier, qualifier="email") is True

    def test_false_with_nonmatching_qualifier(self, container: DIContainer) -> None:
        """is_resolvable(T, qualifier='fax') → False when that qualifier is not registered."""
        container.bind(Notifier, EmailNotifier)  # qualifier='email'

        assert container.is_resolvable(Notifier, qualifier="fax") is False

    def test_true_with_matching_priority(self, container: DIContainer) -> None:
        """is_resolvable(T, priority=1) → True when a binding with priority=1 exists."""
        container.bind(Notifier, LowPriorityNotifier)  # priority=1

        assert container.is_resolvable(Notifier, priority=1) is True

    def test_false_with_nonmatching_priority(self, container: DIContainer) -> None:
        """is_resolvable(T, priority=99) → False when no binding has priority=99."""
        container.bind(Notifier, LowPriorityNotifier)  # priority=1

        assert container.is_resolvable(Notifier, priority=99) is False

    def test_does_not_create_instances(self, container: DIContainer) -> None:
        """is_resolvable() must not create or cache any instances.

        This test verifies there are no side effects: calling is_resolvable()
        before get() must not populate the singleton cache.
        """

        @Singleton
        class TrackingNotifier(Notifier):
            created_count: int = 0

            def __init__(self) -> None:
                TrackingNotifier.created_count += 1

        container.register(TrackingNotifier)

        # Call is_resolvable multiple times
        container.is_resolvable(TrackingNotifier)
        container.is_resolvable(TrackingNotifier)
        container.is_resolvable(TrackingNotifier)

        # No instances must have been created
        assert TrackingNotifier.created_count == 0

    def test_reflects_newly_added_binding(self, container: DIContainer) -> None:
        """is_resolvable() must reflect the current registry state on every call.

        Since results are not cached, a binding added after a False result
        must immediately cause the next call to return True.
        """
        assert container.is_resolvable(Notifier) is False

        container.bind(Notifier, EmailNotifier)

        assert container.is_resolvable(Notifier) is True

    def test_false_after_no_qualifying_qualifier_combination(self, container: DIContainer) -> None:
        """is_resolvable with both qualifier AND priority returns False
        if no single binding matches both simultaneously.
        """
        container.bind(Notifier, EmailNotifier)  # qualifier='email', priority=0
        container.bind(Notifier, LowPriorityNotifier)  # qualifier=None, priority=1

        # 'email' qualifier exists, but not with priority=1
        assert container.is_resolvable(Notifier, qualifier="email", priority=1) is False
        # priority=1 exists, but not with qualifier='email'
        assert container.is_resolvable(Notifier, qualifier="email", priority=1) is False

    def test_true_when_both_qualifier_and_priority_match(self, container: DIContainer) -> None:
        """is_resolvable(T, qualifier='q', priority=p) → True when exact match exists."""

        @Singleton(qualifier="special", priority=7)
        class SpecialNotifier(Notifier):
            pass

        container.bind(Notifier, SpecialNotifier)

        assert container.is_resolvable(Notifier, qualifier="special", priority=7) is True
        # Partial matches must also return True / False correctly
        assert container.is_resolvable(Notifier, qualifier="special") is True
        assert container.is_resolvable(Notifier, priority=7) is True
        assert container.is_resolvable(Notifier, qualifier="other") is False


# ─────────────────────────────────────────────────────────────────
#  ClassVar[Instance[T]] — regression suite for ClassVar unwrapping
# ─────────────────────────────────────────────────────────────────


class TestClassVarInstanceAnnotation:
    """ClassVar[Instance[T]] must be treated identically to plain Instance[T].

    The bug this covers: ClassVar[Annotated[T, InstanceMeta()]] has
    get_origin() == ClassVar, not Annotated — so the container's
    _has_providify_metadata() returned False and the annotation was silently
    skipped.  _unwrap_classvar() is now called at every injection boundary
    to strip the outer ClassVar before dispatching.
    """

    def test_classvar_instance_injects_proxy(self, container: DIContainer) -> None:
        """ClassVar[Instance[T]] must inject an InstanceProxy, not be silently skipped."""

        @Singleton
        class AlertService:
            # ClassVar form — signals this is a class-level attribute, but the
            # container must still inject an InstanceProxy here.
            notifiers: ClassVar[Instance[Notifier]]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        # Must be an InstanceProxy — not the default ClassVar sentinel (missing attr)
        assert isinstance(svc.notifiers, InstanceProxy)

    def test_classvar_instance_proxy_get_works(self, container: DIContainer) -> None:
        """proxy.get() on a ClassVar[Instance[T]] proxy must resolve the correct type."""

        @Singleton
        class AlertService:
            notifiers: ClassVar[Instance[Notifier]]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert isinstance(svc.notifiers.get(), EmailNotifier)

    def test_classvar_instance_proxy_get_all_works(self, container: DIContainer) -> None:
        """proxy.get_all() on a ClassVar[Instance[T]] proxy must return all bindings."""

        @Singleton
        class AlertService:
            notifiers: ClassVar[Instance[Notifier]]

        container.bind(Notifier, EmailNotifier)
        container.bind(Notifier, SmsNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = svc.notifiers.get_all()

        assert len(result) == 2

    def test_classvar_instance_proxy_resolvable(self, container: DIContainer) -> None:
        """proxy.resolvable() on ClassVar[Instance[T]] must reflect registry state."""

        @Singleton
        class AlertService:
            notifiers: ClassVar[Instance[Notifier]]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        assert svc.notifiers.resolvable() is True
        assert svc.notifiers.resolvable(qualifier="fax") is False

    def test_classvar_instance_scope_safety(self, container: DIContainer) -> None:
        """ClassVar[Instance[RequestScoped]] in @Singleton must pass scope validation.

        Instance[T] is exempt from LiveInjectionRequiredError regardless of how
        the annotation is wrapped — ClassVar[Instance[T]] must carry the same
        exemption as plain Instance[T].
        """

        @Singleton
        class ProcessingService:
            # Mimics the user's original question — ClassVar[Instance[Emailer]]
            ctx: ClassVar[Instance[RequestContext]]

        RequestContext.reset()
        container.register(RequestContext)
        container.register(ProcessingService)

        # Must not raise LiveInjectionRequiredError during validation
        with container.scope_context.request():
            svc = container.get(ProcessingService)
            assert isinstance(svc.ctx, InstanceProxy)

    async def test_classvar_instance_proxy_aget_works(self, container: DIContainer) -> None:
        """proxy.aget() on a ClassVar[Instance[T]] proxy must resolve asynchronously."""

        @Singleton
        class AlertService:
            notifiers: ClassVar[Instance[Notifier]]

        container.bind(Notifier, EmailNotifier)
        container.register(AlertService)

        svc = container.get(AlertService)

        result = await svc.notifiers.aget()

        assert isinstance(result, EmailNotifier)
