"""Failing tests for F8: field-level advice via the descriptor protocol.

F8 is a deliberate extension beyond Jakarta CDI parity — CDI Interceptors 2.1
defines no field-level interception in either the Lite or Full profile
(research-F8 §Findings). The reference model is AspectJ's ``get``/``set``
pointcuts, implemented here with Python's data-descriptor protocol
(research-F8 §Python descriptor protocol) — NOT a CDI feature being "closed".
"""

from __future__ import annotations

import dataclasses

import pytest

from providify import Component, DIContainer, Interceptor, InterceptorBinding

# ─────────────────────────────────────────────────────────────────
#  Advised — standalone, no container involved
# ─────────────────────────────────────────────────────────────────


def test_advised_default_value_without_container() -> None:
    from providify.field import Advised

    class Account:
        balance = Advised(0.0)

    acct = Account()

    assert acct.balance == 0.0


def test_advised_get_set_round_trip_without_container() -> None:
    from providify.field import Advised

    class Account:
        balance = Advised(0.0)

    acct = Account()
    acct.balance = 42.0

    assert acct.balance == 42.0


def test_advised_is_per_instance_isolated() -> None:
    from providify.field import Advised

    class Account:
        balance = Advised(0.0)

    a1 = Account()
    a2 = Account()
    a1.balance = 100.0

    assert a1.balance == 100.0
    assert a2.balance == 0.0


def test_advised_accessed_on_class_returns_descriptor() -> None:
    from providify.field import Advised

    class Account:
        balance = Advised(0.0)

    assert isinstance(Account.balance, Advised)


def test_advised_delete_is_unadvised_and_works() -> None:
    # §Non-goals: no __delete__ advice — del passes through unadvised, and
    # must not raise even though it fires no interceptor chain.
    from providify.field import Advised

    class Account:
        balance = Advised(0.0)

    acct = Account()
    acct.balance = 5.0

    del acct.balance  # must not raise


def test_advised_field_read_before_write_with_no_default_raises_attribute_error() -> None:
    from providify.field import Advised

    class Account:
        balance = Advised()  # no default supplied

    acct = Account()

    with pytest.raises(AttributeError, match="balance"):
        acct.balance


# ─────────────────────────────────────────────────────────────────
#  @AroundGet / @AroundSet markers
# ─────────────────────────────────────────────────────────────────


def test_around_get_marker_detected_on_interceptor_class() -> None:
    from providify import AroundGet
    from providify.decorator.interceptor import _get_around_get_method

    @InterceptorBinding
    class Audited:
        pass

    @Interceptor
    @Audited
    class AuditInterceptor:
        @AroundGet
        def on_get(self, ctx):
            return ctx.proceed()

    method = _get_around_get_method(AuditInterceptor)
    assert method is not None
    assert method.__name__ == "on_get"


def test_around_set_marker_detected_on_interceptor_class() -> None:
    from providify import AroundSet
    from providify.decorator.interceptor import _get_around_set_method

    @InterceptorBinding
    class Audited:
        pass

    @Interceptor
    @Audited
    class AuditInterceptor:
        @AroundSet
        def on_set(self, ctx):
            ctx.proceed()

    method = _get_around_set_method(AuditInterceptor)
    assert method is not None
    assert method.__name__ == "on_set"


def test_at_most_one_around_get_method_per_interceptor_class() -> None:
    from providify import AroundGet

    @InterceptorBinding
    class Audited:
        pass

    with pytest.raises(Exception):

        @Interceptor
        @Audited
        class BadInterceptor:
            @AroundGet
            def first(self, ctx):
                return ctx.proceed()

            @AroundGet
            def second(self, ctx):
                return ctx.proceed()


def test_at_most_one_around_set_method_per_interceptor_class() -> None:
    from providify import AroundSet

    @InterceptorBinding
    class Audited:
        pass

    with pytest.raises(Exception):

        @Interceptor
        @Audited
        class BadInterceptor:
            @AroundSet
            def first(self, ctx):
                ctx.proceed()

            @AroundSet
            def second(self, ctx):
                ctx.proceed()


# ─────────────────────────────────────────────────────────────────
#  End-to-end: container-managed bean with field advice
# ─────────────────────────────────────────────────────────────────


@InterceptorBinding
class Audited:
    pass


def _make_audit_interceptor():
    from providify import AroundGet, AroundSet

    @Interceptor
    @Audited
    class AuditInterceptor:
        def __init__(self) -> None:
            self.gets: list[str] = []
            self.sets: list[tuple[str, object]] = []

        @AroundGet
        def on_get(self, ctx):
            self.gets.append(ctx.field)
            return ctx.proceed()

        @AroundSet
        def on_set(self, ctx):
            self.sets.append((ctx.field, ctx.value))
            ctx.proceed()

    return AuditInterceptor


