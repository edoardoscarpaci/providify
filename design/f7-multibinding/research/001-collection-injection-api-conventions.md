# Research 001 — Collection Injection API Conventions Across DI Frameworks

Date: 2026-08-25 · Freshness matters: **yes** — DI API conventions are stable, but version-specific features/syntax can change

## Question

Across DI frameworks that support multibinding/collection injection (Guice, Autofac, and Python DI libraries), what is the actual injection syntax convention? Specifically: does bare `list[T]` / `List[T]` type hints trigger implicit "inject all" behaviour, or does every framework require an explicit marker/wrapper type at injection site? What are the documented risks with bare collection types?

## Findings

### Java: Guice

- **Binding syntax**: Requires explicit registration via `Multibinder.newSetBinder(binder(), Type.class)` at binding time. Contributions are added via `addBinding().to(impl)`, `addBinding().toInstance()`, or `addBinding().toProvider()` — [Multibinder API (Guice 5.1.0)](https://google.github.io/guice/api-docs/5.1.0/javadoc/com/google/inject/multibindings/Multibinder.html)
- **Injection syntax**: Bare `Set<T>` without wrapper type. Example: `@Inject Set<Snack> snacks` — [Multibinder API docs](https://google.github.io/guice/api-docs/5.1.0/javadoc/com/google/inject/multibindings/Multibinder.html)
- **Alternative form**: `Collection<Provider<T>>` can also be injected if access to providers themselves is needed
- **Semantics**: Set is immutable; elements resolved at injection time; if any element is null, injection fails — [Multibinder API](https://google.github.io/guice/api-docs/5.1.0/javadoc/com/google/inject/multibindings/Multibinder.html)

### .NET: Autofac

- **Binding syntax**: No special registration required. Simply register implementations normally (e.g., `builder.RegisterType<TaskA>().As<ITask>()`) — [Implicit Relationship Types (Autofac 9.0.0 docs)](https://autofac.readthedocs.io/en/latest/resolve/relationships.html)
- **Injection syntax**: Bare `IEnumerable<T>` as a constructor parameter automatically collects all registered implementations. Example: `public class Processor(IEnumerable<ITask> tasks)` — [Implicit Relationship Types docs](https://autofac.readthedocs.io/en/latest/resolve/relationships.html)
- **Null safety**: Returns empty enumerable (not null or exception) if no implementations are registered — [docs](https://autofac.readthedocs.io/en/latest/resolve/relationships.html)
- **Keyed services**: Can also use `IIndex<string, ITask>` or similar for key-based collection access, distinct from `IEnumerable<T>` — [docs](https://autofac.readthedocs.io/en/latest/resolve/relationships.html)

### Python: Injector (alecthomas/injector)

- **Binding syntax**: Explicit `@multiprovider` decorator or `binder.multibind()` API. Example with decorator:
  ```python
  class MyModule(Module):
      @multiprovider
      def provide_strs(self) -> List[str]:
          return ['str1']
  ```
  Or via binder: `binder.multibind(list[Interface], to=A)` — [Injector API 0.24.0](https://injector.readthedocs.io/en/latest/api.html)
- **Injection syntax**: Bare `List[T]` or `list[T]` type hints. Multiple `@multiprovider` declarations are automatically combined. Example: `injector.get(List[str])  # ['str1', 'str2']` — [API reference](https://injector.readthedocs.io/en/latest/api.html)
- **Contribution semantics**: Multiple calls to `multibind()` accumulate values into a shared list, not replace — [API reference](https://injector.readthedocs.io/en/latest/api.html)
- **Support**: Added support for `typing.Dict` and `typing.List` in multibindings (v0.18+) — [Changelog](https://injector.readthedocs.io/en/latest/changelog.html)

### Python: python-dependency-injector

- **Binding syntax**: Explicit `providers.List()` wrapper at configuration time:
  ```python
  dispatcher_factory = providers.Factory(
      Dispatcher,
      modules=providers.List(
          providers.Factory(Module, name="m1"),
          providers.Factory(Module, name="m2"),
      ),
  )
  ```
  — [List Provider docs (4.49.0)](https://python-dependency-injector.ets-labs.org/providers/list.html)
- **Injection syntax**: Standard type hint `modules: List[Module]` in the consuming class, but container requires explicit `providers.List()` registration — does not auto-collect from multiple bindings — [List Provider docs](https://python-dependency-injector.ets-labs.org/providers/list.html)
- **Key difference from Guice/Injector**: No implicit "collect all implementations" semantics; you explicitly specify which items form the list — [docs](https://python-dependency-injector.ets-labs.org/providers/list.html)

### Python: Wireup

- **Collection support**: Framework advertises support for `Sequence[T]` and `Mapping[K, V]` injection in its documentation on "Interfaces & Qualifiers" — [Wireup overview](https://maldoinc.github.io/wireup/latest/)
- **Status**: Type-driven DI framework; collection injection capability is mentioned but examples and detailed API documentation not publicly visible in accessible docs
- **Sourced reference**: [Wireup GitHub](https://github.com/maldoinc/wireup), [PyPI](https://pypi.org/project/wireup/)

### Python: Other Libraries

- **Dishka**: No collection injection pattern documented in README or official docs — [Dishka GitHub](https://github.com/reagento/dishka), [docs](https://dishka.readthedocs.io/)
- **Lagom, Punq, Rodi**: Type-based autowiring frameworks; no explicit collection/multibinding support documented — [Lagom docs](https://lagom-di.readthedocs.io/en/1.7.0/), [Punq/Rodi references](https://github.com/meadsteve/lagom), [Rodi](https://github.com/Neoteroi/rodi)
- **svcs**: Service locator pattern; no documented collection injection API — [svcs docs](https://svcs.hynek.me/), [GitHub](https://github.com/hynek/svcs)
- **that-depends**: No public documentation or collection injection examples found

## Options compared

| Framework | Registration Syntax | Injection Syntax | Implicit "All Implementations"? | Wrapper Required? | Auto-accumulation Across Modules? |
|-----------|---------------------|------------------|--------------------------------|--------------------|----------------------------------|
| Guice (Java) | `Multibinder.newSetBinder()` explicit | Bare `Set<T>` | ✅ Yes (via Multibinder) | ❌ No at injection | ✅ Yes (cross-module) |
| Autofac (.NET) | None (normal registration) | Bare `IEnumerable<T>` | ✅ Yes (automatic) | ❌ No | ✅ Yes (automatic) |
| Injector (Python) | `@multiprovider` or `binder.multibind()` | Bare `List[T]` / `list[T]` | ✅ Yes (via decorator/API) | ❌ No at injection | ✅ Yes (combines) |
| python-dependency-injector | `providers.List()` wrapper | Type hint only | ❌ No (explicit list) | ✅ Yes (in config) | ❌ No (static) |
| Wireup | Unknown | `Sequence[T]` (mentioned) | Unknown | Unknown | Unknown |

## Design Implications

### The Bare `list[T]` Question

**What the evidence shows:**
- **Autofac** and **Guice** use bare type hints (`IEnumerable<T>`, `Set<T>`) at injection site, not wrapper types
- **Injector (Python)** also uses bare `List[T]` / `list[T]` for injection
- **python-dependency-injector** splits the concern: bare type hint in the class, but explicit `providers.List()` in container config

### Risks / Gotchas NOT Prominently Documented

1. **Type erasure**: Java generics erase `Set<T>` to `Set` at runtime, but Guice/Injector handle this via explicit multibinder registration, so ambiguity is prevented by API contract, not syntax
2. **Single-value vs. collection ambiguity**: If a provider legitimately wants to return a single `List` object (e.g., a pre-built list), bare `list[T]` syntax could cause confusion. However:
   - Guice mitigates this by requiring explicit `Multibinder` registration (not implicit)
   - Autofac mitigates by requiring explicit registration of the list-returning service as `IEnumerable<T>` (which is the type that means "all implementations")
   - Injector mitigates via `@multiprovider` decorator or `binder.multibind()` call (explicit)
   - None of the reviewed frameworks auto-treat any `list[T]` type hint as "collect all" without explicit registration/decoration
3. **Provider vs. instance distinction**: Guice supports `Collection<Provider<T>>` for access to lazy/proxied providers; other frameworks not explicitly documented

### Cross-Framework Consensus

**Implicit registration patterns are NOT implicit in syntax.** Every framework reviewed requires explicit API usage or decoration to enable multibinding:
- Guice: `Multibinder.newSetBinder()`
- Injector: `@multiprovider` or `binder.multibind()`
- Autofac: implicit in **container registration**, not in type hints (a distinctive design choice)

**Injection syntax consensus**: Once multibinding is enabled, use the bare collection type (`Set<T>`, `List[T]`, `IEnumerable<T>`), not a wrapper. This is consistent across Guice, Injector, and Autofac.

## Version/Compatibility Notes

- **Guice 5.1.0** (latest stable, from Google): Multibinder API stable since ~Guice 2.0 — [Multibinder across versions](https://google.github.io/guice/api-docs/)
- **Autofac 9.0.0** (latest): `IEnumerable<T>` implicit relationship stable for 5+ years — [Autofac versioning](https://autofac.readthedocs.io/)
- **Injector 0.24.0** (latest Python): `@multiprovider` added ~v0.18; `typing.List` / `typing.Dict` support in recent versions — [Changelog](https://injector.readthedocs.io/en/latest/changelog.html)
- **python-dependency-injector 4.49.1** (latest): `providers.List()` has been stable in this form for 3+ years — [PyPI history](https://pypi.org/project/dependency-injector/)

## Evidence Gaps

- **Wireup collection injection**: Claims `Sequence[T]` support but no examples or detailed API docs publicly accessible. Worth a separate investigation if Wireup is a candidate.
- **Python library survey completeness**: Dishka, Lagom, Punq, Rodi, svcs, that-depends do not document multibinding / collection injection patterns in their public READMEs/docs. Could mean: (a) feature absent, (b) undocumented, or (c) achieved via different naming (e.g., "aggregation", "factory composition"). A source code review would be needed to confirm.
- **Ambiguity risk documentation**: No framework explicitly documents the "single-value list vs. collect-all list" ambiguity as a design risk; this gap is inferred from absence of that discussion in all reviewed docs.

## Librarian's note

**What the sources indicate:**
The evidence strongly favours bare collection-type injection syntax (`Set<T>`, `IEnumerable<T>`, `List[T]`) once multibinding is *explicitly enabled* via registration/decoration API. No reviewed framework uses a wrapper type at the injection site. The trade-off between providify's current `InjectInstances[T]` wrapper and bare `list[T]` syntax is not a choice between "implicit magic vs. explicit magic" — it's a choice between:
1. **Explicit wrapping at injection site** (current providify design, similar to... no major framework)
2. **Explicit wrapping in registration config** (like python-dependency-injector's `providers.List()`, but different code location)
3. **Explicit marker at registration only** (Guice's Multibinder, Injector's `@multiprovider`; injection site uses bare type)
4. **Automatic collection with no explicit marker** (Autofac's unique approach: auto-collect all registered `ITask` into `IEnumerable<ITask>`, but this breaks if someone intentionally registers an `IEnumerable<T>` as a concrete service)

Guice and Injector's model (explicit at registration, bare at injection) appears to be the community standard in the JVM world and is replicated in Python Injector. Autofac's approach is more "magical" but safe because .NET's strong typing and explicit service registration make accidental collisions less likely.

For a Python library, the Injector precedent suggests: support bare `list[T]` at injection site (drop `InjectInstances[T]` wrapper), but require explicit `@provide(returns=list[T])` or similar marker at registration to enable multibinding for that type. This matches Guice's mental model while staying Pythonic.

---

**Sources:**
- [Guice Multibinder API (5.1.0)](https://google.github.io/guice/api-docs/5.1.0/javadoc/com/google/inject/multibindings/Multibinder.html)
- [Autofac Implicit Relationship Types (9.0.0)](https://autofac.readthedocs.io/en/latest/resolve/relationships.html)
- [Injector API Reference (0.24.0)](https://injector.readthedocs.io/en/latest/api.html)
- [Injector Changelog](https://injector.readthedocs.io/en/latest/changelog.html)
- [python-dependency-injector List Provider (4.49.0)](https://python-dependency-injector.ets-labs.org/providers/list.html)
- [Wireup GitHub](https://github.com/maldoinc/wireup)
- [Dishka GitHub](https://github.com/reagento/dishka)
- [Lagom Documentation](https://lagom-di.readthedocs.io/en/1.7.0/)
