# Plan 010 — Multibinding collection injection (F7) + field-level interceptors (F8)

Implements backlog items **F7** (🟢 nice, M) and **F8** (🟢 nice, L) from `BACKLOG.md`.

External grounding — cite these, not the backlog prose:

- **research-F7** = `design/f7-multibinding/research/001-collection-injection-api-conventions.md`
- **research-F8** = `design/field-interceptors/research/001-cdi-field-interception-scope.md`

> ⚠️ **The backlog's F8 rationale is factually wrong and must not be reproduced anywhere in code,
> docstrings, README, CHANGELOG or commit messages.** BACKLOG.md claims F8 "closes gap vs Jakarta CDI
> Full profile (providify currently matches CDI Lite/method-only)". research-F8 §Findings shows
> Jakarta Interceptors 2.1 defines exactly five interception types (`@AroundInvoke`,
> `@AroundTimeout`, `@PostConstruct`, `@PreDestroy`, `@AroundConstruct`) — **none field-level** — and
> that the Lite/Full split concerns decorators, scopes and portable extensions, *not* interception
> granularity (research-F8 §Options compared: field-level interception is ❌ in **both** profiles).
> The correct framing, used throughout this plan: **F8 is a deliberate extension beyond CDI parity,
> modelled on AspectJ `get`/`set` pointcuts (research-F8 §AspectJ) and implemented with Python's
> descriptor protocol (research-F8 §Python descriptor protocol), which is the native Python
> equivalent and is what Django ORM fields, SQLAlchemy columns and Traitlets use.** A step below
> corrects `BACKLOG.md` itself.

## Goal

Two independent capabilities, shipped together:

**F7** — a bare `list[T]` annotation injects every registered implementation of `T`, once `T` has
been explicitly declared a collection point:

```python
container.multibind(Handler)          # or: @Multibound on class Handler

@Component
class Dispatcher:
    def __init__(self, handlers: list[Handler]) -> None:   # all impls, priority-ordered
        self.handlers = handlers
```

**F8** — interceptors can advise field reads and writes, not just method calls:

```python
@Interceptor
@Audited
class AuditInterceptor:
    @AroundSet
    def on_set(self, ctx: FieldAccessContext) -> None:
        log.info("%s.%s = %r", type(ctx.target).__name__, ctx.field, ctx.value)
        ctx.proceed()

@Component
@Audited
class Account:
    balance: float = Advised(0.0)      # declared join point
```

## Non-goals

- **F7: no `set[T]`, `tuple[T, ...]`, `Sequence[T]`, `Iterable[T]`, `dict[K, V]` or
  `Mapping[K, V]` collection injection.** Only `list[T]` / `typing.List[T]` (identical runtime
  origin). research-F7 §Python: Wireup "advertises `Sequence[T]`… examples and detailed API
  documentation not publicly visible" — no usable precedent, and Guice/Injector/Autofac each expose
  exactly one collection shape. Keyed collections (Autofac `IIndex<K,V>`, research-F7 §Autofac) are a
  separate future feature.
- **F7: `get_all()` keeps raising `LookupError` on zero matches.** Changing it is a breaking change
  to an unrelated public method and is not needed — see §Design F7.4.
- **F7: `InjectInstances[T]` is not removed or deprecated.** research-F7 §Librarian's note suggests
  dropping the injection-site wrapper; we keep it as the no-declaration escape hatch (§Design F7.5).
- **F8: no interception of undeclared attributes.** No `__getattribute__` / `__setattr__` override on
  target classes; only fields explicitly declared `Advised(...)` are join points (§Design F8.2).
- **F8: dataclasses, frozen dataclasses, `attrs` and pydantic models are unsupported targets** —
  detected and rejected with a clear error (§Design F8.6).
- **F8: no `__delete__` advice.** `del obj.field` passes through unadvised. AspectJ has no delete
  pointcut (research-F8 §AspectJ lists `get`/`set` only).
- **F8: async advice is out of scope**, for the same reason `@AroundInvoke` is sync-only today —
  `InvocationContext.proceed()` (`providify/type.py:1459`) is a sync method. Field advice inherits
  that limitation verbatim; making the whole interceptor chain awaitable is its own plan.
- **No version bump / release notes assembly.** This plan lands the features; the v2.0 release plan
  bumps `pyproject.toml`.

