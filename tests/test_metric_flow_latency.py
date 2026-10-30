"""What measuring the metric adds before a deep worker starts, and after it ends: nothing silent, nothing unbounded.

A deep run used to sit quiet for minutes before it began, which is the failure this repository exists to remove. The selected
iterate-by-spec arm reads the metric around the work, so these tests pin the cost of that: with no specification records or
no known folder it reads nothing, announces nothing and waits for nothing; with records, a cheap reading is instant and a
slow one is announced, bounded by one budget, and given up on without holding the worker back. Offline: stub meters, no model.
"""
from __future__ import annotations

import threading
import time

import pytest

from esoi import spec_index
from esoi import spec_slice
from hubofhubs.ports.events import Mode
from quest_ai_runner.core.goal_iteration import SliceAndWriteback
from quest_ai_runner.core.orchestrator import PlanDecision
from quest_ai_runner.core.orchestrator_turn import _Emitter

from .test_goal_metrics_flow import GREETING, TASK, FixedMeter, Timeline, Worker, orchestrate, repo, world  # noqa: F401

LABEL = "Greeting pass rate"
GOAL_TEXT = GREETING   # the worker is handed the goal text; the timeline records it when the worker starts


class StuckMeter:
    """A meter that never answers until the test lets it go; it takes time, so it is announced."""
    name, cheap = "stuck", False

    def __init__(self):
        self.release = threading.Event()
        self.reads = 0

    def measure(self, spec_slice, working_dir=None):
        self.reads += 1
        self.release.wait(30)
        return None


class FlakyMeter:
    """Stuck on its first reading, answers on the second."""
    name, cheap = "flaky", False

    def __init__(self, answer):
        self.release = threading.Event()
        self.answer, self.reads = answer, 0

    def measure(self, spec_slice, working_dir=None):
        self.reads += 1
        if self.reads == 1:
            self.release.wait(30)
            return None
        return self.answer


def run_goal(orch, timeline):
    emit = _Emitter(timeline, Mode.LIVE, lambda text: None)
    plan = PlanDecision(action="deep", goal=GREETING, deep_brief=GREETING)
    return orch._run_deep(plan, GREETING, "sonnet", emit=emit, ctx_meta={"task_id": TASK}).deep_results[0]


def measuring(timeline):
    return [t for t in timeline.statuses() if t.startswith(("Checking the", "No starting value", "No result check"))]


# -- nothing to read: nothing paid ---------------------------------------------------------------------

def test_a_repository_without_specification_records_pays_nothing(world, tmp_path, monkeypatch):  # noqa: F811
    bare = tmp_path / "bare"
    bare.mkdir()
    meter = FixedMeter(0.5, 0.6)
    timeline = Timeline()
    worker = Worker(world.registry, [["change", ("na", "A note.")]], working_dir=str(bare), timeline=timeline)

    def index_must_not_be_built(*args, **kwargs):
        raise AssertionError("a repository with no records had an index built")

    monkeypatch.setattr(spec_index, "build", index_must_not_be_built)
    started = time.monotonic()
    done = run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[meter])), timeline)
    assert time.monotonic() - started < 2
    assert meter.reads == 0 and measuring(timeline) == []
    assert "NEXT PRIORITY" not in worker.calls[0]["brief"]
    assert [m["source"] for m in done.metric_results] == ["not_applicable"]   # only what the worker declared


def test_a_goal_whose_folder_is_not_known_pays_nothing_and_starts_no_thread(world, monkeypatch):  # noqa: F811
    class NoThreads:
        def __init__(self, *args, **kwargs):
            raise AssertionError("looking for a slice started a thread with no folder to look in")

    monkeypatch.setattr(spec_slice.threading, "Thread", NoThreads)
    meter = FixedMeter(0.5, 0.6)
    timeline = Timeline()
    worker = Worker(world.registry, [["change", ("na", "A note.")]], working_dir=None, timeline=timeline)
    done = run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[meter])), timeline)
    assert meter.reads == 0 and measuring(timeline) == [] and not done.metric_missing


def test_a_consumer_with_no_tool_registry_and_a_folder_with_no_records_sees_no_difference(world, tmp_path):  # noqa: F811
    """quest-backend's way: the brain built without an arm or a tool registry, the worker's folder holds no esoi.toml."""
    bare = tmp_path / "no_esoi_toml"
    bare.mkdir()
    worker = Worker(world.registry, [[]], working_dir=str(bare), landed=1, task_id=None)
    orch = orchestrate(world, worker)
    orch.tools = None
    timeline = Timeline()
    done = run_goal(orch, timeline)
    assert len(worker.calls) == 1 and "NEXT PRIORITY" not in worker.calls[0]["brief"]
    assert done.metric_results == [] and done.metric_missing is False   # no row, no gate, no flag
    assert done.output == "Changed the file." and measuring(timeline) == []


