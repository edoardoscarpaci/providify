# Providify — Agent Usage Guide

> **Audience:** AI coding agents (and the humans steering them) wiring **Providify**
> into a project. This is the *directive* layer: short, opinionated rules for using
> the library **as intended**. For exhaustive API detail, read the deeper docs it
> links to.

Providify is a **zero-dependency, Python 3.12+ dependency-injection container**
inspired by **Jakarta CDI first, Spring second**. It does constructor injection via
type hints, manages component lifecycles across four scopes, and works in both sync
and async code.

---

## Read in this order

1. **[usage-rules.md](usage-rules.md)** — the non-negotiable rules. Start here. If
   you only read one file, read this one.
2. **[quickstart.md](quickstart.md)** — minimal, copy-paste-correct setup →
   resolve, for sync and async.
3. **[choosing-decorators.md](choosing-decorators.md)** — *the* decision: annotate a
   class vs. write a `@Provider` vs. group in `@Configuration`. This is where agents
   most often go wrong.
4. **[injection-cheatsheet.md](injection-cheatsheet.md)** — every injection
   annotation (`Inject` / `Live` / `Lazy` / `Instance` / `Event`) with the **only
   correct option-bearing form**, plus scopes and lifecycle.
5. **[anti-patterns.md](anti-patterns.md)** — what *not* to do, and the error each
   mistake produces.

## Deeper references (not agent-specific)

- **[`../../SKILL.md`](../../SKILL.md)** — complete API reference, every method,
  every error type, architecture map.
- **[`../../PROVIDERS.md`](../../PROVIDERS.md)** — the full producer/`@Configuration`
  guide with the Jakarta-vs-Spring comparison table.
- **[`../../README.md`](../../README.md)** — narrative documentation with extended
  examples.

---

## The one-paragraph mental model

> **Beans are classes.** If you own the class, put a scope decorator on it
> (`@Singleton`, `@Component`, `@RequestScoped`, `@SessionScoped`) and let the
> container build and inject it. Use `@Provider` **only** for types you *can't*
> annotate (third-party, stdlib, runtime-chosen implementations). `@Configuration`
> is just an optional namespace for grouping related `@Provider` methods — **not**
> the Spring-style place where "all beans live." When you must inject a
> shorter-lived dependency into a longer-lived one (e.g. `@RequestScoped` into
> `@Singleton`), wrap it in `Live[T]`.

Everything else is detail. Internalize that paragraph and the rest follows.
