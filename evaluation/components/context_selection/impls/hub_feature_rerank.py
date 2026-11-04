"""ALTERNATIVE: the hub's ``retrieve-context:feature_rerank`` arm (``hubofhubs.retrieval.fusion.FeatureRerank``) answering the context-selection cases, delegated, not copied
(see ``hub_seat.py``). It runs as production code (``FeatureRerankAssembler``, an arm of the ``select-context`` seat). No model call; the dense arms embed with the local bge-small model. Keeps no cards, so the store charge is zero."""
from quest_ai_runner.select_context.retrieval_assembler import FeatureRerankAssembler
from context_selection.hub_seat import files_of
from context_selection.store_cost import store_charged

ALTERNATIVE = True


def build(meter):  # noqa: ARG001
    arm, state = FeatureRerankAssembler(), {}

    def select(task, corpus):
        files = files_of(corpus) if state.get("key") != corpus.key else None
        state["key"] = corpus.key
        return arm.ranked_files(task, files)
    return store_charged(select, meter, cards=lambda corpus: 0)
