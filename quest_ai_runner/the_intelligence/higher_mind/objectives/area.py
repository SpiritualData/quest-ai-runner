"""``hold-the-intents``: a representative's standing view across its quests, read at the start of every request.

The area arm ``separate_stores`` takes the area's members as typed seats and offers the area's one line, ``hold(quest_id, message)``:
what each store holds for one request, read the way the loop reads them when a request begins (what the person asked for that is still owed, and the predictions made for the quest). The members are the past conversations
(Long-Term Memory), the ledger of what was asked (Feedback Learning), the predictions (Intuition) and the choice of representative
(Quest Representatives). Each is built on first
use, so a line that does not read one never asks for it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from esoi import implementation
from esoi import Lazy

from quest_ai_runner.the_intelligence.subconscious.intuition.interface import PredictionStore
from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.interface import ConversationRemembering
from quest_ai_runner.the_consciousness.feedback_learning.feedback_ledger import FeedbackKeeping
from quest_ai_runner.the_consciousness.representatives.interface import RepSelecting



@implementation("quest-intents", "separate_stores")
class SeparateStores:
    """Conversation recall, recent context, feedback guidance, predictions and the feedback ledger, each read separately when a request
    begins. ``hold`` reads the four a request needs; the other lines hand a question to the member that owns it."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, conversations: Lazy[ConversationRemembering], feedback: Lazy[FeedbackKeeping], predictions: Lazy[PredictionStore],
                 reps: Lazy[RepSelecting]) -> None:
        self.conversations, self.feedback, self.predictions, self.reps = conversations, feedback, predictions, reps

    def hold(self, quest_id: str, message: str = "") -> Mapping[str, Any]:
        return {"owed": list(self.feedback.open_items(quest_id)), "standing": list(self.feedback.standing_rules(quest_id)),
                "predictions": list(self.predictions.load_predictions(quest_id))}

    def conversation(self, conv_id: str, query: str) -> Any:
        return self.conversations.current_slice(conv_id, query)

    def rep(self, task: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
        return self.reps.resolve(task)
