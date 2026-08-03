"""Claude Code adapter, built on the official ``claude-agent-sdk`` package.

The SDK bundles the ``claude`` CLI and talks to it over stdio, so this adapter
never spawns a subprocess itself; it drives the SDK's ``query()`` async
iterator and maps every SDK message onto the :class:`~anycli.types.RawEvent`
kinds contract documented in :mod:`anycli.adapters.base`:

- ``"init"`` — from the SDK ``SystemMessage`` with subtype ``init``;
  carries ``{"session_id": str}``.
- ``"text_delta"`` — from ``StreamEvent`` partial-message text deltas
  (``include_partial_messages`` is always enabled); subagent text (a
  non-null ``parent_tool_use_id``) is not surfaced as top-level reply text.
- ``"tool_use"`` — from ``ToolUseBlock`` content in an ``AssistantMessage``.
- ``"tool_result"`` — from ``ToolResultBlock`` content echoed back in a
  ``UserMessage``.
- ``"usage"`` — from per-``AssistantMessage`` usage, keyed by message id;
  SDK cache-token keys are mapped onto the contract's
  ``cache_read_tokens`` / ``cache_creation_tokens``.
- ``"result"`` — from the terminal ``ResultMessage``, reshaped to the
  contract dict; ``data["session_id"]`` is the id to persist together
  with ``cwd``.

Everything else (non-text stream events, non-init system messages, rate-limit
transitions, unknown future SDK message types) is forwarded with an
adapter-specific kind carrying the raw SDK object, which the normalizer
ignores as diagnostics — so newer SDK versions degrade gracefully instead of
breaking the stream.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from anycli.adapters.base import BaseAgentAdapter
from anycli.errors import (
    AdapterError,
    AuthError,
    CLINotFound,
    PlanLimitReached,
    TransientAdapterError,
)
from anycli.types import AuthStatus, RawEvent

# Substrings (lowercased) that mark a failure as an auth problem. These are
# runtime signals from the CLI's own error text, not provider rules.
_AUTH_MARKERS = (
    "invalid api key",
    "please run /login",
    "not logged in",
    "authentication_error",
    "oauth token has expired",
    "oauth token revoked",
    "credential",
)

# Appended to every AuthError so the failure is actionable without reading
# docs. Factual: describes Claude Code's own auth mechanisms and precedence.
_AUTH_REMEDIATION = (
    " To fix: log in with Claude Code on this machine (run `claude` and use "
    "/login), or set CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` for "
    "headless use. If ANTHROPIC_API_KEY is set, note it takes precedence "
    "over subscription auth and bills per token — unset it to use a "
    "subscription."
)

# Appended to CLINotFound so the failure is actionable.
_CLI_NOT_FOUND_REMEDIATION = (
    " To fix: reinstall the bundled agent CLI with "
    "`pip install --force-reinstall claude-agent-sdk`, or install Claude "
    "Code directly (`npm install -g @anthropic-ai/claude-code`) and make "
    "sure `claude` is on PATH."
)

# Substrings (lowercased) that mark a result as a hard plan-limit rejection.
# The reset time is parsed from the message itself; no quota is hard-coded.
_PLAN_LIMIT_MARKERS = (
    "usage limit reached",
    "session limit reached",
    "hit your usage limit",
)

# HTTP statuses on a failing terminal result that are safe to retry:
# throttling (429) and overload (529). Everything else is a hard failure.
_TRANSIENT_STATUSES = (429, 529)

# Trailing epoch timestamp the CLI appends to limit messages,
# e.g. "Claude AI usage limit reached|1735689600".
_EPOCH_SUFFIX = re.compile(r"\|(\d{9,13})\s*$")

# Grace given to the SDK's own stream closure (which terminates the CLI
# subprocess, escalating to kill) before it is left to finish in the
# background so a stuck teardown cannot pin the caller.
_CLOSE_GRACE_SECONDS = 10.0

# Grace given to an in-flight SDK read to settle on its own before it is
# cancelled during shutdown.
_SETTLE_GRACE_SECONDS = 2.0

# Strong references to in-flight background closes so they are not garbage
# collected before the subprocess is terminated.
_background_closes: set[asyncio.Task[None]] = set()


def _epoch_to_datetime(value: float) -> datetime:
    """Convert a Unix timestamp (seconds or milliseconds) to an aware datetime."""
    seconds = float(value)
    if seconds > 1e12:  # millisecond precision
        seconds /= 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _parse_resets_at(text: str) -> datetime | None:
    """Extract a reset time from a limit-rejection message, if present."""
    match = _EPOCH_SUFFIX.search(text)
    if match:
        return _epoch_to_datetime(int(match.group(1)))
    return None


def _result_error_text(message: ResultMessage) -> str:
    """Collect every error-bearing string on a ResultMessage for inspection."""
    parts: list[str] = []
    if message.result:
        parts.append(message.result)
    if message.errors:
        parts.extend(str(e) for e in message.errors)
    return "\n".join(parts)


def _claude_config_dir() -> Path:
    """Directory Claude Code stores credentials in (``CLAUDE_CONFIG_DIR`` aware)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude"


