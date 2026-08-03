# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

Pre-1.0, only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not** report security vulnerabilities through public GitHub
issues.

Instead, report them privately via GitHub's security advisory flow:
[Report a vulnerability](https://github.com/AtharvaJaiswal005/anycli/security/advisories/new)
(the "Security" tab of the repository, then "Report a vulnerability").

Include what you can of the following:

- A description of the issue and its impact.
- Steps to reproduce, or a proof-of-concept.
- Affected version(s) and platform.

You will get an acknowledgment as soon as possible, normally within a few
days. Please allow time for a fix and release before any public disclosure.

## Scope

anycli is a local library that drives agent CLIs running on the user's own
machine. Reports we consider in scope include:

- Subprocess handling flaws: command or argument injection into the agent
  CLI launch, leaked or unreaped subprocesses that a caller cannot bound.
- Concurrency flaws that break the documented safety guarantees (e.g. the
  subprocess cap being bypassable).
- Any code path that logs, persists, or transmits credential material.

## What this library does not handle

anycli never manages credentials by design. Authentication belongs entirely
to the agent CLI on the user's machine: anycli does not read, store,
transport, or proxy OAuth tokens or API keys — its only interaction with
auth is *detecting* misconfiguration locally (for example, warning when
`ANTHROPIC_API_KEY` is set and would override subscription auth). Any
observed behavior to the contrary is a bug; please report it as a
vulnerability.

Vulnerabilities in the agent CLIs themselves (Claude Code and its SDK)
should be reported to their vendors, not here.
