"""The the-science area: its ids answer offline by the strict definition, and the area hands each line to the web member."""
import pytest
from esoi import Pending, targets

import quest_ai_runner
from quest_ai_runner.the_world.tools.web_search.area import KnowingTheWorld


def strict_outcome(solution_id):
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}[solution_id]
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run(solution_id, example, environ={})
    assert {reason.kind for reason in caught.value.reasons} <= {"key", "install", "needs"}, caught.value.lines
    return "pending"


@pytest.mark.parametrize("solution_id", ("the-science", "search-the-web"))
def test_each_id_of_the_area_pends_on_the_search_key_offline(solution_id):
    assert strict_outcome(solution_id) == "pending"


class Web:
    def describe(self):
        return "web"

    def search(self, queries, *, max_results=None, fresh=False):
        return ("search", queries, max_results, fresh)

    def fetch(self, url, *, focus=None, fresh=False, full=False, part=None):
        return ("fetch", url, focus, part)


def test_the_area_hands_each_line_to_the_web_member():
    area = quest_ai_runner.run(KnowingTheWorld, environ={}, provided={"search-the-web": Web()})
    assert area.describe() == "web"
    assert area.search("meditation and attention", max_results=3) == ("search", "meditation and attention", 3, False)
    assert area.fetch("https://example.org/a", focus="attention", part=2) == ("fetch", "https://example.org/a", "attention", 2)
