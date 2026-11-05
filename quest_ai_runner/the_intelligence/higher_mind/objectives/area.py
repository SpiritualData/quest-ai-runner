"""``hold-the-intents``: a representative's standing view across its quests, read at the start of every request.

The area arm ``separate_stores`` takes the area's members as typed seats and offers the area's one line, ``hold(quest_id, message)``:
what each store holds for one request, read the way the loop reads them when a request begins (the cards recently used, the guidance
that applies, what the person asked for that is still owed, and the predictions made for the quest). The members are the past
conversations (``remember-conversations``), the recent context (``recall-recent-context``), the guidance learned from corrections
(``guide-by-feedback``), the ledger of what was asked (``store-feedback``), the predictions (``persist-predictions``), the choice of
representative (``select-a-rep``) and the long-term guidance of a reply (``guide-toward-the-long-term-goal``). Each is built on first
use, so a line that does not read one never asks for it.
"""
from __future__ import annotations

import typing
from typing import Any, Mapping, Optional, Tuple

from esoi import implementation, solution
from esoi.assemble import Lazy

from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.session_file_conversation_store import ConversationRemembering
from quest_ai_runner.the_intelligence.higher_mind.guidance.interface import GuidanceProvider
from quest_ai_runner.the_intelligence.subconscious.intuition.anticipation_store import PredictionStore
from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import LongTermGuiding
from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.recent_context import RecentContextStore
from quest_ai_runner.the_consciousness.feedback_learning.feedback_ledger import FeedbackKeeping
from quest_ai_runner.the_consciousness.representatives.personas import RepSelecting


@solution("hold-the-intents", kind="event", entry="hold", example={"quest_id": "quest-id", "message": "What should I work on first this week?"})
@typing.runtime_checkable
class IntentHolding(typing.Protocol):
    """What is already known for a request: the recent cards, the guidance that applies, what is still owed and the predictions."""

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]: ...


@implementation("hold-the-intents", "separate_stores")
class SeparateStores:
    """Conversation recall, recent context, feedback guidance, predictions and the feedback ledger, each read separately when a request
    begins. ``hold`` reads the four a request needs; the other lines hand a question to the member that owns it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, conversations: Lazy[ConversationRemembering], recent: Lazy[RecentContextStore], guidance: Lazy[GuidanceProvider],
                 feedback: Lazy[FeedbackKeeping], predictions: Lazy[PredictionStore], reps: Lazy[RepSelecting],
                 long_term: Lazy[LongTermGuiding]) -> None:
        self.conversations, self.recent, self.guidance, self.feedback = conversations, recent, guidance, feedback
        self.predictions, self.reps, self.long_term = predictions, reps, long_term

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]:
        return {"recent": list(self.recent.load(quest_id)), "guidance": list(self.guidance.select(message, limit=3)),
                "owed": list(self.feedback.open_items(quest_id)), "standing": list(self.feedback.standing_rules(quest_id)),
                "predictions": list(self.predictions.load_predictions(quest_id))}

    def conversation(self, conv_id: str, query: str) -> Any:
        return self.conversations.current_slice(conv_id, query)

    def rep(self, task: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
        return self.reps.resolve(task)

    def directive(self, message: str) -> str:
        return self.long_term.directive(message)
