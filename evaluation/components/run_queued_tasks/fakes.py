"""The outside edge for the queued-task runner component: a fake Quest client and a scripted model. These
stand in for the network and the model only; every decision between them is made by live runner code."""
from datetime import datetime, timedelta, timezone

from quest_ai_runner.core.adapters import DeepResult, Escalation, Observation


def iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def materialise(tasks):
    """Turn ``due_in_minutes`` into a ``due_at`` timestamp so a case file holds no clock."""
    out = []
    for task in tasks:
        task = dict(task)
        offset = task.pop("due_in_minutes", None)
        if offset is not None:
            task["due_at"] = iso(datetime.now(timezone.utc) + timedelta(minutes=offset))
        out.append(task)
    return out


class FakeQuest:
    """Serves due tasks the way the backend does (honours ``due_at`` and ``team_id``) and records every call."""

    def __init__(self, tasks, claim="ok", configured=True):
        self.tasks = list(tasks)
        self.claim_mode = claim
        self.configured = configured
        self.claimed = []
        self.reports = []

    def discover_due(self, *, now=None, status="queued", team_id=None, team_ids=None, env_id=None):
        now = now or datetime.now(timezone.utc)
        wanted = set(str(",".join(team_ids) if team_ids else team_id or "").split(",")) - {""}
        out = []
        for task in self.tasks:
            due = task.get("due_at")
            if due and datetime.strptime(due, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) > now:
                continue
            if wanted and task.get("team_id") not in wanted:
                continue
            out.append(task)
        return out

    def list_interactive_due(self, **kw):
        return []

    def wait_for_interactive(self, **kw):
        return None

    def claim(self, task_id, handler=None):
        self.claimed.append(task_id)
        return None if self.claim_mode == "fail" else {"id": task_id, "status": "in_progress"}

    def report_done(self, task_id, result):
        self.reports.append((task_id, "done", result, None))

    def report_done_with_data(self, task_id, result, result_data=None):
        self.reports.append((task_id, "done", result, None))
        return {"id": task_id}

    def report_needs_you(self, task_id, result, decision_id):
        self.reports.append((task_id, "needs_you", result, decision_id))

    def report_failed(self, task_id, result):
        self.reports.append((task_id, "failed", result, None))

    def report_incomplete(self, task_id, result):
        self.reports.append((task_id, "incomplete", result, None))

    def report_progress(self, task_id, kind, *, text=None, output=None, data=None):
        return {"ok": True}

    def post_conversation_message(self, conv_id, content, *, kind="progress", task_id=None, card_id=None):
        return {"role": "assistant", "kind": kind, "content": content}

    def post_environment_heartbeat(self, capabilities, **kw):
        return {"enabled": True}

    def whoami(self):
        return {"executor": "fake"}


class ScriptedModel:
    """A ModelProvider that replays the case's planner decisions, then answers plainly."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.models = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]

    def plan(self, prompt, *, model, tool_schema):
        if self.decisions:
            return self.decisions.pop(0)
        return {"action": "answer", "rationale": "fallback", "model_tier": "sonnet"}

    def answer(self, messages, *, model, system=None):
        return "scripted answer"


class FileMap:
    """A retrieval adapter over a tiny in-memory file map."""

    def __init__(self, files):
        self.files = files

    def read_section(self, rel_path, **kw):
        if rel_path not in self.files:
            return Observation(kind="error", rel_path=rel_path, error="not found")
        return Observation(kind="read", rel_path=rel_path, locator="head", text=self.files[rel_path])

    def grep(self, pattern, *, scope=None, max_hits=None):
        return Observation(kind="grep", pattern=pattern, scope=scope, hits=[])

    def query(self, spec):
        return Observation(kind="error", error="query unsupported")


class FixedDeep:
    def __init__(self, met, output, error):
        self.met, self.output, self.error = met, output, error

    def run_goal(self, *, goal, brief, model=None, max_turns=None):
        return DeepResult(met=self.met, output=self.output, error=self.error)


class FixedEscalation:
    def __init__(self, decision_id):
        self.decision_id = decision_id

    def escalate(self, escalation: Escalation):
        return self.decision_id


class FakeFolderClient:
    """The account-wide quest surface a folder sync talks to: one quest, a list of notes, add_note."""

    def __init__(self, state, notes):
        self.state = state
        self.notes = [dict(n) for n in notes]

    def get_my_quest(self, quest_id):
        return {"quest_id": quest_id, "state": dict(self.state)}

    def list_quest_notes(self, quest_id):
        return [dict(n) for n in self.notes]

    def add_quest_note(self, quest_id, text):
        self.notes.append({"note_id": f"note_{len(self.notes) + 1}", "text": text, "author_kind": "ai",
                           "created_at": "2026-07-08T00:00:00"})
        return [dict(n) for n in self.notes]
