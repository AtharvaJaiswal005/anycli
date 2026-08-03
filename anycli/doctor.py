"""Environment checks for auth footguns.

Pure local inspection: no network calls, no subprocesses. The headline
check is ``ANTHROPIC_API_KEY`` contamination — agent CLI credential
precedence puts an API key above subscription auth, so a stale key in the
environment silently switches a subscription user to per-token API billing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FindingLevel = Literal["info", "warning"]


@dataclass(frozen=True)
class Finding:
    """One environment check outcome."""

    level: FindingLevel
    message: str


def default_credentials_dir() -> Path:
    """Directory where the agent CLI keeps local credentials.

    ``CLAUDE_CONFIG_DIR`` relocates it; otherwise it is ``~/.claude``
    (``%USERPROFILE%\\.claude`` on Windows).
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def check_env() -> list[Finding]:
    """Inspect the environment for auth misconfiguration.

    Returns findings in severity order (warnings first). Never raises and
    never touches the network or a subprocess.
    """
    findings: list[Finding] = []

    if os.environ.get("ANTHROPIC_API_KEY"):
        findings.append(
            Finding(
                level="warning",
                message=(
                    "ANTHROPIC_API_KEY is set in the environment. It silently "
                    "overrides subscription auth and bills every run per token "
                    "at API rates. Unset it if you intend to run on your "
                    "subscription."
                ),
            )
        )

    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        findings.append(
            Finding(
                level="info",
                message=(
                    "CLAUDE_CODE_OAUTH_TOKEN is set: headless token auth is in "
                    "use for the agent CLI."
                ),
            )
        )

    creds_dir = default_credentials_dir()
    if creds_dir.is_dir():
        findings.append(
            Finding(
                level="info",
                message=f"Agent CLI credentials directory found at {creds_dir}.",
            )
        )
    else:
        findings.append(
            Finding(
                level="info",
                message=(
                    f"Agent CLI credentials directory not found at {creds_dir}. "
                    "If runs fail with auth errors, log in with the agent CLI "
                    "on this machine (for Claude Code: run `claude` and use "
                    "/login, or set CLAUDE_CODE_OAUTH_TOKEN from "
                    "`claude setup-token` for headless use)."
                ),
            )
        )

    return findings
