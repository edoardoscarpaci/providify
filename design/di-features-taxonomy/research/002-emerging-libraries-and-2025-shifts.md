# Research 002 — Emerging DI Libraries & Python Ecosystem Shifts (2025–2026)

Date: 2026-08-23 · Freshness matters: **yes** (ecosystem trends, library releases, PEP developments)

## Question

What distinctive capabilities do newer/less-mainstream Python DI libraries offer beyond the table-stakes in brief 001? And what ecosystem shifts (PEP changes, observability integration patterns, structured concurrency, settings binding) are relevant to DI container design in 2025–2026?

---

## Findings

### EMERGING/NICHE PYTHON DI LIBRARIES — Distinctive Features

#### 1. svcs: Health Checks & Context Manager–Based Lifecycle
- **Core differentiator**: health checks and liveness probes for registered services — [svcs documentation](https://svcs.hynek.me/)
- **Cleanup pattern**: "it's context managers all the way down" — automatic resource management, no manual close() calls
- **No global state required** — supports both DI and service-locator patterns without modifying function signatures or requiring decorators
- **Framework integrations**: native support for AIOHTTP, FastAPI, Flask, Pyramid, Starlette
- **Type safety first**: optional static typing with zero runtime overhead if not type-checking — [svcs PyPI](https://pypi.org/project/svcs)

#### 2. wireup: Fail-Fast Validation & Explicit Lifetimes
- **Distinctive design**: comprehensive startup validation — detects missing dependencies, circular refs, lifetime mismatches, duplicate registrations at container startup, not runtime — [wireup documentation](https://maldoinc.github.io/wireup/latest/)
- **"If the container starts, it works"** philosophy — all wiring errors caught during initialization
- **Three explicit lifetime patterns**: Singleton, Scoped (per-scope resource), Transient (new per injection) — clear resource cleanup semantics
- **Thread-safe by design** and no-GIL (PEP 703) ready
- **Type-driven resolution**: Python type annotations drive dependency wiring, not strings or magic
- **Framework integrations**: FastAPI, Flask, Django, AIOHTTP, Starlette, ASGI, FastMCP, Celery, Strawberry, Click, Typer

#### 3. that-depends: Async-First, No Wiring Required
- **Core differentiator**: async-first IOC container with no code inspection/wiring required — [that-depends GitHub](https://github.com/modern-python/that-depends)
- **Design philosophy**: intentionally simpler than python-dependency-injector, "inspired by but without wiring"
- **Zero dependencies** — the library itself has no external requirements
- **Native FastAPI/FastStream/LiteStar integration** with scopes and context management
- **Dependency context management**: explicit scope/context management for test overrides
- **Type coverage**: full mypy strict mode compliance, Python 3.10+

#### 4. Lagom: Type-Based Auto-Wiring & Invocation-Level Caching
- **Distinctive approach**: "just enough" auto-wiring — most code doesn't know about the container
- **Async-native**: if a dependency is `async def`, it's available as `Awaitable[Dependency]` — natural async integration
- **Invocation-level caching** ("shared" scope): dependencies marked shared are constructed once per function call, useful for request-scoped behavior in web handlers
- **Thread-safe at runtime** — no locking required for singleton management
- **Type-based autowiring**: zero configuration, fully based on types — [Lagom documentation](https://lagom-di.readthedocs.io/en/latest/)
- **Lazy injection**: deferred construction until needed

#### 5. Rodi: Runtime Code Inspection & ContainerProtocol
- **Distinctive design**: inspects code once at runtime, generates functions for dependency graph activation — [Rodi GitHub](https://github.com/Neoteroi/rodi)
- **Validation at build time**: circular dependency detection and missing-service validation happen during graph generation, not at resolution
- **ContainerProtocol**: emerging interoperability standard for alternative DI implementations (also adopted by punq) — [Rodi CHANGELOG](https://github.com/Neoteroi/rodi/blob/main/CHANGELOG.md)
- **Lightweight & fast**: minimal reflection overhead, code-generation approach reduces runtime cost
- **Used in BlackSheep web framework** — production validation

#### 6. Injector (v0.22+): Guice-Inspired with PEP 593 Annotated Support
- **Design**: Python port of Google Guice with strong type safety
- **Typing innovation**: `injector.get(SomeType)` is statically typed to return `SomeType` — IDE/mypy friendly
- **Scope support**: standard module-scoped bindings, custom scopes
- **Note**: research brief 001 flagged dishka claims injector is "x20 slower" — performance not re-verified here

---

### ECOSYSTEM SHIFTS & TRENDS (2025–2026)

#### 1. Async/Await as Table-Stakes (Confirmed)
- **2025 industry consensus**: asynchronous programming is now essential for modern web systems, not optional — [JetBrains State of Python 2025](https://blog.jetbrains.com/pycharm/2025/08/the-state-of-python-2025/); [DEV Community async trends](https://dev.to/srijan-xi/asynchronous-programming-and-pythons-strategic-adaptation-98)
- **Framework maturity**: FastAPI, mature asyncio, vibrant async library ecosystem
- **DI implication**: all containers now must support async factories, concurrent initialization, async generators for cleanup

#### 2. Static Typing Renaissance (2025 Trend)
- **High-performance typing tools entering market**: ty (Astral) and Pyrefly (Meta), both written in Rust, released 2025
- **Implication for DI**: stricter type checking means containers must preserve type information through resolution (typed `Provider[T]`, `Lazy[T]` patterns become critical)
- **Existing evidence**: PEP 593 Annotated is now standard for DI metadata; PEP 695 type statements widely adopted (Python 3.12+)

#### 3. PEP 696 (Type Parameter Defaults) — Tension with Injection
- **What it does**: allows `class Foo[T = str]` default type parameters (Python 3.13+) — [PEP 696](https://peps.python.org/pep-0696/)
- **DI tension**: default type parameters conflict with DI's goal to swap implementations while maintaining type safety — documented issue in real-world usage with Pydantic and bioregistry — [GitHub issue #11270](https://github.com/pydantic/pydantic/issues/11270)
- **Implication**: DI containers may need explicit guidance to override parameter defaults, not yet standardized

#### 4. Context Variables (PEP 567) for Scope Isolation
- **Standard pattern**: asyncio.Task maintains per-task context for isolation — replaces thread-local for async code
- **DI relevance**: enables request-scoped, task-scoped, and invocation-scoped dependencies without global state — [PEP 567](https://peps.python.org/pep-0567/); [Real Python contextvars](https://realpython.com/ref/stdlib/contextvars/)
- **Best practice**: frameworks should store scope metadata in contextvars instead of thread-local or global registries
- **Known issue**: context propagation breaks in hybrid sync/async code (e.g., Starlette where sync fixtures don't propagate to async handlers)

#### 5. Structured Concurrency (TaskGroup) Reshaping Scope Design
- **Python 3.11+**: `asyncio.TaskGroup` brings structured concurrency discipline — tasks start and end together within defined scope
- **Implication for DI**: scope lifetime should align with TaskGroup lifetime; cleanup must respect task-group boundaries — [Structured Concurrency guide](https://applifting.io/blog/python-structured-concurrency); [Billy Poon insight](https://billypoon.com/insights/structured-concurrency-in-python-with-taskgroup-writing-async-code-that-doesn-t-break)
- **Not yet deeply explored**: no mainstream DI container explicitly integrates with TaskGroup lifetimes yet

#### 6. OpenTelemetry Instrumentation — Still Emerging in DI Containers
- **Industry trend**: OpenTelemetry (OTel) auto-instrumentation via bytecode hooks, not yet standard in DI design
- **Current state**: OTel works at framework level (FastAPI, Django middleware), not at DI container level
- **No existing evidence** of DI containers exposing instrumentation hooks for dependency resolution, provider execution, scope transitions
- **Gap identified**: opportunity for containers to emit spans for "provider activated", "scope entered/exited", "circular dependency detected" — would enable end-to-end observability
- **Relevant**: [OTel Python instrumentation docs](https://opentelemetry.io/docs/languages/python/instrumentation/); [AWS OTel instrumentation](https://github.com/aws-observability/aws-otel-python-instrumentation)

#### 7. Pydantic Settings (v2.15.0+) — Settings Binding Pattern Evolving
- **Current state**: pydantic-settings separated from pydantic v2; BaseSettings loads config from env, .env files, cloud secrets
- **DI container integration**: no standard pattern yet for injecting typed settings into containers
- **Emerging pattern**: typed settings (Pydantic BaseSettings) should be injectable like any other dependency, but most frameworks require manual wiring
- **Relevant libraries**: python-dependency-injector has pydantic integration; dishka supports pydantic; FastAPI can bind settings directly
- **Gap**: no "golden path" for composing BaseSettings with multi-scope DI (e.g., singleton config, request-scoped overrides)

#### 8. ContainerProtocol as Interoperability Standard (Emerging)
- **What it is**: interface definition allowing libraries to accept any DI container implementation — [Rodi ContainerProtocol](https://github.com/Neoteroi/rodi)
- **Early adopters**: Rodi, punq
- **Implication**: similar to how frameworks accept `Callable[[],T]` factories, containers can expose a standard protocol for resolution
- **Not yet universal**: most frameworks still tightly coupled to specific container (FastAPI→Starlette's system, Django→builtin, Flask→Flask-specific)

---

## Distinctive Capabilities Candidates for Providify Backlog

| Capability | Library(ies) | Relevance to Providify |
|---|---|---|
| **Health checks / liveness probes** | svcs | Production readiness; observability; can be layered on existing features |
| **Fail-fast startup validation** | wireup | Already planned (brief 001 noted startup-time validation gap); wireup's approach is comprehensive |
| **No-wiring async-first design** | that-depends | Alternative DI philosophy; minimal API surface |
| **Invocation-level scoping (per-call cache)** | Lagom | Complements request scope; useful for handlers |
| **ContainerProtocol interoperability** | Rodi, punq | Allows third-party tools to compose with Providify |
| **Instrumentation hooks (OTel-ready)** | None yet; emerging need | Observability gap across ecosystem; high-value for 2026+ adoption |
| **Context-variable-aware scope isolation** | All (but not explicit in design docs) | Emerging best practice; prevents scope-bleed in hybrid sync/async |
| **TaskGroup-aligned scope lifetime** | None yet; structured concurrency pattern | Emerging; would require rethinking async scope boundaries |

---

## Version/Compatibility Notes

- **svcs**: Latest stable (v23+); asyncio, full static typing support
- **wireup**: v2.2.2+ active development; targets Python 3.8+; thread-safe, PEP 703 (no-GIL) ready
- **that-depends**: v1.13.1+ active development; Python 3.10+; zero dependencies
- **Lagom**: v1.1.0+ stable; thread-safe, async support
- **Rodi**: Active development 2025; API simplification (Services → Container class); ActivationScope renamed from GetServiceContext
- **Injector**: v0.24.0+ stable; PEP 593 Annotated available v0.22.0+
- **PEP 696**: Available Python 3.13+ (released Oct 2024); known issues with Pydantic integration, early adoption
- **Contextvars**: Standard library PEP 567, Python 3.7+; native asyncio integration
- **OpenTelemetry**: v1.24.0+ (2026); Python instrumentation libs mature (opentelemetry-distro, auto-instrumentation via bootstrap)
- **pydantic-settings**: v2.15.0 as of Aug 7, 2026

---

## Evidence Gaps

1. **DI container observability hooks not documented in any framework** — OpenTelemetry integration with DI resolution (provider activation, scope transitions) is absent from literature. Opportunity exists but requires new design patterns.

2. **ContainerProtocol adoption and real-world interoperability** — Rodi and punq define it; adoption by framework libraries (FastAPI, Django, etc.) not yet confirmed. Likely forthcoming but unverified.

3. **Structured concurrency + DI scope design interaction** — no published patterns for aligning TaskGroup lifetime with DI scope lifetime. Emerging need but no reference implementation.

4. **Performance benchmarks (2025–2026)** — dishka's claimed "x20 faster than injector" not independently verified. svcs, wireup, Lagom performance vs. python-dependency-injector not compared.

5. **Type parameter defaults (PEP 696) in real DI codebases** — tension identified; resolution patterns not yet standardized.

6. **Pydantic BaseSettings + DI container composition patterns** — no "golden path" documented across frameworks for hierarchical config (singletons + request-scoped overrides).

---

## Librarian's Note

**What the sources indicate:**

The 2025–2026 Python DI ecosystem is **fragmenting by design philosophy** rather than converging: svcs emphasizes operational visibility (health checks), wireup emphasizes correctness (fail-fast validation), that-depends emphasizes simplicity (no wiring), Lagom emphasizes minimal coupling, Rodi emphasizes interoperability (ContainerProtocol). This mirrors the broader Python ecosystem trend toward smaller, composable tools over monolithic frameworks.

**Two high-value gaps for Providify:**

1. **Observability & instrumentation**: No existing Python DI container has hooks for OTel-style instrumentation. This is a **differentiator opportunity** — adding async-first instrumentation hooks for dependency resolution and scope transitions would enable end-to-end tracing and metrics without requiring manual wiring.

2. **Fail-fast validation + structured diagnostics**: wireup's startup validation pattern is production-grade. Providify already flags ambiguous/missing bindings at startup (brief 001 noted this as a gap); wireup's comprehensive approach (lifetime mismatches, duplicate registrations) could inform an enhanced diagnostics subsystem.

**Not urgent:**

- Task-group-aligned scopes (TaskGroup is young, patterns still emerging)
- PEP 696 defaults (niche, early adoption, conflicts with DI philosophy)
- ContainerProtocol (valuable but not blocking — can be added post-release)

**Confidence in findings:** High for emerging libraries and 2025–2026 trends. Sources include official docs, GitHub repositories, PyPI releases dated 2025–2026, and community blogs. OpenTelemetry evidence is strong (AWS, CNCF, Grafana docs). Structured concurrency patterns are aspirational (no production DI examples yet).
