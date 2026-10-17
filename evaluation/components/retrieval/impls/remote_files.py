"""The live ``RemoteFilesAdapter`` over the REAL ``serve-files`` request handling (``FileOperations`` through
``RequestHandler.dispatch``), with only the HTTP socket replaced by the adapter's injectable transport.
Search is the adapter's scoped ``grep`` (a scope is required by design), read is ``read_section`` of
``alias:<id>.md``."""
from quest_ai_runner.adapters.remote_files_adapter import RemoteFilesAdapter

from retrieval import policy
from retrieval.fakes import remote_transport
from retrieval.world import Lazy, doc_id_from_path, write_tree

ALIAS = "handbook"


def build(meter):  # noqa: ARG001
    def make(corpus):
        root = write_tree(corpus["docs"])
        return RemoteFilesAdapter(alias=ALIAS, base_url="https://files.invalid", token="offline-token",
                                  transport=remote_transport(root))
    state = Lazy(make)

    def retrieve(question, corpus):
        adapter = state.get(corpus)

        def search(term):
            obs = adapter.grep(term, scope=f"{ALIAS}:", max_hits=200)
            return [doc_id_from_path(h["rel_path"]) for h in obs.hits] if obs.kind == "grep" else []

        def read(doc_id):
            return adapter.read_section(f"{ALIAS}:{doc_id}.md").text
        return policy.gather(question, search, read)
    return retrieve
