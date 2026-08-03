"""Streaming normalizer: RawEvents in, typed chunks out, usage deduped."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from anycli.errors import AdapterError
from anycli.middleware.streaming import normalize
from anycli.types import Chunk, RawEvent, Result, TextDelta, ToolResult, ToolUse, Usage


async def replay(events: Sequence[RawEvent]) -> AsyncIterator[RawEvent]:
    for event in events:
        yield event


async def collect(events: Sequence[RawEvent]) -> list[Chunk]:
    return [chunk async for chunk in normalize(replay(events))]


def usage_dict(input_tokens: int = 1, output_tokens: int = 2) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def result_event(**overrides: object) -> RawEvent:
    data: dict[str, object] = {
        "text": "final",
        "session_id": "sess-9",
        "is_error": False,
        "num_turns": 2,
        "usage": None,
        "total_cost_usd": None,
        "raw": None,
    }
    data.update(overrides)
    return RawEvent(kind="result", data=data)


async def test_scripted_events_become_correct_chunk_sequence() -> None:
    events = [
        RawEvent(kind="init", data={"session_id": "sess-9"}),
        RawEvent(kind="text_delta", data={"text": "th"}),
        RawEvent(kind="text_delta", data={"text": "ink"}),
        RawEvent(
            kind="tool_use",
            data={"tool_name": "Read", "tool_input": {"path": "x"}, "tool_use_id": "tu-1"},
        ),
        RawEvent(
            kind="tool_result",
            data={"tool_use_id": "tu-1", "content": "file body", "is_error": False},
        ),
        result_event(text="", usage=usage_dict(7, 3)),
    ]

    chunks = await collect(events)

    assert [type(c) for c in chunks] == [TextDelta, TextDelta, ToolUse, ToolResult, Result]
    assert chunks[0] == TextDelta(text="th")
    tool_use = chunks[2]
    assert isinstance(tool_use, ToolUse)
    assert tool_use.tool_name == "Read"
    assert tool_use.tool_input == {"path": "x"}
    assert tool_use.tool_use_id == "tu-1"
    tool_result = chunks[3]
    assert isinstance(tool_result, ToolResult)
    assert tool_result.tool_use_id == "tu-1"
    assert tool_result.content == "file body"
    assert tool_result.is_error is False
    result = chunks[-1]
    assert isinstance(result, Result)
    assert result.text == "think"  # falls back to joined deltas
    assert result.session_id == "sess-9"
    assert result.num_turns == 2
    assert result.usage == Usage(
        input_tokens=7, output_tokens=3, cache_read_tokens=0, cache_creation_tokens=0
    )


async def test_duplicate_usage_for_same_message_id_counted_once() -> None:
    events = [
        RawEvent(kind="usage", data={"message_id": "m1", "usage": usage_dict(10, 5)}),
        RawEvent(kind="usage", data={"message_id": "m1", "usage": usage_dict(10, 5)}),
        RawEvent(kind="usage", data={"message_id": "m2", "usage": usage_dict(3, 1)}),
        result_event(usage=None),
    ]

    chunks = await collect(events)

    result = chunks[-1]
    assert isinstance(result, Result)
    assert result.usage == Usage(
        input_tokens=13, output_tokens=6, cache_read_tokens=0, cache_creation_tokens=0
    )


async def test_result_usage_takes_precedence_over_interim_usage() -> None:
    events = [
        RawEvent(kind="usage", data={"message_id": "m1", "usage": usage_dict(100, 100)}),
        result_event(usage=usage_dict(7, 3)),
    ]

    chunks = await collect(events)

    result = chunks[-1]
    assert isinstance(result, Result)
    assert result.usage is not None
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 3


async def test_anonymous_usage_events_are_all_summed() -> None:
    events = [
        RawEvent(kind="usage", data={"message_id": None, "usage": usage_dict(1, 1)}),
        RawEvent(kind="usage", data={"message_id": None, "usage": usage_dict(1, 1)}),
        result_event(usage=None),
    ]

    chunks = await collect(events)

    result = chunks[-1]
    assert isinstance(result, Result)
    assert result.usage == Usage(
        input_tokens=2, output_tokens=2, cache_read_tokens=0, cache_creation_tokens=0
    )


async def test_unknown_kinds_produce_no_chunks() -> None:
    events = [
        RawEvent(kind="rate_limit", data={"status": "allowed"}),
        RawEvent(kind="totally_custom", data=object()),
        result_event(),
    ]

    chunks = await collect(events)

    assert len(chunks) == 1
    assert isinstance(chunks[0], Result)


async def test_session_id_falls_back_to_init_event() -> None:
    events = [
        RawEvent(kind="init", data={"session_id": "sess-init"}),
        result_event(session_id=""),
    ]

    chunks = await collect(events)

    result = chunks[-1]
    assert isinstance(result, Result)
    assert result.session_id == "sess-init"


async def test_stream_without_result_raises_adapter_error() -> None:
    events = [RawEvent(kind="text_delta", data={"text": "hi"})]

    with pytest.raises(AdapterError, match="without a terminal"):
        await collect(events)


async def test_malformed_event_raises_adapter_error() -> None:
    events = [RawEvent(kind="text_delta", data={}), result_event()]

    with pytest.raises(AdapterError, match="malformed"):
        await collect(events)


async def test_normalizer_closes_source_iterator() -> None:
    closed = False

    async def source() -> AsyncIterator[RawEvent]:
        nonlocal closed
        try:
            yield result_event()
            yield RawEvent(kind="text_delta", data={"text": "never reached"})
        finally:
            closed = True

    chunks = [chunk async for chunk in normalize(source())]

    assert isinstance(chunks[-1], Result)
    assert closed, "normalize did not close its source iterator"
