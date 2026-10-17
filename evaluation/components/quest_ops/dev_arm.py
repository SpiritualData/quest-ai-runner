"""The dev-Quest arm of ``quest_ops``: the same fixed signature as the offline implementations,

    run_operation(message: str, world: DevWorld) -> dict

but ``world`` is a disposable quest on the dev Quest backend and the function sends the message through the
in-app chat (the surface a subscriber uses, which runs the production brain). It returns the reply plus the
turn's facts: ``route`` (``delegated`` / ``inline_write`` / ``inline_code`` / ``answer``), the planner
actions, tools, read steps and statuses. ``dev_run.py`` scores it (routing plus the case's real-state
verifier) and skips the whole arm, loudly, when the dev Quest is not configured.

Route classification, in order: ``delegated`` (a background task was enqueued, so the turn left the process;
the only "deep" the cases mean), ``inline_write`` (an exec frame with ``phase == "code"`` and
``mutating``), ``inline_code`` (read-only exec frames), else ``answer``. A planner ``deep`` action that
resolves to inline code is correct behaviour, so it is kept as its own fact (``planner_deep``)."""
import dev_world

STEP_CAP = 15  # the chat's read-step cap; reaching it is flagged


def name_of(event):
    return event.get("event") or event.get("type") or ""


class TurnResult:
    """One chat turn's SSE events folded into the facts the verifiers and the report need."""

    def __init__(self, events):
        self.events = events
        done = [e for e in events if name_of(e) == "done"]
        self.text = " ".join(str(e.get("content") or "") for e in done).strip() or " ".join(
            str(e.get("text") or "") for e in events if name_of(e) in ("token", "result")).strip()
        plans = [e for e in events if name_of(e) in ("plan", "replan")]
        self.actions = [a for a in ((e.get("data") or {}).get("action") for e in plans) if a]
        self.planner_deep = "deep" in self.actions
        self.exec_frames = [(e.get("data") or {}) for e in events if name_of(e) == "exec"]
        self.exec_errors = [f.get("error") for f in self.exec_frames if f.get("phase") == "error" and f.get("error")]
        self.tools = sorted({f.get("tool") for f in self.exec_frames if f.get("tool")})
        reads = [e for e in events if name_of(e) == "read"]
        self.read_frame_count = len(reads)
        steps = [s for s in ((e.get("data") or {}).get("step") for e in plans + reads) if isinstance(s, int)]
        self.max_step = max(steps) if steps else 0
        self.hit_step_cap = self.max_step >= STEP_CAP
        self.statuses = [str(e.get("text") or "") for e in events if name_of(e) == "status"]
        self.delegated = any(name_of(e) in ("delegated", "task_queued") or e.get("delegated")
                             or (e.get("data") or {}).get("delegated") for e in events)
        self.errors = [e for e in events if name_of(e).startswith("_") or name_of(e) == "error"]
        last = done[-1] if done else {}
        undo = last.get("pending_undo") or {}
        ids = [e.get("task_id") for e in events if name_of(e) in ("delegated", "task_queued") and e.get("task_id")]
        if undo.get("kind") == "task" and undo.get("task_id"):
            ids.append(undo["task_id"])  # a short-circuit hand-off can carry its id only here
        self.task_ids = sorted(set(ids))
        if self.delegated:
            self.route = "delegated"
        elif any(f.get("phase") == "code" and f.get("mutating") for f in self.exec_frames):
            self.route = "inline_write"
        elif self.exec_frames:
            self.route = "inline_code"
        else:
            self.route = "answer"

    def facts(self):
        return dict(route=self.route, planner_deep=self.planner_deep, actions=self.actions, tools=self.tools,
                    delegated=self.delegated, max_step=self.max_step, read_frame_count=self.read_frame_count,
                    hit_step_cap=self.hit_step_cap, statuses=self.statuses[:6], exec_errors=self.exec_errors,
                    transport_errors=[str(e)[:200] for e in self.errors])


class DevWorld:
    """Per-pass handle: the fixture plus the auto-run mode. ``last`` holds the latest turn for the scorer."""

    def __init__(self, fixture, auto_run=True):
        self.fx = fixture
        self.auto_run = auto_run
        self.last = None
        self.conv = None
        self.quest_id = fixture["quest"]


def send(world, conv, text):
    """One chat turn; any task it delegated is cancelled at once so no deep run is paid for."""
    dc = dev_world.client()
    res = TurnResult(dc.sse_send(conv, text, auto_run=world.auto_run))
    for task in res.task_ids:
        world.fx["task_ids"].append(task)
        dev_world.cancel_task(task)
    return res


def build(meter):  # noqa: ARG001  (this arm makes no model calls of its own; the backend does)
    """The arm's function. ``world.conv`` is the conversation ``dev_run`` opened for this case (one per
    case, so no case reads another's turns) after its prelude turns and before-hook."""
    def run_operation(message, world):
        res = send(world, world.conv, message)
        world.last = res
        return dict(reply=res.text, **res.facts())
    return run_operation
