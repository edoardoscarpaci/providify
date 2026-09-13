"""`Advised` — field-level AOP via Python's descriptor protocol (plan 010, F8).

F8 is a *deliberate extension beyond* Jakarta CDI parity, not a gap being
closed: Jakarta Interceptors 2.1 defines exactly five interception types
(`@AroundInvoke`, `@AroundTimeout`, `@PostConstruct`, `@PreDestroy`,
`@AroundConstruct`) and none of them is field-level, in either the Lite or
Full profile (research-F8 §Findings, §Options compared). The reference model
here is AspectJ's `get`/`set` pointcuts (research-F8 §AspectJ), which weave at
compile time; Python has no compile-time weaving step, so this module uses the
native runtime equivalent instead — the **data descriptor protocol**, the same
mechanism Django ORM fields, SQLAlchemy columns and Traitlets use
(research-F8 §Python descriptor protocol: *"data descriptors (`__set__` +
`__get__`) take priority over instance dictionaries… cannot be shadowed by
instance assignment, enabling reliable field interception"*).

Two-phase model (plan 010 §Design F8.3):
    1. **Static weaving** — `Advised(...)` in a class body installs the data
       descriptor at class-creation time (`__set_name__`). This always
       happens, container or not.
    2. **Dynamic advice** — `DIContainer._apply_interceptors()` attaches
       `instance.__di_field_chain__ = {"get": [...], "set": [...]}` AFTER
       `binding.create()` returns, i.e. after `__init__`, class-var injection
       and `@PostConstruct` (plan 010 §Design F8.4). An `Advised` field access
       checks for that chain and, if present, walks it via
       `FieldAccessContext.proceed()`; if absent (unmanaged instance, or a
       managed instance whose class carries no matching interceptor), the
       descriptor degrades to a plain attribute with one extra dict lookup.

Import direction: this module is standalone — it does not import from
`container.py`, mirroring `validation.py`'s one-way dependency rule so
`container.py` can import `Advised`/`FieldAccessContext` without a cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as _dc_field
from typing import Any, Literal

#: Sentinel distinguishing "no default supplied" from "default is None/0/''".
#: A plain `None` default cannot be used as the sentinel because `None` is a
#: legal, common default value for an Advised field.
_NO_DEFAULT: Any = object()


class Advised:
    """A data descriptor that is a declared field-advice join point (plan 010 §Design F8.2).

    Only fields whose class body assigns `Advised(...)` are advised — there is
    no `__getattribute__`/`__setattr__` override on the owning class, so every
    other attribute costs nothing and is invisible to the interceptor chain
    (research-F8 flags the performance cost of the broad-hook alternative,
    which this design deliberately avoids).

    Storage: each instance's value lives under a private key
    (`f"__advised_{name}"`) in the instance `__dict__` — never under the
    field's own name, so a data descriptor at the class level and a plain
    value at the instance level can never collide.

    Thread safety:  ⚠️ Same caveat as any per-instance mutable attribute — no
                    locking; concurrent writes to the same instance's same
                    field from multiple threads are a plain race, exactly
                    like unguarded `self.x = value` would be.
    Async safety:   ✅ No await points — synchronous by design (see the
                    module-level non-goal: field advice inherits
                    `InvocationContext.proceed()`'s sync-only limitation).

    Example:
        class Account:
            balance: float = Advised(0.0)

        acct = Account()
        acct.balance = 10.0   # advised if a container attached a chain
        assert acct.balance == 10.0
    """

    __slots__ = ("_name", "_storage_key", "_default", "_has_default")

    def __init__(self, default: Any = _NO_DEFAULT) -> None:
        """Create an `Advised` field descriptor.

        Args:
            default: The value returned by a read before any write, when
                supplied. Omit it entirely (`Advised()`) to require a write
                before the first read — matching a normal instance attribute
                that was never assigned.
        """
        self._has_default = default is not _NO_DEFAULT
        self._default = default
        # Set for real by __set_name__ — placeholders here only for __slots__.
        self._name = ""
        self._storage_key = ""

    def __set_name__(self, owner: type, name: str) -> None:
        """Capture the field name at class-creation time (PEP 487).

        Args:
            owner: The class this descriptor was assigned in.
            name:  The attribute name it was assigned to (e.g. ``"balance"``).
        """
        self._name = name
        self._storage_key = f"__advised_{name}"

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        """Read the field — advised if *instance* carries an armed get-chain.

        Args:
            instance: The instance being read, or `None` for class access
                (e.g. `Account.balance`), which returns this descriptor
                itself — matching how an un-invoked `property` behaves.
            owner: The owning class — unused, accepted for protocol parity.

        Returns:
            The stored value (or the default), possibly transformed by
            `@AroundGet` advice.

        Raises:
            AttributeError: No value was ever written and no default was
                supplied to `Advised(...)`.
        """
        if instance is None:
            return self

        chain = _field_chain(instance, "get")
        if not chain:
            return self._raw_get(instance)

        ctx = FieldAccessContext(
            target=instance,
            field=self._name,
            value=None,
            kind="get",
            _chain=list(chain),
            _terminal=lambda _ctx: self._raw_get(instance),
        )
        return ctx.proceed()

    def __set__(self, instance: object, value: Any) -> None:
        """Write the field — advised if *instance* carries an armed set-chain.

        Advice may transform the write (`ctx.value = ...` before
        `ctx.proceed()`), veto it entirely (never call `ctx.proceed()`, in
        which case the old value stands unchanged), or let it through as-is.

        Args:
            instance: The instance being written to.
            value:    The value being assigned.

        Raises:
            TypeError: *instance* has no per-instance `__dict__` to store
                into (e.g. a `__slots__` class that did not reserve a slot
                or `__dict__` entry) — raised instead of the bare
                `AttributeError` Python's descriptor protocol would produce,
                naming the field so the fix is obvious.
        """
        chain = _field_chain(instance, "set")
        if not chain:
            self._raw_set(instance, value)
            return

        ctx = FieldAccessContext(
            target=instance,
            field=self._name,
            value=value,
            kind="set",
            _chain=list(chain),
            _terminal=lambda _ctx: self._raw_set(instance, _ctx.value),
        )
        ctx.proceed()

    def __delete__(self, instance: object) -> None:
        """Delete the stored value — never advised (§Non-goals: no `__delete__` advice).

        AspectJ itself defines only `get`/`set` pointcuts (research-F8
        §AspectJ) — there is no delete pointcut to mirror, so `del obj.field`
        passes straight through with no interceptor involvement.

        Args:
            instance: The instance to delete the field from.

        Returns:
            None. Silently succeeds even if the field was never written —
            mirrors `dict.pop(key, None)`'s tolerance rather than `del`'s
            usual `KeyError`, because "never set" and "set then deleted" are
            not meaningfully different states for an advised field.
        """
        instance.__dict__.pop(self._storage_key, None)

    def _raw_get(self, instance: object) -> Any:
        """Unadvised read — the terminal operation at the end of the get-chain."""
        storage = instance.__dict__
        if self._storage_key in storage:
            return storage[self._storage_key]
        if self._has_default:
            return self._default
        raise AttributeError(
            f"'{type(instance).__name__}' object has no attribute "
            f"'{self._name}' (Advised field never written and no default "
            f"was supplied to Advised(...))."
        )

    def _raw_set(self, instance: object, value: Any) -> None:
        """Unadvised write — the terminal operation at the end of the set-chain."""
        try:
            instance.__dict__[self._storage_key] = value
        except AttributeError as exc:
            # No __dict__ at all — e.g. a __slots__ class without a
            # __dict__ slot. A bare AttributeError here would be
            # indistinguishable from "field never set"; TypeError plus the
            # field name makes the real cause (no per-instance storage)
            # obvious at the point of failure (plan 010 step 23).
            raise TypeError(
                f"Advised field '{self._name}' cannot be written on "
                f"'{type(instance).__name__}' instances: no per-instance "
                f"__dict__ is available (likely a __slots__ class that did "
                f"not reserve '__dict__'). Advised requires per-instance "
                f"storage."
            ) from exc


def _field_chain(instance: object, kind: Literal["get", "set"]) -> list[tuple[object, str]]:
    """Return the armed advice chain of *kind* for *instance*, or `[]`.

    Args:
        instance: The instance being read from or written to.
        kind:     Either `"get"` or `"set"`.

    Returns:
        The chain list (possibly empty) — `[]` for any instance that was
        never wrapped by `DIContainer._apply_interceptors()`, or one built
        by a container whose interceptors matched no `get`/`set` advice.
    """
    chain_map = getattr(instance, "__di_field_chain__", None)
    if not chain_map:
        return []
    return chain_map.get(kind, [])


@dataclass
class FieldAccessContext:
    """Passed to `@AroundGet`/`@AroundSet` interceptor methods (plan 010 §Design F8.5).

    Mirrors `InvocationContext.proceed()` (`type.py`): walks `_chain` by
    index, then calls `_terminal` — the get-chain's terminal returns the
    stored value; the set-chain's terminal writes `self.value` and returns
    `None`. Advice may mutate `ctx.value` before calling `proceed()` on a
    `"set"` access (transform), skip `proceed()` entirely (veto a write / stop
    a read short of the terminal), or wrap the value `proceed()` returns
    (transform a read).

    Attributes:
        target: The instance whose field is being accessed.
        field:  The field name (e.g. `"balance"`) — matches `Advised`'s
                `__set_name__`-captured name, not the private storage key.
        value:  For `"get"`, unused until a terminal or advice sets it as the
                return value of `proceed()`. For `"set"`, the value being
                written — advice may reassign it before calling `proceed()`.
        kind:   `"get"` or `"set"`.
        _chain: Internal interceptor chain — `(interceptor_instance,
                method_name)` pairs, in registration order. Do not mutate.
        _terminal: Internal — the raw (unadvised) operation, called once the
                chain is exhausted. Takes the context itself so it can read
                a possibly-advice-transformed `.value`.
        _chain_index: Internal cursor — do not mutate.

    Example:
        @AroundSet
        def on_set(self, ctx: FieldAccessContext) -> None:
            ctx.value = normalise(ctx.value)  # transform
            ctx.proceed()                      # writes the transformed value
    """

    target: object
    field: str
    value: Any
    kind: Literal["get", "set"]
    _chain: list[tuple[object, str]]
    _terminal: Callable[[FieldAccessContext], Any]
    _chain_index: int = _dc_field(default=0, repr=False)

    def proceed(self) -> Any:
        """Invoke the next interceptor in the chain, or the terminal operation.

        Returns:
            For `"get"`: the value returned by the next advice, ultimately
            the stored/default value from the terminal. For `"set"`: `None`
            once the write lands (or whatever the deepest advice returns).
        """
        if self._chain_index < len(self._chain):
            interceptor_instance, method_name = self._chain[self._chain_index]
            self._chain_index += 1
            return getattr(interceptor_instance, method_name)(self)
        return self._terminal(self)
