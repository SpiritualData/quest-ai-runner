"""CANDIDATE: the unified search as committed (``core/unified_search.solutions_and_problems_with_links``), offline.

Cards, solutions and problems ranked in one list by the library's own stages (field-weighted lexical, the local vector
search over the same keys, one fusion, the confidence gate); ideals, metrics, intents and implementations reached through
links; "no solution yet" when the top record is a problem or intent no solution serves. No provider is passed, so an
ambiguous request returns the best fused candidates and the arm makes zero model calls. Tags named by a request filter the
candidates (``tag_mode="filter"``), as the search_cards_and_specs tool does when a run passes tags."""
from context_and_specs.world import world
from quest_ai_runner.core.unified_search import solutions_and_problems_with_links


def build(meter):  # noqa: ARG001
    w = world()

    def search(task, tags):
        got = solutions_and_problems_with_links(task, w.views, vector_search=w.vector_search, tags=tags or None,
                                                tag_table=w.tags, tag_mode="filter")
        return {"items": [i.key for i in got.items], "linked": {i.key: i.linked_from for i in got.items if i.linked_from},
                "no_solution_yet": bool(got.no_solution_yet), "calls": got.calls, "route": got.route}
    return search
