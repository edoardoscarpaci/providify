"""Failing tests (RED mode) for Plan 016 — open-generic binding.

Encodes upstream gap **P26-OPEN-GENERIC-BINDING** and
`plans/016-open-generic-binding.md`. `container.provide(factory,
returns=Repo[T])` (or `@Provider(returns=Repo[T])`, or a `-> Repo[T]` return
annotation) registers an *open* binding matched, at resolve time, against
closed requests like `Repo[User]`.

None of `_open_type_params`, `_closing_args`, `_type_arg_param`,
`ProviderBinding.type_params`, `type_args_for`, `_binding_serves`,
`_prefer_closed`, or the `requested=` kwarg threading exist yet, so every
test here is expected to fail — most with `LookupError` (nothing serves the
closed request today), the `TypeError` registration tests because nothing
raises, and a couple with `AttributeError`/`TypeError` from missing
attributes/kwargs.

Covered (grouped by plan class):
    TestGapReproduction        — Step 1, the [GAP] guard port
    TestMatchAndSpecificity    — Step 2, rows 1, 2, 5, 8
    TestCachingAndLifecycle    — Step 3, row 4
    TestBoundsAndConstraints   — Step 4, row 7
    TestValidate               — Step 4, row 6
    TestAsyncAndInjectionPaths — Step 4
    TestFallbackInterplay      — Step 4, requires plan 017's @Fallback
"""

from __future__ import annotations

from typing import Generic, ParamSpec, Protocol, TypeVar

import pytest

from providify import (
    Component,
    Configuration,
    Disposes,
    Inject,
    InjectInstances,
    Lazy,
    Provider,
    Singleton,
)
from providify.container import DIContainer

T = TypeVar("T")
S = TypeVar("S")
A = TypeVar("A")
B = TypeVar("B")
Bounded = TypeVar("Bounded", bound="Entity")
Constrained = TypeVar("Constrained", str, bytes)


class Repo(Generic[T]):
    """Module-level open interface, [GAP:98-107]."""

    def __init__(self, entity: type[T] | None = None) -> None:
        self.entity = entity


class Pair(Generic[A, B]):
    def __init__(self, a: A | None = None, b: B | None = None) -> None:
        self.a = a
        self.b = b


class User:
    pass


class Order:
    pass


class Entity:
    pass


class Product(Entity):
    pass


# ─────────────────────────────────────────────────────────────────
#  TestGapReproduction — Step 1
# ─────────────────────────────────────────────────────────────────


