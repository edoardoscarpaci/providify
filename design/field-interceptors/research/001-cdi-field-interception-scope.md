# Research 001 — Does Jakarta CDI Interceptors spec support field-level interception?

Date: 2026-08-25 · Freshness matters: **Yes** — Jakarta CDI spec versions and capability scope can change between releases.

## Question

Does the Jakarta CDI Interceptors specification (current version 2.1, and CDI 4.1 for the Full profile) actually define field-level interception (i.e., intercepting reads/writes to instance fields), or is CDI interception strictly limited to method invocations (`@AroundInvoke`), constructor calls (`@AroundConstruct`), and timeout methods (`@AroundTimeout`)? Does the CDI Full profile (as opposed to CDI Lite) add field-level interception support? What does CDI Decorators cover?

## Findings

### CDI Interceptors: Method-only scope

- **Jakarta Interceptors 2.1 defines exactly five types of interception, all method/constructor/lifecycle-level only** — no field-level interception. The five types are: `@AroundInvoke` (business methods), `@AroundTimeout` (timeout methods), `@PostConstruct`, `@PreDestroy`, and `@AroundConstruct` (constructor). — [Jakarta Interceptors Specification 2.1](https://jakarta.ee/specifications/interceptors/2.1/jakarta-interceptors-spec-2.1) (Jakarta EE 10/11)

- **CDI 4.1 (Full) does not add field-level interception.** CDI Lite vs. Full distinction relates to build-time vs runtime reflection, session/conversation scopes, specialization, and decorators—not to field-level interception. Both profiles are strictly method/constructor-level for interceptors. — [Jakarta CDI Specification 4.1](https://jakarta.ee/specifications/cdi/4.1/jakarta-cdi-spec-4.1)

- **CDI Interceptors specification explicitly states scope**: "interpose on the invocation of business methods," "invoked after dependency injection completes," "invoked before instances are destroyed"—no mention of field access interception anywhere in the spec. — [Jakarta Interceptors 2.1](https://jakarta.ee/specifications/interceptors/2.1/jakarta-interceptors-spec-2.1)

### CDI Decorators: Also method-only

- **CDI Decorators are a Full-only feature**, but they are also method-scoped only. Decorators are "similar to interceptors, but apply only to beans of a particular Java interface" and are "aware of the semantics of the intercepted method," enabling type-safe decoration—but still of method invocations only, not fields. — [Jakarta CDI Specification 4.1](https://jakarta.ee/specifications/cdi/4.1/jakarta-cdi-spec-4.1)

## Options compared

| Capability | CDI Lite | CDI Full | Field-level? | Evidence |
|---|---|---|---|---|
| `@AroundInvoke` | ✅ | ✅ | No | [Interceptors 2.1](https://jakarta.ee/specifications/interceptors/2.1/jakarta-interceptors-spec-2.1) |
| `@AroundConstruct` | ✅ | ✅ | No | [Interceptors 2.1](https://jakarta.ee/specifications/interceptors/2.1/jakarta-interceptors-spec-2.1) |
| Decorators | ❌ | ✅ | No | [CDI 4.1](https://jakarta.ee/specifications/cdi/4.1/jakarta-cdi-spec-4.1) |
| Field-level interception | ❌ | ❌ | — | Not in spec |

## Reference models for field-level interception

Since CDI does not support field-level interception, here are the actual frameworks used for this:

### AspectJ (Java reference model)

- **AspectJ supports `get` and `set` pointcuts** for field-level interception via bytecode weaving. — [Pointcuts Explained: Spring AOP vs. AspectJ](https://danubius.io/en/blog/pointcuts-explained-part-1)

- **Bytecode weaving mechanism**: AspectJ's compiler performs compile-time weaving, modifying bytecode to inject advice at field access points. Join points (including field reads and writes) are efficiently matched by AspectJ's pointcuts. — [Practical Introduction into Code Injection with AspectJ](https://dzone.com/articles/practical-introduction-code)

- **Spring AOP does NOT support field pointcuts** — Spring AOP only provides method-level interception. Native AspectJ is required for `get`/`set` pointcuts. — [Pointcuts Explained](https://danubius.io/en/blog/pointcuts-explained-part-1)

### Python descriptor protocol (Python native equivalent)

- **Python's descriptor protocol (`__get__`, `__set__`, `__delete__`) is the native mechanism for field-level interception.** Descriptors intercept attribute access by implementing these special methods on class-level objects; when an attribute is accessed (e.g., `obj.x`), Python invokes the descriptor method instead of direct access. — [Python Descriptor HowTo Guide](https://docs.python.org/3/howto/descriptor.html) (Python 3.14 docs)

- **Data descriptors (`__set__` + `__get__`) take priority over instance dictionaries.** They cannot be shadowed by instance assignment, enabling reliable field interception. — [Python Descriptor HowTo Guide](https://docs.python.org/3/howto/descriptor.html)

- **Real-world usage**: Django ORM fields, SQLAlchemy columns, Jupyter Traitlets, and reactive programming frameworks all use descriptors to intercept field reads/writes. — [Python Descriptor HowTo Guide](https://docs.python.org/3/howto/descriptor.html)

- **Alternative hook: `__getattribute__()` override** allows interception of every attribute access on an instance (with performance cost). — [Understanding Descriptors](https://pyguides.dev/guides/python-descriptors-guide/)

## Version/compatibility notes

- **Jakarta Interceptors 2.1** — Current spec as of Jakarta EE 10 (Sep 2021) and EE 11 (Sep 2024). No changes to field-level interception scope between 2.0 and 2.1.
- **Jakarta CDI 4.1** — Current spec as of Jakarta EE 11 (Sep 2024). CDI 4.0 introduced Lite vs Full split; 4.1 maintains this distinction without adding field-level interception.
- **Python 3.8+** — Descriptor protocol is stable; `__set_name__()` was added in Python 3.6.

## Evidence gaps

- **Vendor-specific extensions to CDI**: Some Jakarta EE vendors (e.g., Payara, WildFly) may offer proprietary field-level interception extensions not in the spec itself. This brief only covers the official Jakarta CDI/Interceptors specifications.
- **Pre-CDI field access hooks**: Jakarta EE 8 and earlier had different interceptor specs; this research focuses on current versions (2.1+).

## Librarian's note

**The sources clearly indicate: CDI (both Lite and Full) does NOT support field-level interception. The "closes gap vs CDI Full profile" framing in feature planning is inaccurate.** If field-level interception is desired, the actual reference models are AspectJ (bytecode weaving via compile-time `get`/`set` pointcuts in Java) and Python's descriptor protocol (method interception via `__get__`/`__set__`/`__delete__` at class definition time). CDI's scope is strictly method/constructor/lifecycle callbacks. The Lite/Full distinction is about decorators, scopes, and portable extensions—not interception granularity.
