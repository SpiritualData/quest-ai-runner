"""The live ``CompositeRetrievalAdapter`` (the selected gather-evidence implementation) over two live member
adapters: ``FilesAdapter`` holds the even-numbered handbook documents as files, ``CachedDbAdapter`` the odd
ones as rows. One question needs both members: ``grep`` reaches the files, ``query`` the rows, and the
composite merges, dedupes and attributes. ``read`` tries ``read_section`` (first member that answers) and
falls back to a query by id for a row that no file read can see."""
import re

from quest_ai_runner.adapters.cached_db_adapter import CachedDbAdapter
from quest_ai_runner.adapters.composite_retrieval_adapter import CompositeRetrievalAdapter
from quest_ai_runner.adapters.files_adapter import FilesAdapter

from retrieval import policy
from retrieval.impls.cached_db import COLLECTION, ROW_ID, make_fetch
from retrieval.world import Lazy, doc_id_from_path, write_tree


def make(corpus):
    docs = corpus["docs"]
    files = FilesAdapter(str(write_tree(docs[0::2])))
    db = CachedDbAdapter(make_fetch(docs[1::2]))
    return CompositeRetrievalAdapter([files, db])


def build(meter):  # noqa: ARG001
    state = Lazy(make)

    def retrieve(question, corpus):
        adapter = state.get(corpus)

        def search(term):
            ids = []
            grep = adapter.grep(term, max_hits=200)
            if grep.kind == "grep":
                ids += [doc_id_from_path(h["rel_path"]) for h in grep.hits]
            rows = adapter.query({"collection": COLLECTION, "filter": {"text": term}})
            if rows.kind == "query":
                ids += ROW_ID.findall(rows.text or "")
            return ids

        def read(doc_id):
            obs = adapter.read_section(f"{doc_id}.md")
            if obs.kind == "error" or not re.search(r"\w{4}", obs.text or ""):
                obs = adapter.query({"collection": COLLECTION, "filter": {"id": doc_id}})
            return obs.text
        return policy.gather(question, search, read)
    return retrieve
