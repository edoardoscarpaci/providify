# Providify Production-Readiness Backlog

Compiled 2026-08-24 via `/discover`. Scope: internal-use-first (varco_core and
sibling services are the primary consumers), targeted at a v2.0-style release
where breaking API changes are acceptable if they improve the design. This
supersedes the 2026-08-23 backlog: F1 carries over unchanged (still unbuilt,
plan already exists), F2–F8 carry over unchanged, and one new item (F9,
graceful shutdown) was added from fresh research into what "production ready"
concretely requires beyond feature parity with other DI libraries.

Sources:
- Scout report (2026-08-23): mapped current public API, feature checklist,
  gaps vs stated goals.
- Scout report (2026-08-24): confirmed current state — F1 planned but not yet
  implemented (`plans/003-startup-graph-validation.md`); all table-stakes
  features (scopes, async, lifecycle, circular-dep handling, thread-safety)
  present and tested; 78 public exports, 37 test files.
- Research brief 001: `design/di-features-taxonomy/research/001-table-stakes-vs-differentiators.md`
  (table-stakes vs differentiator features across Spring, Jakarta CDI, Guice,
  dishka, python-dependency-injector, FastAPI, Microsoft.Extensions.DI).
- Research brief 002: `design/di-features-taxonomy/research/002-emerging-libraries-and-2025-shifts.md`
  (newer/niche Python DI libraries — svcs, wireup, that-depends, Lagom, Rodi,
  Injector — and 2025–2026 ecosystem shifts; identifies observability hooks
  as a differentiator opportunity no existing Python DI container has).
- Research brief 003: `design/di-features-taxonomy/research/003-production-readiness-conventions.md`
  (health-check, observability, graceful-shutdown, and testing conventions
  across Spring Boot, .NET Generic Host, Quarkus/CDI, and Python DI libraries;
  identifies graceful shutdown as the one production-lifecycle gap universal
  across mainstream frameworks and absent from the Python DI ecosystem).

## Backlog

| ID | Feature | Severity | Complexity | Rationale | Evidence |
|----|---------|----------|------------|-----------|----------|
| F1 | Startup-time full graph validation (`container.validate()`) | 🔴 must | S–M | Only must-have carried from the original interview; catches missing/ambiguous bindings at boot instead of first-request-in-prod. Builds on existing `warm_up()`/`describe()` introspection. Already has an approved, unbuilt plan. | Table-stakes across Spring/.NET/StructureMap — research 001 §Table-stakes #8; wireup's "if it starts, it works" fail-fast philosophy — research 002 |
| F9 | Graceful shutdown (`container.shutdown()`, reverse-dependency-order teardown) | 🔴 must | M | Promoted to must in this pass: the one production-lifecycle gap that's universal table-stakes in Spring/.NET/Quarkus and absent from every Python DI library surveyed. Same risk class as F1 — without ordered teardown, a shutdown can use a dependency after it's been disposed, causing data loss or connection errors mid-deploy. | Reverse-dependency-order teardown convention across Spring Boot, .NET Generic Host, Quarkus/CDI — research 003 §3; confirmed absent in python-dependency-injector/dishka — research 003 §3 |
| F2 | Profile-based provider activation (`@Profile`, env-driven `@Alternative`) | 🟡 should | S | Natural extension of the existing `@Alternative` mechanism — smallest lift of the should-haves. Removes manual toggling for env-specific wiring. | Spring `@Profile`/`@Conditional` — research 001 §Differentiators #7; `intuition` on providify fit given `@Alternative` already exists |
| F3 | Configuration binding (env/YAML/JSON → typed `@Configuration` objects) | 🟡 should | M | Confirmed pain point in interview. Removes hand-written `@Provider` factories that read env/config manually — the #1 gap scout identified. | Table-stakes: python-dependency-injector, Spring, FastAPI+pydantic — research 001 §Table-stakes #6 |
| F4 | Pytest integration (fixture-based container overrides) | 🟡 should | M | Confirmed pain point. Cuts test boilerplate vs manual `override()`/`reset_binding()` calls; testing-first DI is expected baseline, not bolted on. | FastAPI, python-dependency-injector, Spring Test — research 001 §Table-stakes #5; testing ergonomics confirmed as baseline "expected" checklist item — research 003 §4 |
| F5 | Multi-module startup/shutdown ordering (module DAG for `@Configuration`) | 🟡 should | M | Needed once internal services split into >1 module with cross-module lifecycle dependencies. | Spring Modules, Guice Modules, dishka Components — research 001 §Differentiators #9 |
| F6 | Container observability hooks (creation/disposal/timing events, OTel-span-ready) | 🟢 nice | S–M | Kept below should-haves, but evidence strengthened this pass: no existing Python DI container exposes instrumentation hooks for dependency resolution or scope transitions — a genuine differentiator, not just parity-catchup. | Gap confirmed across python-dependency-injector, dishka, and all newer entrants (svcs, wireup, that-depends, Lagom, Rodi) — research 002 §OpenTelemetry Instrumentation; scout gap #4 |
| F7 | Multibinding / collection injection (`list[Handler]` for all impls of a type/qualifier) | 🟢 nice | M | Useful for plugin/handler-registry patterns. | Guice multibinding, Autofac keyed collections — research 001 §Differentiators #11 |
| F8 | Field/class-level interceptors (AOP scope expansion beyond method-only) | 🟢 nice | L | Largest lift of all candidates; included in this v2.0 push rather than deferred to its own release, since breaking changes are already in scope. Closes gap vs Jakarta CDI Full profile (providify currently matches CDI Lite/method-only). Severity stays "nice" — size doesn't imply urgency. | Jakarta CDI Interceptors 2.1 spec — research 001 §Differentiators #3; scout gap #5 |

## Parked

- **Health-check / readiness hook** — considered this pass (candidate from
  research 003, modeled on svcs/Spring Actuator). Parked: Providify has no
  HTTP layer of its own, and in every Python DI library surveyed, health/
  readiness probes are the consuming web framework's responsibility (FastAPI/
  Starlette), not the container's. Revisit only if a concrete internal
  consumer asks for a narrowly-scoped `container.health_check()` that reports
  singleton/resource liveness for the app layer to wire into its own probe.
- **ContainerProtocol-style interoperability** (Rodi/punq pattern) — research
  002 flags as early/unproven; not adopted by any mainstream framework yet.
  Not proposed as a candidate this pass.
- **TaskGroup-aligned scope lifetimes** (structured concurrency) — research
  002 flags as aspirational; no reference implementation exists in any DI
  library yet. Not proposed as a candidate this pass.

(Two candidates raised in earlier phases were validated as already implemented
rather than parked: circular-dependency handling and thread-safety guarantees
— both confirmed present and solid by the scout report, so they were never
proposed as backlog items.)

## Not proposed (already resolved)

`upstream_gaps.md` U-20 (explicit `returns=` override for `container.provide()`
and `@Provider`) — shipped in v1.1.1, confirmed by scout report. No longer a gap.
