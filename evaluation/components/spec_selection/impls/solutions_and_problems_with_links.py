"""CANDIDATE: the unified card and specification search (``core/unified_search.solutions_and_problems_with_links``) on this
component's own labelled requests, the spec_selection half of metric-search-cards-and-specs. Cards are absent: the views are
the fixture index's records (``views_from_spec_index``), the vector search is the local one the free ``lexical_vector`` arm
uses (``arms.real_vector_search``), and no provider is passed, so an ambiguous request returns the best fused candidates and
the arm makes zero model calls. The implementation keeps its own settings, the ones its context_and_specs arm runs with:
solutions and problems ranked, at most five, ideals, metrics, intents and implementations brought by links. As for every arm
here, the ranked items are the selected ids and the linked ones are not checked."""
from quest_ai_runner.core.unified_search import solutions_and_problems_with_links, views_from_spec_index
from spec_selection import arms


def build(meter):  # noqa: ARG001
    views = views_from_spec_index(arms.INDEX)
    vector_search = arms.real_vector_search()

    def run(kind, args):  # noqa: ARG001
        got = solutions_and_problems_with_links(args["task"], views, vector_search=vector_search)
        selected = [i.key for i in got.items if not i.linked_from]
        linked = [i.key for i in got.items if i.linked_from]
        return {"ids": selected, "linked": linked, "n_ids": len(got.items), "n_selected": len(selected), "route": got.route,
                "calls": got.calls, "failed_batches": got.failed_batches}
    return run
