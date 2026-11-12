"""A quest that is gone (404) or not this account's (403) is an expected answer: kept off the terminal transcript."""
import logging

import pytest

from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestApiError

from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient


@pytest.mark.parametrize("status,shown", [(404, False), (403, False), (500, True)])
def test_get_my_quest_transcript_flag(status, shown, caplog):
    client = QuestClient("http://quest.example", "key")

    def refuse(*args, **kwargs):
        raise QuestApiError("refused", status=status)

    client._request = refuse
    with caplog.at_level(logging.WARNING):
        assert client.get_my_quest("q1") == {}
    record = [r for r in caplog.records if "get_my_quest" in r.getMessage()][0]
    assert getattr(record, "in_transcript", True) is shown
