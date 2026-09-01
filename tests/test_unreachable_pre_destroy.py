"""RED-mode tests for `IssueKind.UNREACHABLE_PRE_DESTROY` (plan 012).

Upstream gap: P22-PROVIDER-PREDESTROY
(see plans/012-unreachable-pre-destroy-validation.md).

`container.validate()` must report a `UNREACHABLE_PRE_DESTROY` WARNING
whenever a `SINGLETON`-scoped `ProviderBinding` has no `@Disposes` disposer
and the type it produces carries a `@PreDestroy` hook that will therefore
never run.

Every test in this file is expected to fail until `IssueKind`,
`_unreachable_pre_destroy`, and the pass-1b check land (Steps 3-7 of the
plan). `test_generic_produced_type_is_flagged_without_crashing` (E4) is the
one exception worth calling out explicitly: it must fail with
`AttributeError` — not an assertion failure — proving the `get_origin`
normalisation guard is load-bearing and not merely defensive.

DESIGN NOTE: every domain class and `@Configuration`/`@Provider` factory
below lives at module level, not nested inside test functions. Provider
return annotations are resolved against the declaring function's *module*
globals only (no container-supplied localns at registration time — see
`providify/binding.py`'s `_resolve_provider_interface`), so a function-local
class used as a return annotation raises `TypeError` at `install()`/
`provide()` time. Module level is the only shape that works here.
"""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

from providify import (
    Disposes,
    PreDestroy,
    Provider,
)
from providify.container import DIContainer
from providify.decorator.module import Configuration
from providify.decorator.scope import Singleton
from providify.metadata import Scope
from providify.validation import IssueKind, Severity

T = TypeVar("T")


def _unreachable_issues(report):
    kind = IssueKind.UNREACHABLE_PRE_DESTROY  # forces AttributeError if the member is missing
    return [i for i in report.issues if i.kind == kind]


# ─────────────────────────────────────────────────────────────────
#  Domain types + configurations — module level (see docstring above)
# ─────────────────────────────────────────────────────────────────


class RedisCache:
    @PreDestroy
    def close(self) -> None:
        pass


@Configuration
class RedisCacheConfiguration:
    @Provider(singleton=True)
    def redis_cache(self) -> RedisCache:
        return RedisCache()


class AsyncResource:
    @PreDestroy
    async def stop(self) -> None:
        pass


@Configuration
class AsyncResourceConfiguration:
    @Provider(singleton=True)
    def make_resource(self) -> AsyncResource:
        return AsyncResource()


class BaseResource:
    @PreDestroy
    def teardown(self) -> None:
        pass


class SubResource(BaseResource):
    pass


@Configuration
class SubResourceConfiguration:
    @Provider(singleton=True)
    def make_sub(self) -> SubResource:
        return SubResource()


class User:
    pass


class Repo(Generic[T]):
    @PreDestroy
    def close(self) -> None:
        pass


@Configuration
class RepoConfiguration:
    @Provider(singleton=True, returns=Repo[User])
    def make_repo(self) -> Repo[User]:
        return Repo()


class SmsGateway:
    @PreDestroy
    def shutdown(self) -> None:
        pass


@Configuration
class SmsConfiguration:
    @Provider(singleton=True, qualifier="sms")
    def make_sms(self) -> SmsGateway:
        return SmsGateway()


class BareResource:
    @PreDestroy
    def close(self) -> None:
        pass


@Provider(singleton=True)
def make_bare() -> BareResource:
    return BareResource()


class ConnectionWithDisposer:
    @PreDestroy
    def close(self) -> None:
        # Deliberately unreachable — @Disposes below is the real teardown
        # path. Having @PreDestroy present at all is what makes this a
        # meaningful control for the check.
        pass


@Configuration
class InfraModule:
    @Provider(scope=Scope.SINGLETON)
    def make_conn(self) -> ConnectionWithDisposer:
        return ConnectionWithDisposer()

    @Disposes(ConnectionWithDisposer)
    def close_conn(self, conn: ConnectionWithDisposer) -> None:
        conn.close()


class PlainValue:
    pass


@Configuration
class PlainConfiguration:
    @Provider(singleton=True)
    def make_value(self) -> PlainValue:
        return PlainValue()


class DependentResource:
    @PreDestroy
    def close(self) -> None:
        pass


@Configuration
class DependentConfiguration:
    @Provider(singleton=False)
    def make_dependent(self) -> DependentResource:
        return DependentResource()


class RequestResource:
    @PreDestroy
    def close(self) -> None:
        pass


@Configuration
class RequestConfiguration:
    @Provider(scope=Scope.REQUEST)
    def make_request(self) -> RequestResource:
        return RequestResource()


class SessionResource:
    @PreDestroy
    def close(self) -> None:
        pass


@Configuration
class SessionConfiguration:
    @Provider(scope=Scope.SESSION)
    def make_session(self) -> SessionResource:
        return SessionResource()


@Singleton
class ClassBoundResource:
    @PreDestroy
    def close(self) -> None:
        pass


@Configuration
class NoneConfiguration:
    @Provider(singleton=True)
    def make_none(self) -> None:
        return None


@Provider(singleton=True, returns=Annotated[int, "marker"])
def make_marked() -> Annotated[int, "marker"]:
    return 1


# ─────────────────────────────────────────────────────────────────
#  Positive cases — Step 1
# ─────────────────────────────────────────────────────────────────


