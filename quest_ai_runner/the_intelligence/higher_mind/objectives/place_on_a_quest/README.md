# Place On A Quest

**Purpose:** Say which of the person's quests a message, goal, note or task belongs to, or none, the same way on every surface that picks one.

Breadcrumb: [Top](../../../../../esoi/README.md) > [Intelligence](../../../README.md) > [Quest Intents](../README.md) > Place On A Quest

Part of [Quest Intents](../README.md)

## Place On A Quest (`place-on-a-quest`)

- Out of scope: Holding the quests and their intents as the standing view and framing work with them (Quest Intents). A quest the person chose explicitly, such as the Quest chat of one quest: an explicit choice is not classification, so the pin stays. Whether a write may bind to a quest unasked, which is the host's write guard over the placement.
- Delegates to: quest-intents
- Entry call: `quest_ai_runner.run("place-on-a-quest", ...)`
- Selected implementation: Words and meaning candidates, the model picks (`hybrid-candidate-ranker`)
- Metric: Quest placement accuracy over labelled messages, balanced with cost and time
- Implemented by: [`evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py`](../../../../../evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py), [`quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/__init__.py`](__init__.py), [`quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/interface.py`](interface.py), [`quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/quest_placement.py`](quest_placement.py)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Embedding-led candidates, the model picks | `place-on-a-quest:shared-quest-ranker` | The quests' own embeddings (vision statement, purpose, category and scope) order the candidates, then the model picks which numbered quests a text is about, or none, and the user's priority orders what is left. quest-backend app/business/quests/quest_selection.py (rank_quest_batch, select_and_rank_quests_for_text before the candidates came from the hybrid ranking); measured by evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py (embedding order, no model) and shared_quest_ranker_model.py (the shipped prompt answered by a model). | [`evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py`](../../../../../evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py) |  |
| Words and meaning candidates, the model picks | `place-on-a-quest:hybrid-candidate-ranker` | Candidates ranked by IDF-weighted word overlap over each quest's whole document (vision statement, purpose, category, scope, state, open outcomes, notes, goals, collection entries) plus an embedding bonus from the quest cards, with evidence snippets saying where each match came from; then the model picks which numbered quests the text is about, or none, shown each quest's vision statement and purpose as their own lines under the owner's binding scope. quest-backend app/business/quests/quest_candidates.py (rank_quests_for_message, lexical_rank, semantic_quest_candidates) leads the candidates of quest_selection.select_and_rank_quests_for_text; measured by evaluation/components/place_on_a_quest/impls/hybrid_candidate_ranker.py (no model) and hybrid_candidate_ranker_model.py. | [`quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/quest_placement.py`](quest_placement.py) | selected |
| The library judge over the quests it is given | `place-on-a-quest:library-quest-judge` | The runner's own judge for a runner with no host: the model sees the quests it was handed in the order given and returns the one the message is about (or none) through a tool schema, with the home quest as the default on a failed call. quest_ai_runner/the_intelligence/higher_mind/objectives/quest_judge.py and quest_placement.py. | [`quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/quest_placement.py`](quest_placement.py) |  |

## Metrics

- Quest placement accuracy over labelled messages, balanced with cost and time (`metric-place-on-a-quest`): leader `hybrid_candidate_ranker_model`, 48 of 50 cases

## Problems

- Messages Not Placed On The Quest They Belong To (`prob-message-lands-on-no-quest`): A message, a goal, a note or a task is attached to a quest by whichever surface received it, each with its own way of picking (the first quest by priority, a word match, an embedding search, a model asked about the quests it was shown), so the same sentence lands on different quests in different places, a quest past the model's window is never offered, and a message no quest fits is given one anyway or none is asked about.

## Ideals

- Message Served By Its Quest (`ideal-message-served-by-its-quest`): Every message, goal, note and task is served by the quest it is about, or by none when no quest fits.

## Where to go next

- Up: [quest_ai_runner](../../../../README.md)
- Intent: [Quest Intents](../README.md)
- Repository: [top README](../../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
