"""Adapters: one thin module per agent CLI, all implementing BaseAgentAdapter."""

from anycli.adapters.base import BaseAgentAdapter
from anycli.adapters.claude_code import ClaudeCodeAdapter

__all__ = ["BaseAgentAdapter", "ClaudeCodeAdapter"]
