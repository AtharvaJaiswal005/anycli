"""Minimal FastAPI service over one shared anycli Bridge.

Endpoints:

- ``POST /run``    — one-shot run, returns the final result as JSON.
- ``POST /stream`` — streaming run, Server-Sent Events; each event's data
  is one typed chunk serialized as a JSON line.
- ``GET /health``  — the bridge's concurrency state.

On ``/run``, ``PlanLimitReached`` is mapped to HTTP 429 with the reset
time in the body — a typed, actionable rejection instead of a generic
500. On ``/stream`` the HTTP status is already sent when a run fails, so
errors are reported as a terminal SSE ``error`` event instead. Throughput
is capped by the plan of whoever is logged in to the agent CLI on this
machine; this service must serve only that one user (one subscription
serving many users is prohibited by the provider's terms).

Run (see README.md for setup):
    uvicorn app:app --reload
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from anycli import (
    AnycliError,
    AuthError,
    Bridge,
    ClaudeCodeAdapter,
    PlanLimitReached,
    Result,
    TextDelta,
    ToolResult,
    ToolUse,
)

app = FastAPI(title="anycli example service")

# One Bridge for the whole process: its semaphore caps live agent CLI
# subprocesses across all requests.
bridge = Bridge(
    ClaudeCodeAdapter(),
    max_concurrency=3,
    default_cwd=os.environ.get("ANYCLI_EXAMPLE_CWD", "."),
)


class RunRequest(BaseModel):
    prompt: str
    cwd: str | None = None
    max_turns: int | None = 1  # keep example runs small by default


def _plan_limit_response(error: PlanLimitReached) -> JSONResponse:
    """Backpressure is explicit: 429 with the reset time, not a 500."""
    return JSONResponse(
        status_code=429,
        content={
            "error": "plan limit reached",
            "detail": str(error),
            "resets_at": error.resets_at.isoformat() if error.resets_at else None,
        },
    )


@app.post("/run")
async def run(request: RunRequest) -> JSONResponse:
    try:
        result = await bridge.run(
            request.prompt,
            cwd=request.cwd,
            max_turns=request.max_turns,
        )
    except PlanLimitReached as error:
        return _plan_limit_response(error)
    except AuthError as error:
        return JSONResponse(
            status_code=503,
            content={"error": "agent CLI not authenticated", "detail": str(error)},
        )
    return JSONResponse(
        content={
            "text": result.text,
            # Persist these two together: one without the other cannot
            # resume the session.
            "session_id": result.session_id,
            "cwd": result.cwd,
            "is_error": result.is_error,
            "num_turns": result.num_turns,
            "total_cost_usd": result.total_cost_usd,
            "usage": asdict(result.usage) if result.usage else None,
        }
    )


def _chunk_to_event(chunk: TextDelta | ToolUse | ToolResult | Result) -> dict[str, Any]:
    """Serialize a typed chunk to a JSON-safe dict with a ``type`` tag."""
    if isinstance(chunk, TextDelta):
        return {"type": "text_delta", "text": chunk.text}
    if isinstance(chunk, ToolUse):
        return {
            "type": "tool_use",
            "tool_name": chunk.tool_name,
            "tool_input": chunk.tool_input,
            "tool_use_id": chunk.tool_use_id,
        }
    if isinstance(chunk, ToolResult):
        return {
            "type": "tool_result",
            "tool_use_id": chunk.tool_use_id,
            "is_error": chunk.is_error,
        }
    return {
        "type": "result",
        "text": chunk.text,
        "session_id": chunk.session_id,
        "cwd": chunk.cwd,
        "is_error": chunk.is_error,
        "num_turns": chunk.num_turns,
        "total_cost_usd": chunk.total_cost_usd,
        "usage": asdict(chunk.usage) if chunk.usage else None,
    }


@app.post("/stream")
async def stream(request: RunRequest) -> StreamingResponse:
    # With stream=True, run() hands back the iterator without executing
    # anything yet — every failure (PlanLimitReached, AuthError, adapter
    # faults) surfaces while iterating, after the HTTP status line has
    # already been sent. So errors are reported in-band, as a terminal
    # SSE "error" event, instead of as an HTTP status.
    chunks = await bridge.run(
        request.prompt,
        cwd=request.cwd,
        max_turns=request.max_turns,
        stream=True,
    )

    async def events():
        try:
            async for chunk in chunks:
                yield f"data: {json.dumps(_chunk_to_event(chunk))}\n\n"
        except PlanLimitReached as error:
            payload = {
                "type": "error",
                "error": "plan limit reached",
                "detail": str(error),
                "resets_at": error.resets_at.isoformat() if error.resets_at else None,
            }
            yield f"data: {json.dumps(payload)}\n\n"
        except AnycliError as error:
            payload = {
                "type": "error",
                "error": type(error).__name__,
                "detail": str(error),
            }
            yield f"data: {json.dumps(payload)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/health")
async def health() -> dict[str, int]:
    return bridge.health()
