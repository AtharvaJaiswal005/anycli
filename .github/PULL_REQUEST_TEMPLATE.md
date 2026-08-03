<!-- Keep PRs focused: one change per PR. -->

## What does this PR do?

<!-- A short summary of the change and, if it isn't obvious, why. -->

## Checklist

- [ ] `uv run pytest` passes (unit tests, no agent CLI or auth needed)
- [ ] `uv run ruff check .` and `uv run ruff format .` are clean
- [ ] `uv run pyright` reports no errors
- [ ] New behavior is covered by tests (concurrency claims by real concurrency tests)
- [ ] No engineering invariant from [CONTRIBUTING.md](https://github.com/AtharvaJaiswal005/anycli/blob/main/CONTRIBUTING.md) is violated
  (bounded subprocesses, subprocesses always reaped, typed chunks and typed errors
  only, auth never centralized, no retry of hard plan-limit rejections)
- [ ] Public API changes (`anycli/__init__.py`) were discussed in an issue first
- [ ] `CHANGELOG.md` (`Unreleased` section) updated for user-visible changes
- [ ] No credentials, tokens, or agent CLI configuration files are included

<!-- If you ran the integration tests locally (`uv run pytest -m integration`,
     real Claude Code on your own subscription), mention it — they are skipped
     in CI. -->
