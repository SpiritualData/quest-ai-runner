"""The What You Asked section of an autopilot batch, and the QuestClient calls the ask tools make.

Pinned: open asks are listed with their keys and the closed ones are not; a standing rule in force
is shown as a rule; an unsorted ask is marked so the run sorts it; an unreadable ledger emits
nothing; what already waits on a person is listed so it is not raised twice; the ledger is read
once per quest per pass; and record_ask / update_ask reach the Quest API as the request shapes the
backend's POST /api/asks and PATCH /api/asks/{key} (by_run) expect, driven through the real tools.
"""
from datetime import datetime, timezone

from quest_ai_runner.core import tools_quest_ops as ops
from quest_ai_runner.core.tools import ToolContext
from quest_ai_runner.runner.autopilot import AutopilotPass
from quest_ai_runner.runner.autopilot_briefs import compose_batch_text
from quest_ai_runner.runner.quest_asks_brief import asks_brief_section, fetch_asks
from quest_ai_runner.runner.quest_client import QuestClient

from .test_autopilot import FakeAutopilotClient, _goal, _goals_payload, _now, _quest

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

ASKS = [
    {"key": "k_open", "state": "open", "kind": "request", "author": "Zee",
     "occurred_at": "2026-10-12T12:00:00Z", "text": "Send the board the Q3 numbers"},
    {"key": "k_new", "state": "open", "kind": "unknown", "author": "Ana",
     "occurred_at": "2026-10-15T09:00:00Z", "text": "Can we also try a podcast?"},
    {"key": "k_prog", "state": "in progress", "kind": "request", "author": "Zee",
     "text": "Draft the grant letter"},
    {"key": "k_done", "state": "awaiting acceptance", "kind": "request", "text": "Fix the logo"},
    {"key": "k_acc", "state": "accepted", "kind": "request", "text": "Old finished thing"},
    {"key": "k_rule", "state": "in force", "kind": "standing", "text": "Always cc Dana"},
]


def test_open_asks_are_listed_with_keys_and_closed_ones_are_not():
    text = asks_brief_section(ASKS, now=NOW)
    assert text.startswith("WHAT YOU ASKED")
    assert "- k_open [open, request] Zee, 3d ago: Send the board the Q3 numbers" in text
    assert "k_prog [in progress, request]" in text
    for closed in ("k_done", "k_acc", "Fix the logo", "Old finished thing"):
        assert closed not in text


def test_an_unsorted_ask_is_marked_and_a_standing_rule_is_shown_as_a_rule():
    text = asks_brief_section(ASKS, now=NOW)
    assert "k_new [open, NOT SORTED] Ana, today:" in text
    assert "update_ask (kind)" in text
    assert "Standing rules in force" in text and "- k_rule Always cc Dana" in text


def test_an_unreadable_or_empty_ledger_emits_nothing():
    assert asks_brief_section(None, [{"kind": "approve", "summary": "x"}]) == ""
    assert asks_brief_section([], now=NOW) == ""
    assert asks_brief_section([ASKS[3], ASKS[4]], now=NOW) == ""

    class Boom:
        def list_asks(self, **kw):
            raise RuntimeError("down")

    assert fetch_asks(Boom(), "q1") is None
    assert fetch_asks(object(), "q1") is None


def test_what_already_waits_on_a_person_is_listed_so_it_is_not_raised_twice():
    waiting = [{"kind": "approve", "summary": "Approve the $50 ad test?"}]
    text = asks_brief_section(ASKS[:1], waiting, now=NOW)
    assert "Already waiting on a person (do not raise these again):" in text
    assert "- [approve] Approve the $50 ad test?" in text
    assert "Already waiting" not in asks_brief_section(ASKS[:1], [], now=NOW)
    assert "Already waiting" not in asks_brief_section(ASKS[:1], None, now=NOW)


def test_the_section_rides_in_the_batch_text_and_changes_nothing_when_absent():
    base = compose_batch_text("Grow")
    assert compose_batch_text("Grow", asked=None) == base
    assert "WHAT YOU ASKED" in compose_batch_text("Grow", asked="WHAT YOU ASKED\n- k1")