---

## Design — F7: multibinding

### F7.1 What already works (scout-verified)

- `container.py:532` — `_bindings` already holds many bindings per interface.
- `container.py:1346-1401` — `_filter()` already returns *all* matches, honouring qualifier,
  `exact_only` and `@Profile`/`@Alternative` activation.
- `container.py:1160-1201` — `get_all()` already instantiates them sorted by ascending priority.
- `container.py:2885-2891` — `InjectInstances[T]` (= `Annotated[list[T], InjectMeta(all=True)]`)
  already routes to `get_all()`.

The only gap is the **injection-site syntax**: a bare `list[T]` hint resolves to `_UNRESOLVED` today
(`_is_resolvable(list[T])` is `False` because `_interface_matches(Handler, list[Handler])` fails the
MRO walk in `utils.py:167+`).

### F7.2 Chosen model: explicit collection point, bare `list[T]` at injection

research-F7 §Cross-Framework Consensus: *"Implicit registration patterns are NOT implicit in syntax.
Every framework reviewed requires explicit API usage or decoration to enable multibinding"* and
*"Once multibinding is enabled, use the bare collection type … not a wrapper."*

Two equivalent ways to declare the collection point:

| Form | Where | Mirrors |
|---|---|---|
| `container.multibind(Handler)` | container API | Guice `Multibinder.newSetBinder(binder(), Snack.class)` — research-F7 §Guice |
| `@Multibound` on `class Handler` | the interface itself | travels with the type across modules; needed because you cannot call `multibind` for a type you register by scanning, and cannot decorate a third-party ABC — hence both forms |

