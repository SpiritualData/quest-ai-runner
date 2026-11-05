"""A mounted terminal with a stand-in session, for tests and screenshots of the work trail, updates, asks and stopped replies.

No model, no network: events are fed straight to ``_handle_event`` and the turn is closed with ``_finish_turn``, the two calls the
real stream makes.
"""
from __future__ import annotations

from types import SimpleNamespace

from hubofhubs.the_world.tools.deep import ProgressEvent
from hubofhubs.the_world.tools.events import EVENT_DECISION, EVENT_INTERIM, EVENT_RESULT, EVENT_WORK_TRACE

from quest_ai_runner.core.orchestrator import OrchestratorResult
from quest_ai_runner.textual_ui import QuestAITerminal


class FakeClient:
    def __init__(self, follow_up=None, fail=False):
        self.calls, self.follow_up, self.fail = [], follow_up, fail

    def resolve_decision(self, decision_id, resolution, human_statement=None, response_text=None):
        if self.fail:
            raise RuntimeError("refused")
        self.calls.append((decision_id, resolution, response_text))
        return {"ok": True, "follow_up_task_id": self.follow_up}


class Session:
    _rep_name = "Tester"
    _goal_id = None
    _model_hint = None

    def __init__(self, client=None):
        self._cfg = SimpleNamespace(corpus_root=None, deep_runner=None)
        self._last_user, self._last_assistant = "", ""
        self._session_history, self._turns, self._turn_count = [], [], 0
        self._orch = SimpleNamespace(context_assembler=None, input_inbox=None, cfg=SimpleNamespace(interim_replies=False))
        self._client = client

    def _write_session_file(self):
        pass

    def _maybe_refresh_next_steps(self, final):
        pass

    def _quest_client(self):
        return self._client


def trace(step, state="running", read=1, items=None, kind="sources", counts=None):
    return ProgressEvent(type=EVENT_WORK_TRACE, text=f"{step} {state}", data={
        "v": 1, "step_id": step, "kind": kind, "state": state, "label_key": "", "solution_id": "s", "affects_user": False,
        "counts": counts or {"read": read}, "items": items or []})


def site(ref, label=""):
    return {"ref": ref, "label": label or ref, "kind": "read"}


def interim(done, why="", nxt=""):
    return ProgressEvent(type=EVENT_INTERIM, text=done, data={"done": done, "why_more": why, "next": nxt, "will_continue": True})


def ask_event(**data):
    base = {"outcome": "asked", "question": "Which spreadsheet should I use?", "why_only_you": "Only you know which one is current",
            "done": "I read both sheets.", "options": ["The 2025 sheet", "The 2026 sheet"]}
    base.update(data)
    return ProgressEvent(type=EVENT_DECISION, text="I read both sheets.\n\nWhich spreadsheet should I use?", decision_id="dec_1", data=base)


def result_event(outcome="met", reason=""):
    return ProgressEvent(type=EVENT_RESULT, text="x", data={"outcome": outcome, "stop_reason": reason})


def transcript(app) -> list:
    return [s.text.rstrip() for s in app._tlog.lines]


class Harness:
    """``async with Harness() as h``: a mounted app; ``h.begin()`` starts a turn's state, ``h.feed(*events)``, ``h.finish(text)``."""

    def __init__(self, client=None, size=(100, 34)):
        self.app = QuestAITerminal(Session(client))
        self.app._build_session_worker = lambda: None
        self.size = size

    async def __aenter__(self):
        self.ctx = self.app.run_test(size=self.size)
        self.pilot = await self.ctx.__aenter__()
        await self.pilot.pause()
        self.app._console = self.app._console or None
        return self

    async def __aexit__(self, *exc):
        return await self.ctx.__aexit__(*exc)

    def begin(self):
        self.app._turn_active = True
        self.app.term_begin_turn()

    def feed(self, *events):
        for e in events:
            self.app._handle_event(e)

    def finish(self, text="Here is the answer.", kind="answer"):
        final = OrchestratorResult(kind=kind, text=text)
        self.app._finish_turn("question", final, 4.0, False, None)

    def lines(self):
        return transcript(self.app)
