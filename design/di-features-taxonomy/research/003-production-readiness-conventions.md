# Research 002 — Production-Readiness Conventions: Observability, Health-Checks & Shutdown Patterns

Date: 2026-08-23 · Freshness matters: **yes** (Spring Boot 4 GA 2025, .NET 8+ guidance, Quarkus/CDI lifecycle patterns evolving)

## Question

What operational/observability conventions are considered "production-ready table-stakes" for DI containers and the applications built on them? Specifically: (1) health-check/readiness-probe conventions, (2) observability/tracing integration, (3) graceful shutdown ordering, (4) testing/mocking ergonomics beyond basic override.

## Findings

### 1. Health-Check & Readiness-Probe Conventions

#### Spring Boot Actuator (JVM table-stakes)
- **Auto-configured Health Indicators** registered as beans in DI container for common technologies (DataSource, Redis, RabbitMQ, Elasticsearch, DiskSpace, Ping) — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Kubernetes native support**: Two dedicated health endpoints (`/actuator/health/liveness` and `/actuator/health/readiness`) managed by `LivenessStateHealthIndicator` and `ReadinessStateHealthIndicator` — [Spring Boot 4 Liveness/Readiness Probes](https://dimitri.codes/actuator-health-probes/) (v4 default-enabled since 2025)
- **Application Lifecycle States**: Distinct phases (Starting/REFUSING_TRAFFIC, Started/REFUSING_TRAFFIC, Ready/ACCEPTING_TRAFFIC, Shutdown) — [Spring Boot Actuator](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Health Groups**: Organize indicators by purpose (readiness, liveness, startup) — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Configuration properties** for detail visibility, scope, and per-indicator enable/disable — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)

#### Jakarta CDI / Quarkus Lifecycle Events
- **@Startup/@Shutdown annotations** mark beans for automatic initialization/termination at app lifecycle — [Quarkus Lifecycle Guide](https://quarkus.io/guides/lifecycle)
- **StartupEvent / ShutdownEvent** observed via `@Observes` in `@ApplicationScoped` beans; fired after context startup and before context shutdown — [Getting Started With Jakarta EE 10](https://www.azul.com/blog/getting-started-with-jakarta-ee-10-jakarta-cdi/)
- **@ShutdownDelayInitiated** callback for pre-shutdown phase (readiness probe fails, traffic drained, but app still responsive) — [Quarkus Lifecycle Guide](https://quarkus.io/guides/lifecycle)
- **Graceful shutdown timeout** configured via `quarkus.shutdown.timeout` — [Quarkus Lifecycle Guide](https://quarkus.io/guides/lifecycle)

#### .NET Generic Host
- **IHostApplicationLifetime** injectable interface exposes cancellation tokens for `ApplicationStarted`, `ApplicationStopping`, `ApplicationStopped` — [.NET Generic Host](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host) (v8+)
- **IHostedService / IHostedLifecycleService** lifecycle hooks: `StartingAsync` → `StartAsync` → `StartedAsync` → `OnStarted`, then on shutdown: `OnStopping` → `StoppingAsync` → `StopAsync` → `StoppedAsync` → `OnStopped` — [.NET Generic Host](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host)
- **Framework-managed service registration** in DI container; no manual probe registration needed — [.NET Generic Host](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host)

#### Python Ecosystem (Absence as Finding)
- **FastAPI**: No native health-check convention beyond manual endpoint implementation; reliant on user-defined Starlette routes — [FastAPI Testing Dependencies](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- **python-dependency-injector & dishka**: No built-in health indicators or probe hooks — libraries focus on dependency wiring, not observability plumbing — [python-dependency-injector docs](https://python-dependency-injector.ets-labs.org/); [dishka docs](https://dishka.readthedocs.io/en/stable/concepts.html)
- **Implication**: Python DI frameworks delegate health-check responsibility to framework/application layer (e.g., FastAPI, Litestar, Starlette)

---

### 2. Observability/Tracing Integration Conventions

#### Spring Boot Micrometer (JVM baseline)
- **Auto-configured metrics export** to Prometheus, Datadog, New Relic, CloudWatch, and OpenTelemetry backends via `management.metrics.export.*` configuration — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Out-of-box metrics**: JVM (memory, GC, threads), HTTP requests (Spring MVC/WebFlux), cache performance, database connections — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Pattern**: Micrometer beans auto-registered in Spring DI container; no manual instrumentation of bean creation/disposal events (not a built-in feature) — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)

#### OpenTelemetry + DI Integration
- **Instrumentation class with ActivitySource registered as singleton** in DI container (standard pattern in .NET and recommended for language-agnostic design) — [OpenTelemetry for .NET Instrumentation](https://opentelemetry.io/docs/languages/dotnet/instrumentation/)
- **Vendor-neutral approach**: Libraries inject TracerProvider or use global provider; no hard dependency on OTel at composition root except where needed — [OpenTelemetry Library Instrumentation](https://opentelemetry.io/docs/concepts/instrumentation/libraries/)
- **Kubernetes auto-instrumentation**: OpenTelemetry Operator supports injecting and configuring auto-instrumentation for .NET, Java, and other languages without code changes — [OpenTelemetry Kubernetes Auto-Instrumentation](https://opentelemetry.io/docs/platforms/kubernetes/operator/automatic/)
- **Bean/provider instantiation events**: Not a standard built-in; requires custom interceptors (Spring AOP) or decorators (Jakarta CDI) to observe creation timing — [Jakarta CDI 4.0 Specification](https://jakarta.ee/specifications/cdi/4.0/jakarta-cdi-spec-4.0.html)

#### Python Ecosystem (Limited baseline)
- **No built-in OTel integration**: python-dependency-injector and dishka do not expose hooks for instantiation timing or creation/disposal events — [python-dependency-injector docs](https://python-dependency-injector.ets-labs.org/); [dishka docs](https://dishka.readthedocs.io/en/stable/concepts.html)
- **Manual instrumentation required**: Apps must wrap providers with custom decorators or factories to emit OTel spans — not a platform convention
- **Implication**: Python frameworks prioritize developer ergonomics over built-in observability; observability is bolt-on via middleware or manual instrumentation

---

### 3. Graceful Shutdown Ordering Conventions

#### Fundamental Principle
- **Reverse-dependency-order teardown**: Services are destroyed in reverse order of their initialization; dependers shut down before their dependencies — [Go Graceful Shutdown with Dig](https://medium.com/@greeflas/go-graceful-with-dig-en-8d8efebafae6); [Handling Graceful Shutdown in Containers](https://medium.com/@ankit.deo/handling-graceful-shutdown-in-containers-48866927c80e)
- **Rationale**: Ensures no service attempts to use a dependency after it has been disposed; prevents data loss and connection errors during shutdown — [Graceful Shutdown blog post](https://blog.eightnoteight.dev/p/graceful-shutdown)

#### Spring Boot Implementation
- **Graceful shutdown mode**: Configured via `server.shutdown=graceful` and `spring.lifecycle.timeout-per-shutdown-phase` — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Sequence**: Readiness probe returns "refusing traffic" → Kubernetes removes pod from service load balancer → in-flight requests complete within timeout → application context closes → liveness probe fails (triggers restart if configured) — [Spring Boot Production-Ready Features](https://docs.spring.io/spring-boot/docs/2.4.1/reference/html/production-ready-features.html)
- **Automatic handling**: Spring DI container manages singleton disposal order; developers do not manually order teardown — [Spring bean lifecycle callbacks](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/)

#### .NET Generic Host Implementation
- **Automatic disposal by lifetime**: Transient/Scoped services disposed at end of scope; Singleton services disposed when `IServiceProvider` is disposed (application shutdown) — [.NET Graceful Shutdown](https://www.c-sharpcorner.com/article/graceful-shutdown-in-asp-net-core-10-safely-stopping-applications-without-losin/)
- **IDisposable / IAsyncDisposable contract**: Services managing unmanaged resources must implement disposal interfaces; container automatically disposes on scope exit — [.NET DI Guidelines](https://learn.microsoft.com/en-us/dotnet/core/extensions/dependency-injection-guidelines)
- **Shutdown sequence**: `ApplicationStopping` → `StoppingAsync` → `StopAsync` → `StoppedAsync` → `ApplicationStopped` — [.NET Generic Host](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host)
- **Shutdown timeout**: Default 30 seconds; configured per host; pending connections aborted if timeout expires — [.NET Generic Host](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host)
- **Signal handling**: `ConsoleLifetime` handles SIGINT, SIGQUIT, SIGTERM to initiate graceful shutdown (native POSIX signal support in .NET 6+) — [.NET Generic Host](https://learn.microsoft.com/en-us/dotnet/core/extensions/generic-host)

#### Quarkus/CDI Pattern
- **@ShutdownDelayInitiated callback** allows readiness probe to fail before shutdown, giving orchestrators time to drain traffic — [Quarkus Lifecycle Guide](https://quarkus.io/guides/lifecycle)
- **ShutdownEvent** fires after requests drained; observing methods can perform cleanup in order declared — [Quarkus Lifecycle Guide](https://quarkus.io/guides/lifecycle)
- **Graceful shutdown timeout** configurable; native builds vs. JVM builds have different initialization timing — [Quarkus Lifecycle Guide](https://quarkus.io/guides/lifecycle)

#### Python Ecosystem (Limited/Manual)
- **FastAPI/Starlette**: No built-in graceful shutdown framework; apps must implement signal handlers and manual cleanup — [FastAPI Testing Dependencies](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- **python-dependency-injector / dishka**: Container lifetime not linked to application lifecycle; Resource providers with `__exit__` can be used, but no automatic reverse-order teardown — [python-dependency-injector Resource providers](https://python-dependency-injector.ets-labs.org/); [dishka testing docs](https://dishka.readthedocs.io/en/latest/advanced/testing/index.html)
- **Manual pattern**: Register shutdown handler with application framework, then call container cleanup methods explicitly

---

### 4. Testing/Mocking Ergonomics (Production-Ready Conventions)

#### FastAPI Dependency Override (Python baseline)
- **app.dependency_overrides dict**: Maps original dependency → override function; application-wide scope; simple dictionary mutation — [FastAPI Testing Dependencies](https://fastapi.tiangolo.com/advanced/testing-dependencies/)
- **Per-test isolation**: Override at test start, reset to `{}` at test end; pytest fixture with autouse handles cleanup — [FastAPI Unit Testing with Dependency Overrides](https://medium.com/@augustinfotech/fastapi-unit-testing-with-dependency-overrides-a-complete-guide-1db5b451226f)
- **Common use case**: Mock external services (APIs, auth providers) to avoid costs, latency, or side effects — [FastAPI Testing Dependencies](https://fastapi.tiangolo.com/advanced/testing-dependencies/)

#### python-dependency-injector Testing Pattern
- **provider.override()** method replaces any provider with mock on the fly — [python-dependency-injector testing](https://python-dependency-injector.ets-labs.org/)
- **Pytest fixture pattern**: Create child injector for each test, configure mocks, reset after — [python-dependency-injector docs](https://python-dependency-injector.ets-labs.org/)
- **Binding-only tests**: Verify all binds/providers are available without executing code (compile-time-like safety for runtime) — [Best Practices for Guice](https://www.technowizardry.net/2022/05/best-practices-for-working-with-google-guice/)

#### Dishka Testing Convention
- **Container immutable after creation**: Cannot adjust after build; configure all test providers beforehand — [dishka testing docs](https://dishka.readthedocs.io/en/latest/advanced/testing/index.html)
- **Test provider strategy**: Create separate provider class with mock objects; swap entire provider at container initialization — [dishka testing docs](https://dishka.readthedocs.io/en/latest/advanced/testing/index.html)
- **Pytest fixture integration**: Manage container lifecycle in fixtures; provide mocked dependencies via container retrieval — [dishka testing docs](https://dishka.readthedocs.io/en/latest/advanced/testing/index.html)
- **Scope management for testing**: Use Scope.APP for singletons, Scope.REQUEST for per-request mocks in test containers — [dishka Key Concepts](https://dishka.readthedocs.io/en/stable/concepts.html)

#### Guice Testing Pattern (JVM baseline)
- **Modules.override()** creates test modules that override production bindings — [Best Practices for Guice](https://www.technowizardry.net/2022/05/best-practices-for-working-with-google-guice/)
- **Binding-to-interfaces principle**: Enables swapping concrete implementations; construct mocks instead of real classes in unit tests — [Best Practices for Guice](https://www.technowizardry.net/2022/05/best-practices-for-working-with-google-guice/)
- **@Provides methods** in test modules allow conditional binding and factory logic — [Best Practices for Guice](https://www.technowizardry.net/2022/05/best-practices-for-working-with-google-guice/)

#### Spring Testing with Mocking
- **WebApplicationFactory + Moq pattern**: Configure test DI container by replacing bindings with mocks; base production config, swap specific factories — [Mocking Dependencies in ASP.NET Core Tests](https://cezarypiatek.github.io/post/mocking-dependencies-in-asp-net-core/)
- **@SpringBootTest utilities**: Test lifecycle management, context caching, dependency override via environment — [Spring Testing Support](https://docs.spring.io/spring-framework/docs/4.2.5.RELEASE/spring-framework-reference/htmlsingle/)
- **Scoped proxy pattern**: Register proxy as Scoped; forward calls to test-specific fake implementations; container scope holds test fakes — [Unit Testing with DI Containers](https://jenkov.com/tutorials/java-unit-testing/testing-with-dependency-injection-containers.html)

#### Integration Testing: Testcontainers (Emerging Standard)
- **Disposable Docker containers**: Spin up PostgreSQL, Redis, Kafka, etc. for integration tests; real dependencies, not mocks — [Testcontainers: Testing with Real Dependencies](https://www.docker.com/blog/testcontainers-testing-with-real-dependencies/)
- **Per-test container lifecycle**: Containers created per test/suite, destroyed after completion; clean state guaranteed — [Testcontainers Best Practices for .NET](https://milanjovanovic.tech/blog/testcontainers-best-practices-dotnet-integration-testing)
- **Rationale over pure mocking**: "Mocking won't reliably verify system behavior in production; real dependencies catch incompatibilities and runtime issues" — [Testcontainers: Testing with Real Dependencies](https://www.docker.com/blog/testcontainers-testing-with-real-dependencies/)
- **Supported in Python (testcontainers-python), JVM (testcontainers-java), .NET (Testcontainers.DotNet)** — [Testcontainers](https://www.docker.com/blog/testcontainers-testing-with-real-dependencies/)

#### Auto-Mocking Containers (Advanced)
- **Dynamic test doubles**: Automatically inject stubs into system under test without boilerplate; examples: AutoFixture (C#), Moq (C#) — [Auto-mocking Container](https://blog.ploeh.dk/2013/03/11/auto-mocking-container/)
- **Proxy for interface-based design**: Classes should depend on abstract interfaces, not concrete implementations; container can auto-create mocks for unmocked interfaces — [Auto-mocking Container](https://blog.ploeh.dk/2013/03/11/auto-mocking-container/)
- **Rare in Python** (reflection/introspection overhead); more mature in .NET and JVM ecosystems

---

## Options Compared

| Aspect | Spring Boot | .NET Generic Host | Quarkus/CDI | FastAPI/Python DI |
|--------|-------------|------------------|-------------|-------------------|
| **Health Indicators** | ✅ Auto-registered, Kubernetes-native (v4 default) | ✅ IHostApplicationLifetime events | ✅ @Startup/@Shutdown events, lifecycle callbacks | ⛔ Manual endpoint implementation |
| **Readiness/Liveness Probes** | ✅ Built-in endpoints, health groups | ✅ Via IHostedService callbacks | ✅ Lifecycle events + shutdown delay | ⛔ User responsibility |
| **Metrics/Observability** | ✅ Micrometer auto-config (Prometheus, OTel, etc.) | ✅ Built-in OTel support | 🟡 Via extensions | ⛔ Manual instrumentation |
| **Graceful Shutdown** | ✅ Automatic, reverse-order disposal, configurable timeout | ✅ Automatic, IDisposable contract, POSIX signals | ✅ Automatic with @ShutdownDelayInitiated | ⛔ Manual signal handlers + cleanup |
| **Shutdown Timeout** | ✅ Configurable per phase | ✅ Default 30s, configurable | ✅ Configurable | ⛔ Not standardized |
| **Test Override/Mock** | ✅ WebApplicationFactory + @MockBean | ✅ Interfaces + mocking frameworks | ✅ Modules.override() | ✅ app.dependency_overrides (FastAPI) |
| **Fixture Management** | ✅ @SpringBootTest context caching | ✅ WebApplicationFactory lifetime management | ✅ Pytest fixtures + container rebuild | ✅ Pytest autouse fixtures |
| **Testcontainers Integration** | ✅ Mature ecosystem | ✅ Mature ecosystem | ✅ Supported | ✅ testcontainers-python available |
| **Auto-Mocking Containers** | 🟡 Limited (AOP decorators) | ✅ .NET ecosystems (AutoFixture) | ⛔ Not standard | ⛔ Not standard |

---

## Version/Compatibility Notes

### Spring Boot / Spring Framework
- **Spring Boot 4.0** (GA 2025): Liveness/Readiness probes now enabled by default; Micrometer 1.14+
- **Spring Framework 6.x**: Graceful shutdown, servlet lifecycle management fully integrated
- **Spring Cloud / Kubernetes**: Comprehensive health indicator ecosystem

### .NET Ecosystem
- **Microsoft.Extensions.DependencyInjection** (v8+): Source Generated DI, native POSIX signal handling, IHostedLifecycleService full lifecycle hooks
- **.NET 8 LTS**: ConsoleLifetime handles SIGTERM natively (breaking change from .NET 5 pattern with ProcessExit)
- **Testcontainers.DotNet**: Stable, mature integration

### Jakarta CDI / Quarkus
- **Jakarta CDI 4.0** (current spec): No native health probes; Quarkus adds via lifecycle annotations
- **Quarkus 3.x+**: @Startup, @Shutdown, @ShutdownDelayInitiated standard; graceful shutdown timeout configurable
- **Quarkus native image**: Distinct build-time vs. runtime initialization (e.g., @Initialized(ApplicationScoped.class) fires at build)

### Python Ecosystem
- **FastAPI 0.109+**: TestClient stable, dependency_overrides pattern standard
- **python-dependency-injector 4.49.1+**: Resource providers with context manager protocol
- **dishka 1.0+**: Scopes stable (APP, REQUEST, etc.); test provider pattern documented
- **testcontainers-python 4.x+**: Fully functional; Docker/Podman compatible

---

## Evidence Gaps

1. **OpenTelemetry span generation hooks for DI bean creation/disposal** — Spring AOP and Jakarta CDI Interceptors can wrap creation, but not a built-in convention across frameworks. Worth separate brief: instrumentation patterns for DI instantiation timing.

2. **Standardized health check aggregation in Python** — No equivalent to Spring Boot Actuator health groups or .NET's health check middleware; each framework implements health endpoints independently. No baseline convention identified.

3. **Graceful shutdown in Python DI without manual signal handling** — Fast becoming table-stakes in modern Python frameworks (e.g., Litestar, Starlette 0.40+) but not yet a DI-container-level responsibility.

4. **Auto-mocking container maturity in Python** — Reflection overhead and PEP constraints make AutoFixture-style auto-mocking impractical; unclear if this will become table-stakes in Python DI.

5. **Testcontainers adoption rates** — Emerging best practice, not yet universal; many teams still rely on mocks for speed. Real-world cost/benefit trade-offs not widely documented.

---

## Librarian's Note

**What the sources indicate:**

The evidence reveals a **maturity tier gap**: Spring Boot and .NET Generic Host treat observability (health checks, metrics, graceful shutdown) as **built-in platform concerns** delegated to the DI container. Quarkus/CDI follows suit for lifecycle events, though less opinionated.

By contrast, **Python DI frameworks are deliberately minimalist** — FastAPI, python-dependency-injector, and dishka focus on dependency wiring; observability and graceful shutdown are framework-level (FastAPI, Starlette) or application-level responsibilities.

**For production readiness, a baseline "expected" checklist emerges**:
- ✅ Health-check hook (Spring Actuator, .NET IHostedService, Quarkus lifecycle events, or manual endpoint)
- ✅ Graceful shutdown with reverse-order teardown and configurable timeout
- ✅ Test override/mocking ergonomic enough that no test setup requires manual container construction
- ✅ Fixture/scope management that isolates test state from other tests

**For providify (Python target)**: Current approach (container.provide() override, contextual scopes) aligns with **FastAPI/dishka baseline**. Nearest gaps for "production-ready" positioning:
1. **Explicit graceful shutdown hook** (e.g., `container.shutdown()` with reverse-order resource cleanup) — not yet expected in Python DI, but increasingly required by async frameworks
2. **Health-check aggregation pattern** (optional) — bolt-on, not core, but valuable for Kubernetes readiness/liveness
3. **Testcontainers-friendly fixture patterns** — document container lifetime management with pytest to make integration testing ergonomic
