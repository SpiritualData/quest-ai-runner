"""Shared machinery of the arms: the fake Quest client, the boundary patches and one runner per case kind.

Nothing here decides an answer. A provider (or none) is handed in by the arm; the library's own functions do the rest."""
import contextlib
import io
import json
import types
from unittest import mock

import common  # noqa: F401  (puts the repository on the path)

ROSTER = [{"user_id": "u1", "name": "Ann"}, {"user_id": "u2", "name": "Bo"}]   # the roster of tests/test_plan_reconcile.py
REFERENCE_ACK = "I am looking into your request."   # the acknowledgement the repository's own tests stub
EM_DASHES = ("\u2014", "\u2013", "--")
MAX_ACK_WORDS = 20   # the ack prompt spec: ONE sentence (max 20 words)


class FakeQuestClient:
    """The Quest client at the boundary: queues a task as the case says."""
    mode = "ok"

    def __init__(self, *args, **kwargs):  # noqa: ARG002
        pass

    def create_task(self, text, **kwargs):  # noqa: ARG002
        if self.mode == "raises":
            from quest_ai_runner.runner.quest_client import QuestApiError
            raise QuestApiError("queue refused")
        return {} if self.mode == "no_id" else {"id": "task-1"}

    def list_quest_goals(self, quest_id):  # noqa: ARG002
        return {"period_groups": []}


class ReplayProvider:
    """A model provider that answers from the case instead of a model. ``tokens_in`` and ``tokens_out`` are the
    character counts divided by four, the estimate the cost line of an offline run is built on."""

    def __init__(self, respond):
        self.respond = respond
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        self.tokens_in += (len(system or "") + sum(len(str(m.get("content", ""))) for m in messages)) // 4
        text = self.respond(messages)
        self.tokens_out += len(text or "") // 4
        return text


class PinnedModel:
    """A real provider that answers with one pinned model whatever model the caller asks for: the tier pin of a live run.
    Only ``answer`` is exposed, so the tier registry falls back to its defaults and makes no listing call."""

    def __init__(self, provider, model):
        self.provider, self.model = provider, model

    def answer(self, messages, *, model=None, system=None):  # noqa: ARG002
        return self.provider.answer(messages, model=self.model, system=system)


def words(text):
    return len(str(text).split())


def reference_answer(case):
    """What a model that is exactly as right as the label would answer: the label replayed in the answer's format."""
    kind = case["kind"]
    if kind == "ack":
        spec = case.get("stub_answer") or {"mode": "reference"}
        if spec["mode"] == "raises":
            raise RuntimeError("provider down")
        return spec.get("text") if spec["mode"] == "text" else REFERENCE_ACK
    if kind == "classify":
        label = case["label"]
        return json.dumps({"kind": label["kind"], "assignee_id": label.get("stated_assignee"), "confidence": 0.95,
                           "reason": "reference answer", "title": case["ask"]["text"][:80]})
    if kind == "judge":
        return json.dumps({"ai_doable": case["label"]["ai_doable"], "confidence": 0.95, "reason": "reference answer"})
    return json.dumps({"kind": "context", "confidence": 0.9, "reason": "reference answer"})


def run_ack(case, provider_factory, send=None):
    """The real ``send`` command with the task queue and the model provider at the boundary."""
    from quest_ai_runner import cli_quest
    send = send or cli_quest._cmd_send
    FakeQuestClient.mode = case.get("create_task", "ok")
    args = types.SimpleNamespace(text=case["request"], team_id="", goal_id=None, scheduled_at=None, card_ids=[],
                                 no_auto_cards=True)

    asked = []

    def no_provider():
        raise RuntimeError("no model configured")

    def counted():
        asked.append(1)
        return provider_factory()

    out = io.StringIO()
    with common.hermetic_env({"QUEST_BASE_URL": common.OFFLINE_QUEST_URL, "QUEST_API_KEY": "offline-eval"}), \
            mock.patch("quest_ai_runner.runner.quest_client.QuestClient", FakeQuestClient), \
            mock.patch.object(cli_quest, "_model_provider_from_env", counted if provider_factory else no_provider), \
            contextlib.redirect_stdout(out):
        code = send(args, mock.MagicMock())
    return {"stdout": out.getvalue().strip(), "exit": code, "provider_requests": len(asked)}


def helpers_from(provider, model="stub-model"):
    """The real ``_ask_classifier`` and ``_goal_judge`` with the model resolution replaced by the arm's provider."""
    from quest_ai_runner import cli_config
    with mock.patch.object(cli_config, "_quest_model", lambda: (provider, model)):
        return cli_config._ask_classifier("q1", FakeQuestClient()), cli_config._goal_judge("q1", FakeQuestClient())


def run_classify(case, classify):
    from quest_ai_runner.runner.plan_reconcile import validate_decision
    raw = classify(dict(case["ask"]), ROSTER) if classify else None
    return {"raw": raw, "decision": validate_decision(raw, {m["user_id"] for m in ROSTER})}


def run_judge(case, judge):
    return {"raw": judge(dict(case["goal"])) if judge else None}


def run_gate(case, classify):
    from quest_ai_runner.runner.plan_reconcile import classify_asks
    sent = []

    def spy(ask, roster):
        sent.append(ask["key"])
        return classify(ask, roster) if classify else None
    classify_asks([dict(a) for a in case["asks"]], ROSTER, spy)
    return {"sent": sent}
