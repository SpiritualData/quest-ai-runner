"""The live ``FilesAdapter`` over the handbook written as ``<id>.md`` files: one ``grep`` per question term,
then ``read_section`` of the top documents. No fake needed (it is a local source); nothing is re-implemented."""
from quest_ai_runner.adapters.files_adapter import FilesAdapter

from retrieval import policy
from retrieval.world import Lazy, doc_id_from_path, write_tree


def build(meter):  # noqa: ARG001
    state = Lazy(lambda corpus: FilesAdapter(str(write_tree(corpus["docs"]))))

    def retrieve(question, corpus):
        adapter = state.get(corpus)

        def search(term):
            obs = adapter.grep(term, max_hits=200)
            return [doc_id_from_path(h["rel_path"]) for h in obs.hits] if obs.kind == "grep" else []

        def read(doc_id):
            return adapter.read_section(f"{doc_id}.md").text
        return policy.gather(question, search, read)
    return retrieve
