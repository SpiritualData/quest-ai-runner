#!/usr/bin/env python3
"""End-to-end smoke test of the executor lane against a LIVE Quest backend.

It proves the whole round-trip with the REAL runner code path and only the LLM stubbed:

  LIVE (real network + real `qsk_` auth):
    - enqueue assistant_tasks (POST /api/assistant-tasks), both due now
    - Poller.run_once -> discover (GET due_before=now) -> claim (PATCH in_progress)
    - report (PATCH done | needs_you + decision_id)
    - escalate the human-only task (POST /api/teams/{team}/decisions)
    - re-read both tasks to PROVE the reported status round-tripped through Quest
  STUBBED:
    - only the ModelProvider (planner/answer), so no ANTHROPIC_API_KEY is needed. The stub
      returns "confirm" for a task carrying the human-only marker, else "answer".

Everything the runner LIBRARY does (discover/claim/run-loop/report/escalate) is the real code.

Run:
  QUEST_BASE_URL=https://api.example.org QUEST_API_KEY=qsk_... QUEST_TEAM_ID=team_... \
  python examples/e2e_demo.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from examples.custom_consumer import build_config  # noqa: E402
from quest_ai_runner.runner.poller import Poller  # noqa: E402
from quest_ai_runner.runner.quest_client import QuestClient  # noqa: E402

HUMAN_ONLY_MARKER = "[HUMAN-ONLY]"


class StubProvider:
    """Deterministic ModelProvider — the ONLY stubbed surface."""

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if HUMAN_ONLY_MARKER in prompt:
            return {"action": "confirm",
                    "confirm_question": "This task requires a human-only approval before I act. "
                                        "Approve?",
                    "model_tier": "sonnet", "rationale": "human-only step detected"}
        return {"action": "answer", "model_tier": "haiku", "rationale": "trivial answerable task"}

    def answer(self, messages: List[Dict[str, str]], *, model: str, system=None) -> str:
        return "STUB-ANSWER: task acknowledged and completed by the quest-ai-runner brain."

    def list_models(self) -> List[str]:
        return ["claude-haiku-stub", "claude-sonnet-stub", "claude-opus-stub"]


def _client(cfg) -> QuestClient:
    return QuestClient(cfg.quest_base_url, cfg.quest_api_key, team_id=cfg.team_id)


def _enqueue(client: QuestClient, text: str) -> Dict[str, Any]:
    resp = client._request("POST", "/api/assistant-tasks", body={"text": text, "source": "chat"})
    return resp or {}


def main() -> int:
    cfg = build_config(with_model_provider=False)
    problems = [p for p in cfg.validate() if "model_provider" not in p]
    if problems:
        print("Set the required env vars first:")
        for p in problems:
            print(f"  - {p}")
        return 2
    cfg.model_provider = StubProvider()

    print(f"{'='*70}\nLANE  team={cfg.team_id}  url={cfg.quest_base_url}\n{'='*70}")
    client = _client(cfg)
    print("whoami (LIVE):", client.whoami())

    # 1) LIVE enqueue: an answerable task + a human-only task (both due now).
    t_answer = _enqueue(client, "What is the capital of France? Answer briefly.")
    t_human = _enqueue(client, f"{HUMAN_ONLY_MARKER} Approve and send the partner agreement.")
    aid = t_answer.get("task_id") or t_answer.get("id")
    hid = t_human.get("task_id") or t_human.get("id")
    print(f"ENQUEUED (LIVE): answerable={aid}  human-only={hid}")

    # 2) LIVE run_once: discover -> claim -> run(brain, stub provider) -> report.
    state = "/tmp/qar_e2e_demo.json"
    if os.path.exists(state):
        os.remove(state)
    handled = Poller(cfg, state_path=state).run_once()
    print(f"run_once handled (LIVE): {handled}")

    # 3) LIVE re-read both tasks to prove the reported status round-tripped through Quest.
    a_final = client.get_task(aid) if aid else {}
    h_final = client.get_task(hid) if hid else {}
    print(f"answerable {aid}: status={a_final.get('status')!r}")
    print(f"human-only {hid}: status={h_final.get('status')!r} "
          f"decision_id={h_final.get('decision_id')!r}")

    result = {
        "team_id": cfg.team_id,
        "answerable": {"task_id": aid, "status": a_final.get("status")},
        "human_only": {"task_id": hid, "status": h_final.get("status"),
                       "decision_id": h_final.get("decision_id")},
        "handled": handled,
    }
    print(f"\n{'='*70}\nSUMMARY\n{'='*70}\n" + json.dumps(result, indent=2, default=str))

    ok = (a_final.get("status") == "done"
          and h_final.get("status") == "needs_you"
          and bool(h_final.get("decision_id")))
    print("\nE2E PASS (answer->done, human-only->needs_you+decision):", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