class TestGapReproduction:
    """Port of [GAP:110-131] plus the control [GAP:139-142]."""

    def test_open_alias_registration_resolves_closed_request(self, container: DIContainer) -> None:
        # [GAP] guard 1 — a `returns=Repo[T]` registration must serve
        # `get(Repo[User])`; today nothing matches -> LookupError.
        def repo_factory(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        container.provide(repo_factory, returns=Repo[T])

        result = container.get(Repo[User])
        assert isinstance(result, Repo)
        assert result.entity is User

    def test_open_alias_factory_receives_closed_type_argument(self, container: DIContainer) -> None:
        # [GAP] guard 2 — the factory's `type[T]` param is filled with the
        # closed type argument, not resolved from the container.
        received: list[type] = []

        def repo_factory(entity: type[T]) -> Repo[T]:
            received.append(entity)
            return Repo(entity)

        container.provide(repo_factory, returns=Repo[T])

        container.get(Repo[Order])
        assert received == [Order]

    def test_closed_alias_still_matches_exactly(self, container: DIContainer) -> None:
        # [GAP:139-142] control — a closed alias registration is untouched
        # by this plan and must keep passing today.
        @Singleton
        class UserRepo(Repo[User]):
            pass

        container.bind(Repo[User], UserRepo)

        assert isinstance(container.get(Repo[User]), UserRepo)


# ─────────────────────────────────────────────────────────────────
#  TestMatchAndSpecificity — Step 2, rows 1, 2, 5, 8
# ─────────────────────────────────────────────────────────────────


class TestMatchAndSpecificity:
    def test_bare_request_does_not_match_open_binding(self, container: DIContainer) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        with pytest.raises(LookupError):
            container.get(Repo)
        assert container.is_resolvable(Repo) is False

    def test_request_with_typevar_does_not_match_open_binding(self, container: DIContainer) -> None:
        # E3: request-side TypeVar stays literal — never a wildcard.
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        with pytest.raises(LookupError):
            container.get(Repo[T])

    def test_closed_binding_beats_open_regardless_of_priority(self, container: DIContainer) -> None:
        def open_factory(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.provide(open_factory, returns=Repo[T])
        container.provide.__self__  # noqa: B018 — no-op, keeps flake quiet
        container.bind(Repo[User], ClosedUserRepo)

        assert isinstance(container.get(Repo[User]), ClosedUserRepo)
        assert isinstance(container.get(Repo[Order]), Repo)

        # reverse registration order — closed still wins
        reversed_container = DIContainer()
        reversed_container.bind(Repo[User], ClosedUserRepo)
        reversed_container.provide(open_factory, returns=Repo[T])
        assert isinstance(reversed_container.get(Repo[User]), ClosedUserRepo)

    def test_get_all_closed_request_includes_open_binding_once(
        self, container: DIContainer
    ) -> None:
        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.bind(Repo[User], ClosedUserRepo)
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        results = container.get_all(Repo[User])
        assert len(results) == 2
        assert sum(1 for r in results if type(r) is Repo) == 1

    def test_get_all_bare_request_excludes_open_binding(self, container: DIContainer) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        with pytest.raises(LookupError):
            container.get_all(Repo)

        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.bind(Repo[User], ClosedUserRepo)
        assert len(container.get_all(Repo)) == 1

    def test_inject_instances_closed_request_collects_open_binding(
        self, container: DIContainer
    ) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Singleton
        class Consumer:
            def __init__(self, repos: InjectInstances[Repo[User]]) -> None:
                self.repos = repos

        container.register(Consumer)
        consumer = container.get(Consumer)
        assert len(consumer.repos) == 1

    def test_two_typevars_all_open_supported(self, container: DIContainer) -> None:
        def pair_factory(a: type[A], b: type[B]) -> Pair[A, B]:
            return Pair(a, b)

        container.provide(pair_factory, returns=Pair[A, B])

        result = container.get(Pair[int, str])
        assert (result.a, result.b) == (int, str)

    def test_repeated_typevar_must_close_consistently(self, container: DIContainer) -> None:
        def pair_factory(a: type[T], b: type[T]) -> Pair[T, T]:
            return Pair(a, b)

        container.provide(pair_factory, returns=Pair[T, T])

        result = container.get(Pair[int, int])
        assert (result.a, result.b) == (int, int)

        with pytest.raises(LookupError):
            container.get(Pair[int, str])

    def test_mixed_alias_rejected_at_registration(self, container: DIContainer) -> None:
        with pytest.raises(TypeError, match="all-TypeVar or all-concrete"):
            container.provide(lambda t: Pair("x", t), returns=Pair[str, T])

        with pytest.raises(TypeError, match="all-TypeVar or all-concrete"):
            container.provide(lambda t: Repo(t), returns=Repo[list[T]])

        @Provider
        def bad_factory() -> dict[str, T]:
            return {}

        # Plan 016 E7: the TypeError fires at REGISTRATION (provide() /
        # install() / scan() — wherever a ProviderBinding is actually built),
        # never at decoration time — @Provider only stores metadata.
        with pytest.raises(TypeError, match="all-TypeVar or all-concrete"):
            container.provide(bad_factory)

    def test_paramspec_and_typevartuple_rejected(self, container: DIContainer) -> None:
        P = ParamSpec("P")

        with pytest.raises(TypeError):
            container.provide(lambda: Repo(None), returns=Repo[P])

    def test_pep695_class_syntax(self, container: DIContainer) -> None:
        exec_globals: dict = {}
        exec(
            "class Box[X]:\n    def __init__(self, entity=None):\n        self.entity = entity\n",
            exec_globals,
        )
        Box = exec_globals["Box"]

        def box_factory(entity: type[T]) -> Box[T]:
            return Box(entity)

        container.provide(box_factory, returns=Box[T])

        result = container.get(Box[User])
        assert result.entity is User

    def test_pep695_generic_function_type_param_matched_by_name(
        self, container: DIContainer
    ) -> None:
        exec_globals: dict = {"Repo": Repo, "Provider": Provider}
        exec(
            "@Provider\ndef factory[T](entity: type[T]) -> Repo[T]:\n    return Repo(entity)\n",
            exec_globals,
        )
        factory = exec_globals["factory"]

        container.provide(factory)

        result = container.get(Repo[User])
        assert result.entity is User

    def test_open_binding_via_return_annotation_without_returns(
        self, container: DIContainer
    ) -> None:
        @Provider
        def f(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        container.provide(f)

        result = container.get(Repo[User])
        assert result.entity is User

    def test_reset_binding_closed_request_removes_open_binding_and_evicts_all_closed_instances(
        self, container: DIContainer
    ) -> None:
        @Provider(singleton=True)
        def factory(entity: type[T] = None) -> Repo[T]:  # type: ignore[assignment]
            return Repo(entity)

        binding = container.provide(factory)

        container.get(Repo[User])
        container.get(Repo[Order])

        container.reset_binding(Repo[User])

        assert binding not in container._bindings
        with pytest.raises(LookupError):
            container.get(Repo[User])

    def test_describe_renders_open_binding_without_crashing(self, container: DIContainer) -> None:
        binding = container.provide(lambda entity: Repo(entity), returns=Repo[T])

        descriptor = binding.describe(container)
        assert "~T" in str(descriptor.interface)
        assert ", open" in repr(binding)


# ─────────────────────────────────────────────────────────────────
#  TestCachingAndLifecycle — Step 3, row 4
# ─────────────────────────────────────────────────────────────────


class TestCachingAndLifecycle:
    def test_singleton_caches_per_closed_alias(self, container: DIContainer) -> None:
        calls: list[type] = []

        @Provider(singleton=True)
        def factory(entity: type[T]) -> Repo[T]:
            calls.append(entity)
            return Repo(entity)

        container.provide(factory)

        u1 = container.get(Repo[User])
        u2 = container.get(Repo[User])
        o1 = container.get(Repo[Order])

        assert u1 is u2
        assert u1 is not o1
        assert calls == [User, Order]

    def test_dependent_open_binding_creates_new_instance_each_time(
        self, container: DIContainer
    ) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        assert container.get(Repo[User]) is not container.get(Repo[User])

    def test_request_scoped_open_binding_caches_per_closed_alias_per_request(
        self, container: DIContainer
    ) -> None:
        from providify import Scope

        @Provider(scope=Scope.REQUEST)
        def factory(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        container.provide(factory)

        with container.request():
            u1 = container.get(Repo[User])
            u2 = container.get(Repo[User])
            o1 = container.get(Repo[Order])
            assert u1 is u2
            assert u1 is not o1

        with container.request():
            u3 = container.get(Repo[User])
            assert u3 is not u1

    def test_every_closed_singleton_instance_is_disposed_at_shutdown(
        self, container: DIContainer
    ) -> None:
        disposed: list[object] = []

        @Configuration
        class Config:
            @Provider(singleton=True)
            def make(self, entity: type[T]) -> Repo[T]:
                return Repo(entity)

            @Disposes(Repo)
            def dispose(self, repo: Repo) -> None:
                disposed.append(repo)

        container.install(Config)

        u = container.get(Repo[User])
        o = container.get(Repo[Order])

        container.shutdown()

        assert len(disposed) == 2
        assert disposed[-1] is u  # reverse creation order
        assert disposed[0] is o

    def test_disposes_closed_alias_wires_to_open_binding(self, container: DIContainer) -> None:
        disposed: list[object] = []

        @Configuration
        class Config:
            @Provider(singleton=True)
            def make(self, entity: type[T]) -> Repo[T]:
                return Repo(entity)

            @Disposes(Repo[User])
            def dispose(self, repo: Repo) -> None:
                disposed.append(repo)

        container.install(Config)
        container.get(Repo[User])
        container.shutdown()

        assert len(disposed) == 1

    async def test_async_disposer_on_open_binding_runs_per_instance(
        self, container: DIContainer
    ) -> None:
        disposed: list[object] = []

        @Configuration
        class Config:
            @Provider(singleton=True)
            async def make(self, entity: type[T]) -> Repo[T]:
                return Repo(entity)

            @Disposes(Repo)
            async def dispose(self, repo: Repo) -> None:
                disposed.append(repo)

        container.install(Config)
        await container.aget(Repo[User])
        await container.aget(Repo[Order])
        await container.ashutdown()

        assert len(disposed) == 2

    def test_warm_up_skips_open_bindings(self, container: DIContainer) -> None:
        calls: list[type] = []

        @Provider(singleton=True)
        def open_factory(entity: type[T]) -> Repo[T]:
            calls.append(entity)
            return Repo(entity)

        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.provide(open_factory)
        container.bind(Repo[User], ClosedUserRepo)

        container.warm_up()

        assert calls == []
        assert container._singleton_cache  # closed singleton warmed

    def test_instance_created_event_reports_closed_alias(self, container: DIContainer) -> None:
        from providify.observability import InstanceCreated

        events: list[InstanceCreated] = []
        container.add_hook(InstanceCreated, events.append)

        container.provide(lambda entity: Repo(entity), returns=Repo[T])
        container.get(Repo[User])

        assert any(e.interface is Repo[User] for e in events)

    def test_open_binding_requested_without_closing_alias_raises_typeerror(
        self, container: DIContainer
    ) -> None:
        # entity has a default so the raw factory call itself cannot raise
        # TypeError for an unrelated reason (missing positional arg) --
        # only the programmer-error guard should.
        binding = container.provide(lambda entity=None: Repo(entity), returns=Repo[T])

        with pytest.raises(TypeError):
            container._instantiate_sync(binding)

    def test_nested_closed_requests_through_same_open_binding_do_not_false_cycle(
        self, container: DIContainer
    ) -> None:
        def factory(entity: type[T]) -> Repo[T]:
            if entity is Order:
                container.get(Repo[Order]) if False else None
            return Repo(entity)

        container.provide(factory, returns=Repo[T])

        assert isinstance(container.get(Repo[User]), Repo)

    def test_open_binding_self_request_is_a_real_cycle(self, container: DIContainer) -> None:
        from providify.exceptions import CircularDependencyError

        def factory(entity: type[T]) -> Repo[T]:
            return container.get(Repo[User])

        container.provide(factory, returns=Repo[T])

        with pytest.raises(CircularDependencyError, match=r"Repo\[User\]"):
            container.get(Repo[User])


# ─────────────────────────────────────────────────────────────────
#  TestBoundsAndConstraints — Step 4, row 7
# ─────────────────────────────────────────────────────────────────


class TestBoundsAndConstraints:
    def test_bound_violation_is_non_match_not_error(self, container: DIContainer) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[Bounded])

        with pytest.raises(LookupError):
            container.get(Repo[User])

        assert isinstance(container.get(Repo[Product]), Repo)

    def test_bound_violation_lets_second_binding_serve(self, container: DIContainer) -> None:
        @Provider(priority=1)
        def bounded_factory(entity: type[Bounded]) -> Repo[Bounded]:
            return Repo(entity)

        @Provider(priority=0)
        def unbounded_factory(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        container.provide(bounded_factory)
        container.provide(unbounded_factory)

        assert isinstance(container.get(Repo[User]), Repo)
        # Product satisfies the bound too -> both match, higher priority wins.
        assert isinstance(container.get(Repo[Product]), Repo)

    def test_subscripted_generic_bound_is_checked_against_origin(
        self, container: DIContainer
    ) -> None:
        BoundRepo = TypeVar("BoundRepo", bound=Repo[Entity])

        class OuterBox(Generic[BoundRepo]):
            def __init__(self, value: type[BoundRepo] | None = None) -> None:
                self.value = value

        container.provide(lambda value: OuterBox(value), returns=OuterBox[BoundRepo])  # type: ignore[call-arg]

        assert isinstance(container.get(OuterBox[Repo[Product]]), OuterBox)
        with pytest.raises(LookupError):
            container.get(OuterBox[User])

    def test_non_runtime_checkable_protocol_bound_is_permissive(
        self, container: DIContainer
    ) -> None:
        class NotCheckable(Protocol):
            def foo(self) -> None: ...

        NCBound = TypeVar("NCBound", bound=NotCheckable)

        container.provide(lambda entity: Repo(entity), returns=Repo[NCBound])  # type: ignore[call-arg]

        assert isinstance(container.get(Repo[User]), Repo)

    def test_constraints_use_identity_membership(self, container: DIContainer) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[Constrained])  # type: ignore[call-arg]

        assert isinstance(container.get(Repo[str]), Repo)
        with pytest.raises(LookupError):
            container.get(Repo[int])

        class MyStr(str):
            pass

        with pytest.raises(LookupError):
            container.get(Repo[MyStr])


# ─────────────────────────────────────────────────────────────────
#  TestValidate — Step 4, row 6
# ─────────────────────────────────────────────────────────────────


class TestValidate:
    def test_validate_does_not_flag_type_param_as_missing(self, container: DIContainer) -> None:
        @Provider(singleton=True)
        def factory(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        container.provide(factory)

        report = container.validate(raise_on_error=False)
        assert report.ok is True
        assert report.issues == ()

    def test_validate_checks_closed_request_against_open_binding(
        self, container: DIContainer
    ) -> None:
        from providify import IssueKind

        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Singleton
        class Consumer:
            def __init__(self, repo: Inject[Repo[User]]) -> None:
                self.repo = repo

        container.register(Consumer)

        report = container.validate(raise_on_error=False)
        missing = [i for i in report.issues if i.kind == IssueKind.MISSING_BINDING]
        assert missing == []

    def test_validate_reports_missing_with_bound_near_miss_message(
        self, container: DIContainer
    ) -> None:
        from providify import IssueKind

        container.provide(lambda entity: Repo(entity), returns=Repo[Bounded])  # type: ignore[call-arg]

        @Singleton
        class Consumer:
            def __init__(self, repo: Inject[Repo[User]]) -> None:
                self.repo = repo

        container.register(Consumer)

        report = container.validate(raise_on_error=False)
        missing = [i for i in report.issues if i.kind == IssueKind.MISSING_BINDING]
        assert len(missing) == 1
        assert "bound" in missing[0].message
        assert "Repo[~Bounded]" in missing[0].message

    def test_validate_closed_plus_open_is_not_ambiguous(self, container: DIContainer) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.bind(Repo[User], ClosedUserRepo)

        @Singleton
        class Consumer:
            def __init__(self, repo: Inject[Repo[User]]) -> None:
                self.repo = repo

        container.register(Consumer)

        report = container.validate(raise_on_error=False)
        assert report.issues == ()

    def test_validate_type_param_on_closed_provider_is_still_missing(
        self, container: DIContainer
    ) -> None:
        from providify import IssueKind

        def factory(entity: type[T]) -> Repo[User]:
            return Repo(entity)

        container.provide(factory, returns=Repo[User])

        report = container.validate(raise_on_error=False)
        missing = [i for i in report.issues if i.kind == IssueKind.MISSING_BINDING]
        assert len(missing) == 1


# ─────────────────────────────────────────────────────────────────
#  TestAsyncAndInjectionPaths — Step 4
# ─────────────────────────────────────────────────────────────────


class TestAsyncAndInjectionPaths:
    async def test_aget_open_binding_async_factory(self, container: DIContainer) -> None:
        async def factory(entity: type[T]) -> Repo[T]:
            return Repo(entity)

        container.provide(factory, returns=Repo[T])

        result = await container.aget(Repo[User])
        assert result.entity is User

        with pytest.raises(RuntimeError):
            container.get(Repo[User])

    async def test_aget_all_closed_request_includes_open_binding(
        self, container: DIContainer
    ) -> None:
        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.bind(Repo[User], ClosedUserRepo)
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        results = await container.aget_all(Repo[User])
        assert len(results) == 2

    def test_constructor_inject_closed_alias_resolves_open_binding(
        self, container: DIContainer
    ) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Component
        class Svc:
            def __init__(self, repo: Inject[Repo[User]]) -> None:
                self.repo = repo

        container.register(Svc)
        svc = container.get(Svc)
        assert svc.repo.entity is User

    def test_constructor_bare_annotation_closed_alias_resolves_open_binding(
        self, container: DIContainer
    ) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Component
        class Svc:
            def __init__(self, repo: Repo[User]) -> None:
                self.repo = repo

        container.register(Svc)
        svc = container.get(Svc)
        assert svc.repo.entity is User

    def test_constructor_bare_generic_class_annotation_does_not_match_open_binding(
        self, container: DIContainer
    ) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Component
        class Svc:
            def __init__(self, repo: Repo = None) -> None:  # type: ignore[assignment]
                self.repo = repo

        container.register(Svc)
        svc = container.get(Svc)
        assert svc.repo is None
        assert container.is_resolvable(Repo) is False

    def test_lazy_and_live_wrappers_resolve_closed_alias_through_open_binding(
        self, container: DIContainer
    ) -> None:
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        @Component
        class Svc:
            def __init__(self, repo: Lazy[Repo[User]]) -> None:
                self.repo = repo

        container.register(Svc)
        svc = container.get(Svc)
        assert svc.repo.get().entity is User


# ─────────────────────────────────────────────────────────────────
#  TestFallbackInterplay — Step 4, requires plan 017's @Fallback
# ─────────────────────────────────────────────────────────────────


class TestFallbackInterplay:
    def test_open_fallback_loses_to_closed_non_fallback(self, container: DIContainer) -> None:
        Fallback = pytest.importorskip("providify").Fallback

        fallback_factory = Fallback(lambda entity: Repo(entity))
        container.provide(fallback_factory, returns=Repo[T])

        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.bind(Repo[User], ClosedUserRepo)

        assert isinstance(container.get(Repo[User]), ClosedUserRepo)

    def test_closed_fallback_loses_to_open_non_fallback(self, container: DIContainer) -> None:
        Fallback = pytest.importorskip("providify").Fallback

        @Fallback
        @Singleton
        class ClosedUserRepo(Repo[User]):
            pass

        container.bind(Repo[User], ClosedUserRepo)
        container.provide(lambda entity: Repo(entity), returns=Repo[T])

        # Fallback dropped first (explicit intent wins), so the open
        # non-fallback binding serves the request even though it is
        # structurally less specific than the closed one.
        result = container.get(Repo[User])
        assert type(result) is Repo
