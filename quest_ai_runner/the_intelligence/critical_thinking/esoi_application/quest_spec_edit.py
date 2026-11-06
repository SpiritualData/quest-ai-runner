"""QAR's arm of ESOI Application: the hub's field edit and ensure-one additions, with Quest as the records location.

The hub's ``RecordFieldEdit`` already does the job (apply a proposed edit to the one record and field it names, add a record or milestone once,
refuse with the reason). All QAR states is where the records live: the specification of a quest, kept by Quest and reached through its client
(``the_body/internal_context/quest/spec_records.py``). A caller inside Quest passes its own ``client`` (any object with ``get_spec``,
``create_spec_record`` and ``edit_spec_record``); a lane passes ``base_url`` and ``api_key`` and gets the Quest client.
"""
from __future__ import annotations

from typing import Any, Optional

from esoi import implementation
from hubofhubs.api import RecordFieldEdit

from quest_ai_runner.the_body.internal_context.quest.spec_records import QuestSpecRecords


QUEST_SPEC_RECORDS = "quest_spec_records"


@implementation("esoi-application", "quest_spec_records", scope="quest_ai_runner")
class QuestSpecEdit(RecordFieldEdit):
    """Applies a proposed edit to a quest's own specification records through Quest, so the edit is made where the person sees and edits it, under the
    same access, autopilot, edited-by-a-person and approval rules as the specification editor. An edit Quest holds for the owner is reported as proposed."""
    REQUIRES = ()     # the Quest client is a constructor argument, or built from base_url and api_key

    def __init__(self, client: Optional[Any] = None, base_url: str = "", api_key: str = "", team_id: str = "") -> None:
        if client is None:
            from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient
            client = QuestClient(base_url=base_url, api_key=api_key, team_id=team_id or None)
        super().__init__(source=QuestSpecRecords(client))
