# Security Policy

## Supported Versions

Only the latest `2.x` release line receives security updates.

| Version | Supported |
|---------|-----------|
| 2.x     | ✅        |
| 1.x     | ❌        |
| 0.x     | ❌        |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report privately via one of the following:

- GitHub's ["Report a vulnerability"](https://github.com/lmousom/hyperhttp/security/advisories/new) flow (preferred), or
- Email the maintainer at **latifulmousom@gmail.com** with the subject line
  `[hyperhttp security]`.

Please include:

- A description of the issue and its impact.
- Steps to reproduce, a minimal proof of concept, or a failing test case.
- The version(s) affected.
- Any suggested mitigation.

## Response Expectations

- **Acknowledgement:** within 72 hours.
- **Initial assessment:** within 7 days.
- **Fix + coordinated disclosure:** target 30 days from initial report, faster
  for actively exploited issues.

Fixes are released as a patch version (`2.x.y`) with an advisory on the
[Security Advisories](https://github.com/lmousom/hyperhttp/security/advisories)
page. Credit is given in the advisory unless the reporter requests anonymity.

## Scope

In scope:

- The HTTP/1.1 parser and state machine.
- HTTP/2 framing and stream handling (via `h2`).
- TLS configuration, ALPN negotiation, certificate validation.
- Connection pool, retry handler, circuit breaker.
- Anything in the public `hyperhttp.*` import surface.

Out of scope:

- Vulnerabilities in upstream dependencies — please report those to the
  relevant project.
- Issues only reproducible with `verify=False` or a deliberately weak
  `ssl.SSLContext` supplied by the caller.
- Resource exhaustion that requires the attacker to control both the client
  configuration and the server (e.g. setting `max_redirects=1e9`).
