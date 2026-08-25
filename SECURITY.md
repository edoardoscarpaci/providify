# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 2.x     | ✅ |
| < 2.0   | ❌ |

Only the latest `2.x` release receives security fixes. Interim `1.0.x`–`1.1.1`
releases were unannounced pre-stable versions and are not supported.

## Reporting a vulnerability

**Primary channel: GitHub private vulnerability reporting.** Go to this
repository's **Security** tab → **Report a vulnerability**. This creates a
private advisory visible only to the maintainer and you, and is the
preferred way to report.

**Fallback:** if you cannot use GitHub's private reporting flow, email
edoardo.scarpaci@gmail.com.

**Do not open a public GitHub issue for a security report.**

## Response timeframe

- Acknowledgement of a report: within **7 days**.
- Initial assessment (severity, whether it is accepted as a vulnerability):
  within **14 days**.
- Coordinated disclosure once a fix has shipped, or after **90 days**,
  whichever comes first.

## Scope

providify is a dependency-injection library — it has no network or I/O
surface of its own. In scope: issues in binding resolution, type/annotation
scanning, or configuration/module loading that could cause unintended code
execution or leak configuration values. Out of scope: vulnerabilities in
application code that merely *uses* providify, or in providify's optional
`yaml` extra's upstream dependency (report those to PyYAML directly).
