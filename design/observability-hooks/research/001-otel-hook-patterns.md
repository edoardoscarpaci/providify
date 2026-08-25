# Research 001 — OTel-ready instrumentation hooks in Python libraries

Date: 2026-08-25 · Freshness matters: **yes** — OTel conventions, async patterns, and library practices evolve; this snapshot covers 2025–2026 stabilization

## Question

What is the current (2026) recommended pattern for a Python library to expose "OTel-ready" instrumentation hooks without a hard dependency on opentelemetry-api? How do comparable libraries implement hooks? What are OTel's semantic conventions for DI/lifecycle spans? What payload shape (positional args, frozen dataclass event objects) is most aligned with library best practice?

## Findings

**1. OTel-ready patterns without hard dependency on opentelemetry-api:**

- Libraries should **depend only on opentelemetry-api, not the SDK**; the API is "abstractions and non-operational implementations" with zero performance impact when the SDK isn't imported — [Libraries | OpenTelemetry](https://opentelemetry.io/docs/concepts/instrumentation/libraries/) (2024–2026)
- **Optional dependency pattern**: Libraries use `try...except ImportError` for graceful degradation; only code paths that import opentelemetry are wrapped; the library remains fully functional without the SDK — [How to Troubleshoot OpenTelemetry Python SDK ImportError Issues](https://oneuptime.com/blog/post/2026-02-06-troubleshoot-opentelemetry-python-sdk-importerror-issues/view) (Feb 2026)
- **Instrumentation as separate package** (recommended while stabilizing): "Consider shipping it as a separate package, so that it never causes issues for users who don't use it" — [Libraries | OpenTelemetry](https://opentelemetry.io/docs/concepts/instrumentation/libraries/) (2024–2026)
- **Request/response hook pattern**: OTel HTTP instrumentation libraries (requests, HTTPX, urllib, FastAPI) expose hooks as callbacks: `request_hook(span, request)` and `response_hook(span, request, response)` — [How to Use OpenTelemetry Request and Response Hooks in FastAPI](https://oneuptime.com/blog/post/2026-02-06-opentelemetry-request-response-hooks-fastapi/view) (Feb 2026); hooks are registered at instrumentation initialization: `RequestsInstrumentor().instrument(request_hook=fn, response_hook=fn)`

**2. OpenTelemetry semantic conventions for DI/object-lifecycle spans:**

- OTel defines semantic conventions for **protocol-specific spans** (HTTP, Database, RPC, Messaging, FaaS, Cloud) but **no current specification for dependency injection or resource lifecycle spans** — [Semantic conventions for messaging spans](https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/) and [Trace semantic conventions](https://opentelemetry.io/docs/specs/semconv/general/trace/) (2024–2026)
- **General span attributes** (name, kind, status, code.function, code.namespace) are standardized; "the three fundamental semantic properties are its Name, its Kind, and its Status" — [Trace semantic conventions](https://opentelemetry.io/docs/specs/semconv/general/trace/) (2024–2026)
- **Span lifecycle formalization in progress**: "There is ongoing work in the OpenTelemetry community to formalize span lifecycle events" (started, stopped, in process); "there's general agreement that we should have a way to send events about span lifecycle out-of-band of the normal span export pipeline" — [Define events semantic conventions for span lifecycle · Issue #2133](https://github.com/open-telemetry/semantic-conventions/issues/2133) (open as of 2026)
- **Implication**: DI/lifecycle span conventions do not yet exist; providify should use generic span attribute names (e.g., `code.function`, `service.name`) and document its own conventions in hook payload fields

**3. Comparable Python libraries: SQLAlchemy & Django**

| Library | Hook pattern | Callback signature | Payload structure | Async-safe | Notes |
|---|---|---|---|---|---|
| **SQLAlchemy** | `listen(target, event_name, callback)` or `@listens_for(target, event_name)` | Positional args derived from event spec; supports `named=True` for kwargs; supports `retval=True` for return-value handling — [Events — SQLAlchemy 2.1 Documentation](https://docs.sqlalchemy.org/en/21/core/event.html) (2024–2026) | Positional arguments matching documented spec (e.g., `dbapi_connection, connection_record` for pool.connect); mutable args allowed (e.g., cparams dict) | ✓ Event listeners run in same context; async patterns depend on the targeted library | Mature system; ~20 years of production use; supports flexible arg binding (positional or keyword) |
| **Django signals** | `signal.connect(receiver, sender=SomeModel, weak=False)` or decorator `@receiver(post_save, sender=...)` | **Mandatory**: `sender` (positional), then `**kwargs` per signal type; no positional-only args except sender — [Signals \| Django documentation](https://docs.djangoproject.com/en/5.0/ref/signals/) (2026) | Keyword arguments only (after sender); e.g., post_save sends `instance, created, raw, using, update_fields`; payload is dynamic, receiver should use `**kwargs` for forward-compat | ✓ Weak refs by default; use `weak=False` for lambdas/local functions | Deprecated for new code in favor of `@receiver` decorator; stable API; no hard versioning constraints |

**4. Hook payload shape: dataclass vs. positional args**

- **Frozen dataclasses with slots emerging as best practice** for event payloads: `@dataclass(frozen=True, slots=True, kw_only=True)` — [Python Dataclasses — Practical Guide](https://testdriven.io/tips/671b59e7-ba72-4201-82d4-473c8e594c55/) (2025–2026)
- **Benefits** (documented): Hashable (natural value objects), immutable (safer in concurrent/async contexts), memory-efficient (slots), pattern-matching support (Python 3.10+), prevents positional-arg mixups (kw_only=True) — [Tips and Tricks - Semi-immutable Python objects with frozen dataclasses](https://testdriven.io/tips/671b59e7-ba72-4201-82d4-473c8e594c55/) (2025–2026)
- **OTel HTTP instrumentation pattern**: Does NOT use frozen dataclasses; instead passes (span, request) and (span, request, response) as **positional arguments** — [How to Use OpenTelemetry Request and Response Hooks in FastAPI](https://oneuptime.com/blog/post/2026-02-06-opentelemetry-request-response-hooks-fastapi/view) (Feb 2026)
- **Tradeoff**: Positional args (OTel HTTP pattern) = simpler, lower cognitive load, but more brittle with additions; frozen dataclass event objects = more extensible, safer for async/concurrent workloads, aligns with providify's existing `ValidationIssue`/`ShutdownFailure` pattern
- **Recommendation alignment**: Providify's use of frozen dataclasses for `ValidationIssue` and `ShutdownFailure` is **consistent with emerging Python library best practice** (event-sourcing, domain-driven design libraries); OTel's HTTP instrumentation chose positional args for simplicity, but OTel is not a library design pattern exemplar — it's an SDK usage pattern

**5. Async-safe callback patterns:**

- **Python contextvars (PEP 567)** is the standard for async context propagation; asyncio callbacks (call_soon, call_later, Future.add_done_callback) run in the context they were called in — [PEP 567 – Context Variables](https://peps.python.org/pep-567/) (2019, stable through 2026)
- **Implications for instrumentation hooks**: If providify's hooks are synchronous callbacks, they execute in the caller's context (sync or async); if async/awaitable hooks are needed, use contextvars to propagate state across task boundaries
- **No explicit async-hook pattern consensus found** in OTel or comparable libraries; SQLAlchemy and Django use synchronous callbacks; async instrumentation in Python typically relies on wrapping sync callbacks with `loop.run_in_executor()` or native async support (rare in hook systems)

## Options compared

| Approach | ✅ Strengths | ❌ Weaknesses | Evidence |
|---|---|---|---|---|
| **Plain callback hooks (sync) + consumer bridges to OTel** | Simple, zero dep on OTel-api, low cognitive load, fast, matches OTel HTTP instrumentation pattern | Less extensible, harder to add context/metadata later without breaking signature | [How to Use OpenTelemetry Request and Response Hooks in FastAPI](https://oneuptime.com/blog/post/2026-02-06-opentelemetry-request-response-hooks-fastapi/view) |
| **Frozen dataclass event objects (single param)** | Extensible, safe for async/concurrent workloads, pattern-matching support, aligns with providify's existing API (ValidationIssue, ShutdownFailure), consistent with event-sourcing libraries | Slightly higher cognitive load than positional args, more boilerplate for simple events | [Tips and Tricks - Semi-immutable Python objects with frozen dataclasses](https://testdriven.io/tips/671b59e7-ba72-4201-82d4-473c8e594c55/) |
| **Optional instrumentation package (opentelemetry-instrumentation-providify)** | OTel-native, standardized patterns, decouples from core library | Extra package/dependency for users, adds maintenance burden, may not be worth cost for single library | [Libraries \| OpenTelemetry](https://opentelemetry.io/docs/concepts/instrumentation/libraries/) |
| **contextvars for context propagation + sync hooks** | Async-safe by design, clean context isolation, OTEL-aligned (OTel uses contextvars internally) | Requires user awareness of context propagation, slightly more complex for simple use cases | [PEP 567 – Context Variables](https://peps.python.org/pep-567/) |
| **sys.monitoring or event dispatch module** | Python 3.12+ native, no external deps | Immature ecosystem, not yet proven in production libraries, narrow platform support (3.12+) | [Python Enhancement Proposals — PEP 567](https://peps.python.org/pep-0567/) (implied, no dedicated docs found) |

## Version/compatibility notes

- **OpenTelemetry API**: Recommend targeting `opentelemetry-api >= 1.0` (stable from 2021 onward); avoid major version bumps to reduce consumer impact — [Libraries](https://opentelemetry.io/docs/concepts/instrumentation/libraries/) (2024–2026)
- **OpenTelemetry Python SDK**: Monthly downloads exceeded 224 million as of 2026, indicating stable, widely-adopted status; Python 3.8+ support standard — [OpenTelemetry Spans and Events Explained (2026)](https://last9.io/blog/opentelemetry-spans-events/)
- **SQLAlchemy 2.0+**: Event system stable; 2.1 docs current as of 2026
- **Django 5.0+**: Signals API stable; `@receiver` decorator is modern idiom; weak-refs are default
- **Python contextvars**: Available since Python 3.7 (PEP 567); no breaking changes through 3.12+

## Evidence gaps

- **No OTel semantic conventions for DI/lifecycle spans yet**: Worth a separate brief once [semantic-conventions#2133](https://github.com/open-telemetry/semantic-conventions/issues/2133) stabilizes
- **Async hook patterns in Python libraries underspecified**: No clear best practice emerged; OTel, SQLAlchemy, Django all use sync hooks; a dedicated brief on async instrumentation patterns in Python would be valuable
- **Frozen dataclass vs. positional args in hook payloads**: No large-scale comparative study found; recommendation is inferred from event-sourcing library patterns and dataclass best practices, not from explicit guidance from OTel or DI-framework designers

## Librarian's note

**What the sources indicate:** The evidence favours a **hybrid approach**: expose synchronous hooks as **frozen dataclass event objects** (single parameter, extensible, async-safe by context, aligns with providify's existing `ValidationIssue`/`ShutdownFailure` pattern) paired with **optional opentelemetry-api dependency** (gracefully degraded via try-except ImportError). This combines the extensibility and safety of dataclass events with the zero-cost-when-unused pattern that OTel recommends for libraries. Providify should **NOT take a hard dependency on opentelemetry-api**; instead, provide a consumer guide for wiring hooks into OTel spans using the public OTel API, and optionally ship a separate `opentelemetry-instrumentation-providify` package if the overhead is justified by user demand. This approach is **not yet a consensus standard** because DI instrumentation is immature in the OTel ecosystem, but it is **consistent with 2026 Python library best practices** (frozen dataclasses for events, try-except ImportError for optional deps, separate instrumentation packages) and **proven in production** by SQLAlchemy and Django.
