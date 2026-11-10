"""``quest_ai_runner.run("choose-the-context", ...)`` with a host's assembler: the one line a host calls to choose the context for a request, with the
seats it does not read left unset."""
import pytest

import quest_ai_runner
from hubofhubs.api import AssembledContext


class RecordingAssembler:
    def __init__(self):
        self.calls = []

    def assemble(self, task_text, meta=None, on_event=None):
        self.calls.append((task_text, meta))
        return AssembledContext(context_view="view", card_metadata=[{"id": "card:one", "title": "One"}])


def test_the_one_line_hands_the_request_and_scope_to_the_hosts_assembler_with_no_other_seats():
    assembler = RecordingAssembler()
    chosen = quest_ai_runner.run("choose-the-context", {"task_text": "sleep", "meta": {"scope_tags": ["quest:q1"]}, "assembler": assembler}, environ={})
    assert chosen.context_view == "view" and chosen.card_metadata[0]["id"] == "card:one"
    assert assembler.calls == [("sleep", {"scope_tags": ["quest:q1"]})]


def test_choosing_without_an_assembler_says_what_to_pass():
    with pytest.raises(ValueError, match="assembler"):
        quest_ai_runner.run("choose-the-context", {"task_text": "sleep"}, environ={})