def _make_account_cls():
    from providify.field import Advised

    @Component
    @Audited
    class Account:
        def __init__(self) -> None:
            self.owner = "anon"

        balance: float = Advised(0.0)

    return Account


def test_container_managed_bean_fires_set_advice_on_write(
    container: DIContainer,
) -> None:
    AuditInterceptor = _make_audit_interceptor()
    Account = _make_account_cls()

    container.register(Account)
    container.add_interceptor(AuditInterceptor)
    interceptor = container.get(AuditInterceptor)

    acct = container.get(Account)
    acct.balance = 10.0

    assert ("balance", 10.0) in interceptor.sets


def test_container_managed_bean_fires_get_advice_on_read(
    container: DIContainer,
) -> None:
    AuditInterceptor = _make_audit_interceptor()
    Account = _make_account_cls()

    container.register(Account)
    container.add_interceptor(AuditInterceptor)
    interceptor = container.get(AuditInterceptor)

    acct = container.get(Account)
    _ = acct.balance

    assert "balance" in interceptor.gets


def test_advice_can_transform_value_on_set(container: DIContainer) -> None:
    from providify import AroundSet

    @Interceptor
    @Audited
    class DoublingInterceptor:
        @AroundSet
        def on_set(self, ctx):
            ctx.value = ctx.value * 2
            ctx.proceed()

    Account = _make_account_cls()
    container.register(Account)
    container.add_interceptor(DoublingInterceptor)

    acct = container.get(Account)
    acct.balance = 5.0

    assert acct.balance == 10.0


def test_advice_that_does_not_call_proceed_vetoes_write(
    container: DIContainer,
) -> None:
    from providify import AroundSet

    @Interceptor
    @Audited
    class VetoInterceptor:
        @AroundSet
        def on_set(self, ctx):
            pass  # deliberately not calling proceed()

    Account = _make_account_cls()
    container.register(Account)
    container.add_interceptor(VetoInterceptor)

    acct = container.get(Account)
    acct.balance = 999.0

    assert acct.balance == 0.0  # old value stands


def test_advice_ordering_for_multiple_interceptors(container: DIContainer) -> None:
    from providify import AroundSet

    calls: list[str] = []

    @Interceptor
    @Audited
    class FirstInterceptor:
        @AroundSet
        def on_set(self, ctx):
            calls.append("first-before")
            ctx.proceed()
            calls.append("first-after")

    @Interceptor
    @Audited
    class SecondInterceptor:
        @AroundSet
        def on_set(self, ctx):
            calls.append("second-before")
            ctx.proceed()
            calls.append("second-after")

    Account = _make_account_cls()
    container.register(Account)
    container.add_interceptor(FirstInterceptor)
    container.add_interceptor(SecondInterceptor)

    acct = container.get(Account)
    acct.balance = 1.0

    assert calls == [
        "first-before",
        "second-before",
        "second-after",
        "first-after",
    ]


def test_bean_built_directly_outside_container_fires_no_advice() -> None:
    Account = _make_account_cls()

    acct = Account()
    acct.balance = 7.0  # no chain attached — plain descriptor behaviour

    assert acct.balance == 7.0


def test_construction_time_writes_are_not_advised(container: DIContainer) -> None:
    # §Design F8.4 — writes performed in __init__/class-var injection/
    # @PostConstruct happen before the chain is armed, so no advice fires.
    from providify import AroundSet
    from providify.field import Advised

    write_log: list[object] = []

    @Interceptor
    @Audited
    class RecordingInterceptor:
        @AroundSet
        def on_set(self, ctx):
            write_log.append(ctx.value)
            ctx.proceed()

    @Component
    @Audited
    class Account:
        balance: float = Advised(0.0)

        def __init__(self) -> None:
            self.balance = 123.0  # construction-time write

    container.register(Account)
    container.add_interceptor(RecordingInterceptor)

    container.get(Account)

    assert 123.0 not in write_log


def test_isinstance_is_preserved_with_only_field_advice(
    container: DIContainer,
) -> None:
    # §Design F8.3 — no proxy for field-only advice, identity/isinstance intact.
    AuditInterceptor = _make_audit_interceptor()
    Account = _make_account_cls()

    container.register(Account)
    container.add_interceptor(AuditInterceptor)

    acct = container.get(Account)

    assert isinstance(acct, Account)