def _usage_data(usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Map an SDK usage dict onto the RawEvent contract's usage keys.

    The SDK reports cache tokens as ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens``; the contract uses ``cache_read_tokens``
    / ``cache_creation_tokens``.
    """
    if not usage:
        return None
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }


def _result_data(message: ResultMessage) -> dict[str, Any]:
    """Reshape the terminal SDK ResultMessage into the contract's result dict."""
    raw: dict[str, Any] | None = None
    # Deliberately broad: a future SDK field that resists dict conversion
    # must degrade to raw=None, not break the terminal event.
    with suppress(Exception):
        raw = asdict(message)
    return {
        "text": message.result or "",
        "session_id": message.session_id,
        "is_error": message.is_error,
        "num_turns": message.num_turns,
        "usage": _usage_data(message.usage),
        "total_cost_usd": message.total_cost_usd,
        "raw": raw,
    }


def _stream_event_text(message: StreamEvent) -> str | None:
    """Top-level assistant text carried by a partial-message event, if any."""
    if message.parent_tool_use_id is not None:
        return None  # subagent output, not part of the top-level reply
    event = message.event
    if event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta") or {}
    if delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def _assistant_events(message: AssistantMessage) -> list[RawEvent]:
    """Contract events carried by an assistant message: tool uses and usage."""
    events: list[RawEvent] = []
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            events.append(
                RawEvent(
                    kind="tool_use",
                    data={
                        "tool_name": block.name,
                        "tool_input": block.input,
                        "tool_use_id": block.id,
                    },
                )
            )
    usage = _usage_data(message.usage)
    if usage is not None:
        events.append(
            RawEvent(kind="usage", data={"message_id": message.message_id, "usage": usage})
        )
    return events


def _user_events(message: UserMessage) -> list[RawEvent]:
    """Contract events carried by a user message: tool results echoed back."""
    events: list[RawEvent] = []
    content = message.content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, ToolResultBlock):
                events.append(
                    RawEvent(
                        kind="tool_result",
                        data={
                            "tool_use_id": block.tool_use_id,
                            "content": block.content,
                            "is_error": bool(block.is_error),
                        },
                    )
                )
    return events


async def _aclose_quietly(stream: AsyncIterator[Any]) -> None:
    """Close the SDK stream; closure failure must not mask the run's outcome."""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    # Deliberately broad: this is last-resort cleanup and the run's outcome
    # was already decided before it started.
    with suppress(Exception):
        await aclose()


async def _shutdown_stream(stream: AsyncIterator[Any], pending: asyncio.Future[Any] | None) -> None:
    """Settle any in-flight SDK read, then close the SDK stream.

    Runs in its own task so no caller cancellation can reach the SDK's
    close path — its terminate/kill escalation is skipped when a raw
    asyncio cancellation is delivered inside it. A read that does not
    settle within the grace is cancelled; in that case the SDK's own
    fallback (an atexit reaper for still-alive children) is the last
    resort, which is the SDK's documented limitation, not a new one.
    """
    if pending is not None:
        if not pending.done():
            await asyncio.wait({pending}, timeout=_SETTLE_GRACE_SECONDS)
        if not pending.done():
            pending.cancel()
            await asyncio.wait({pending}, timeout=_SETTLE_GRACE_SECONDS)
        if pending.done() and not pending.cancelled():
            # Retrieve any exception so the loop does not log it as
            # "never retrieved"; the run's outcome was already decided.
            pending.exception()
    await _aclose_quietly(stream)


