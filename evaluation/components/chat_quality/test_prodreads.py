"""Offline tests of the production chat arm's read evidence: locator mapping, the read-only re-read and the unsupported versus unverifiable figure split."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prodchat as PC  # noqa: E402
import prodreads as PR  # noqa: E402
import prod_probes as P  # noqa: E402

READS = [{"event": "read", "data": {"step": 1, "reads": 2, "locators": ["operation(get_quest_details)", "operation(get_quest_notes)"]}},
         {"event": "read", "data": {"step": 2, "reads": 1, "locators": ["operation(get_quest_details)"]}}]


class FakeClient:
    quest_id, team_id = "quest_x", "team_x"

    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def send_request(self, method, path, body=None, params=None, timeout=60):
        self.calls.append((method, path, params))
        if path in self.fail:
            raise RuntimeError("boom")
        return {"path": path, "subscribers": 412}


def test_locators_are_distinct_and_ordered():
    assert PR.locators_of(READS) == ["operation(get_quest_details)", "operation(get_quest_notes)"]


def test_mapping_fills_the_pinned_quest_and_team_and_reads_a_route_once():
    calls, unmapped = PR.plan_rereads(["operation(get_quest_details)", "operation(list_quest_goals)", "operation(get_quest_goals)", "operation(list_asks)", "operation(made_up)", "web search"], "q1", "t1")
    assert [(c[1], c[2]) for c in calls] == [("/api/quests/q1/state", None), ("/api/teams/t1/quests/q1/goals", None), ("/api/teams/decisions/for-quest", {"quest_id": "q1"})]
    assert unmapped == ["operation(made_up)", "web search"]


def test_reread_is_get_only_and_labelled():
    client = FakeClient()
    items, unverifiable = PR.reread(client, READS)
    assert {c[0] for c in client.calls} == {"GET"}
    assert [i["source"] for i in items] == [f"{PR.AFTER_TURN}: operation(get_quest_details)", f"{PR.AFTER_TURN}: operation(get_quest_notes)"]
    assert "412" in items[0]["text"] and unverifiable == []


def test_failed_route_unmapped_locator_and_unnamed_reads_are_recorded_as_unverifiable():
    events = READS + [{"event": "read", "data": {"step": 3, "reads": 2, "locators": ["operation(made_up)"]}}]
    items, unverifiable = PR.reread(FakeClient(fail=("/api/quests/quest_x/notes",)), events)
    assert [i["source"] for i in items] == [f"{PR.AFTER_TURN}: operation(get_quest_details)"]
    assert any("get_quest_notes" in u and "failed" in u for u in unverifiable)
    assert any("made_up" in u and "no GET route" in u for u in unverifiable)
    assert any("1 read(s) named no operation" in u for u in unverifiable)
    assert "unverifiable, not contradicted" in PR.unverifiable_note(unverifiable)


def test_figure_check_uses_the_reread_and_splits_unverifiable_from_unsupported():
    items, unverifiable = PR.reread(FakeClient(), READS)
    truth = P.reads_text(items)
    absent = P.unsupported_figures("You have 412 subscribers and 77 trials.", truth, "")
    assert absent == ["77"]                                   # 412 is shown by the re-read
    assert PR.classify_figures(absent, unverifiable) == (["77"], [])
    assert PR.classify_figures(absent, ["operation(made_up): no GET route is known for it"]) == ([], ["77"])


def test_usage_reads_a_usage_object_under_another_event_name():
    assert PC.usage_of([{"event": "done", "data": {"usage": {"input_tokens": 90, "output_tokens": 12}}}]) == {"model_calls": 0, "tokens_in": 90, "tokens_out": 12}
    assert PC.usage_of([{"event": "done"}]) == {"model_calls": 0, "tokens_in": 0, "tokens_out": 0}