def test_self_access_inside_own_method_is_advised(container: DIContainer) -> None:
    # The single case a method-interception proxy could never handle.
    from providify import AroundGet
    from providify.field import Advised

    reads: list[str] = []

    @Interceptor
    @Audited
    class ReadTrackingInterceptor:
        @AroundGet
        def on_get(self, ctx):
            reads.append(ctx.field)
            return ctx.proceed()

    @Component
    @Audited
    class Account:
        balance: float = Advised(0.0)

        def read_balance(self) -> float:
            return self.balance

    container.register(Account)
    container.add_interceptor(ReadTrackingInterceptor)

    acct = container.get(Account)
    reads.clear()
    acct.read_balance()

    assert "balance" in reads


def test_subclass_of_advised_class_without_binding_gets_no_chain(
    container: DIContainer,
) -> None:
    # A subclass without the @Audited annotation inherits the descriptor but
    # gets no interceptor chain.
    Account = _make_account_cls()

    class SavingsAccount(Account):
        pass

    savings = SavingsAccount()
    savings.balance = 50.0

    assert savings.balance == 50.0


def test_managed_and_unmanaged_instances_are_independent(
    container: DIContainer,
) -> None:
    AuditInterceptor = _make_audit_interceptor()
    Account = _make_account_cls()

    container.register(Account)
    container.add_interceptor(AuditInterceptor)
    interceptor = container.get(AuditInterceptor)

    managed = container.get(Account)
    unmanaged = Account()

    managed.balance = 1.0
    unmanaged.balance = 2.0

    assert ("balance", 1.0) in interceptor.sets
    assert ("balance", 2.0) not in interceptor.sets


def test_interceptor_with_around_get_but_target_has_no_advised_field_is_a_noop(
    container: DIContainer,
) -> None:
    from providify import AroundGet

    @Interceptor
    @Audited
    class NoopGetInterceptor:
        @AroundGet
        def on_get(self, ctx):
            return ctx.proceed()

    @Component
    @Audited
    class PlainService:
        def __init__(self) -> None:
            self.name = "svc"

    container.register(PlainService)
    container.add_interceptor(NoopGetInterceptor)

    svc = container.get(PlainService)  # should not raise
    assert svc.name == "svc"


def test_advice_raising_propagates_and_write_is_not_partial(
    container: DIContainer,
) -> None:
    from providify import AroundSet

    @Interceptor
    @Audited
    class RaisingInterceptor:
        @AroundSet
        def on_set(self, ctx):
            raise ValueError("nope")

    Account = _make_account_cls()
    container.register(Account)
    container.add_interceptor(RaisingInterceptor)

    acct = container.get(Account)

    with pytest.raises(ValueError, match="nope"):
        acct.balance = 55.0

    assert acct.balance == 0.0  # write never landed


# ─────────────────────────────────────────────────────────────────
#  Rejected target types (§Design F8.6)
# ─────────────────────────────────────────────────────────────────


def test_dataclass_target_with_advised_field_is_rejected(
    container: DIContainer,
) -> None:
    from providify.field import Advised

    @dataclasses.dataclass
    @Component
    @Audited
    class BadAccount:
        balance: float = Advised(0.0)

    container.register(BadAccount)

    with pytest.raises(TypeError, match="balance"):
        container.get(BadAccount)


def test_frozen_dataclass_target_with_advised_field_is_rejected(
    container: DIContainer,
) -> None:
    from providify.field import Advised

    @dataclasses.dataclass(frozen=True)
    @Component
    @Audited
    class FrozenAccount:
        balance: float = Advised(0.0)

    container.register(FrozenAccount)

    with pytest.raises(TypeError, match="balance"):
        container.get(FrozenAccount)


def test_pydantic_basemodel_target_with_advised_field_is_rejected(
    container: DIContainer,
) -> None:
    pydantic = pytest.importorskip("pydantic")
    from providify.field import Advised

    @Component
    @Audited
    class PydanticAccount(pydantic.BaseModel):
        balance: float = Advised(0.0)

    container.register(PydanticAccount)

    with pytest.raises(TypeError):
        container.get(PydanticAccount)


def test_attrs_target_with_advised_field_is_rejected(container: DIContainer) -> None:
    attr = pytest.importorskip("attr")
    from providify.field import Advised

    @attr.s
    @Component
    @Audited
    class AttrsAccount:
        balance = attr.ib(default=Advised(0.0))

    container.register(AttrsAccount)

    with pytest.raises(TypeError):
        container.get(AttrsAccount)


def test_slots_class_with_advised_field_raises_type_error_on_first_write() -> None:
    from providify.field import Advised

    class SlottedAccount:
        __slots__ = ()
        balance = Advised(0.0)

    acct = SlottedAccount()

    with pytest.raises(TypeError):
        acct.balance = 5.0
