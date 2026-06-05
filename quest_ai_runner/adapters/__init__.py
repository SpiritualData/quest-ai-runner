"""Reference adapter implementations.

These satisfy the core interfaces; a consumer wires the ones it needs into a RunnerConfig:
  * FilesAdapter        — RetrievalAdapter over a configured file root (quest-docs + corpus).
  * CachedDbAdapter     — RetrievalAdapter: live DB reads via a short-TTL cache (no file sync).
  * AnthropicProvider   — ModelProvider (plan / answer / live models.list bucketing). Needs an
                          ANTHROPIC_API_KEY (per-token billing).
  * ClaudeCliProvider   — ModelProvider that drives the local ``claude`` CLI headless, KEYLESS:
                          plan/answer run on the box's Claude Code subscription login (no API key).
The DeepRunner reference (SubprocessGoalRunner) lives in core.goal_runner; the EscalationSink
reference (the Quest team decision-request) lives in runner.quest_client.
"""
from .anthropic_provider import AnthropicProvider
from .cached_db_adapter import CachedDbAdapter
from .claude_cli_provider import ClaudeCliProvider
from .files_adapter import FilesAdapter

__all__ = ["FilesAdapter", "CachedDbAdapter", "AnthropicProvider", "ClaudeCliProvider"]
