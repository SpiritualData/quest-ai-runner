# Quest Account Truth

What the AI says about a person's account is the account as it is now.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `quest-account-truth:card_and_reads` of its parent intent [`the_body`](../README.md). Opportunities: The Quest account is the AI's own consistent state, which opens questions beyond the parent solution: how the AI states what it can reach (inside the host, outside it, in the world now, or not reachable) and is held to that claim, how heavy work is handed to a deep runner within a fixed turn and cost bound and its result checked, how a file change is kept bounded to one folder and reversible, how one named change on an outside tool server is made only when an allowlist permits it, and who a new goal belongs to and whether an AI representative can take it over.

## How the solutions fit together

Quest Account Truth reads the account at the time of the reply and sets what the assistant says against it, so a claim about a goal, a decision or a person's settings is checked before it is said.

## Where to go next

- Parent intent: [Quest Body](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_body/internal_context/quest](quest/README.md)
- Records: [intent.csv](intent.csv)

---

## The full record

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Cached live-database reader | `quest-context:cached_db` | [quest_ai_runner/the_body/internal_context/quest/cached_db_adapter.py](quest/cached_db_adapter.py) |  |
| Quest API context reader | `quest-context:quest` | [quest_ai_runner/the_body/internal_context/quest/quest_retrieval_adapter.py](quest/quest_retrieval_adapter.py) |  |
