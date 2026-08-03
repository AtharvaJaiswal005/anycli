"""Streaming normalizer: adapter RawEvents in, public typed chunks out.

Adapter-agnostic. Interprets only the RawEvent ``kind`` values documented in
the module docstring of :mod:`anycli.adapters.base`; anything else passes
through silently as adapter diagnostics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from anycli.errors import AdapterError
from anycli.types import Chunk, RawEvent, Result, TextDelta, ToolResult, ToolUse, Usage

_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def _usage_from_dict(data: dict[str, Any] | None) -> Usage | None:
    if not data:
        return None
    return Usage(
        input_tokens=int(data.get("input_tokens", 0)),
        output_tokens=int(data.get("output_tokens", 0)),
        cache_read_tokens=int(data.get("cache_read_tokens", 0)),
        cache_creation_tokens=int(data.get("cache_creation_tokens", 0)),
    )


def _sum_usages(usages: list[dict[str, Any]]) -> Usage | None:
    if not usages:
        return None
    totals = {key: 0 for key in _USAGE_KEYS}
    for usage in usages:
        for key in _USAGE_KEYS:
            totals[key] += int(usage.get(key, 0))
    return Usage(**totals)


async def normalize(events: AsyncIterator[RawEvent], *, cwd: str = "") -> AsyncIterator[Chunk]:
    """Translate raw adapter events into public typed chunks.

    Yields :class:`TextDelta`, :class:`ToolUse`, and :class:`ToolResult` as
    they arrive, then exactly one terminal :class:`Result`. ``cwd`` is the
    resolved working directory of the run; it is stamped onto the
    :class:`Result` so the ``session_id``/``cwd`` pair travels together.
    Interim ``"usage"`` events are deduplicated by message id (parallel
    tool calls can repeat the same message's usage) and summed as a
    fallback when the terminal event carries no usage of its own. Raises
    :class:`AdapterError` if the stream ends without a ``"result"`` event
    or an event payload is malformed.
    """
    text_parts: list[str] = []
    session_id = ""
    usage_by_message: dict[str, dict[str, Any]] = {}
    anonymous_usages: list[dict[str, Any]] = []
    result_seen = False
    try:
        async for event in events:
            kind = event.kind
            data = event.data
            try:
                if kind == "text_delta":
                    text = data["text"]
                    text_parts.append(text)
                    yield TextDelta(text=text)
                elif kind == "tool_use":
                    yield ToolUse(
                        tool_name=data["tool_name"],
                        tool_input=data["tool_input"],
                        tool_use_id=data["tool_use_id"],
                    )
                elif kind == "tool_result":
                    yield ToolResult(
                        tool_use_id=data["tool_use_id"],
                        content=data.get("content"),
                        is_error=bool(data.get("is_error", False)),
                    )
                elif kind == "usage":
                    message_id = data.get("message_id")
                    usage = data.get("usage") or {}
                    if message_id is None:
                        anonymous_usages.append(usage)
                    else:
                        # First report wins: duplicates for one message id
                        # (parallel tool calls) must not be double-counted.
                        usage_by_message.setdefault(message_id, usage)
                elif kind == "init":
                    session_id = data.get("session_id") or session_id
                elif kind == "result":
                    result_seen = True
                    usage = _usage_from_dict(data.get("usage"))
                    if usage is None:
                        usage = _sum_usages(list(usage_by_message.values()) + anonymous_usages)
                    yield Result(
                        text=data.get("text") or "".join(text_parts),
                        session_id=data.get("session_id") or session_id,
                        cwd=cwd,
                        is_error=bool(data.get("is_error", False)),
                        num_turns=int(data.get("num_turns", 0)),
                        usage=usage,
                        total_cost_usd=data.get("total_cost_usd"),
                        raw=data.get("raw"),
                    )
                    break
                # Unknown kinds are adapter diagnostics; produce no chunk.
            except (KeyError, TypeError, AttributeError) as exc:
                raise AdapterError(f"malformed {kind!r} event from adapter: {exc}") from exc
    finally:
        aclose = getattr(events, "aclose", None)
        if aclose is not None:
            await aclose()
    if not result_seen:
        raise AdapterError("adapter event stream ended without a terminal 'result' event")