class TestUnreachablePreDestroy:
    """Cases that must produce exactly one UNREACHABLE_PRE_DESTROY warning."""

    def test_singleton_provider_with_sync_pre_destroy_is_flagged(
        self, container: DIContainer
    ) -> None:
        """The gap-report reproduction: sync @PreDestroy on a produced type."""
        container.install(RedisCacheConfiguration)
        report = container.validate()

        issues = _unreachable_issues(report)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.WARNING
        assert issue.owner == "@Provider(redis_cache)"
        assert issue.param_name == "close"
        assert issue.requested == "RedisCache"
        assert issue.qualifier is None

    def test_singleton_provider_with_async_pre_destroy_is_flagged(
        self, container: DIContainer
    ) -> None:
        """Async-ness of the hook must not change kind, severity, or firing."""
        container.install(AsyncResourceConfiguration)
        report = container.validate()

        issues = _unreachable_issues(report)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.WARNING
        assert issue.owner == "@Provider(make_resource)"
        assert issue.param_name == "stop"
        assert issue.requested == "AsyncResource"
        assert issue.qualifier is None

    def test_inherited_pre_destroy_is_flagged(self, container: DIContainer) -> None:
        """The @PreDestroy hook lives on a base class; MRO walk must find it."""
        container.install(SubResourceConfiguration)
        report = container.validate()

        issues = _unreachable_issues(report)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.WARNING
        assert issue.owner == "@Provider(make_sub)"
        assert issue.param_name == "teardown"
        assert issue.requested == "SubResource"
        assert issue.qualifier is None

    def test_generic_produced_type_is_flagged_without_crashing(
        self, container: DIContainer
    ) -> None:
        """E4 — a parameterised generic produced type must not crash validate().

        Repo[User] is a `typing._GenericAlias`; its `__getattr__` refuses
        to forward dunder attributes, so `Repo[User].__mro__` raises
        `AttributeError` unless the `get_origin` normalisation guard runs
        first. Before Step 3 lands, this test is expected to fail with that
        exact `AttributeError` — not an assertion failure — which is the
        proof the guard is required, not merely defensive.
        """
        container.install(RepoConfiguration)
        report = container.validate()

        issues = _unreachable_issues(report)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.WARNING
        assert issue.owner == "@Provider(make_repo)"
        assert issue.param_name == "close"
        assert issue.qualifier is None

    def test_qualified_provider_is_flagged_and_records_qualifier(
        self, container: DIContainer
    ) -> None:
        """A qualified provider still gets flagged and records its qualifier."""
        container.install(SmsConfiguration)
        report = container.validate()

        issues = _unreachable_issues(report)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.WARNING
        assert issue.owner == "@Provider(make_sms)"
        assert issue.param_name == "shutdown"
        assert issue.requested == "SmsGateway"
        assert issue.qualifier == "sms"

    def test_bare_provide_outside_configuration_is_flagged(self, container: DIContainer) -> None:
        """A provider registered via bare `container.provide()` is flagged too."""
        container.provide(make_bare)
        report = container.validate()

        issues = _unreachable_issues(report)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == Severity.WARNING
        assert issue.owner == "@Provider(make_bare)"
        assert issue.param_name == "close"
        assert issue.requested == "BareResource"
        assert issue.qualifier is None


# ─────────────────────────────────────────────────────────────────
#  Negative / control cases — Step 2
# ─────────────────────────────────────────────────────────────────


class TestUnreachablePreDestroyNegatives:
    """Cases that must produce zero UNREACHABLE_PRE_DESTROY issues."""

    def test_provider_with_disposes_produces_no_issue(self, container: DIContainer) -> None:
        """E6 — @Disposes present is the designed teardown path; no issue."""
        container.install(InfraModule)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_produced_class_without_pre_destroy_produces_no_issue(
        self, container: DIContainer
    ) -> None:
        """E7 — no @PreDestroy hook at all means nothing is unreachable."""
        container.install(PlainConfiguration)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_dependent_provider_produces_no_issue(self, container: DIContainer) -> None:
        """E9 — DEPENDENT-scope providers are deliberately out of scope."""
        container.install(DependentConfiguration)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_request_scoped_provider_produces_no_issue(self, container: DIContainer) -> None:
        """E9 — REQUEST-scope providers are deliberately out of scope."""
        container.install(RequestConfiguration)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_session_scoped_provider_produces_no_issue(self, container: DIContainer) -> None:
        """E9 — SESSION-scope providers are deliberately out of scope."""
        container.install(SessionConfiguration)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_class_binding_with_pre_destroy_produces_no_issue(self, container: DIContainer) -> None:
        """E8 — a ClassBinding's @PreDestroy runs today; never flag it."""
        container.register(ClassBoundResource)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_provider_returning_none_produces_no_issue(self, container: DIContainer) -> None:
        """E10 — `def p() -> None` produces NoneType, which has no hook."""
        container.install(NoneConfiguration)
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_provider_returning_non_class_produces_no_issue(self, container: DIContainer) -> None:
        """E11 — a `returns=` override resolving to a non-`type` is guarded out."""
        container.provide(make_marked, returns=Annotated[int, "marker"])
        report = container.validate()

        assert _unreachable_issues(report) == []

    def test_empty_container_report_is_unchanged(self, container: DIContainer) -> None:
        """E17 — regression on TestReportShape's empty-report contract."""
        report = container.validate()

        assert report.issues == ()
        assert report.checked_bindings == 0
        assert report.ok is True