def test_the_default_arm_of_a_consumer_with_no_records_changes_nothing_in_the_brief_or_the_events(world):  # noqa: F811
    """The brain built without an arm (quest-backend's way) and a worker that has no folder: the run is as it was."""
    timeline = Timeline()
    worker = Worker(world.registry, [["change", ("na", "A note.")]], timeline=timeline)
    done = run_goal(orchestrate(world, worker), timeline)   # the arm the Orchestrator picks for itself
    assert measuring(timeline) == [] and "NEXT PRIORITY" not in worker.calls[0]["brief"]
    assert [m["source"] for m in done.metric_results] == ["not_applicable"] and not done.metric_missing


# -- something to read: cheap is instant, slow is announced and bounded -------------------------------------

def test_a_cheap_reading_is_instant_and_says_nothing(world, repo):  # noqa: F811
    timeline = Timeline()
    worker = Worker(world.registry, [["change"]], working_dir=str(repo), timeline=timeline)
    done = run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6, cheap=True)])),
                    timeline)
    assert measuring(timeline) == []
    assert [(m["before"], m["after"], m["source"]) for m in done.metric_results] == [(0.5, 0.6, "measured")]


def test_a_slow_reading_is_announced_before_it_runs_and_again_after_the_work(world, repo):  # noqa: F811
    timeline = Timeline()
    worker = Worker(world.registry, [["change"]], working_dir=str(repo), timeline=timeline)
    run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6)])), timeline)
    assert measuring(timeline) == ["Checking the starting value before any change.", "Checking the result after the change."]
    assert not [t for t in timeline.statuses() if LABEL in t], "the status never prints the metric's name"


def test_the_wait_is_announced_before_the_line_that_says_the_worker_is_starting(world, repo):  # noqa: F811
    """The last thing said before a worker spawns is that it is starting (``test_deep_pre_spawn_status``)."""
    timeline = Timeline()
    worker = Worker(world.registry, [["change"]], working_dir=str(repo), timeline=timeline)
    run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6)])), timeline)
    order = [e.text for e in timeline.events if e.type in ("status", "worker")]
    before = order.index("Checking the starting value before any change.")
    starting = next(i for i, t in enumerate(order) if t.startswith("Starting"))
    assert before < starting < order.index(GOAL_TEXT)


def test_a_meter_that_never_answers_does_not_hold_the_worker_back(world, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("QAR_METRIC_ARM_TIMEOUT_SECONDS", "0.3")
    stuck = StuckMeter()
    timeline = Timeline()
    worker = Worker(world.registry, [["change"], [("na", "Stuck meter; none recorded.")]], working_dir=str(repo),
                    timeline=timeline)
    started = time.monotonic()
    done = run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[stuck])), timeline)
    elapsed = time.monotonic() - started
    stuck.release.set()
    assert elapsed < 3   # two readings of 0.3s each, and the run itself
    assert stuck.reads == 2 and len(worker.calls) == 2   # the worker did start, and was asked, as without a meter
    said = measuring(timeline)
    assert "Checking the starting value before any change." in said
    assert "No starting value in time. Going on without it." in said
    assert not [t for t in said if "—" in t]
    assert [m["source"] for m in done.metric_results] == ["not_applicable"]   # no row without a number


def test_the_row_written_after_a_missed_reading_says_so(world, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("QAR_METRIC_ARM_TIMEOUT_SECONDS", "0.3")
    flaky = FlakyMeter(0.8)
    timeline = Timeline()
    worker = Worker(world.registry, [["change"]], working_dir=str(repo), timeline=timeline)
    done = run_goal(orchestrate(world, worker, iteration=SliceAndWriteback(meters=[flaky])), timeline)
    flaky.release.set()
    [entry] = done.metric_results
    assert (entry["before"], entry["after"], entry["source"]) == (None, 0.8, "measured")
    assert entry["reason"] == "measured by flaky; no reading before the change: flaky did not finish within 0.3s"
    assert not done.metric_missing   # a measured row is a record, even with one end missing


def test_the_added_seconds_before_the_worker_starts_are_the_lookup_and_one_cheap_reading(world, repo):  # noqa: F811
    """The scripted run, stub meter: how long the arm makes the worker wait, measured at the moment the worker starts."""
    timeline = Timeline()
    stamps = {}

    class Timed(Worker):
        def run_goal(self, **kwargs):
            stamps.setdefault("worker", time.monotonic())
            return super().run_goal(**kwargs)

    worker = Timed(world.registry, [["change"]], working_dir=str(repo), timeline=timeline)
    orch = orchestrate(world, worker, iteration=SliceAndWriteback(meters=[FixedMeter(0.5, 0.6, cheap=True)]))
    stamps["begin"] = time.monotonic()
    run_goal(orch, timeline)
    assert stamps["worker"] - stamps["begin"] < 1.0   # a real lookup over a real (scaffolded) repository, in well under a second
