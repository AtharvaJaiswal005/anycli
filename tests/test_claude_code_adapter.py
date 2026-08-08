"""ClaudeCodeAdapter against a stubbed SDK ``query()``: no subprocess, no auth.

These tests pair the real adapter with the real middleware (guard +
normalizer), so a drift between the adapter's emitted RawEvent kinds and the
contract in ``anycli.adapters.base`` fails here instead of only in the
deselected integration test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import anycli.bridge
from anycli import (
    Bridge,
    ClaudeCodeAdapter,
    Result,
    TextDelta,
    ToolResult,
    ToolUse,
    TransientAdapterError,
    Usage,
)
from anycli.adapters import claude_code

SDK_USAGE = {
    "input_tokens": 7,
    "output_tokens": 3,
    "cache_read_input_tokens": 11,
    "cache_creation_input_tokens": 5,
}


def make_result_message(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "sdk-sess",
        "total_cost_usd": 0.01,
        "usage": dict(SDK_USAGE),
        "result": "pong",
    }
    fields.update(overrides)
    return ResultMessage(**fields)


def sdk_script() -> list[Any]:
    """A realistic successful SDK stream: init -> deltas -> tools -> result."""
    return [
        SystemMessage(subtype="init", data={"session_id": "sdk-sess"}),
        StreamEvent(
            uuid="u1",
            session_id="sdk-sess",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "po"}},
        ),
        StreamEvent(
            uuid="u2",
            session_id="sdk-sess",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ng"}},
        ),
        AssistantMessage(
            content=[ToolUseBlock(id="tu-1", name="Read", input={"path": "x"})],
            model="m",
            usage=dict(SDK_USAGE),
            message_id="msg-1",
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="tu-1", content="body", is_error=False)]),
        make_result_message(),
    ]


def stub_query(
    monkeypatch: pytest.MonkeyPatch,
    scripts: Sequence[Sequence[Any]],
) -> Callable[[], int]:
    """Replace the SDK query() with a playback of ``scripts``, one per call."""
    calls = {"count": 0}

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        index = min(calls["count"], len(scripts) - 1)
        calls["count"] += 1
        script = scripts[index]

        async def replay() -> AsyncIterator[Any]:
            for message in script:
                yield message

        return replay()

    monkeypatch.setattr(claude_code, "query", fake_query)
    return lambda: calls["count"]


def make_bridge(**kwargs: object) -> Bridge:
    kwargs.setdefault("default_cwd", "/tmp/fake-cwd")
    kwargs.setdefault("warn_on_auth_conflict", False)
    return Bridge(ClaudeCodeAdapter(), **kwargs)  # type: ignore[arg-type]


async def test_buffered_run_produces_contract_result(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_query(monkeypatch, [sdk_script()])
    bridge = make_bridge()

    result = await bridge.run("ping")

    assert isinstance(result, Result)
    assert result.text == "pong"
    assert result.session_id == "sdk-sess"
    assert result.cwd == str(Path("/tmp/fake-cwd").resolve())
    assert result.is_error is False
    assert result.num_turns == 1
    assert result.total_cost_usd == 0.01
    # SDK cache keys must be mapped onto the contract's cache keys.
    assert result.usage == Usage(
        input_tokens=7, output_tokens=3, cache_read_tokens=11, cache_creation_tokens=5
    )


async def test_streaming_run_yields_typed_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_query(monkeypatch, [sdk_script()])
    bridge = make_bridge()

    stream = await bridge.run("ping", stream=True)
    chunks = [chunk async for chunk in stream]

    deltas = [c for c in chunks if isinstance(c, TextDelta)]
    assert "".join(d.text for d in deltas) == "pong"
    tool_uses = [c for c in chunks if isinstance(c, ToolUse)]
    assert tool_uses == [ToolUse(tool_name="Read", tool_input={"path": "x"}, tool_use_id="tu-1")]
    tool_results = [c for c in chunks if isinstance(c, ToolResult)]
    assert tool_results == [ToolResult(tool_use_id="tu-1", content="body", is_error=False)]
    assert isinstance(chunks[-1], Result)
    assert chunks[-1].session_id == "sdk-sess"


async def test_result_usage_falls_back_to_interim_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    script = sdk_script()
    script[-1] = make_result_message(usage=None)
    stub_query(monkeypatch, [script])
    bridge = make_bridge()

    result = await bridge.run("ping")

    assert isinstance(result, Result)
    # Deduplicated interim usage (from the assistant message) fills the gap,
    # with SDK cache keys mapped onto the contract's cache keys.
    assert result.usage == Usage(
        input_tokens=7, output_tokens=3, cache_read_tokens=11, cache_creation_tokens=5
    )


async def test_transient_api_error_status_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anycli.bridge, "_BACKOFF_BASE_SECONDS", 0.001)
    overloaded = [make_result_message(is_error=True, api_error_status=529, result="API overloaded")]
    calls = stub_query(monkeypatch, [overloaded, sdk_script()])
    bridge = make_bridge()

    result = await bridge.run("ping")

    assert calls() == 2, "529 result was not retried"
    assert isinstance(result, Result)
    assert result.text == "pong"


async def test_transient_api_error_status_gives_up_after_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(anycli.bridge, "_BACKOFF_BASE_SECONDS", 0.001)
    overloaded = [make_result_message(is_error=True, api_error_status=429, result="throttled")]
    calls = stub_query(monkeypatch, [overloaded])
    bridge = make_bridge()

    with pytest.raises(TransientAdapterError):
        await bridge.run("ping")

    assert calls() == 3


async def test_non_transient_error_result_is_returned_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errored = [
        make_result_message(
            is_error=True, result="execution failed", subtype="error_during_execution"
        )
    ]
    calls = stub_query(monkeypatch, [errored])
    bridge = make_bridge()

    result = await bridge.run("ping")

    assert calls() == 1
    assert isinstance(result, Result)
    assert result.is_error is True