def pass_client(**kw):
    q1 = _quest("q1", mode="act")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    return FakeAutopilotClient(quests=[q1], goals_by_quest=goals, **kw)


def test_a_pass_puts_the_open_asks_in_the_batch_and_reads_the_ledger_once():
    client = pass_client(asks_by_quest={"q1": ASKS})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "WHAT YOU ASKED" in text and "k_open" in text and "Old finished thing" not in text
    assert client.list_asks_calls == ["q1"]


def test_a_failed_read_leaves_the_batch_without_the_section():
    client = pass_client(list_asks_fails_for={"q1"})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert "WHAT YOU ASKED" not in client.created_tasks[0]["text"]


# --- the QuestClient calls, driven through the real tools ---------------------------------

def make_client():
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    sent = []

    def fake_request(method, path, *, params=None, body=None):
        sent.append((method, path, body))
        if method == "GET":
            return {"items": [{"key": "k9", "state": "open", "kind": "unknown", "text": "Send Ana the deck"}]}
        return {"key": "k9", "state": "awaiting acceptance" if (body or {}).get("state") == "done" else "open",
                "kind": (body or {}).get("kind") or "request", "text": "Send Ana the deck"}

    client._request = fake_request  # type: ignore[assignment]
    return client, sent


def call(spec, client, args):
    ctx = ToolContext(quest_id="q1")
    return spec({}, client_factory=lambda: client).handler(args, ctx)


def test_record_ask_sends_the_post_the_backend_expects():
    client, sent = make_client()
    result = call(ops.record_ask_spec, client,
                  {"text": "Send Ana the deck", "author": "ana@example.org", "kind": "request"})
    assert result.ok
    assert sent == [("POST", "/api/asks", {
        "quest_id": "q1", "text": "Send Ana the deck", "source": "ai_recorded",
        "author_id": "ana@example.org", "kind": "request"})]


def test_update_ask_sends_a_run_claim_and_never_an_acceptance():
    client, sent = make_client()
    done = call(ops.update_ask_spec, client, {"ask": "k9", "state": "done", "note": "Sent the deck"})
    assert done.ok and "can accept it" in done.text
    assert sent[-1] == ("PATCH", "/api/asks/k9",
                        {"by_run": True, "state": "done", "note": "Sent the deck"})

    sorted_ask = call(ops.update_ask_spec, client, {"ask": "k9", "kind": "standing"})
    assert sorted_ask.ok
    assert sent[-1] == ("PATCH", "/api/asks/k9", {"by_run": True, "kind": "standing"})

    before = len(sent)
    assert not call(ops.update_ask_spec, client, {"ask": "k9", "state": "accepted"}).ok
    assert len(sent) == before


def test_the_client_methods_return_empty_on_failure_and_omit_unset_fields():
    from quest_ai_runner.runner.quest_client_support import QuestApiError
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    sent = []

    def refuse(method, path, *, params=None, body=None):
        sent.append(body)
        raise QuestApiError("not found")

    client._request = refuse  # type: ignore[assignment]
    assert client.record_ask("q1", "words") == {}
    assert client.update_ask("k1", "done") == {}
    assert sent == [{"quest_id": "q1", "text": "words", "source": "ai_recorded"},
                    {"by_run": True, "state": "done"}]


# --- answers and notes come back to the next run -------------------------------------------

def entry(state, by, note, at="2026-10-14T10:00:00Z"):
    return {"at": at, "state": state, "by": by, "note": note}


