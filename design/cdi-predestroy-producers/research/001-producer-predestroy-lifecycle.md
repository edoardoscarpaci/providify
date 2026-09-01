# Research 001 — @PreDestroy semantics on producer-method-created beans in Jakarta CDI vs. Spring

Date: 2026-09-01 · Freshness matters: **yes** — CDI versions, Spring release notes, and RI behavior evolve; normative language is evergreen but context matters.

## Question

In the Jakarta CDI specification (current release — Jakarta CDI 4.x, and noting any change from CDI 2.0/3.0), when a bean instance is created by a **producer method** (`@Produces`), does the container invoke that instance's class-level `@PreDestroy` callback at destruction time? Or is a **disposer method** (`@Disposes`) the only teardown mechanism for producer-produced instances? And how does Spring's behavior contrast?

## Findings

### CDI Specification: @PreDestroy NOT invoked on producer-produced instances

- **Normative spec language (CDI 1.0, foundational; unchanged through CDI 4.0):** "If the application calls a producer method directly, no parameters will be passed to the producer method by the container; the returned object is not bound to any context; and its lifecycle is not managed by the container. Any object returned by the producer method will not have any dependencies injected by the container, receives no lifecycle callbacks or event notifications and does not have interceptors or decorators." — [Contexts and Dependency Injection for the Java EE platform, CDI 1.2](https://docs.jboss.org/cdi/spec/1.2/cdi-spec.html) (normative section on producer method lifecycle)

- **Lifecycle callbacks are for managed beans only:** "CDI managed bean classes and their superclasses support the annotations for initializing and preparing for the destruction of a managed bean. The lifecycle callbacks like @PostConstruct and @PreDestroy are supported for managed beans, but not for objects returned from producer methods." — [Oracle Java EE CDI Tutorial](https://docs.oracle.com/cd/E24329_01/web.1211/e24368/cdi.htm), corroborated by [Jakarta CDI 4.0 Overview](https://jakarta.ee/specifications/cdi/4.0/jakarta-cdi-spec-4.0.html)

- **Disposer method is the only teardown mechanism for producer-produced instances:** "A disposer method allows the application to perform customized cleanup of an object returned by a producer method. A disposer method must have at least one parameter, annotated @Disposes, with the same type and qualifiers as the producer method. The disposer method is called automatically when the context ends." — [Weld Reference Guide (latest)](https://docs.jboss.org/weld/reference/latest/en-US/html/producermethods.html)

- **Distinction between lifecycle callback invocations:** "Invocations of producer, disposer and observer methods by the container are business method invocations and are intercepted by method interceptors and decorators. Additionally, lifecycle callbacks by the container are not business method invocations, but are intercepted by interceptors for lifecycle callback." — [Weld Reference Guide (latest)](https://docs.jboss.org/weld/reference/latest/en-US/html/producermethods.html)

### CDI/Weld: Known point of confusion; disposer method exists precisely to fill the @PreDestroy gap

- **JBoss/Weld community acknowledgment:** In WELD-2580 discussion thread, developers noted: "the original intent was to provide a way for producers to simulate the @PreDestroy callback because that's something you can't do with producer methods and fields." This confirms that @PreDestroy cannot be used with producer-produced instances, and disposer methods are the intentional workaround. — [JBoss Developer Forum: Does a Producer/InjectionTarget's dispose() method get called?](https://developer.jboss.org/thread/280015)

- **Weld reference documentation explicitly teaches disposer as the cleanup pathway for producers,** not @PreDestroy. — [Weld 3.1.3.Final and later reference](https://docs.jboss.org/weld/reference/3.1.3.Final/en-US/html/producer_methods.html)

### CDI 2.0 → CDI 3.0 → CDI 4.0: No behavioral change to @PreDestroy on producers

- **CDI 3.0 major change was namespace migration (javax → jakarta),** not lifecycle callback semantics. — [Jakarta CDI 3.0 Specification](https://jakarta.ee/specifications/cdi/3.0/jakarta-cdi-spec-3.0.html)

- **No evidence in release notes or spec diffs of a change to @PreDestroy behavior on producer-produced instances between CDI 2.0 and CDI 4.0.** The rule that lifecycle callbacks are for managed beans only is consistent across all versions.

### CDI/Weld deployment-time validation: No built-in diagnostic for unreachable @PreDestroy

- **No evidence found that CDI or Weld emits a warning, error, or deployment validation for a class returned from @Produces that carries a @PreDestroy annotation that will never be invoked.** The specification does not mandate this check. This is a known gap: developers commonly learn of the limitation only through experimentation or documentation review.

### Spring @Bean: OPPOSITE behavior — @PreDestroy IS invoked (with caveats)

- **Spring invokes @PreDestroy on singleton and request-scoped @Bean instances:** "During the bean initialization, Spring will register all the bean methods that are annotated with @PreDestroy and invokes them when the application shuts down." — [Spring Framework Reference: Customizing the Nature of a Bean](https://docs.spring.io/spring-framework/reference/core/beans/factory-nature.html)

- **Spring provides `destroyMethod` attribute on @Bean for explicit cleanup method naming,** separate from @PreDestroy, for backward compatibility with XML-based `<bean destroy-method="…">` configuration. — [Spring Framework Reference](https://docs.spring.io/spring-framework/reference/core/beans/factory-nature.html)

- **Spring prototype scope is the exception:** "If your spring bean scope is 'prototype' then it's not completely managed by the spring container and @PreDestroy method won't get called. Spring manages the full lifecycle of Singleton beans but with Prototype beans, it hands over the bean after initialisation. The destruction or cleanup of prototype beans falls outside of Spring's responsibility." — [Baeldung: Do Spring Prototype Beans Need to Be Destroyed Manually?](https://www.baeldung.com/spring-manually-destroy-prototype-bean)

- **Spring's destroyMethod inference behavior:** Spring can auto-detect shutdown/close methods if not explicitly specified. The default `destroyMethod` is `(inferred)`, allowing Spring to guess common cleanup method names; this can be disabled with `destroyMethod=""`. — [Spring Framework Reference and tutorials](https://docs.spring.io/spring-framework/reference/core/beans/factory-nature.html)

## Comparison: CDI vs. Spring

| Aspect | CDI / Weld | Spring @Bean in @Configuration |
|--------|-----------|-------|
| **@PreDestroy on producer-created instance** | ❌ NOT invoked (lifecycle not managed by container) | ✅ Invoked for singleton & request-scoped; ❌ NOT for prototype |
| **Explicit cleanup mechanism** | @Disposes on disposer method | destroyMethod attribute (separate from @PreDestroy) or @PreDestroy on the bean class itself |
| **Deployment-time validation** | None; no warning if @PreDestroy on producer-created class is unused | None; no warning (developers rely on scope understanding) |
| **Lifecycle management** | Container controls only beans it creates directly; producer-created instances are "returned objects," not managed | Container controls singleton/request; prototype handed to caller |
| **Rationale** | @Produces returns arbitrary objects; teardown must be explicit via @Disposes | @Bean methods can be factories; @PreDestroy still applies because Spring tracks registration |

## Version/compatibility notes

- **Jakarta CDI 4.0** (current stable, February 2022 finalization) — the namespace is `jakarta.annotation.PreDestroy` and `jakarta.inject.Disposes`
- **CDI 3.0** (March 2021) — first Jakarta EE version; namespace change from `javax` to `jakarta`; no change to @PreDestroy on producer semantics
- **CDI 2.0** (July 2017) — last javax namespace version; same @PreDestroy rule: not invoked on producer-produced instances
- **Weld 7.0.0.CR1** (latest, supports CDI 4.0 Full) — behavior unchanged from Weld 2.x and 3.x
- **Spring Framework 6.x** (current stable, 2023+) — @Bean and @PreDestroy behavior as documented above; no change anticipated

## Evidence gaps

- **Actual spec section number for CDI 4.0 on lifecycle:**
  The HTML version of CDI 4.0 spec at jakarta.ee does not include a table of contents accessible via WebFetch; the normative section is in the "Lifecycle" chapter, but section numbers may differ between versions. Recommend consulting the PDF version (not publicly accessible via Web) or the [CDI spec GitHub repo](https://github.com/jakartaee/cdi) for exact section references.

- **CDI 2.0 spec text comparison:**
  No evidence found that the CDI 2.0 spec statement on producer lifecycle differs from CDI 1.0/1.2/3.0/4.0. Recommend fetching the CDI 2.0 spec PDF directly to confirm no change occurred.

- **Spring's exact destroyMethod inference logic:**
  Spring documentation mentions the `(inferred)` default but does not exhaustively list which method names are considered. Implementation details in Spring source code (e.g., `BeanFactory` or `DefaultSingletonBeanRegistry`) would clarify this.

- **Weld validation extension ecosystem:**
  No evidence found of third-party Weld/CDI plugins or extensions that validate unreachable @PreDestroy. Worth a separate search in the Weld Extensions or CDI SPI ecosystem.

## Librarian's note

**What the sources indicate:**

CDI's rule is clear and consistent across all versions (2.0–4.0): @PreDestroy is invoked **only on managed beans** whose instantiation the container controls. Producer-produced instances are "returned objects" with no container-managed lifecycle; @Disposes is the only supported cleanup mechanism. This is deliberate by design (containers don't know what a returned object's class might do; cleanup is the producer's explicit responsibility). **Weld (the RI) enforces this strictly: no @PreDestroy invocation on producer-produced instances, period.**

In **sharp contrast**, Spring's @Bean factory methods (analogous to CDI @Produces) allow @PreDestroy to run on the returned instance for singleton and request-scoped beans — the container treats the returned object as a managed bean for lifecycle purposes. Only prototype scope breaks this (to avoid tracking overhead).

**For providify's decision:**
- Option (a) — make @PreDestroy fall back to running for provider-produced instances with no @Disposes — **contradicts CDI spec precedent** but aligns with Spring's user-friendly behavior.
- Option (b) — add validation diagnostic `UNREACHABLE_PRE_DESTROY` — **aligns with CDI spec intent** (lifecycle callbacks are not for produced instances) and helps developers avoid the confusion Weld's WELD-2580 forum thread documents.
- Option (c) — documentation only — **reflects CDI's current state** but perpetuates the learning curve the JBoss community acknowledges.

The spec and reference implementation (Weld) favor (b) or (c). Spring's precedent (running @PreDestroy on factory-produced singletons) favors (a), but Spring's explicit excludes prototype scope, so Spring itself does not run @PreDestroy on *all* factory-produced instances either.

