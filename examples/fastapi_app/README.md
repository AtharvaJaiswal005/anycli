# FastAPI example

A minimal HTTP service over one shared anycli `Bridge`:

- `POST /run` — one-shot run, JSON result.
- `POST /stream` — streaming run as Server-Sent Events; each event's
  data is one typed chunk serialized as a JSON line
  (`text_delta`, `tool_use`, `tool_result`, `result`).
- `GET /health` — concurrency state (`active_runs`, `max_concurrency`,
  `queued`).

On `/run`, `PlanLimitReached` becomes HTTP 429 with `resets_at` in the
body: the plan wall surfaces as an explicit, typed rejection instead
of a mystery 500. On `/stream` the HTTP status is already sent when a
run fails, so errors arrive as a terminal SSE `error` event.

## Honest constraints

- Every request drives a real agent CLI on this machine, on the
  credentials of whoever is logged in here, and spends tokens from that
  plan. This example must serve **one user: yourself**. Exposing one
  subscription to many users is prohibited by the provider's terms;
  do not deploy this pattern multi-tenant.
- Throughput is capped by the plan, not by the service. The bridge
  queues past `max_concurrency` and rejects with 429 at the plan wall;
  it cannot raise the ceiling.

## Setup and run

FastAPI and uvicorn are intentionally **not** dependencies of anycli;
install them just for this example:

```bash
pip install fastapi uvicorn
```

Then, from this directory:

```bash
uvicorn app:app --reload
```

By default runs execute in the server process's current directory; set
`ANYCLI_EXAMPLE_CWD` or pass `"cwd"` in the request body to change that.

## Try it

```bash
curl -X POST http://127.0.0.1:8000/run \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Reply with exactly one short sentence."}'

curl -N -X POST http://127.0.0.1:8000/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "List the files here, then say done.", "max_turns": 3}'

curl http://127.0.0.1:8000/health
```

Keep prompts small: requests default to `max_turns=1`. Prompts that
need a tool round (like the `/stream` one above) must raise `max_turns`;
one turn is not enough for tool use plus the final reply.
