"""One tiny end-to-end run against the real agent CLI.

Marked ``integration``: deselected by default (see pyproject addopts), never
run in CI, and sized to be safe on a personal subscription — one small
prompt, one turn.

Run locally with: ``uv run pytest -m integration``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anycli import Bridge, ClaudeCodeAdapter, Result

pytestmark = pytest.mark.integration


async def test_one_shot_pong(tmp_path: Path) -> None:
    bridge = Bridge(ClaudeCodeAdapter(), default_cwd=str(tmp_path))

    result = await bridge.run("Reply with exactly: pong", max_turns=1)

    assert isinstance(result, Result)
    assert "pong" in result.text.lower()
    assert result.session_id
    assert result.is_error is False
