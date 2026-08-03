# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Before 1.0.0, minor versions may contain breaking changes.

## [Unreleased]

## [0.1.0] - 2026-08-03

First release: one-shot runs against a local agent CLI, with the production
middleware that point wrappers skip.

### Added

- `Bridge.run()` — one async surface for one-shot turns against a local
  agent CLI. `stream=False` returns the final `Result`; `stream=True`
  returns an async iterator of typed chunks ending with a `Result`.
- Typed chunk model: `TextDelta`, `ToolUse`, `ToolResult`, `Result` (with
  `Usage` token accounting), plus the `Chunk` union. Public streaming
  surfaces never yield raw strings or SDK objects.
- `BaseAgentAdapter` — the minimal adapter contract (launch, stream raw
  events as `RawEvent` envelopes, report auth state as `AuthStatus`).
- `ClaudeCodeAdapter` — the first adapter, built on the official
  `claude-agent-sdk` package. Maps SDK messages onto the raw-event
  contract; unknown future SDK message types are forwarded as diagnostics
  instead of breaking the stream.
- Concurrency governor: a global semaphore caps live runs (default 3;
  one run = one subprocess), and a hard per-turn max-lifetime watchdog
  (default 30 minutes) kills hung turns. Generator closure cascades down
  the pipeline so abandoned streams still reap their subprocess.
- Retry policy: transient failures (`TransientAdapterError`, or 429/529
  signals) are retried up to three attempts with exponential backoff and
  jitter. `PlanLimitReached` is never retried. Streaming runs are only
  retried before the first chunk is yielded.
- Typed error hierarchy in `anycli.errors`: `AnycliError`, `AdapterError`,
  `TransientAdapterError`, `CLINotFound`, `AuthError`, and
  `PlanLimitReached` carrying the plan-limit reset time (`resets_at`)
  when the agent CLI reports one.
- `check_env()` doctor checks: warns loudly when `ANTHROPIC_API_KEY` is
  set (it silently overrides subscription auth and bills per token at API
  rates), and reports headless-token and credentials-directory state.
  `Bridge` runs these checks at construction by default.
- `Bridge.health()` — current concurrency state (active runs, cap, queued
  waiters).
- Test suite: middleware and bridge tested against a deterministic
  `FakeAdapter` with no subprocesses or auth; concurrency behavior covered
  by real concurrency tests; a small opt-in integration smoke test
  (`-m integration`) exercises a real Claude Code run locally.

[Unreleased]: https://github.com/AtharvaJaiswal005/anycli/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AtharvaJaiswal005/anycli/releases/tag/v0.1.0
