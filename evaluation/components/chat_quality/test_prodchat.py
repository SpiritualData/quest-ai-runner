"""Offline tests of the production chat arm's guards, stream parsing and fingerprint (no network, no model)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prodchat as PC  # noqa: E402

BASE, EMAIL, UID = "https://api.prod.example.org", "ai@example.org", "u123"


@pytest.fixture(autouse=True)
def target(monkeypatch):
    for key, value in {"QAR_PROD_BASE_URL": BASE, "QAR_PROD_EXPECT_ACCOUNT": EMAIL, "QAR_PROD_EXPECT_USER_ID": UID, "QAR_PROD_QUEST_ID": "quest_x", "QAR_PROD_TEAM_ID": "team_x"}.items():
        monkeypatch.setenv(key, value)


def test_stream_parsing_keeps_order_skips_done_marker_and_counts_calls():
    lines = ['data: {"event": "start"}', "", 'data: {"event": "interim", "text": "Looking now"}', 'data: {"event": "tokens", "data": {"tokens_in": 10, "tokens_out": 2}}',
             "data: not json", 'data: {"event": "tokens", "data": {"tokens_in": 30, "tokens_out": 9}}', 'data: {"event": "tokens", "data": {"tokens_in": 30, "tokens_out": 9, "final": true}}',
             'data: {"event": "done", "content": "The answer"}', "data: [DONE]"]
    events = list(PC.parse_sse(lines))
    assert [PC.event_name(e) for e in events] == ["start", "interim", "tokens", "_unparsed", "tokens", "tokens", "done"]
    assert PC.usage_of(events) == {"model_calls": 2, "tokens_in": 30, "tokens_out": 9}
    assert [f["type"] for f in PC.frames_of(events)][1] == "interim"
    assert PC.route_of(events) == "inline"


def test_account_guard_refuses_any_other_account_or_base_url():
    assert PC.check_account({"user_id": UID, "email": EMAIL})["email"] == EMAIL
    for who in ({"user_id": "other", "email": EMAIL}, {"user_id": UID, "email": "someone@example.org"}, {}, {"error": "boom"}):
        with pytest.raises(PC.GuardRefusal):
            PC.check_account(who)
    PC.check_base_url(BASE + "/")
    for url in ("https://api.dev.example.org", "", BASE + ".evil.org"):
        with pytest.raises(PC.GuardRefusal):
            PC.check_base_url(url)


def test_the_client_makes_no_write_but_its_own_conversations():
    client = PC.ProdClient(BASE, "k")
    client.allow_write("POST", "/api/quest-ai/conversations")
    for method, path in (("POST", "/api/quest-ai/conversations/qaconv_1/messages/stream"), ("DELETE", "/api/quests/quest_x/notes/n1"), ("POST", "/api/quests/quest_x/notes")):
        with pytest.raises(PC.GuardRefusal):
            client.allow_write(method, path)
    client.own_conversations.add("qaconv_1")
    client.allow_write("POST", "/api/quest-ai/conversations/qaconv_1/messages/stream")
    with pytest.raises(PC.GuardRefusal):
        client.allow_write("POST", "/api/quest-ai/conversations/qaconv_2/stop")


def test_id_whitelist_refuses_other_ids_and_other_text(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text(json.dumps({"case_id": "BAD-02", "input_text": "approved text"}) + "\n")
    allowed = PC.load_allowed_cases(f)
    assert PC.check_case({"id": "BAD-02", "message": " approved text "}, allowed) == "approved text"
    for case in ({"id": "BAD-99", "message": "approved text"}, {"id": "BAD-02", "message": "edited text"}):
        with pytest.raises(PC.GuardRefusal):
            PC.check_case(case, allowed)


def reads(**over):
    base = {"notes": [{"note_id": "n1", "text": "a", "updated_at": "1"}], "goals": [{"id": "g1", "name": "G", "completed": False}], "workflows": [], "spec_records": [{"recordId": "r1", "revision": 1, "fields": {"a": 1}}],
            "tasks": [{"task_id": "t1", "title": "old"}], "decisions": [], "conversations": [{"id": "c1", "title": "old chat"}]}
    base.update(over)
    return base


def test_fingerprint_detects_an_unexpected_write_and_allows_an_expected_one():
    before = PC.fingerprint_from(reads())
    after = PC.fingerprint_from(reads(notes=[{"note_id": "n1", "text": "a", "updated_at": "1"}, {"note_id": "n2", "text": "new"}],
                                      conversations=[{"id": "c1"}, {"id": "own"}]))
    changes = PC.diff_fingerprints(before, after, own_conv="own")
    assert [(c["category"], c["change"], c["id"]) for c in changes] == [("notes", "added", "n2")]      # the run's own conversation is not a change
    lines = PC.unexpected_writes(changes, {"allow": []}, zero_writes=True)
    assert len(lines) == 1 and lines[0].startswith("UNEXPECTED WRITE: notes added n2")
    assert PC.unexpected_writes(changes, {"allow": [{"tool": "add_quest_note", "args": {}}]}) == []             # explained by meaning: a note tool allows a note
    assert PC.unexpected_writes(changes, {"allow": [{"tool": "set_goal_strategy_link"}]})                       # a goal tool does not explain a note
    assert PC.unexpected_writes(changes, {"allow": [{"tool": "add_quest_note"}]}, zero_writes=True)            # a zero-writes case explains nothing


def test_changed_removed_and_queued_items_are_all_seen():
    before = PC.fingerprint_from(reads())
    after = PC.fingerprint_from(reads(goals=[], spec_records=[{"recordId": "r1", "revision": 2, "fields": {"a": 2}}], tasks=[{"task_id": "t1"}, {"task_id": "t2", "conv_id": "own"}],
                                      decisions=[{"decision_id": "d1", "conv_id": "own"}]))
    kinds = {(c["category"], c["change"]) for c in PC.diff_fingerprints(before, after, own_conv="own")}
    assert kinds == {("goals", "removed"), ("spec_records", "changed"), ("tasks", "added"), ("decisions", "added")}
    assert PC.diff_fingerprints(before, before) == []


# ---- cleanup: nothing a run creates may need a human to undo

class FakeApi(PC.ProdClient):
    """The client with the network replaced by a record of the calls; ``fail`` names ids whose removal is refused."""
    def __init__(self, fail=()):
        super().__init__(BASE, "k")
        self.calls, self.fail = [], set(fail)

    def send_request(self, method, path, body=None, params=None, timeout=60):
        if method != "GET":
            self.allow_write(method, path)
            if any(f in path for f in self.fail):
                raise RuntimeError("403 forbidden")
        self.calls.append((method, path))
        return {}


T0 = PC.parse_time("2026-11-06T10:00:00")
WINDOW = (T0, T0 + 60)


def run_cleanup_for(fp_after, fail=(), effect=None, zero=True, own="own", evidence="", message=""):
    before = PC.fingerprint_from(reads(decisions=[{"decision_id": "d_old", "conv_id": "someone-else", "status": "open"}]))
    after = PC.fingerprint_from(fp_after)
    changes = PC.diff_fingerprints(before, after, own_conv=own)
    plan = PC.plan_cleanup(changes, own, WINDOW, UID, effect, zero, evidence, message)
    api = FakeApi(fail)
    return PC.run_cleanup(api, plan), api.calls


def test_a_created_ask_is_withdrawn_and_a_pre_existing_ask_is_untouched():
    result, calls = run_cleanup_for(reads(decisions=[{"decision_id": "d_old", "conv_id": "someone-else", "status": "open"},
                                                     {"decision_id": "d_new", "conv_id": "own", "kind": "blocker", "status": "open"}]))
    assert result["withdrawn"] == ["d_new"] and result["left"] == []
    assert calls == [("POST", "/api/teams/decisions/d_new/withdraw")]
    assert PC.cleanup_line(result) == "cleanup: withdrawn ['d_new'], deleted none, left none, kept (the case's intended result) none"


def test_a_duplicate_workflow_a_queued_task_and_the_runs_note_are_deleted_only_on_a_strong_match():
    result, calls = run_cleanup_for(reads(
        workflows=[{"workflowId": "wf_dup", "createdAt": "2026-11-06T10:00:09"}, {"workflowId": "wf_old_new", "createdAt": "2026-11-01T00:00:00"}],
        tasks=[{"task_id": "t1", "title": "old"}, {"task_id": "t_mine", "conv_id": "own"}, {"task_id": "t_other", "conv_id": "another"}],
        notes=[{"note_id": "n1", "text": "a", "updated_at": "1"},
               {"note_id": "n_mine", "text": "Instagram caption about subscribers growth", "author_id": UID, "created_at": "2026-11-06T10:00:07"},
               {"note_id": "n_human", "text": "Instagram caption about subscribers growth", "author_id": "human", "created_at": "2026-11-06T10:00:07"}]),
        evidence='{"event": "exec", "output": "created workflow wf_dup"}', message="Write an Instagram caption about subscribers growth")
    assert sorted(result["deleted"]) == ["n_mine", "t_mine", "wf_dup"]
    assert sorted(c[1] for c in calls) == ["/api/assistant-tasks/t_mine", "/api/quests/quest_x/notes/n_mine", "/api/workflows/wf_dup"]
    assert not any("t_mine" in x or "n_mine" in x or "wf_dup" in x for x in result["left"])      # the other actors' items are left alone, and reported


def test_a_note_in_the_window_that_is_not_tied_to_the_turn_is_ambiguous_left_and_never_deleted():
    result, calls = run_cleanup_for(reads(notes=[{"note_id": "n1", "text": "a", "updated_at": "1"},
                                                  {"note_id": "n_auto", "text": "Weekly KPI baseline refreshed", "author_id": UID, "created_at": "2026-11-06T10:00:07"}],
                                          workflows=[{"workflowId": "wf_x", "createdAt": "2026-11-06T10:00:08"}]),
                                    evidence="{}", message="Write an Instagram caption about subscribers growth")
    assert calls == [] and result["deleted"] == []
    assert len(result["left"]) == 2 and all("AMBIGUOUS" in x for x in result["left"])      # a workflow has no author, so only its id in the turn's frames ties it
    outcome = {"cleanup": result, "line": PC.cleanup_line(result), "unexpected": [], "failed": True}
    import prodsafety as PS
    record = PS.TurnSafety(FakeApi(), UID, "BAD-X", {"allow": []}, True, "m", say=lambda *_: None).apply({"passed": True}, outcome)
    assert record["passed"] is False and "AMBIGUOUS" in record["lane_gaps"][0]


AUTOPILOT = {"cadence": "daily", "run_time": "14:30", "run_timezone": "America/Los_Angeles"}
NOON_PT = PC.local_epoch("2026-11-06", "12:00", "America/Los_Angeles")


def test_autopilot_window_blocks_when_a_run_is_active_queued_unscheduled_or_due_soon():
    assert PC.autopilot_window(NOON_PT, AUTOPILOT, []) is None
    assert PC.autopilot_window(NOON_PT, AUTOPILOT, [{"task_id": "a", "status": "done"}, {"task_id": "b", "status": "needs_you"}]) is None
    assert "running" in PC.autopilot_window(NOON_PT, AUTOPILOT, [{"task_id": "a", "status": "running"}])
    assert "runnable now" in PC.autopilot_window(NOON_PT, AUTOPILOT, [{"task_id": "a", "status": "queued"}])
    assert PC.autopilot_window(NOON_PT, AUTOPILOT, [{"task_id": "a", "status": "queued", "scheduled_date": "2026-11-06", "scheduled_time": "12:05"}])
    assert PC.autopilot_window(NOON_PT, AUTOPILOT, [{"task_id": "a", "status": "queued", "scheduled_date": "2026-11-07", "scheduled_time": "12:05"}]) is None
    at_0528_utc = PC.local_epoch("2026-11-06", "21:28", "America/Los_Angeles")      # 05:28 UTC next day; a task for 05:00 in the quest's zone is hours away, not overdue
    assert PC.autopilot_window(at_0528_utc, AUTOPILOT, [{"task_id": "a", "status": "queued", "scheduled_date": "2026-11-07", "scheduled_time": "05:00"}]) is None
    assert PC.autopilot_window(PC.local_epoch("2026-11-06", "14:25", "America/Los_Angeles"), AUTOPILOT, [])      # the daily run time is five minutes away
    assert PC.autopilot_window(PC.local_epoch("2026-11-06", "14:35", "America/Los_Angeles"), AUTOPILOT, [])      # and just began


class ClockApi(FakeApi):
    def __init__(self, busy_reads):
        super().__init__()
        self.busy_reads = busy_reads

    def send_request(self, method, path, body=None, params=None, timeout=60):
        if path.endswith("/state"):
            return {"state": {"autopilot": AUTOPILOT}}
        if path == "/api/assistant-tasks":
            self.busy_reads -= 1
            return {"tasks": [{"task_id": "a", "status": "running"}] if self.busy_reads >= 0 else []}
        return super().send_request(method, path, body, params, timeout)


def test_a_write_capable_case_waits_for_the_window_then_runs_or_is_skipped_as_an_autopilot_window():
    import prodsafety as PS
    sleeps = []
    clock = [NOON_PT]
    def sleep(n):
        sleeps.append(n)
        clock[0] += n
    safe = PS.TurnSafety(ClockApi(busy_reads=2), UID, "M-01", {"allow": [{"tool": "x"}]}, False, "m", clock=lambda: clock[0], sleep=sleep, say=lambda *_: None)
    assert safe.gate() is None and sleeps == [30, 30]                  # busy twice, then clear
    stuck = PS.TurnSafety(ClockApi(busy_reads=10 ** 6), UID, "M-01", {"allow": [{"tool": "x"}]}, False, "m", clock=lambda: clock[0], sleep=sleep, say=lambda *_: None)
    assert "autopilot window" in stuck.gate() and sum(sleeps) >= 900   # waited the full 15 minutes, then skipped
    zero = PS.TurnSafety(ClockApi(busy_reads=10 ** 6), UID, "BAD-02", {"allow": []}, True, "m", clock=lambda: clock[0], sleep=sleep, say=lambda *_: None)
    assert zero.gate() is None                                          # a zero-writes case does not wait


def test_an_item_that_cannot_be_removed_is_left_and_a_change_that_cannot_be_undone_is_left():
    result, _ = run_cleanup_for(reads(decisions=[{"decision_id": "d_old", "conv_id": "someone-else"}, {"decision_id": "d_new", "conv_id": "own"}],
                                      spec_records=[{"recordId": "r1", "revision": 2, "fields": {"a": 2}}]), fail=("d_new",))
    assert result["withdrawn"] == [] and len(result["left"]) == 2        # the refused withdrawal and the changed spec record
    assert any("removal failed" in x for x in result["left"]) and any("spec_records" in x for x in result["left"])


def test_the_intended_result_of_a_case_is_kept_not_removed():
    result, calls = run_cleanup_for(reads(notes=[{"note_id": "n1", "text": "a", "updated_at": "1"}, {"note_id": "n_new", "author_id": UID, "created_at": "2026-11-06T10:00:07"}]),
                                    effect={"allow": [{"tool": "add_quest_note"}]}, zero=False)
    assert result["kept"] == ["n_new"] and calls == []


def test_the_client_refuses_to_undo_anything_it_was_not_told_this_run_created():
    api = FakeApi()
    for method, path in (("POST", "/api/teams/decisions/d_old/withdraw"), ("DELETE", "/api/workflows/wf_old"), ("DELETE", "/api/quests/quest_x/notes/n1")):
        with pytest.raises(PC.GuardRefusal):
            api.send_request(method, path)
    assert "undo" in PC.describe_undo({"allow": [{"tool": "add_quest_note"}]}, False) or "taken back" in PC.describe_undo(None, True)


class Changing(FakeApi):
    """GET /decisions/for-quest answers nothing before the turn and one ask after it (what a lane arm sees: no conversation id, the AI account as creator)."""
    def __init__(self, ask):
        super().__init__()
        self.ask, self.reads = ask, 0

    def send_request(self, method, path, body=None, params=None, timeout=60):
        if method == "GET" and path == "/api/teams/decisions/for-quest":
            self.reads += 1
            return [self.ask] if self.reads > 1 else []
        return super().send_request(method, path, body, params, timeout)


def test_the_lane_arms_share_the_safety_net_an_ask_the_lane_run_raised_is_withdrawn_and_the_case_fails():
    import prodsafety as PS
    clock = iter([T0, T0 + 20, T0 + 21])
    ask = {"decision_id": "d_lane", "created_by": UID, "created_at": "2026-11-06T10:00:10", "kind": "blocker", "summary": "Should the post invite people", "status": "open"}
    api = Changing(ask)
    safe = PS.TurnSafety(api, UID, "BAD-07", {"allow": []}, True, "Write a caption", clock=lambda: next(clock), say=lambda *_: None)
    safe.begin()
    outcome = safe.finish(own_conv=None, frames=[{"type": "done", "content": "created ask d_lane"}])
    assert outcome["cleanup"]["withdrawn"] == ["d_lane"] and outcome["failed"] and outcome["unexpected"]      # taken back, and still a failure of the case
    assert ("POST", "/api/teams/decisions/d_lane/withdraw") in api.calls


# ---- rate limits, the cross-process lock, and an allowed call that changed nothing

import io
import urllib.error


def http_429(retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("u", 429, "Too many requests", headers, io.BytesIO(b"slow down"))


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_429_honours_retry_after_then_backs_off_with_jitter_and_gives_up_as_rate_limited(monkeypatch):
    answers = [http_429(7), http_429(None), FakeResponse('{"ok": 1}')]
    monkeypatch.setattr(PC.urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(answers.pop(0)) if isinstance(answers[0], Exception) else answers.pop(0))
    api = PC.ProdClient(BASE, "k")
    slept = []
    api.sleep, api.jitter = slept.append, lambda: 1.0
    assert api.send_request("GET", "/x") == {"ok": 1}
    assert slept[0] == 7 * 1.25 and slept[1] == 4 * 1.25              # the server's Retry-After first, then 2^(attempt+1), each with 25 percent jitter
    monkeypatch.setattr(PC.urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(http_429(40)))
    slept.clear()
    with pytest.raises(PC.RateLimited):
        api.send_request("GET", "/x")
    assert sum(slept) <= PC.MAX_BACKOFF + 1                             # never waits past the 90 second budget


def test_the_fingerprint_lock_serialises_readers(tmp_path):
    with PC.fingerprint_lock(str(tmp_path)):
        with pytest.raises(BlockingIOError):
            with PC.fingerprint_lock(str(tmp_path), blocking=False):
                pass
    with PC.fingerprint_lock(str(tmp_path), blocking=False):            # released: the next reader gets it
        pass


def test_a_rate_limited_read_is_retried_once_after_waiting_and_a_second_429_is_not_run_rate_limited():
    import prodsafety as PS
    calls, slept = [], []
    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise PC.RateLimited("429")
        return "read"
    safe = PS.TurnSafety(FakeApi(), UID, "M-08", {"allow": []}, True, "m", sleep=slept.append, say=lambda *_: None)
    assert safe.retry_once(flaky) == "read" and slept == [PS.TurnSafety.RATE_WAIT]
    def always():
        raise PC.RateLimited("429")
    with pytest.raises(PC.RateLimited):
        safe.retry_once(always)


class NoChange(FakeApi):
    pass


def test_an_allowed_call_that_created_nothing_shows_no_change_and_no_cleanup():
    """BAD-03: the lane's tool call was allowed to run (a DONE line) but the tool refused the duplicate; the before/after read is the truth."""
    import prodsafety as PS
    clock = iter([T0, T0 + 20])
    safe = PS.TurnSafety(NoChange(), UID, "BAD-03", {"allow": [{"tool": "create_quest_workflow"}]}, False, "m", clock=lambda: next(clock), say=lambda *_: None)
    safe.begin()
    outcome = safe.finish(own_conv=None, frames=[])
    assert outcome["changes"] == [] and outcome["failed"] is False and outcome["cleanup"] == {"withdrawn": [], "deleted": [], "left": [], "kept": []}


