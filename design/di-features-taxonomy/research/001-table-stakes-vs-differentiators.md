# Research 001 — DI Library Table-Stakes vs. Differentiators

Date: 2026-08-23 · Freshness matters: **yes** (PEP changes, async ecosystem trends, version releases)

## Question

What features are considered "table stakes" (expected/essential) vs. "differentiators" (competitive edge) for production-ready dependency injection (DI) frameworks? Research across Python, JVM, and .NET ecosystems to inform feature prioritization for providify.

## Findings

### TABLE-STAKES Features

#### 1. Scope/Lifecycle Management (Essential)
- **Multiple standard scopes** (singleton, transient/prototype, request/scoped, session, application) are expected across all mature DI frameworks — [Spring Framework Reference](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/) (Spring 4.2.5); [Jakarta CDI 4.0 Specification](https://jakarta.ee/specifications/cdi/4.0/jakarta-cdi-spec-4.0.html); [Autofac Documentation](https://docs.autofac.org/en/latest/integration/netcore.html)
- **Custom scope support** is standard — [Guice Guide](https://www.baeldung.com/guice) (Baeldung); dishka, python-dependency-injector both support custom scopes ([dishka alternatives docs](https://dishka.readthedocs.io/en/stable/alternatives.html))
- **Request scope** is foundational for web frameworks — universal across Spring, Jakarta CDI, FastAPI, python-dependency-injector

#### 2. Type-Safe Resolution with Qualifier/Annotation Support (Essential)
- **Type-based resolution** is universal — all major frameworks resolve by type first — [Spring autowiring](https://medium.com/dev-spring/autowired-in-spring-boot-6e46e7c9c4a3)
- **Annotation-based disambiguation** via @Qualifier/@Named when multiple implementations of same type exist — [Spring reference](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/); [Guice](https://www.baeldung.com/guice)
- **PEP 593 Annotated metadata** now standard practice in Python DI (Injector 0.22.0+, AnyDI support) — [PEP 593](https://peps.python.org/pep-0593/); [FastAPI Annotated support #3323](https://github.com/fastapi/fastapi/issues/3323)

#### 3. Constructor, Field/Property, and Method Injection (Essential)
- Constructor injection is the recommended pattern — [Spring best practices](https://medium.com/dev-spring/autowired-in-spring-boot-6e46e7c9c4a3); [Guice](https://www.baeldung.com/guice)
- Field/property injection supported (though sometimes discouraged) — all major frameworks support
- Method injection (setter/parameter injection) supported — [Spring reference](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/)

#### 4. Async/Await-Native Support (Essential for modern Python/async contexts)
- **Async providers/factories** expected in modern frameworks — [python-dependency-injector async docs](https://python-dependency-injector.ets-labs.org/providers/async.html) (v4.49.1); [dishka](https://dishka.readthedocs.io/en/stable/alternatives.html) supports async factories; [FastAPI dependency integration](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- **Concurrent dependency initialization** — async providers can initialize multiple dependencies concurrently — [Dependency Injector async providers](https://python-dependency-injector.ets-labs.org/providers/async.html)
- **Async generators for resource cleanup** (context managers) — FastAPI yield dependencies, python-dependency-injector Resource providers

#### 5. Testing Utilities: Override/Mock Injection (Essential)
- **Dependency override for testing** is expected — [FastAPI testing guide](https://fastapi.tiangolo.com/advanced/testing-dependencies/) (app.dependency_overrides); [python-dependency-injector provider override](https://python-dependency-injector.ets-labs.org/); [Spring @MockBean](https://medium.com/@augustinfotech/fastapi-unit-testing-with-dependency-overrides-a-complete-guide-1db5b451226f)
- **Fixture/context management** for test setup/teardown — FastAPI yield, context managers, Spring @SpringBootTest utilities

#### 6. Configuration Binding (YAML/JSON/ENV) (Essential for 12-factor apps)
- **Environment variable binding** is standard — [python-dependency-injector config](https://python-dependency-injector.ets-labs.org/) reads .yaml, .ini, .json, env vars, pydantic settings; [Spring config](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/)
- **Pydantic integration** for Python — python-dependency-injector, dishka, FastAPI support pydantic BaseSettings/ConfigDict

#### 7. Resource Cleanup & Shutdown Hooks (Essential)
- **Initialization and cleanup lifecycle** — [python-dependency-injector Resource providers](https://python-dependency-injector.ets-labs.org/); [Spring bean lifecycle callbacks](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/); [Jakarta CDI lifecycle](https://jakarta.ee/specifications/cdi/4.0/jakarta-cdi-spec-4.0.html)
- **Context managers / async context managers** — standard pattern in Python DI

#### 8. Clear Error Diagnostics (Essential for DX)
- **Ambiguous binding detection** — [Jakarta CDI AmbiguousResolutionException](https://github.com/eclipse-lsp4jakarta/lsp4jakarta/issues/164); Spring/Guice similar exceptions
- **Missing dependency detection** at startup or deployment — [.NET diagnostics](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection); [StructureMap validation](https://structuremap.github.io/diagnostics/validating-container-configuration/)
- **Validation at startup time** — modern frameworks validate wiring on container initialization rather than lazy-on-first-use

#### 9. Thread Safety Guarantees (Table-stakes for production)
- **Singleton thread-safety** — [dishka alternatives](https://dishka.readthedocs.io/en/stable/alternatives.html); [Thread safety scope isolation](https://codeopinion.com/thread-safety-scoped-lifetime-in-dependency-injection-containers/)
- **Scope isolation design** — "better design is isolation rather than locking" — [WireBox thread safety](https://wirebox.ortusbooks.com/advanced-topics/object-persistence-and-thread-safety)

#### 10. Typing/IDE Support (Essential for Python)
- **Type stub files (.pyi) or native generics** — [python-dependency-injector mypy-friendly stubs](https://python-dependency-injector.ets-labs.org/); PEP 695 type statements (Python 3.12+)
- **Generic support for `Provider[T]` patterns** — enables IDE autocomplete and static type checking

---

### DIFFERENTIATORS — Advanced/Competitive Features

#### 1. Compile-Time Dependency Graph Validation (Rare, High Value)
- **Dagger 2 differentiator** — annotation processor validates entire object graph at compile time, fails compilation if any binding is incomplete/ambiguous — [Dagger 2 Baeldung](https://www.baeldung.com/dagger-2); [Compile-time DI validation](https://blog.stackademic.com/compile-time-dependency-injection-using-dagger-bddeb51e8242)
- **Code generation** — Dagger generates source code mimicking hand-written injector logic for speed and traceability
- **Not common in Python/JVM Spring** (Spring uses runtime validation)

#### 2. Advanced Auto-Wiring / Isolated Sub-Graphs (Rare in Python, Important in dishka)
- **Component isolation** — dishka allows creating isolated sub-graphs to handle duplicate types across modules — [dishka alternatives](https://dishka.readthedocs.io/en/stable/alternatives.html)
- **Automatic wire-up with context data passing** — dishka passes context data to factories post-creation
- **Zero-globals design** — genuine DI without relying on implicit global state — emphasized in dishka

#### 3. Interceptors / AOP (Cross-Cutting Concerns)
- **Spring AOP @Aspect** — enable method-level interception for logging, timing, security, etc. — [Spring AOP](https://medium.com/dev-spring/autowired-in-spring-boot-6e46e7c9c4a3)
- **Jakarta CDI Interceptors & @InterceptorBinding** — spec-defined interceptors with typesafe binding — [Jakarta Interceptors 2.1](https://jakarta.ee/specifications/interceptors/2.1/jakarta-interceptors-spec-2.1)
- **Not standard in Python DI** (would require decorators or middleware)

#### 4. Decorators for Semantic Wrapping (CDI Full feature, not Lite)
- **Jakarta CDI Decorators** — wrap beans implementing interfaces with semantic awareness — [Jakarta CDI 4.0](https://jakarta.ee/specifications/cdi/4.0/jakarta-cdi-spec-4.0.html)
- **Different from Interceptors** — decorators understand the business domain, interceptors handle technical concerns
- **Rare outside Jakarta CDI**

#### 5. Assisted Injection / Automatic Factory Generation
- **Guice AssistedInject** — automatically generates factory implementations, reducing boilerplate — [Guice 3.0+](https://www.baeldung.com/guice)
- **Useful for parameterized object creation** without writing custom providers

#### 6. Circular Dependency Detection & Resolution
- **Spring detects circular refs and offers solutions** — @Lazy deferral, setter injection, refactoring — [Spring circular dependency handling](https://www.baeldung.com/circular-dependencies-in-spring)
- **Dagger fails at compile time** (prevents circular deps entirely)
- **Other frameworks**: Guice, injector don't explicitly handle; requires design discipline

#### 7. Conditional / Profile-Based Provider Selection
- **Spring @Conditional/@Profile** — select beans based on environment, properties, or custom conditions — Spring reference
- **Fast-track feature** for environment-specific overrides without code branching

#### 8. Lazy Injection / Providers-of-Providers
- **Spring @Lazy** — defers initialization until first use — [Spring @Lazy](https://www.baeldung.com/circular-dependencies-in-spring)
- **Provider<T> / Factory<T>** patterns — inject a provider rather than the object directly — supported in Guice, Spring, python-dependency-injector

#### 9. Multi-Module / Plugin Composition
- **Spring Module system / @Import** — modular bean definition
- **dishka Components** — composition of isolated sub-graphs
- **Guice Modules** — separable binding modules
- **Useful for large-scale, multi-team codebases**

#### 10. Performance Optimizations (Source Generated DI)
- **.NET Source Generated DI** — generates IL code at compile time for faster startup and Native AOT compatibility — [.NET Source Generated DI](https://devblogs.microsoft.com/cesardelatorre/comparing-asp-net-core-ioc-service-life-times-and-autofac-ioc-instance-scopes/)
- **Compile-time code generation** reduces reflection overhead
- **Not yet widespread in Python**

#### 11. Metadata/Keyed Resolution & Tagging
- **Autofac tagged lifetime scopes** — services resolved with associated metadata, named/keyed variants — [Autofac documentation](https://docs.autofac.org/en/latest/integration/netcore.html)
- **Guice multibinding** — handle collections of similar types
- **Advanced, niche use cases**

---

### ECOSYSTEM SHIFTS & Trends

#### 1. PEP 593 Annotated as Standard DI Metadata Vehicle (Python 3.9+)
- **Annotated[Type, metadata]** now the standard way to attach DI hints to parameters
- **Adopted by**: FastAPI, Injector (0.22.0+), AnyDI, and emerging frameworks — [PEP 593 official](https://peps.python.org/pep-0593/); Pydantic v2, msgspec, typer all use Annotated for metadata
- **Implication**: providify should support reading Annotated metadata for dependency hints

#### 2. PEP 695 Type Statements (Python 3.12+)
- **type PositiveInt = Annotated[int, Gt(0)]** — cleaner type aliases with metadata
- **Composes with Annotated** — enables richer, more expressive type-based DI hints

#### 3. Async-First Architecture Becoming Default
- **Async providers/factories** no longer optional for frameworks targeting async contexts
- **Concurrent dependency initialization** expected (await multiple async dependencies in parallel)
- **Structured concurrency** patterns (context managers, async generators for cleanup)

#### 4. Thread Safety & Isolation Over Locking
- **Design principle shift**: prefer isolation (scope per thread/request) over shared locks
- **"Better design is isolation rather than locking"** — industry consensus — [WireBox thread safety](https://wirebox.ortusbooks.com/advanced-topics/object-persistence-and-thread-safety)

#### 5. Zero-Globals Design as Best Practice
- **Genuine dependency injection** — no reliance on implicit global state (e.g., service locators)
- **dishka emphasizes this** — [dishka alternatives](https://dishka.readthedocs.io/en/stable/alternatives.html)

#### 6. Testing-First DI (Override/Mock as first-class feature)
- **Dependency override** no longer bolted-on, but core feature — [FastAPI testing](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- **Expectation**: Container.provide() or provider.override() is trivial

---

## Options Compared

| Aspect | Python Ecosystem | JVM Ecosystem | .NET Ecosystem |
|--------|------------------|---------------|----------------|
| **Standard Scopes** | ✅ dishka, python-dependency-injector, FastAPI | ✅ Spring, Guice, Jakarta CDI | ✅ Microsoft.Extensions, Autofac |
| **Async Support** | ✅ dishka, python-dependency-injector, FastAPI | 🟡 Spring (async handlers only); Jakarta CDI (no native async) | 🟡 Async/await support growing |
| **Circular Dep Detection** | ⛔ Most don't detect; injector is x20 slower to handle | ✅ Spring @Lazy; Dagger compile-time | ✅ Some validation |
| **Compile-Time Validation** | ❌ None | ✅ Dagger 2 (Android/CLI tools) | 🟡 Source Generated DI (emerging) |
| **Testing Overrides** | ✅ FastAPI, python-dependency-injector | ✅ Spring, Guice (manual overrides) | ✅ Microsoft.Extensions |
| **Interceptors/AOP** | ⛔ Not standard | ✅ Spring AOP; Jakarta CDI Interceptors | 🟡 Emerging patterns |
| **Type Support** | ✅ PEP 593 Annotated (Python 3.9+) | ✅ Native generics | ✅ Native generics |
| **Performance (Reflection)** | 🟡 Most use runtime reflection | ✅ Dagger (codegen); .NET Source Gen (codegen) | ✅ Source Generated DI (v8+) |
| **Multi-Module Composition** | 🟡 dishka Components (emerging) | ✅ Spring Modules; Guice Modules; Jakarta CDI | ✅ Autofac well-established |

---

## Version/Compatibility Notes

### Python Ecosystem
- **python-dependency-injector**: v4.49.1 (as of research); supports async, Cython optimization, mypy stubs
- **dishka**: Latest version emphasizes async factories, thread-safe singletons, component isolation
- **Injector**: v0.22.0+ adds PEP 593 Annotated support; notably 20x slower than dishka per dishka docs
- **FastAPI**: Uses starlette dependency system (request-scoped by default)
- **PEP 593**: Backported to Python 3.9; standard in 3.9+
- **PEP 695**: Type statements available Python 3.12+

### JVM Ecosystem
- **Spring Framework**: Latest (5.x/6.x); supports all table-stakes features; async improved in recent versions
- **Jakarta CDI 4.0** (current spec): replaces javax.enterprise.inject namespace (Java EE → Jakarta EE)
- **Google Guice**: Stable; common for Android (Dagger 2 is Guice-inspired but compile-time)
- **Dagger 2**: Compile-time validation via annotation processor; common in Android development

### .NET Ecosystem
- **Microsoft.Extensions.DependencyInjection**: Built-in (v8+); Source Generated DI added for performance
- **Autofac**: v9.0.0+ (mature); more features than Microsoft.Extensions
- **.NET AOT / Native AOT**: Requires compile-time codegen (not reflection-friendly); drives adoption of Source Generated DI

---

## Evidence Gaps

1. **Lazy injection / Providers-of-Providers implementation details** — sparse documentation on real-world patterns beyond basic examples. Worth separate brief: usage patterns and design trade-offs.

2. **Python DI performance benchmarks** — dishka claims "x20 faster than injector" but no comprehensive suite comparing all libraries. Methodology unclear.

3. **Circular dependency resolution in pure Python DI** — Python libraries largely don't solve this; design practice is to avoid. No reference implementation of Spring-style @Lazy or Dagger-style compile-time detection in Python.

4. **Plugin/multi-module composition in modern Python DI** — dishka Components are newer; adoption and real-world patterns unclear.

5. **Structured concurrency impact on DI scope design** — async context vars, .NET AsyncLocal<T> patterns not deeply explored in research.

6. **IDE/type-checker behavior** with PEP 593 Annotated DI hints — real-world mypy/pyright behavior with complex Annotated types not thoroughly documented.

---

## Librarian's Note

**What the sources indicate:**

The evidence **strongly favours** a two-tier feature model:
1. **Table-stakes tier** (11 core features) — required for any production framework, expected universally across Spring, Jakarta CDI, Guice, dishka, FastAPI, Microsoft.Extensions
2. **Differentiator tier** (11 advanced features) — competitive edges, not always necessary, valued by specific use cases (compile-time safety, multi-module systems, AOP/interceptors)

**Python-specific insight:** Async/await is now **table-stakes**, not optional. PEP 593 Annotated is the emerging standard for DI metadata (not @dataclass-style or string-based configuration). Most Python libraries lag on circular dependency handling and compile-time validation compared to JVM/Dagger.

**For providify:** Current Jakarta CDI parity (scopes, @Provider, @Configuration, container.provide()) covers table-stakes. Nearest gaps for production readiness are **clear diagnostics (error messages on ambiguous/missing bindings)** and **explicit async scope support** (async factories, concurrent initialization, async cleanup). Compile-time validation is a differentiator, not blocking.

