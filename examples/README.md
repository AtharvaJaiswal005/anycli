# Examples

Runnable examples for the anycli public API. Each script drives a **real
agent CLI on your own machine, on your own auth** — running one spends
tokens from your own plan. Prompts here are deliberately tiny and capped
at `max_turns=1` where possible; keep them that way.

Before running anything, make sure the Claude Code agent CLI is
authenticated on this machine (log in with the CLI itself). anycli never
manages credentials. If `ANTHROPIC_API_KEY` is set in your environment it
silently overrides subscription auth and bills per token at API rates.
anycli warns about this at startup; take the warning seriously.

## Index

| Example | What it shows |
| --- | --- |
| [`basic.py`](basic.py) | One-shot `Bridge.run()`; every field of the final `Result`, including the `session_id` / `cwd` pair, token usage, and cost. |
| [`streaming.py`](streaming.py) | `stream=True`: rendering `TextDelta`, `ToolUse`, and `ToolResult` chunks distinctly as they arrive, and handling `PlanLimitReached`. |
| [`concurrent_runs.py`](concurrent_runs.py) | Several prompts through one `Bridge`; the concurrency semaphore caps live subprocesses while `health()` reports active/queued runs. |
| [`fastapi_app/`](fastapi_app/) | A minimal FastAPI service exposing `POST /run` and `POST /stream` (SSE), mapping `PlanLimitReached` to HTTP 429 with `resets_at`. |

## How to run

From the repository root:

```bash
uv sync
uv run python examples/basic.py
uv run python examples/streaming.py
uv run python examples/concurrent_runs.py
```

The scripts run against the current directory by default; pass a path as
the first argument to use a different working directory:

```bash
uv run python examples/basic.py /path/to/some/project
```

The FastAPI example has its own [README](fastapi_app/README.md); it
needs two extra packages that are intentionally not dependencies of
anycli.

## A note on throughput

Throughput is capped by your own plan, and anycli cannot raise that
ceiling. When the plan limit is hit, runs fail with a typed
`PlanLimitReached` carrying the reset time; the examples show how to
surface it instead of retrying into a wall.
