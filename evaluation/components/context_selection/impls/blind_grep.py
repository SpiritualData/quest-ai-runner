"""ALTERNATIVE: zero-model blind grep, a real method (text search over the files is how a coding agent finds context today). Ranks every file by how many distinct task tokens (tokenized the
way the store does) appear in its text. No cards, no docstrings, no index: what any context layer
must beat to justify its existence. The ranking is production code (``BlindGrepAssembler``, an arm of the ``select-context`` seat), delegated, not copied."""
from quest_ai_runner.the_intelligence.subconscious.context_retriever.retrieval_assembler import BlindGrepAssembler
from context_selection.hub_seat import files_of
from context_selection.store_cost import store_charged


def build(meter):  # noqa: ARG001
    arms = {}

    def select(task, corpus):
        arm = arms.get(corpus.key)
        if arm is None:
            arm = arms[corpus.key] = BlindGrepAssembler(files=lambda: files_of(corpus))
        return arm.ranked_files(task)
    return store_charged(select, meter, cards=lambda corpus: 0)
