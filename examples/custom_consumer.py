"""Example consumer — build a RunnerConfig for your own org/team, all from the environment.

`quest-ai-runner` is domain-free: it bakes in NO org, key, team, corpus, or persona. A *consumer*
supplies those specifics by constructing a `RunnerConfig`. This module is a reference for doing
that — copy it, adapt it, point it at your own Quest backend.

Everything is read from environment variables so the SAME module serves dev and prod unchanged.
There are NO real keys, ids, or paths in this file — only env lookups with safe placeholders.

Required env:
  QUEST_BASE_URL    your Quest API base, e.g. https://api.example.org
  QUEST_API_KEY     the executor identity, a `qsk_...` key (NEVER commit this)
  QUEST_TEAM_ID     the team this lane serves (drives the heartbeat + decision routing)

Model backend (pick one; auto-selected if unset):
  QAR_MODEL_BACKEND anthropic | claude_cli. Unset => claude_cli (keyless, runs on this box's
                    Claude Code subscription login) unless ANTHROPIC_API_KEY is set.
  ANTHROPIC_API_KEY only for the `anthropic` backend (per-token billing); omit to run keyless.

Optional env:
  QAR_CORPUS_ROOT       file root the FilesAdapter grounds on (your docs/corpus)
  QAR_CLAUDE_PATH       the deep-runner worker binary (default: `claude` on PATH)
  QAR_DECISION_ASSIGNEE user id that human-only confirm/decision requests route to
  QAR_CONTEXT_PREAMBLE  org/persona context prepended to every deep-run brief
  QAR_RUNNER_LABEL      a human-readable tag sent on the env heartbeat

See `.env.example` at the repo root for the full list.
"""
from __future__ import annotations

import os
from typing import Optional

from quest_ai_runner.adapters import AnthropicProvider, ClaudeCliProvider, FilesAdapter
from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner


def _model_provider():
    """Pick the model backend from env: keyless `claude_cli` by default, `anthropic` if a key is set."""
    backend = (os.getenv("QAR_MODEL_BACKEND") or "").strip().lower()
    if not backend:
        backend = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "claude_cli"
    if backend == "claude_cli":
        return ClaudeCliProvider(claude_path=os.getenv("QAR_CLAUDE_PATH", "claude"))
    return AnthropicProvider()


def build_config(*, with_model_provider: bool = True) -> RunnerConfig:
    """Construct a RunnerConfig for one lane entirely from the environment.

    ``with_model_provider=False`` omits the model provider so a test can inject a stub provider
    without needing an ANTHROPIC_API_KEY or the `claude` CLI.
    """
    corpus = os.getenv("QAR_CORPUS_ROOT")
    retrieval = FilesAdapter(corpus) if corpus else None

    deep: Optional[SubprocessGoalRunner] = None
    if corpus:
        deep = SubprocessGoalRunner(SubprocessConfig(
            working_dir=corpus,
            claude_path=os.getenv("QAR_CLAUDE_PATH", "claude"),
            context_preamble=os.getenv(
                "QAR_CONTEXT_PREAMBLE",
                "You are executing an AI task for this team. Ground on the configured corpus; "
                "work to the written goal; surface any human-only step as a decision-request.",
            ),
        ))

    return RunnerConfig(
        quest_base_url=os.getenv("QUEST_BASE_URL", ""),
        quest_api_key=os.getenv("QUEST_API_KEY", ""),
        team_id=os.getenv("QUEST_TEAM_ID", ""),
        runner_label=os.getenv("QAR_RUNNER_LABEL", "example-runner"),
        retrieval=retrieval,
        model_provider=_model_provider() if with_model_provider else None,
        deep_runner=deep,
        corpus_root=corpus,
        default_assignee_user_id=os.getenv("QAR_DECISION_ASSIGNEE"),
    )


if __name__ == "__main__":
    cfg = build_config(with_model_provider=False)
    problems = cfg.validate()
    if problems:
        print("RunnerConfig is incomplete — set the missing env vars:")
        for p in problems:
            print(f"  - {p}")
    else:
        print("RunnerConfig looks complete.")
    print(f"team_id={cfg.team_id!r} base_url={cfg.quest_base_url!r} corpus_root={cfg.corpus_root!r}")
