# Contributing to anycli

Thanks for your interest in contributing. anycli is a small library with a
deliberately small public surface, so most contributions land in the
middleware, the adapters, or the tests. This document covers everything you
need to get a change from clone to merged.

## Development setup

anycli targets Python 3.10+ and is managed with [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/AtharvaJaiswal005/anycli.git
cd anycli
uv sync
```

`uv sync` installs the runtime dependency (`claude-agent-sdk`) and the dev
group (pytest, pytest-asyncio, ruff, pyright).

## Running the tests

There are two tiers of tests. Know which one you are touching.

### Unit tests (default)

```bash
uv run pytest
```

Unit tests exercise the bridge and middleware against a deterministic
`FakeAdapter` (`tests/fake_adapter.py`). They launch no subprocesses and
require no agent CLI and no credentials. All concurrency, retry, streaming,
and error-mapping logic must be fully testable this way; if your middleware
change can only be verified against a real agent CLI, restructure it.

### Integration tests (opt-in, local only)

```bash
uv run pytest -m integration
```

Integration tests launch a real Claude Code run with real auth. They are
deselected by default (see `addopts` in `pyproject.toml`) and are skipped in
CI. Because they run against **your own personal subscription**, they must
stay cheap: small prompts, low `max_turns`, one short turn per test. Do not
add an integration test that burns meaningful plan quota, and never commit
credentials or anything from your agent CLI's config directory to make one
pass.

## Lint and type checks

Both must be clean before a PR is reviewed:

```bash
uv run ruff check . && uv run ruff format .
uv run pyright
```

## Code style

- **Async-first:** the public API is async; there is no sync wrapper in v1.
- **Full type hints** on all code, public and private.
- **Typed errors only:** everything anycli raises publicly comes from the
  hierarchy in `anycli/errors.py`. Never raise bare `Exception`.
- **Small public surface:** only names exported from `anycli/__init__.py` are
  public; everything else is private and may change without notice. Additions
  to the public surface need discussion in an issue first.
- **Honest docstrings:** state constraints plainly (plan-capped throughput,
  bring-your-own auth). No marketing language in code or docs.

## Engineering invariants

These are the rules the library exists to enforce. A PR that violates one
will not be merged, however clean the code.

1. **Auth is never centralized.** Each user runs their own agent CLI on their
   own credentials. No code path may transport, store, or proxy a user's
   OAuth token off their machine. A design that puts one subscription token
   on a server serving multiple users is prohibited, full stop.
2. **One run = one subprocess, and the count is bounded.** A global
   semaphore caps live subprocesses (default 3, configurable). Naive
   concurrency leaks processes.
3. **Never run two turns of one session id concurrently.** Per-session
   serialization is mandatory; parallel resume of one id corrupts transcripts
   and leaks subprocesses.
4. **`session_id` and `cwd` travel together, always.** Sessions are stored on
   disk keyed by working directory; resuming from the wrong cwd silently
   creates a fresh session. Persist and pass them as a pair.
5. **Subprocesses are always reaped.** Async context managers and cascading
   generator closure everywhere, plus a hard max-lifetime kill for hung
   turns. A test that asserts "no leaked subprocess" is not optional
   ceremony; add one for any change to the run pipeline.
6. **Retry only transient errors** (429 / 529) with exponential backoff and
   jitter. Never retry a hard plan-limit rejection; raise
   `PlanLimitReached` carrying the reset time instead.
7. **Yield typed chunks** (`TextDelta`, `ToolUse`, `ToolResult`, `Result`)
   from every public streaming surface — never raw strings, never
   SDK-specific objects.
8. **Guard the API-key footgun.** `ANTHROPIC_API_KEY` in the environment
   silently overrides subscription auth and bills per token. `check_env()`
   must keep detecting it and warning loudly.
9. **Never hard-code provider quotas or billing rules.** They change. Read
   limits from runtime signals (rate-limit events), not constants.

### Where code goes

If a piece of logic would be identical for a second agent CLI, it belongs in
`anycli/middleware/`. If it only makes sense for one agent CLI, it belongs in
that adapter under `anycli/adapters/`. Middleware never imports an SDK and
never touches a subprocess; it talks only to `BaseAgentAdapter`. When in
doubt, push logic up into middleware and keep the adapter dumb. Do not add
speculative hooks or config knobs for agent CLIs that are not integrated yet.

## Pull request expectations

- Keep PRs focused: one change per PR.
- Tests, `ruff check`, `ruff format`, and `pyright` must all pass.
- New behavior needs tests; concurrency claims need real concurrency tests
  (leaked-subprocess assertions, semaphore saturation, etc.).
- Public API changes (anything in `anycli/__init__.py`) need a prior issue
  discussing the design.
- Update the `Unreleased` section of `CHANGELOG.md` for user-visible changes.
- Commit messages: short imperative subject line; add a body only when the
  "why" is not obvious from the diff.
- Never commit credentials, tokens, `.credentials.json`, or anything from
  your agent CLI's configuration directory.

## Reporting bugs and requesting features

Use the issue templates. For suspected security problems, do **not** open a
public issue — see [SECURITY.md](SECURITY.md).