class ClaudeCodeAdapter(BaseAgentAdapter):
    """Adapter driving Claude Code through the ``claude-agent-sdk`` transport.

    One-shot only in v0.1: each :meth:`run_once` call launches a fresh CLI
    subprocess via the SDK, streams its messages, and terminates it when the
    iterator is exhausted or closed. Throughput is capped by the user's own
    plan; this adapter reports limit rejections, it cannot lift them.
    """

    name = "claude-code"

    async def check_auth(self) -> AuthStatus:
        """Report local auth state from the environment, without a subprocess.

        Checks, in the CLI's own precedence order: ``ANTHROPIC_API_KEY``
        (which silently overrides subscription auth and bills per token),
        ``CLAUDE_CODE_OAUTH_TOKEN`` (headless subscription token), and the
        on-disk subscription credentials file. The file check runs in a
        worker thread so a slow home directory cannot stall the event loop.
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        creds_file = _claude_config_dir() / ".credentials.json"
        creds_present = await asyncio.to_thread(creds_file.is_file)

        if api_key:
            detail = (
                "ANTHROPIC_API_KEY is set and takes precedence over subscription "
                "auth: every run will be billed per token at API rates. If you "
                "intend to use a Claude subscription, unset ANTHROPIC_API_KEY."
            )
            if oauth_token or creds_present:
                detail += " Subscription credentials are also present but will be ignored."
            return AuthStatus(ok=True, method="api_key", detail=detail)

        if oauth_token:
            return AuthStatus(ok=True, method="oauth_token", detail=None)

        if creds_present:
            return AuthStatus(ok=True, method="subscription", detail=None)

        return AuthStatus(
            ok=False,
            method=None,
            detail=(
                "No credentials detected: ANTHROPIC_API_KEY and "
                "CLAUDE_CODE_OAUTH_TOKEN are unset and no credentials file was "
                f"found at {creds_file}. Credentials stored in an OS keychain "
                "cannot be detected without launching the CLI; if you have "
                "logged in that way, runs may still succeed."
            ),
        )

    async def run_once(
        self,
        prompt: str,
        cwd: str,
        *,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        max_turns: int | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> AsyncIterator[RawEvent]:
        """One-shot turn via the SDK's ``query()``: launch, stream, exit.

        Yields :class:`RawEvent` envelopes following the kinds contract in
        :mod:`anycli.adapters.base` (mapping documented in this module's
        docstring). The underlying subprocess is reaped in all cases: the
        SDK stream is closed in a ``finally`` block from a dedicated task,
        so abandoning or cancelling this iterator mid-stream still
        terminates the CLI — a raw cancellation cannot skip the SDK's
        terminate/kill escalation, and a closure slower than the grace
        period finishes in the background instead of pinning the caller.

        Raises:
            CLINotFound: the bundled CLI executable could not be launched.
            AuthError: the CLI rejected the run for credential reasons.
            PlanLimitReached: the run was rejected by a hard plan limit;
                ``resets_at`` is parsed from the CLI's own message when present.
            TransientAdapterError: the run failed with a retryable provider
                signal (HTTP 429 throttling or 529 overload).
            AdapterError: any other subprocess or protocol failure.
        """
        option_kwargs: dict[str, Any] = dict(extra_options) if extra_options else {}
        option_kwargs["cwd"] = cwd
        option_kwargs["include_partial_messages"] = True
        if allowed_tools is not None:
            option_kwargs["allowed_tools"] = allowed_tools
        if permission_mode is not None:
            option_kwargs["permission_mode"] = permission_mode
        if max_turns is not None:
            option_kwargs["max_turns"] = max_turns

        try:
            options = ClaudeAgentOptions(**option_kwargs)
        except TypeError as exc:
            raise AdapterError(f"Invalid options for Claude Code adapter: {exc}") from exc

        stream = query(prompt=prompt, options=options)
        # Reset time from the most recent rate-limit event, used to enrich a
        # limit rejection whose result text does not carry a timestamp itself.
        rate_limit_resets_at: datetime | None = None
        rate_limit_rejected = False
        loop = asyncio.get_running_loop()
        # Each SDK read runs in its own task behind a shield: cancelling this
        # generator must not unwind the SDK's query() generator with a raw
        # CancelledError, because that would skip its terminate/kill
        # escalation. Shutdown instead goes through _shutdown_stream.
        next_task: asyncio.Future[Any] | None = None

        try:
            while True:
                if next_task is None:
                    next_task = asyncio.ensure_future(stream.__anext__(), loop=loop)
                try:
                    message = await asyncio.shield(next_task)
                except StopAsyncIteration:
                    next_task = None
                    break
                next_task = None
                if isinstance(message, ResultMessage):
                    if message.is_error:
                        error_text = _result_error_text(message)
                        lowered = error_text.lower()
                        if rate_limit_rejected or any(
                            marker in lowered for marker in _PLAN_LIMIT_MARKERS
                        ):
                            resets_at = _parse_resets_at(error_text) or rate_limit_resets_at
                            raise PlanLimitReached(
                                error_text or "Plan limit reached", resets_at=resets_at
                            )
                        if any(marker in lowered for marker in _AUTH_MARKERS):
                            raise AuthError(
                                (error_text or "Claude Code authentication failed")
                                + _AUTH_REMEDIATION
                            )
                        if message.api_error_status in _TRANSIENT_STATUSES:
                            raise TransientAdapterError(
                                error_text
                                or f"transient API failure (HTTP {message.api_error_status})"
                            )
                    yield RawEvent(kind="result", data=_result_data(message))
                elif isinstance(message, RateLimitEvent):
                    info = message.rate_limit_info
                    if info.resets_at is not None:
                        rate_limit_resets_at = _epoch_to_datetime(info.resets_at)
                    if info.status == "rejected":
                        rate_limit_rejected = True
                    yield RawEvent(kind="rate_limit", data=message)
                elif isinstance(message, StreamEvent):
                    text = _stream_event_text(message)
                    if text is not None:
                        yield RawEvent(kind="text_delta", data={"text": text})
                    else:
                        yield RawEvent(kind="stream_event", data=message)
                elif isinstance(message, AssistantMessage):
                    for event in _assistant_events(message):
                        yield event
                elif isinstance(message, UserMessage):
                    for event in _user_events(message):
                        yield event
                elif isinstance(message, SystemMessage):
                    session_id = (
                        message.data.get("session_id") if message.subtype == "init" else None
                    )
                    if session_id:
                        yield RawEvent(kind="init", data={"session_id": str(session_id)})
                    else:
                        yield RawEvent(kind="system", data=message)
                else:
                    yield RawEvent(kind=type(message).__name__, data=message)
        except CLINotFoundError as exc:
            raise CLINotFound(str(exc) + _CLI_NOT_FOUND_REMEDIATION) from exc
        except ProcessError as exc:
            blob = f"{exc} {exc.stderr or ''}".lower()
            if any(marker in blob for marker in _AUTH_MARKERS):
                raise AuthError(str(exc) + _AUTH_REMEDIATION) from exc
            raise AdapterError(str(exc)) from exc
        except CLIJSONDecodeError as exc:
            raise AdapterError(str(exc)) from exc
        except ClaudeSDKError as exc:
            # Covers CLIConnectionError, MessageParseError, and any future
            # SDK error types.
            raise AdapterError(str(exc)) from exc
        finally:
            # Always close the SDK stream so its subprocess is reaped, even
            # when this generator is abandoned or cancelled mid-stream. The
            # shutdown runs in its own task, immune to this frame's
            # cancellation, so the SDK's terminate/kill escalation is not
            # skipped. Waiting is bounded so a stuck teardown finishes in
            # the background instead of pinning the caller.
            close_task = loop.create_task(_shutdown_stream(stream, next_task))
            _background_closes.add(close_task)
            close_task.add_done_callback(_background_closes.discard)
            await asyncio.wait({close_task}, timeout=_CLOSE_GRACE_SECONDS)