ANSWERED = {
    "key": "k_ans", "state": "open", "kind": "request", "author": "Zee", "text": "Write up the numbers",
    "occurred_at": "2026-10-10T12:00:00Z", "last_note": "US only, please.",
    "history": [entry("awaiting answer", "run", "Only the US, or every region?"),
                entry("open", "person", "US only, please.")],
}
PLAIN_NOTE = {
    "key": "k_note", "state": "open", "kind": "request", "text": "Fix the logo",
    "history": [entry("open", "person", "Make it blue, not green")],
}
MID_WAY = {
    "key": "k_mid", "state": "in progress", "kind": "request", "text": "Draft the grant letter",
    "last_note": "Intro and budget done", "history": [entry("in progress", "run", "Intro and budget done")],
}
STUCK = {
    "key": "k_stuck", "state": "blocked", "kind": "request", "text": "Post to the blog",
    "history": [entry("blocked", "run", "No login for the blog")],
}
WAITING = {
    "key": "k_wait", "state": "awaiting answer", "kind": "request", "text": "Pick the venue",
    "last_note": "Which city?", "decision_id": "teamdec_1",
    "history": [entry("awaiting answer", "run", "Which city?")],
}


def test_an_answer_is_shown_with_the_question_it_answers():
    text = asks_brief_section([ANSWERED], now=NOW)
    assert "    You asked: Only the US, or every region?" in text
    assert "    Their answer: US only, please." in text
    assert "act on it first" in text


def test_a_plain_note_from_the_person_is_shown_without_a_question():
    text = asks_brief_section([PLAIN_NOTE], now=NOW)
    assert "    Their note: Make it blue, not green" in text
    assert "You asked" not in text


def test_the_runs_own_last_note_shows_on_asks_in_progress_or_blocked_only():
    text = asks_brief_section([MID_WAY, STUCK, ASKS[0]], now=NOW)
    assert "    Your last note: Intro and budget done" in text
    assert "    Your last note: No login for the blog" in text
    assert text.count("Your last note") == 2
    done = dict(MID_WAY, state="open")
    assert "Your last note" not in asks_brief_section([done], now=NOW)


def test_an_unanswered_question_is_shown_and_the_run_is_told_not_to_work_or_reask():
    text = asks_brief_section([WAITING], now=NOW)
    assert "    You asked: Which city? (still waiting on their answer)" in text
    assert "do not work that ask or ask again until it is answered" in text


def test_answers_and_notes_are_cut_and_budgeted():
    long_answer = dict(ANSWERED, history=[entry("awaiting answer", "run", "Q" * 500),
                                          entry("open", "person", "A" * 900)])
    text = asks_brief_section([long_answer], now=NOW)
    assert "A" * 400 not in text and "Q" * 200 not in text and "..." in text

    many = [dict(PLAIN_NOTE, key=f"k{i}", history=[entry("open", "person", f"note {i} " + "x" * 300)])
            for i in range(12)]
    section = asks_brief_section(many, now=NOW, max_chars=20000)
    shown = section.count("Their note:")
    assert 3 <= shown < 12, "the answers budget stops notes before every ask has one"
    assert section.count("- k") == 12, "every ask is still listed"


def test_an_ask_with_no_history_renders_as_before():
    assert "Their" not in asks_brief_section(ASKS[:2], now=NOW)
    assert "Your last note" not in asks_brief_section(ASKS[:3], now=NOW)


def test_update_ask_awaiting_answer_needs_a_question_and_sends_it():
    client, sent = make_client()
    before = len(sent)
    refused = call(ops.update_ask_spec, client, {"ask": "k9", "state": "awaiting_answer"})
    assert not refused.ok and "question" in refused.text and "Asks for You" in refused.text
    assert len(sent) == before, "nothing is sent without the question"

    asked = call(ops.update_ask_spec, client, {
        "ask": "k9", "state": "awaiting_answer", "question": "Which city?"})
    assert asked.ok
    assert sent[-1] == ("PATCH", "/api/asks/k9",
                        {"by_run": True, "state": "awaiting_answer", "question": "Which city?"})


def test_the_awaiting_answer_text_points_at_asks_for_you():
    spec = ops.update_ask_spec({}, client_factory=lambda: None)
    assert "Asks for You" in spec.description and "awaiting_answer" in spec.when_to_use
    assert "question" in spec.parameters["properties"]
    decision = ops.create_decision_spec({}, client_factory=lambda: None)
    assert "update_ask" in decision.when_not_to_use
