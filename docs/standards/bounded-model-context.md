# Bounded model context

**Read when:** Writing or changing anything that puts a list of goals, notes, quests, cards, turns or files into a prompt.

**Problem it prevents:** A prompt builder that loops over a whole collection (a 536-goal quest, every note) is slow, costly and wrong in the long run: the collection only grows, and the model reads everything to find the few things that bear on the request.

**Tags:** unbounded-model-context, token-waste
**Paths:** quest_ai_runner/**/*.py
**Rule:** A prompt never lists a whole collection: filter by structure (window, status, scope), rank by rarity-weighted words, and when the relevant set exceeds the token budget show TF-DF-IDF representatives plus a digest of what was left out, through the hub's `shortlist` or `plan_retrieval` (see `QuestRetrievalAdapter.bounded_goal_lines`).
**Check:** regex:\bfor \w+ in (everything|all_goals|all_rows|all_notes|every_\w+|all_quests)\b.*\n? => This loops over a whole collection: if it feeds a prompt, bound it with the hub's shortlist (window, status, rarity ranking, representatives, token budget) and say what was left out, or waive with the reason it is code-only counting.