def test_an_extra_workflow_the_run_created_is_deleted_and_named_in_the_cleanup_line_before_the_state_is_counted():
    result, calls = run_cleanup_for(reads(workflows=[{"workflowId": "wf_extra", "createdAt": "2026-11-06T10:00:09"}]),
                                    evidence='{"output": "created wf_extra"}', message="Set a weekly check")
    assert result["deleted"] == ["wf_extra"] and "deleted ['wf_extra']" in PC.cleanup_line(result)


def test_a_changed_person_goal_is_an_unexpected_write_even_when_the_case_allows_goal_writes():
    import prodchat as PC
    effect = {"allow": [{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title~": "P3 "}}]}
    before = {c: {} for c in PC.CATEGORIES}
    before["goals"] = {"h1": {"digest": "a", "label": "Submit the Quest app to Google Play"}, "k1": {"digest": "a", "label": "P3 next step: week of 2026-11-02"}}
    after = {c: {} for c in PC.CATEGORIES}
    after["goals"] = {"h1": {"digest": "b", "label": "Submit the Quest app to Google Play"}, "k1": {"digest": "b", "label": "P3 Mobile app next step: week of 2026-11-09"},
                      "n1": {"digest": "c", "label": "P3 Mobile app next step: week of 2026-11-16"}}
    lines = PC.unexpected_writes(PC.diff_fingerprints(before, after), effect)
    assert len(lines) == 1 and "goals changed h1" in lines[0] and "a goal the case may not change" in lines[0]
    # the keyed goal updated in place under its exact call title is explained too
    effect_no_title = {"allow": [{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1"}}]}
    before["goals"] = {"x": {"digest": "a", "label": "FB push post A"}}
    after["goals"] = {"x": {"digest": "b", "label": "FB push post A"}}
    assert PC.unexpected_writes(PC.diff_fingerprints(before, after), effect_no_title) != []
    assert PC.unexpected_writes(PC.diff_fingerprints(before, after), effect_no_title, titles={"FB push post A"}) != []       # a title equal to the call's is no reason to change a goal
    after["goals"] = {"x": {"digest": "b", "label": "FB push post A", "kept": True}}
    before["goals"] = {"x": {"digest": "a", "label": "FB push post A", "kept": True}}
    assert PC.unexpected_writes(PC.diff_fingerprints(before, after), effect_no_title) == []          # a goal Quest AI keeps (marked) may be updated
    assert PC.unexpected_writes(PC.diff_fingerprints(before, after), effect_no_title, zero_writes=True) != []


def test_a_goal_the_case_allows_updated_in_place_is_the_intended_result_and_is_not_left_behind():
    import prodchat as PC
    effect = {"allow": [{"tool": "create_or_update_quest_goal", "args": {"quest_id": "q1", "title~": "P2 "}}]}
    changes = [{"category": "goals", "change": "changed", "id": "g1", "label": "P2 Mobile app next step: week of 2026-11-09", "before_label": "P2 Mobile app next step: week of 2026-11-02"},
               {"category": "goals", "change": "changed", "id": "g2", "label": "Submit the app", "before_label": "Submit the app"}]
    plan = PC.plan_cleanup(changes, "conv", (0, 1), "ai", effect, False, "", "", ())
    assert [c["id"] for c in plan["keep"]] == ["g1"] and [c["id"] for c in plan["left"]] == ["g2"]


# ---- concurrent writes by the same account (in-process arm only)

def test_an_addition_no_executed_write_could_have_made_is_concurrent_and_the_http_arm_stays_strict():
    added = [{"category": "tasks", "change": "added", "id": "t1", "label": "Run autopilot"}, {"category": "conversations", "change": "added", "id": "c1", "label": "hi"},
             {"category": "notes", "change": "added", "id": "n1", "label": "x"}, {"category": "goals", "change": "changed", "id": "g1", "label": "g"}]
    mine, other = PC.split_concurrent(added, [])
    assert [c["id"] for c in other] == ["t1", "c1", "n1"] and [c["id"] for c in mine] == ["g1"]      # a changed item is never called concurrent
    mine, other = PC.split_concurrent(added, ["tool add_quest_note {}"])
    assert [c["id"] for c in other] == ["t1", "c1"] and "n1" in [c["id"] for c in mine]
    mine, other = PC.split_concurrent(added, ["quest api POST /api/assistant-tasks"])
    assert [c["id"] for c in other] == ["c1", "n1"]                          # a task write does not reach decisions or notes
    assert PC.split_concurrent(added, None) == (added, [])                  # the HTTP arm cannot see the turn's calls
    assert all(l.startswith("CONCURRENT WRITE") for l in PC.concurrent_lines(other))


def test_the_safety_net_reports_concurrent_writes_apart_leaves_them_and_does_not_fail_the_case():
    import prodsafety as PS
    clock = iter([T0, T0 + 20, T0 + 21])
    ask = {"decision_id": "d_other", "created_by": UID, "created_at": "2026-11-06T10:00:10", "kind": "blocker", "summary": "Another lane's ask", "status": "open"}
    api = Changing(ask)
    safe = PS.TurnSafety(api, UID, "X-1", {"allow": []}, True, "Write a caption", clock=lambda: next(clock), say=lambda *_: None)
    safe.begin()
    outcome = safe.finish(own_conv=None, frames=[], executed=[])
    assert outcome["unexpected"] == [] and outcome["failed"] is False and len(outcome["concurrent"]) == 1
    assert not any(c[0] != "GET" for c in api.calls)                         # nothing was taken back
    record = safe.apply({"passed": True}, outcome)
    assert record["passed"] is True and record["concurrent_writes"] == outcome["concurrent"] and record["unexpected_writes"] == []


HISTORY = [{"role": "user", "text": "first question"}, {"role": "assistant", "text": "first reply"}]


def test_a_cases_prior_turns_become_session_pairs_and_the_chat_arm_sends_only_the_approved_user_turns(tmp_path):
    import prod_probes as P
    case = {"id": "X-1", "message": "second question", "history": HISTORY + [{"role": "user", "text": "dangling"}]}
    assert P.history_turns(case)[0] == ("user", "first question")
    assert P.history_pairs(case) == [("first question", "first reply"), ("dangling", "")]
    assert P.history_pairs({"message": "x"}) == []
    file = tmp_path / "cases.jsonl"
    file.write_text(json.dumps({"case_id": "X-1", "input_text": "second question", "history": HISTORY}) + "\n" + json.dumps({"case_id": "X-2", "input_text": "z"}) + "\n")
    approved = PC.load_allowed_histories(str(file))
    assert PC.check_history({"id": "X-1", "history": HISTORY}, approved) == ["first question"]
    assert PC.check_history({"id": "X-2"}, approved) == []
    with pytest.raises(PC.GuardRefusal):
        PC.check_history({"id": "X-1", "history": [{"role": "user", "text": "something else"}]}, approved)
    sent = []
    client = PC.ProdClient.__new__(PC.ProdClient)
    client.stream_turn = lambda conv, text, started, **kw: sent.append((conv, text)) or iter(())
    client.seed_history("c1", ["first question"], 0.0)
    assert sent == [("c1", "first question")]
