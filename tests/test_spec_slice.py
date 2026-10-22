"""The slice of the specification a goal serves, and the meters that read its metric.

The slice is found with no model and no guessing: a clear solution, its next open milestone, the one metric that measures it.
The meters give that metric a number from the runner's own side: a recorded row while it is current, or an offline arm run
now, and never an arm that could spend money. A throwaway repository written by ``esoi init`` stands in for a real one, so
the records, the results log and the evaluation component are the real shapes. Offline: no model, no network.
"""
import csv
import subprocess
import threading
import time
from pathlib import Path

import pytest
from esoi import scaffold

from quest_ai_runner.core import spec_selection, spec_slice
from quest_ai_runner.core.metric_meter import (
    AFTER, BEFORE, OfflineArmMeter, Reading, RecordedRowMeter, meters_from_names, read_value, run_bounded,
    score_from_output)
from quest_ai_runner.core.spec_slice import (
    RepoSliceSource, SpecSlice, next_milestone, next_priority_block, parse_milestones, target_of)

GREETING = "make the greeting function greet a person by name in the standard form"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A scaffolded esoi repository whose one measured solution is greet-a-person (arm ``live``, recorded 2 of 2)."""
    root = tmp_path / "repo"
    scaffold.init(root, "demo_pkg", say=lambda *_: None)
    monkeypatch.setenv("HOME", str(tmp_path))   # the specification index cache lives under the home folder
    return root


def edit_milestones(root: Path, text: str) -> None:
    path = root / "esoi" / "solutions.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header, rows = reader.fieldnames, list(reader)
    for row in rows:
        if row["solution_id"] == "greet-a-person":
            row["milestones"] = text
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def slice_of(root: Path) -> SpecSlice:
    return SpecSlice(root=str(root), solution_id="greet-a-person", metric_id="greeting-pass-rate",
                     metric_command="esoi run greet_a_person --impl live")


# -- reading the milestones --------------------------------------------------------------------

def test_milestones_are_the_numbered_items_in_order():
    items = parse_milestones("1. First thing: 100%. 2. Second thing. 3. Third thing on 2026-10-19. 4 of them.")
    assert [(m.number, m.text) for m in items] == [
        (1, "First thing: 100%."), (2, "Second thing."), (3, "Third thing on 2026-10-19. 4 of them.")]


def test_an_unnumbered_cell_is_one_milestone_and_an_empty_one_is_none():
    assert [m.text for m in parse_milestones("Reach the target.")] == ["Reach the target."]
    assert parse_milestones("") == [] and next_milestone("   ") is None


def test_the_next_milestone_is_the_first_one_not_archived():
    cell = "1. Archived: an old goal. 2. (archived) another. 3. The real next one at 50%. 4. After that."
    step = next_milestone(cell)
    assert (step.number, step.text) == (3, "The real next one at 50%.")
    assert next_milestone("1. Archived: only one.") is None


def test_a_target_is_a_number_only_when_the_milestone_holds_exactly_one():
    assert target_of("Share of cases passed: 100%.") == (1.0, "100%")
    assert target_of("Latency under 2.5 seconds") == (2.5, "2.5")
    assert target_of("recorded for 3, then 10, then 30 runs") == (None, "")
    assert target_of("no number here") == (None, "")
    assert target_of("set on 2026-10-19") == (None, "")


# -- finding the slice -------------------------------------------------------------------------

def test_a_clear_request_gets_the_solution_its_next_milestone_and_its_metric(repo):
    found = RepoSliceSource().slice_for(GREETING, str(repo))
    assert (found.solution_id, found.metric_id, found.milestone_number) == ("greet-a-person", "greeting-pass-rate", 1)
    assert found.milestone.startswith("Share of sample names") and (found.target, found.target_text) == (1.0, "100%")
    assert found.metric_name == "Greeting pass rate" and found.metric_command == "esoi run greet_a_person --impl live"
    assert "root" not in found.to_dict()


def test_archived_milestones_are_skipped_in_the_repository_too(repo):
    edit_milestones(repo, "1. Archived: done long ago. 2. Greetings keep the name: 50%.")
    found = RepoSliceSource().slice_for(GREETING, str(repo))
    assert (found.milestone_number, found.target) == (2, 0.5)


def test_no_slice_outside_a_repository_with_records_or_without_a_folder(repo, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert RepoSliceSource().slice_for(GREETING, str(bare)) is None
    assert RepoSliceSource().slice_for(GREETING, None) is None   # no folder to look from: no guess about where


def test_an_unclear_or_unrelated_request_gets_no_slice_rather_than_a_wrong_one(repo):
    assert RepoSliceSource().slice_for("write a poem about the sea", str(repo)) is None
    assert RepoSliceSource().slice_for("", str(repo)) is None


def test_the_search_uses_no_model_and_never_raises(repo, monkeypatch):
    monkeypatch.setattr(spec_selection, "answer_with_reasoning",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a model was called")))
    assert RepoSliceSource().slice_for(GREETING, str(repo)) is not None
    monkeypatch.setattr(spec_slice.repo_specs, "repo_index", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
    assert RepoSliceSource().slice_for(GREETING, str(repo)) is None


def test_a_slow_lookup_is_given_up_on(repo, monkeypatch):
    monkeypatch.setattr(spec_slice, "slice_from_repo", lambda request, working_dir: time.sleep(5))
    started = time.monotonic()
    assert RepoSliceSource(timeout_seconds=0.2).slice_for(GREETING, str(repo)) is None
    assert time.monotonic() - started < 2


def test_the_brief_block_names_the_milestone_the_metric_and_the_tool(repo):
    block = next_priority_block(RepoSliceSource().slice_for(GREETING, str(repo)))
    assert block.startswith("NEXT PRIORITY") and "Share of sample names" in block
    assert "Greeting pass rate (metric_id greeting-pass-rate)" in block
    assert "record_metric_result" in block and "not_applicable" in block and "—" not in block
    assert len(block) < 700   # short: it rides in every brief of a repository that has records
    assert "none is set" in next_priority_block(SpecSlice(root="/x", solution_id="s", metric_id="m"))


# -- the meters --------------------------------------------------------------------------------

def test_the_recorded_row_meter_reads_the_latest_row_while_it_is_current(repo):
    assert RecordedRowMeter().measure(slice_of(repo)) == 1.0   # 2 of 2 cases passed


def test_a_row_recorded_on_other_cases_is_no_reading(repo):
    cases = repo / "evaluation" / "components" / "greet_a_person" / "cases.json"
    cases.write_text(cases.read_text(encoding="utf-8").replace("Ada", "Grace"), encoding="utf-8")
    assert RecordedRowMeter().measure(slice_of(repo)) is None


def test_a_metric_with_no_row_or_no_command_gives_none_without_raising(repo):
    assert RecordedRowMeter().measure(SpecSlice(root=str(repo), solution_id="s", metric_id="m")) is None
    assert RecordedRowMeter().measure(SpecSlice(root="/no/such/folder", solution_id="s", metric_id="m",
                                                metric_command="esoi run a --impl b")) is None


def test_the_offline_arm_meter_runs_an_arm_that_cannot_spend_and_reads_its_score(repo):
    assert OfflineArmMeter().measure(slice_of(repo)) == 1.0


def test_the_offline_arm_is_run_with_no_model_budget_and_no_credentials(repo, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "t")
    seen = {}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return "greet_a_person / live: 1/2 passed (dev 1/2, held-out 0/0)\n", None

    def fake_popen(argv, **kwargs):
        seen.update(argv=list(argv), **kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert OfflineArmMeter().measure(slice_of(repo)) == 0.5
    assert seen["argv"][-4:] == ["--impl", "live", "--max-calls", "0"]
    assert "OPENAI_API_KEY" not in seen["env"] and "SOME_SERVICE_TOKEN" not in seen["env"]
    assert seen["cwd"] == str(repo) and seen["start_new_session"] is True


def refuses(repo, monkeypatch):
    def popen_must_not_run(*args, **kwargs):
        raise AssertionError("an arm that may spend money was run")

    monkeypatch.setattr(subprocess, "Popen", popen_must_not_run)
    assert OfflineArmMeter().measure(slice_of(repo)) is None


def test_the_offline_arm_meter_refuses_an_arm_listed_as_paid(repo, monkeypatch):
    with open(repo / "esoi.toml", "a", encoding="utf-8") as fh:
        fh.write('paid_arms = ["greet_a_person/live"]\n')
    refuses(repo, monkeypatch)


def test_the_offline_arm_meter_refuses_an_arm_that_declares_itself_paid(repo, monkeypatch):
    impl = repo / "evaluation" / "components" / "greet_a_person" / "impls" / "live.py"
    impl.write_text("PAID = True\n" + impl.read_text(encoding="utf-8"), encoding="utf-8")
    refuses(repo, monkeypatch)


def test_the_offline_arm_meter_refuses_an_arm_whose_last_run_called_a_model(repo, monkeypatch):
    results = repo / "evaluation" / "components" / "results.csv"
    lines = results.read_text(encoding="utf-8").splitlines()
    fields = lines[1].split(",")
    fields[7], fields[14] = "5", "some-model"   # model_calls, model
    results.write_text("\n".join([lines[0], ",".join(fields), *lines[2:]]) + "\n", encoding="utf-8")
    refuses(repo, monkeypatch)


def test_the_offline_arm_meter_refuses_an_arm_with_no_recorded_run(repo, monkeypatch):
    (repo / "evaluation" / "components" / "results.csv").write_text(
        "date,component,impl,cases,passed,dev,heldout,model_calls,tokens_in,tokens_out,cost_usd,seconds,git_sha,cases_sha,model,details\n",
        encoding="utf-8")
    refuses(repo, monkeypatch)


def test_a_slow_arm_is_stopped_at_the_time_limit(repo):
    impl = repo / "evaluation" / "components" / "greet_a_person" / "impls" / "live.py"
    impl.write_text("import time\ntime.sleep(60)\n" + impl.read_text(encoding="utf-8"), encoding="utf-8")
    started = time.monotonic()
    assert OfflineArmMeter(timeout_seconds=1.5).measure(slice_of(repo)) is None
    assert time.monotonic() - started < 15


def test_the_score_is_what_the_leaderboard_ranks_by():
    assert score_from_output("x: 3/4 passed (dev ...)\nbalanced score 0.8123 = accuracy 0.75") == 0.8123
    assert score_from_output("greet / live: 3/4 passed (dev 3/4, held-out 0/0)") == 0.75
    assert score_from_output("") is None and score_from_output("usage: esoi run ...") is None


def test_the_first_meter_with_a_number_wins_and_unknown_names_are_left_out():
    class Fixed:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def measure(self, spec_slice, working_dir=None):
            return self.value

    found = SpecSlice(root="/x", solution_id="s")
    assert read_value([Fixed("a", None), Fixed("b", 0.4), Fixed("c", 0.9)], found) == Reading(0.4, "b")
    assert read_value([Fixed("a", None)], found) == Reading()
    assert [m.name for m in meters_from_names("offline_arm, nonsense ,recorded_row")] == ["offline_arm", "recorded_row"]


def test_after_the_work_the_arm_reads_what_a_current_looking_row_cannot_see(repo):
    """A change the row's staleness check does not see (here a non-git folder, in the repository a prose file the
    configuration leaves out of the measured code) leaves the row looking current: only a run of the arm sees it."""
    (repo / "demo_pkg" / "greet.py").write_text(
        (repo / "demo_pkg" / "greet.py").read_text(encoding="utf-8").replace('f"Hello, {name}!"', 'f"Hi {name}"'),
        encoding="utf-8")
    both = [RecordedRowMeter(), OfflineArmMeter()]
    assert RecordedRowMeter().measure(slice_of(repo)) == 1.0   # the premise: the row still reads as current
    assert read_value(both, slice_of(repo), when=BEFORE) == Reading(1.0, "recorded_row")
    assert read_value(both, slice_of(repo), when=AFTER) == Reading(0.5, "offline_arm")


def test_each_meter_declares_the_moments_it_can_answer():
    assert RecordedRowMeter.serves == (BEFORE,) and OfflineArmMeter.serves == (BEFORE, AFTER)


def test_a_reading_after_the_work_asks_only_the_meters_that_serve_it_and_a_missing_one_is_not_a_failure():
    class Row(Stub):
        serves = (BEFORE,)

    found = SpecSlice(root="/x", solution_id="s")
    row, arm = Row("row", 1.0), Stub("arm", 0.4, cheap=False)
    told = []
    assert read_value([row, arm], found, when=AFTER, announce=lambda: told.append(1)) == Reading(0.4, "arm")
    assert row.calls == 0 and told == [1]   # the row was never asked, and the wait is announced for the meter that runs
    assert read_value([row, arm], found, when=BEFORE).value == 1.0 and arm.calls == 1
    alone = Row("row", 1.0)
    assert read_value([alone], found, when=AFTER) == Reading() and alone.calls == 0   # no note: nothing ran, nothing failed


# -- a reading is bounded -------------------------------------------------------------------------

class Stub:
    """A meter that answers at once, or after ``seconds`` on the fake clock, or never (``stuck``)."""

    def __init__(self, name, value=None, *, cheap=True, advance=0.0, clock=None, stuck=None):
        self.name, self.value, self.cheap = name, value, cheap
        self.advance, self.clock, self.stuck = advance, clock, stuck
        self.calls = 0

    def measure(self, spec_slice, working_dir=None):
        self.calls += 1
        if self.clock is not None:
            self.clock["now"] += self.advance
        if self.stuck is not None:
            self.stuck.wait(30)
        return self.value


def test_the_whole_reading_has_one_budget_not_one_per_meter():
    clock = {"now": 0.0}
    first = Stub("first", None, cheap=False, advance=10.0, clock=clock)
    second = Stub("second", 0.9, cheap=False, clock=clock)
    told = []
    done = read_value([first, second], SpecSlice(root="/x", solution_id="s"), budget_seconds=8.0,
                      announce=lambda: told.append("measuring"), clock=lambda: clock["now"])
    assert done.value is None and second.calls == 0   # the budget was gone before the second meter could start
    assert "8s" in done.note and "second" in done.note
    assert told == ["measuring"]


def test_the_wait_is_announced_once_before_the_first_meter_that_takes_time_and_never_for_a_cheap_one():
    found = SpecSlice(root="/x", solution_id="s")
    told = []
    assert read_value([Stub("cheap", 0.4)], found, announce=lambda: told.append(1)).value == 0.4
    assert told == []
    both = [Stub("cheap", None), Stub("slow", None, cheap=False), Stub("slower", 0.7, cheap=False)]
    assert read_value(both, found, announce=lambda: told.append(1)).value == 0.7
    assert told == [1]   # once, before "slow" ran; the cheap one before it was silent


def test_a_meter_that_never_answers_is_left_behind_at_the_budget():
    stuck = threading.Event()
    started = time.monotonic()
    done = read_value([Stub("stuck", 0.5, cheap=False, stuck=stuck)], SpecSlice(root="/x", solution_id="s"),
                      budget_seconds=0.3)
    assert time.monotonic() - started < 2 and done.value is None
    assert done.note == "stuck did not finish within 0.3s"
    stuck.set()


def test_a_meter_that_raises_gives_no_value_and_the_next_one_is_tried():
    class Boom:
        name, cheap = "boom", True

        def measure(self, spec_slice, working_dir=None):
            raise RuntimeError("broken")

    done = read_value([Boom(), Stub("fine", 0.2)], SpecSlice(root="/x", solution_id="s"))
    assert done == Reading(0.2, "fine")


def test_run_bounded_reports_whether_the_call_finished():
    assert run_bounded(lambda: 7, 1.0) == (7, True)
    release = threading.Event()
    value, finished = run_bounded(lambda: release.wait(30), 0.1)
    assert value is None and finished is False
    release.set()


def test_a_slow_offline_arm_costs_the_budget_and_no_more(repo):
    impl = repo / "evaluation" / "components" / "greet_a_person" / "impls" / "live.py"
    impl.write_text("import time\ntime.sleep(60)\n" + impl.read_text(encoding="utf-8"), encoding="utf-8")
    announced = []
    started = time.monotonic()
    done = read_value([OfflineArmMeter()], slice_of(repo), budget_seconds=1.0, announce=lambda: announced.append(1))
    assert time.monotonic() - started < 6
    assert done.value is None and done.note == "offline_arm did not finish within 1s" and announced == [1]


def test_an_offline_arm_made_for_one_reading_carries_its_own_limit_and_the_shared_one_is_untouched():
    shared = OfflineArmMeter()
    own = shared.within(2.5)
    assert own.timeout_seconds == 2.5 and shared.timeout_seconds is None and own is not shared
    assert shared.cheap is False and RecordedRowMeter.cheap is True
