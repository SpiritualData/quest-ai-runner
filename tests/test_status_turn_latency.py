"""A status-shaped turn reads the person's quests and goals (many round trips), and the screen must not wait on that to say what the turn understood or what it is waiting for.

Measured 2026-11-13 on a real turn ("What's the primary human blocker right now in 1000 subscribers project?", run from a quest folder): the line "Understood as" arrived 57 seconds
into the turn, because it was emitted only after the status snapshot was read, and the quest listing walked the teams one after another. Now the understanding is said as soon as it is
known, the listing reads together, and a wait of three seconds or more gets a row that names what the turn is on."""
import threading
import time
from types import SimpleNamespace

from hubofhubs.api import Mode

from .conftest import StubRetrieval
from .test_answer_explanation import RecordingSink
from .test_status_snapshot import Registry, StatusProvider, _tools
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import reachable_quest_rows
from quest_ai_runner.the_consciousness.terminal import opencode_terminal
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry


def test_the_understanding_is_said_before_the_status_snapshot_is_read():
    sink = RecordingSink()
    seen = []
    base = _tools()
    original = base.invoke

    def watching(name, args, ctx):
        if name in ("read_spec_records", "list_quest_goals") and not seen:
            seen.append("understanding" in sink.types())
        return original(name, args, ctx)
    base.invoke = watching
    provider = StatusProvider("Say where the quest stands.\n" + '{"asks_status": true}')
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), tools=base, config=OrchestratorConfig(overseer=True))
    orch.run("where are we with the big quest?", mode=Mode.LIVE, sink=sink, quest_id="q1", context_meta={"user_id": "u1"})
    assert seen == [True], "the snapshot was read before the turn said what it understood"


class SlowClient:
    def __init__(self, delay):
        self.delay, self.started = delay, []

    def list_my_quests(self):
        time.sleep(self.delay)
        return [{"quest_id": "own1", "state": {"name": "Mine", "outcome": "o"}}]

    def list_teams(self):
        return [{"team_id": "t1"}, {"team_id": "t2"}, {"team_id": "t3"}]

    def list_quests(self, *, team_id):
        time.sleep(self.delay)
        return [{"quest_id": f"{team_id}-q", "name": team_id, "outcome": "x"}, {"quest_id": "own1", "name": "dup"}]


def test_the_quest_listing_reads_the_teams_together_and_keeps_the_sequential_order_and_dedupe():
    began = time.monotonic()
    rows = reachable_quest_rows(SlowClient(0.4))
    took = time.monotonic() - began
    assert [r["quest_id"] for r in rows] == ["own1", "t1-q", "t2-q", "t3-q"]
    assert took < 1.0, f"four 0.4 second reads took {took:.1f}s: they still run one after another"


def test_a_wait_gets_a_row_naming_the_last_status_and_the_next_event_closes_it(monkeypatch):
    monkeypatch.setattr(opencode_terminal, "WAIT_ROW_AFTER_SECONDS", 0.05)
    monkeypatch.setattr(opencode_terminal, "WAIT_TICK_SECONDS", 0.02)
    sent = []
    harness = SimpleNamespace(owner=SimpleNamespace(say=lambda text: None))
    turn = opencode_terminal.Turn(harness, None, sent.append, time.monotonic())
    turn.status_text = "Reading where the quest stands…"
    done = threading.Event()
    thread = threading.Thread(target=turn.watch_waits, args=(done,), daemon=True)
    thread.start()
    time.sleep(0.3)
    running = [e for e in sent if getattr(e, "state", "") == "running"]
    assert len(running) == 1 and running[0].label == "Reading where the quest stands…"      # one row for one wait, not one per tick
    turn.close_wait()
    assert [getattr(e, "state", "") for e in sent] == ["running", "done"]
    done.set()
    thread.join(1)


def test_the_goal_pages_of_a_big_quest_are_read_together_in_order_and_a_failed_page_means_not_read():
    from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import status_snapshot
    page, total = status_snapshot.PAGE, status_snapshot.PAGE * 5 + 7
    lock, active, peak = threading.Lock(), [0], [0]

    class Paged:
        def get(self, name):
            return object()

        def invoke(self, name, args, ctx):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            start = args["offset"]
            if start == args.get("fail_at", -1):
                return SimpleNamespace(ok=False, data=[], text="")
            return SimpleNamespace(ok=True, data=[{"id": n} for n in range(start, min(total, start + args["limit"]))], text="")
    rows = status_snapshot.all_goal_rows(Paged(), "q1")
    assert [g["id"] for g in rows] == list(range(total)) and peak[0] > 1
    original = Paged.invoke
    Paged.invoke = lambda self, name, args, ctx: original(self, name, dict(args, fail_at=2 * page), ctx)
    assert status_snapshot.all_goal_rows(Paged(), "q1") is None
