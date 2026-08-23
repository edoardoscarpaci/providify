
---

### U-20 · `container.provide()` has no way to register a factory whose interface is known only at call time {#u-20}

**Raised by:** `varco_core`'s own Plan 014 (DI settings + provider-helper refactor), 2026-08-23,
while consolidating six independently hand-rolled copies of the same workaround into one shared
helper (`varco_core.providify_compat.provide_factory()`).
**Status:** ✅ verified in source — `providify/binding.py:456-505` (`ProviderBinding.__init__`),
`providify/container.py:658-672` (`DIContainer.provide`), `providify/decorator/scope.py:489-570`
(`Provider`).

**What providify does today.** `@Provider` stamps registration metadata on a function and returns
it unchanged (`scope.py:538-566` — never reads `__annotations__`). The interface a provider binds
under is derived later, exactly once, when `container.provide(fn)` constructs a `ProviderBinding`:
it reads `fn`'s **raw, static return annotation** (`_raw_annotations(fn)["return"]`) and resolves it
against `fn.__globals__` (`binding.py:496-505`). `_resolve_return_annotation()` already handles a
`str` annotation under PEP 563 correctly (`_eval_annotation` + `get_type_hints`, `binding.py:337-425`)
— quoted forward references and nested generics both work. **Neither `@Provider` nor
`DIContainer.provide()` accepts an explicit interface override** (`scope.py:479-486`,
`container.py:658`) — the return annotation is the *only* channel providify offers for stating what
a factory produces.

**Why this is a real gap, not a PEP-563 quirk.** The annotation-resolution machinery works exactly
as documented for every *statically expressible* return type. It cannot work for a factory whose
target interface is a **parameterised generic alias computed at runtime** — e.g. one `AsyncRepository[D]`
provider built per domain-model class inside a loop (`varco_sa.di`, `varco_fastapi.client.bind_clients_from`),
or a factory built inside `varco_ws.di`/`varco_fastapi.router.mcp`/`router.skill` where the concrete
class is a constructor argument, not a name in the closure's own signature. No annotation string could
name `D` before the loop iteration exists to bind it — this isn't a case `_resolve_return_annotation`
declined to support, it's a case with no annotation to write in source at all.

**The workaround this forced, independently, six times.** Every one of those call sites reached
past the documented API into `factory.__annotations__["return"] = <computed type>`, mutating the
closure's `__annotations__` dict by hand immediately before calling `container.provide(factory)` —
relying on the fact (verified, not assumed) that neither `@Provider`'s decorator body nor
`container.provide()` itself reads the annotation before `ProviderBinding.__init__` does. Each site
carried its own copy-pasted `DESIGN:` comment justifying the ordering; one (`varco_fastapi.di`, prior
to this cleanup) had the reasoning **factually wrong** about why the ordering mattered. `varco_core`
has now collapsed six of the seven sites into one internal helper
(`varco_core.providify_compat.provide_factory()`) precisely so there is one place to delete when this
lands upstream — but every one of the six is still reaching into a private attribute
(`__annotations__`) that providify's public API never promised as a registration mechanism.

**The ask.** Give `container.provide()` (and/or `@Provider`) a supported, explicit way to state the
interface, bypassing annotation derivation entirely:

```python
# one shape that would work:
container.provide(factory, returns=AsyncRepository[User])

# or, mirroring @Provider's own kwarg style:
@Provider(returns=lambda: AsyncRepository[User])   # deferred — evaluated at provide() time
def _repo_factory(uow: Inject[IUoWProvider]) -> Any: ...
```

This removes the only reason any varco call site currently mutates `__annotations__` on someone
else's function object, and removes the trap where the *ordering* of decorate-vs-patch is
load-bearing but invisible in the type signature of either `@Provider` or `provide()`.

**Priority: P2 — hygiene / API-surface completeness, not a blocker.** The workaround is understood,
centralised, and tested (`ProviderBinding.__init__` never reads the annotation before `provide()`
does, so the patch-then-register ordering is safe and will stay safe unless that internal detail
changes) — nothing is broken today. It is filed because it is the kind of gap that would otherwise
get re-invented a seventh time by the next caller who needs a dynamically-typed provider and doesn't
know the five prior sites exist.

**Interim:** `varco_core.providify_compat.provide_factory()` — one shared, documented, tested helper
that does the annotation-patch-then-register dance, explicitly named and positioned (module
docstring) as a shim to be deleted the day this lands.

---