"""``place-on-a-quest``: which quest a message belongs to, or none, as one call the host supplies.

QAR is a library the host calls. The Quest backend owns the person's quests, their embeddings and their words, so it owns the placement:
``HostFunction`` wraps the function the host injects (the backend's ``select_and_rank_quests_for_text``) and QAR never guesses a quest by
its own heuristics when that function is present. ``library-quest-judge`` is the arm for a runner with no host (the terminal on a folder of
quests): it asks the model to pick among the quests it was handed (``quest_judge.select_and_rank_quests``). Both answer the same
``{"picked", "ranked"}``; an empty ``picked`` means no quest fits.

A conversation the person pinned to a quest (the Quest chat of one quest, ``meta["quest_ids"]``) is an explicit choice and is not placed.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from esoi import implementation

from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_judge import select_and_rank_quests


def answer(picked: Sequence[Any], ranked: Sequence[Any]) -> Dict[str, List[str]]:
    """The one answer shape: quest ids as text, no repeats, picks never outside the ranking."""
    ordered: List[str] = []
    for quest_id in [*picked, *ranked]:
        text = str(quest_id or "").strip()
        if text and text not in ordered:
            ordered.append(text)
    chosen = [str(q).strip() for q in picked if str(q or "").strip()]
    return {"picked": list(dict.fromkeys(chosen)), "ranked": ordered}


@implementation("place-on-a-quest", "hybrid-candidate-ranker")
class HostFunction:
    """The selected arm, run in the host: the candidates ranked by words and meaning, then the model's pick, which only the host can do because it owns the quests, their words and their embeddings. This is the runner's side of it, the adapter that calls the function the host supplies: ``place_message(message=, previous=, home_quest_id=, max_quests=)`` returns a mapping with ``picked``
    and ``ranked``. A failure of the host function is no placement (``picked`` empty), never a guess."""
    REQUIRES = ()     # the host hands the function in

    def __init__(self, place_message: Callable[..., Mapping[str, Any]]) -> None:
        self.place_message = place_message

    def place(self, message: str, previous: str = "", home_quest_id: Optional[str] = None, max_quests: int = 1) -> Mapping[str, Any]:
        try:
            found = self.place_message(message=message, previous=previous, home_quest_id=home_quest_id, max_quests=max_quests)
        except Exception:  # noqa: BLE001 - a placement must never break the turn
            return answer([], [])
        return answer(found.get("picked") or [], found.get("ranked") or [])


@implementation("place-on-a-quest", "library-quest-judge")
class LibraryQuestJudge:
    """The library's own judge over the quests it was handed (``[{"quest_id", "title", "state"}]``), with ``call_judge(prompt, tool_schema)``
    making the one model call. When the call fails the answer is the HOME quest, where the person is working, or none."""
    REQUIRES = ()     # the quests and the model call are handed in

    def __init__(self, quests: Sequence[Mapping[str, str]], call_judge: Callable[[str, Any], Any]) -> None:
        self.quests, self.call_judge = [dict(q) for q in quests], call_judge

    def place(self, message: str, previous: str = "", home_quest_id: Optional[str] = None, max_quests: int = 1) -> Mapping[str, Any]:  # noqa: ARG002
        chosen, ranked = select_and_rank_quests(self.quests, message, self.call_judge, previous_message=previous, home_quest_id=home_quest_id)
        return answer([chosen] if chosen else [], ranked)