**Contributions need no marker at all.** Register implementations normally (`@Component`,
`@Provider`, `bind()`); `_filter(Handler)` already collects them. This is the one deliberate
departure from research-F7's literal suggestion of a per-provider `@provide(returns=list[T])` marker
(Injector's `@multiprovider`, research-F7 §Injector): providify's registry is *already*
multi-capable, so a per-contribution marker would be pure redundant bookkeeping that could silently
disagree with `get_all()`. The *principle* research-F7 establishes — explicit enablement at
registration time, bare type at the injection site — is fully honoured; only the marker's location
moves from each contribution to the collection point, which is exactly Guice's placement.

### F7.3 Resolution rule and precedence

```
resolve(list[T]):
  1. a binding whose interface literally matches list[T]  → that binding wins   (unchanged v1 behaviour)
  2. else T is a declared collection point                → [every active binding for T], priority-ascending
  3. else                                                  → _UNRESOLVED        (unchanged v1 behaviour)
```

Rule 1 before rule 2 is what makes this non-breaking: `container.provide(fn, returns=list[Handler])`
(plan 002) keeps meaning "this factory produces a literal list". research-F7 §Risks #2 names exactly
this single-value-vs-collect-all ambiguity as the undocumented hazard across all reviewed frameworks;
we resolve it by defined precedence **plus** a `validate_bindings()` message when both exist.

**Implementation site — three touch points, not two dozen.** Do *not* add branches to
`_resolve_hint_sync`/`_resolve_hint_async` separately:

- `container.py:2342 _is_resolvable()` — also return `True` for `list[T]` with `T` a collection point.
- `container.py get()` / `aget()` — detect `list[T]` + collection point, return `_collect_sync/_collect_async`.
- Everything else inherits it for free, because `_resolve_hint_sync:2963-2969` and
  `_resolve_hint_async` already funnel plain hints through `_is_resolvable()` → `self.get(hint)`, and
  `_inject_class_vars_sync/_async` (`container.py:3119`, `3194`) funnel through `_resolve_hint_*`.
  Sync/async parity is structural rather than duplicated.

### F7.4 Empty collections

`get_all()` raises `LookupError` on zero matches (`container.py:1182-1183`). **Collection injection
of a declared collection point with zero contributions injects `[]` instead of raising.** Rationale:
declaring the collection point *is* the statement that "zero is a legal count", and this matches
Autofac, the only reviewed framework that documents the case — research-F7 §Autofac: *"Returns empty
enumerable (not null or exception) if no implementations are registered."* `get_all()` itself is
untouched (see §Non-goals); the new `_collect_sync()` is a thin `_filter` + sort + instantiate that
tolerates emptiness, sharing the async-provider guard with `get_all()`.

### F7.5 `InjectInstances[T]` stays

It requires no collection-point declaration because it is already explicit *at the injection site* —
the user has spelled out "give me all of them" in the annotation. It is the escape hatch for types
you cannot decorate and did not `multibind()`. Documented as "prefer `list[T]`; use
`InjectInstances[T]` when you cannot declare the collection point".

### F7 — alternatives considered

- **Autofac model: bare `list[T]` always collects, no declaration** (research-F7 §Autofac) —
  ✅ zero ceremony, ✅ fewest concepts. ❌ Rejected: research-F7 §Cross-Framework Consensus says no
  reviewed framework makes collection implicit *in syntax alone*; research-F7 §Librarian's note #4
  flags that it "breaks if someone intentionally registers an `IEnumerable<T>` as a concrete
  service"; and it is silently breaking for existing providify code where a `list[T]` parameter
  currently falls back to its default value.
- **Injector model: per-contribution `@Provider(returns=list[T])` accumulation** (research-F7
  §Injector) — ✅ literally what research-F7 §Librarian's note recommends. ❌ Rejected: it makes the
  meaning of an existing v1 registration shape (`returns=list[T]`, plan 002) depend on how many
  *other* bindings exist, so adding a second registration retroactively changes the first one's
  semantics; and it duplicates the collection logic `_filter`/`get_all` already own.
- **Keep `InjectInstances[T]` as the only form** — ✅ zero work, zero risk. ❌ Rejected: research-F7
  §Librarian's note places providify's injection-site wrapper in category 1, *"similar to… no major
  framework"*.
- **New `MultiBinding(Binding)` subclass** — ✅ fits the `Binding` ABC extension pattern
  (`binding.py:42-105`). ❌ Rejected: a collection point creates no instances of its own and owns no
  scope/qualifier/lifecycle; it is a *resolution rule*, not a binding. A `set` on the container is
  the whole state it needs.

---

## Design — F8: field-level interceptors

### F8.1 Framing (mandatory)

CDI has no field interception in either profile (research-F8 §Findings, §Options compared). The
reference models are AspectJ's `get`/`set` pointcuts, which work by **bytecode weaving at
compile time** (research-F8 §AspectJ), and Python's **descriptor protocol**, where
`__get__`/`__set__` on a class-level object intercept attribute access, and *"data descriptors
(`__set__` + `__get__`) take priority over instance dictionaries… cannot be shadowed by instance
assignment, enabling reliable field interception"* (research-F8 §Python descriptor protocol). We use
data descriptors. research-F8 explicitly notes the alternative — *"`__getattribute__()` override
allows interception of every attribute access on an instance (with performance cost)"* — which we
reject in F8.2.

### F8.2 Scope of interception: declared fields only

Only fields whose class body assigns an `Advised(...)` descriptor are join points:

```python
@Component
@Audited
class Account:
    balance: float = Advised(0.0)     # advised
    owner: str = "anon"               # NOT advised
```

- ✅ Zero cost for every other attribute — no `__getattribute__` hook, no per-access dispatch on
  unrelated fields (research-F8 flags the performance cost of the broad hook).
- ✅ The join point is visible in the source, matching AspectJ's explicit pointcut declaration.
- ✅ No annotation evaluation at class-decoration time, so PEP-563 string annotations and forward
  references cannot break weaving — the descriptor is a real object in the class body with
  `__set_name__` (research-F8 notes `__set_name__` since Python 3.6; the project targets ≥3.12).
- ❌ Not "every write to the instance is advised". Documented explicitly.

### F8.3 Two-phase model: static weaving, dynamic advice

```
class body:  balance = Advised(0.0)   ← the "weave": data descriptor installed at class creation
                                         (AspectJ compile-time weaving analogue)
                    │
container:   _apply_interceptors(instance, cls)
                    │  builds get-chain + set-chain from the SAME @InterceptorBinding walk
                    │  already used for @AroundInvoke (container.py:1613-1635)
                    ▼
             object.__setattr__(instance, "__di_field_chain__", {"get": [...], "set": [...]})
                    │
runtime:     obj.balance      → Advised.__get__ → chain present? → FieldAccessContext(kind="get").proceed()
             obj.balance = 5  → Advised.__set__ → chain present? → FieldAccessContext(kind="set").proceed()
```

Two consequences that fall out of this for free and should be stated in the docstrings:

1. **No proxy.** Unlike `_InterceptorProxy` (`container.py:333`, whose docstring at line 342 warns
   `isinstance(proxy, TargetClass)` is `False`), field advice preserves object identity and
   `isinstance`.
2. **Instances built outside the container carry no chain**, so `Advised` degrades to a plain
   attribute with one extra dict lookup. `Account()` in a unit test behaves normally.

### F8.4 When advice is armed

`_apply_interceptors()` runs *after* `binding.create()` returns (`container.py:2162-2167`,
`2196-2199`), i.e. after `__init__`, after `_inject_class_vars_sync()` and after `@PostConstruct`.
The chain attribute therefore does not exist yet during construction, so **writes performed by the
constructor, by class-var injection, and by `@PostConstruct` are not advised** — with no extra flag
or guard code; it is a property of the attach point.

This is a deliberate divergence from AspectJ, whose `set` pointcut does match constructor
assignments. It is the right default here because it matches the surrounding framework's own rule —
Jakarta Interceptors 2.1 describes interception as *"invoked after dependency injection completes"*
(research-F8 §CDI Interceptors: Method-only scope). Documented in `Advised`'s docstring.

### F8.5 API surface

| Name | File | Purpose |
|---|---|---|
| `Advised` | `providify/field.py` (new) | data descriptor; `Advised()` or `Advised(default)` |
| `FieldAccessContext` | `providify/field.py` (new) | `.target`, `.field`, `.value`, `.kind`, `.proceed()` |
| `@AroundGet` | `providify/decorator/interceptor.py` | marks the read-advice method |
| `@AroundSet` | `providify/decorator/interceptor.py` | marks the write-advice method |

`FieldAccessContext.proceed()` mirrors `InvocationContext.proceed()` (`type.py:1459-1465`): walk
`_chain` by index, then call a terminal. Terminal for `kind="get"` returns the stored value; terminal
for `kind="set"` writes `ctx.value` to storage and returns `None`. Advice may mutate `ctx.value`
before proceeding (transform), skip `proceed()` (veto a write / return a substitute), or wrap the
returned value (transform a read).

An interceptor class may carry any combination of `@AroundInvoke`, `@AroundGet`, `@AroundSet`; at
most one of each, matching the existing one-`@AroundInvoke` rule.

### F8.6 Rejected target types

`Advised` on a `dataclasses.is_dataclass()` class raises `TypeError` at `__set_name__` time — no,
`__set_name__` runs before `@dataclass` processes the class, so the check goes in the
`@InterceptorBinding` decorator path *and* in `_apply_interceptors` (whichever sees the finished
class). Rationale for rejecting rather than supporting:

- A frozen dataclass's generated `__init__` uses `object.__setattr__`, writing straight into the
  instance `__dict__` and bypassing `Advised.__set__`; but `Advised` is a **data descriptor**, so
  `__get__` still wins over the instance dict (research-F8: *"Data descriptors… cannot be shadowed by
  instance assignment"*). Reads would return the descriptor's own (unwritten) storage — silent data
  corruption, the worst possible failure mode.
- Non-frozen dataclasses additionally treat a descriptor-valued class attribute as a default via a
  documented-but-subtle protocol, doubling the semantics to specify and test.

Pydantic `BaseModel` (own `__setattr__` + `__pydantic_fields__`) and `attrs` are rejected by the same
check for the same reason. This is the plan-level decision for the scout's open question "whether
dataclasses/frozen dataclasses are in scope".

### F8.7 Pre-existing bug this feature exposes

`_InterceptorProxy` (`container.py:333-372`) defines `__slots__ = ("_target", "_chain")`, `__getattr__`
and `__repr__` — **no `__setattr__` and no `__delattr__`**. So on any bean that carries an
`@AroundInvoke` interceptor today, `proxy.anything = value` raises `AttributeError` instead of writing
through to the target. Independent of F8 this is a bug; with F8 it also makes set-advice unreachable
whenever a bean has both method and field interceptors. Fixed as a step below, with its own
regression test.

### F8 — alternatives considered

- **`__getattribute__` / `__setattr__` override on the target class, advising every attribute** —
  ✅ no per-field declaration, catches everything. ❌ Rejected: research-F8 names the performance cost
  explicitly; it advises framework-internal writes (class-var injection, `@PostConstruct`) with no
  clean way to exclude them; and it makes the join-point set invisible at the call site.
- **Extend `_InterceptorProxy` to intercept non-callables** (the "obvious" fix, since
  `container.py:355` returns non-callables unchanged) — ✅ smallest diff, reuses the existing chain.
  ❌ Rejected: the proxy cannot intercept *writes* (`__slots__`, F8.7), cannot intercept access from
  *inside* the target's own methods (`self.balance` bypasses the proxy entirely — the single most
  important case for field advice), and keeps the `isinstance` breakage documented at
  `container.py:342`.
- **Annotation-driven declaration, `balance: Annotated[float, FieldMeta()]`, woven by a class
  decorator** — ✅ matches providify's `Annotated`-heavy house style. ❌ Rejected: requires resolving
  class annotations at decoration time, before the module finishes executing; forward references and
  PEP-563 strings make that unreliable, and `_annotations.py`'s tiered resolver
  (`_annotations.py:608-680`) exists precisely because that resolution is hard *even at container
  time*. A descriptor object in the class body needs no resolution at all.
- **Bytecode rewriting to mirror AspectJ literally** (research-F8 §AspectJ weaving) — ❌ Rejected
  outright: research-F8 §Python descriptor protocol names descriptors as the Python-native
  equivalent, used by Django/SQLAlchemy/Traitlets.

---

## Steps

TDD-ordered. Each numbered step is independently verifiable.

### Part A — F7: multibinding

1. [x] `tests/test_multibinding.py` (new) — failing tests for the container API:
   `container.multibind(Handler)` is idempotent; `container.get(list[Handler])` returns every active
   implementation, priority-ascending; returns `[]` when none registered; raises nothing.
2. [x] `providify/container.py` — add `self._collection_points: set[Any] = set()` next to
   `_bindings` (`container.py:532`), and `multibind(self, cls, *, qualifier=None) -> None` in the
   registration section near `provide()` (`container.py:897`). Full docstring citing Guice
   `Multibinder.newSetBinder` (research-F7 §Guice). Must call `_invalidate_type_caches()`
   (`container.py:2356`) — resolvability of `list[T]` changes.
3. [x] `providify/container.py` — add private `_is_collection_point(self, cls) -> bool` (checks the
   set **and** the `@Multibound` marker) and `_multibound_inner(self, hint) -> Any | None` returning
   `T` when `hint` is `list[T]`/`List[T]` with `T` a collection point, else `None`.
4. [x] `providify/container.py` — add `_collect_sync(self, inner, qualifier)` /
   `_collect_async(...)`: `_filter` → async-provider guard (copy the guard at
   `container.py:1185-1194`) → `validate_bindings()` gate → instantiate sorted by ascending
   priority → return list, `[]` when empty. Docstring must state the empty-list rule and cite
   research-F7 §Autofac.
5. [x] `providify/container.py:2342` — `_is_resolvable()` returns `True` when
   `_multibound_inner(hint)` is not `None`, **after** the existing `any(_interface_matches(...))`
   check so a literal `list[T]` binding still wins (§Design F7.3 rule 1).
6. [x] `providify/container.py` — in `get()` and `aget()`, before the normal candidate search:
   if no binding literally matches the hint **and** `_multibound_inner(hint)` is not `None`, return
   `_collect_sync/_collect_async`. Keep the precedence comment inline.
7. [x] `tests/test_multibinding.py` — failing tests for the injection site: constructor param
   `handlers: list[Handler]`; class-var `handlers: list[Handler]` (verifies the
   `_inject_class_vars_sync` path at `container.py:3119` inherits it); async resolution via `aget`;
   qualifier-filtered collection point; `@Profile`/`@Alternative`-inactive contributions excluded
   (via `_filter`); `exact_only` self-bindings not duplicated (`container.py:1389-1396`).
8. [x] `providify/decorator/multibinding.py` (new) — `@Multibound` class decorator stamping
   `__di_multibound__`, plus `_is_multibound(cls)`. Same shape as `decorator/interceptor.py:31-78`.
9. [x] `tests/test_multibinding.py` — failing tests for `@Multibound`: declared on the interface,
   discovered by module scanning, works without any `multibind()` call.
10. [x] `providify/validation.py:391` — `_classify_hint()` gains an optional
    `is_collection_point: Callable[[Any], bool] | None = None` parameter; bare `list[T]` with a
    truthy predicate → `_HintSpec(base_type=T, multi=True, optional=True, deferred=False)`, matching
    the `InjectInstances` branch at `validation.py:504-521`; otherwise `None` (today's behaviour).
    Update **every** call site (grep `_classify_hint`; `container.py:5476` and `5489`-region) to pass
    `self._is_collection_point`.
11. [x] `tests/test_validation.py` — startup graph validation does not report a multibound
    `list[T]` as a missing binding, and does report contributions' own missing deps.
12. [x] `providify/container.py` `validate_bindings()` (`container.py:4744`) — emit a message when a
    type is both a declared collection point and has a binding whose interface literally matches
    `list[T]`, naming the defined precedence. Cites research-F7 §Risks #2. Test in
    `tests/test_multibinding.py`.
13. [x] `providify/__init__.py` — export `Multibound` (`__all__` + import, alongside the
    `decorator.interceptor` imports at line 123).

### Part B — F8: field interceptors

14. [x] `tests/test_field_interceptor.py` (new) — failing tests for `Advised` **without any
    container**: default value, get/set round-trip, per-instance isolation, `Account.balance`
    (class access) returns the descriptor, `del obj.balance` works unadvised.
15. [x] `providify/field.py` (new) — `Advised` data descriptor (`__slots__`, `__set_name__`,
    `__get__`, `__set__`, `__delete__`; storage key `f"__advised_{name}"` in the instance `__dict__`)
    and `FieldAccessContext` dataclass with `target`/`field`/`value`/`kind`/`_chain`/`_chain_index`/
    `_terminal` and `proceed()`. Docstrings must cite research-F8 §Python descriptor protocol for the
    data-descriptor choice and state the armed-after-construction rule (§Design F8.4) and the
    no-`__delete__`-advice rule.
16. [x] `tests/test_field_interceptor.py` — failing tests for `@AroundGet`/`@AroundSet` markers:
    detected on an interceptor class, MRO-walked, at most one of each.
17. [x] `providify/decorator/interceptor.py` — add `_AROUND_GET_ATTR`, `_AROUND_SET_ATTR`,
    `AroundGetMarker`, `AroundSetMarker`, `AroundGet`, `AroundSet`; generalise
    `_get_around_invoke_method` (line 129) into a shared `_get_marked_method(cls, attr, marker_cls)`
    and keep `_get_around_invoke_method` as a thin wrapper (no behaviour change for existing tests).
    Docstrings cite AspectJ `get`/`set` pointcuts (research-F8 §AspectJ) — **not** CDI.
18. [x] `tests/test_interceptor.py` — failing regression test for the F8.7 bug: a bean with an
    `@AroundInvoke` interceptor supports `proxy.attr = value` and `del proxy.attr`, writing through
    to the target.
19. [x] `providify/container.py:333-372` — add `__setattr__` and `__delattr__` to `_InterceptorProxy`
    delegating to `_target` via `object.__getattribute__(self, "_target")`, with `_target`/`_chain`
    still routed through `object.__setattr__`. Comment referencing the slots trap.
20. [x] `tests/test_field_interceptor.py` — failing end-to-end tests: a container-resolved bean with
    `@Audited` + `Advised` field fires set-advice on write and get-advice on read; advice can
    transform the value, veto a write by not calling `proceed()`, and order multiple interceptors;
    a bean built directly (`Account()`) fires nothing; construction-time writes
    (`__init__`, class-var `Inject[T]`, `@PostConstruct`) fire nothing (§Design F8.4);
    `isinstance(bean, Account)` stays `True` when only field advice is present.
21. [x] `providify/container.py:1595-1640` — extend `_apply_interceptors()`: in the single existing
    interceptor-class walk, collect three chains (`invoke`, `get`, `set`) instead of one; when
    get/set chains are non-empty, `object.__setattr__(instance, "__di_field_chain__", {...})`
    **before** any `_InterceptorProxy` wrapping (the chain must live on the real target, not the
    proxy); return the proxy only when the invoke chain is non-empty. Resolve each interceptor
    instance once per bean, as today (`container.py:1631-1634`).
22. [x] `providify/container.py` — reject unsupported targets: if a class declares any `Advised`
    field and is a dataclass / pydantic `BaseModel` / `attrs` class, raise `TypeError` naming the
    class, the field and the reason (§Design F8.6). Put the check in `_apply_interceptors()`'s
    weave path so it fires once per class, memoised in a `set[type]`. Test each rejected shape in
    `tests/test_field_interceptor.py`.
23. [x] `tests/test_field_interceptor.py` — failing test: a class using `__slots__` with an
    `Advised` field raises a clear `TypeError` on first write (no instance `__dict__` to store in),
    not a bare `AttributeError`. Implement the guard in `Advised.__set__`.
24. [x] `providify/__init__.py` — export `Advised`, `FieldAccessContext`, `AroundGet`, `AroundSet`
    (`__all__` near lines 81/104-108 and the imports at 123/191-197).

### Part C — documentation and backlog correction

25. [ ] `BACKLOG.md` — rewrite F8's rationale column: remove "Closes gap vs Jakarta CDI Full profile
    (providify currently matches CDI Lite/method-only)"; replace with the AspectJ/descriptor framing
    and a pointer to research-F8, which shows CDI has **no** field interception in either profile.
    Mark F7 and F8 done.
26. [ ] `README.md` — a multibinding section (`multibind` / `@Multibound` / bare `list[T]`, empty-list
    rule, relationship to `InjectInstances[T]`) and a field-interceptor section (must state that this
    goes **beyond** CDI, which is method-only, and is modelled on AspectJ `get`/`set` pointcuts).
27. [ ] `docs/agents/injection-cheatsheet.md` — add the `list[T]` row (with the "requires a declared
    collection point" caveat) and the `Advised` row.
28. [ ] `docs/agents/usage-rules.md` + `SKILL.md` — rules: *declare the collection point before using
    `list[T]`*; *only `Advised` fields are join points*; *no field advice during construction*; *no
    dataclasses as field-advice targets*.
29. [ ] `CHANGELOG.md` — entries for F7 and F8 under Unreleased, with the corrected F8 framing.

## Edge cases

**F7**

- `list[T]`, `T` not a collection point, no literal binding → `_UNRESOLVED` → parameter default or
  the existing missing-binding error. Unchanged from v1.
- `list[T]` with a literal `provide(fn, returns=list[T])` binding **and** `T` a collection point →
  the literal binding wins; `validate_bindings()` reports the ambiguity (step 12).
- Collection point with zero contributions → `[]`.
- Collection point where some contribution is an async provider, resolved via sync `get()` →
  `RuntimeError` naming the providers, identical wording to `get_all()` (`container.py:1191-1194`).
- `list[T]` where a contribution is `@Alternative`-disabled or `@Profile`-inactive → excluded;
  `_filter` already enforces this.
- `Inject[list[T]]` (marker + bare collection) → `InjectMeta.all` is `False`, so the `InjectMeta`
  branch calls `get(list[T])`, which hits the new rule. Same result as bare `list[T]`. Add a test.
- `InjectInstances[T]` on a type that is *also* a collection point → still `get_all(T)`, still raises
  on zero matches. Intentional: the two spellings differ on the empty case only, and this is
  documented.
- `list[T]` where `T` is itself generic (`list[Repository[User]]`) → `_filter` + `_interface_matches`
  handle parameterised interfaces already (`utils.py:143-146`). Add a test.

**F8**

- `Advised` field read before any write, no default → `AttributeError` naming the field, like a
  normal missing attribute.
- `Advised` field on a `__slots__` class → `TypeError` on first write (step 23).
- Bean with `@AroundInvoke` *and* field advice → chain attached to the target, proxy wraps it;
  `proxy.balance` and `proxy.balance = x` both reach the descriptor (requires step 19).
- `self.balance` inside the bean's own method → advised (the descriptor is on the class; this is the
  case a proxy could never handle).
- Advice that does not call `proceed()` on a set → the write is vetoed and the old value stands.
- Advice that raises → propagates to the caller unwrapped; no partial write.
- Subclass of an advised class → inherits the descriptor; chain attaches per instance, so a subclass
  bean without the `@InterceptorBinding` annotation gets no chain and no advice.
- Two beans of the same advised class, one container-managed and one not → independent; only the
  managed one has `__di_field_chain__`.
- Interceptor with `@AroundGet` but the target has no `Advised` field → no chain attached, no error.

## Verification

```bash
cd /home/edoardo/projects/providify
uv run pytest tests/test_multibinding.py tests/test_field_interceptor.py -q     # new suites
uv run pytest tests/test_interceptor.py tests/test_inject.py tests/test_validation.py -q  # touched
uv run pytest -q                                                                 # full suite, no regressions
uv run ruff check providify tests
uv run ruff format --check providify tests
```

The full suite must stay green: `InjectInstances[T]` tests (`tests/test_inject.py:186-247`) and all
existing interceptor tests are the back-compat contract for both features.

## Risks

- **⚠️ ASSUMPTION — F8's rationale must be reframed.** research-F8 proves Jakarta CDI Interceptors
  2.1 / CDI 4.1 have **no** field-level interception in either the Lite or the Full profile, so
  `BACKLOG.md`'s "closes gap vs CDI Full profile" justification is false. This plan assumes the
  correct rationale is *"AspectJ-style field advice, implemented with Python's descriptor
  protocol"*, and step 25 corrects the backlog. **Invariant: no artefact produced by this plan may
  claim CDI parity for field interception.**
- **⚠️ ASSUMPTION — collection injection returns `[]` on zero contributions while `get_all()`
  continues to raise `LookupError`.** The scout flagged this as an open question. Decided in favour
  of divergence: declaring a collection point is an explicit statement that zero is legal
  (research-F7 §Autofac), whereas changing `get_all()` would break existing callers who rely on the
  raise. Risk: two public spellings with different empty-case behaviour is a documentation burden.
  Invariant: `get_all()`'s behaviour is byte-for-byte unchanged.
- **⚠️ ASSUMPTION — the collection-point marker belongs on the type, not on each contribution.**
  research-F7's librarian explicitly recommends the per-provider form (`@provide(returns=list[T])`,
  Injector's `@multiprovider`). We follow Guice's placement instead for the reasons in §F7.2. Risk:
  users arriving from Injector look for a per-provider marker and do not find one — mitigated by
  documenting the mapping in the README.
- **⚠️ ASSUMPTION — field interception covers only descriptor-declared fields, never broad instance
  `__dict__` access.** Scout open question, decided in §F8.2. Risk: a user expecting AspectJ's
  `set(* Account.*)` wildcard finds they must annotate each field. Invariant: no
  `__getattribute__`/`__setattr__` override is ever installed on a user class.
- **⚠️ ASSUMPTION — dataclasses, frozen dataclasses, pydantic and attrs are out of scope and
  actively rejected.** Scout open question, decided in §F8.6. Risk: this is a real usability cliff
  for codebases whose beans are dataclasses. Invariant: rejection is a loud `TypeError`, never
  silent partial support — the frozen-dataclass failure mode (data descriptor shadowing the
  `object.__setattr__`-written instance dict) is silent data corruption, which must never ship.
- **⚠️ ASSUMPTION — field advice is *not* armed during construction** (§F8.4). Diverges from
  AspectJ, which does match constructor assignments. Risk: an auditing interceptor misses the
  initial value. Invariant: the arming point is the existing `_apply_interceptors()` call site — no
  new flag, so it cannot drift out of sync with `@PostConstruct` ordering.
- **Performance.** F7 adds one `set` membership test to `_is_resolvable`; keep the multibound check
  *after* the existing `any(_interface_matches(...))` loop so the hot path is unchanged. F8 adds one
  instance-`__dict__` lookup per `Advised` access and **zero** cost to every other attribute.
  Invariant: `container.py:2145-2192`'s singleton cache fast path is not touched by either feature.
- **`_classify_hint` signature change** (step 10) is module-private (`validation.py:288-292` notes
  `_HintSpec` is not exported) but has multiple call sites; a missed one silently degrades startup
  validation rather than failing loudly. Mitigation: step 11's test, plus grep for every call site.
- **`_InterceptorProxy.__setattr__`** (step 19) changes behaviour for existing users who currently
  get `AttributeError` on writes to an intercepted bean. That is a bug fix, but it is observable;
  note it in the CHANGELOG.
