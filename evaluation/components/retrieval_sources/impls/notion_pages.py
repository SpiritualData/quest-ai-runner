"""The live ``NotionAdapter`` over a scripted Notion HTTP API (``urlopen`` is the only fake). The handbook is a
database whose rows carry Name, Summary and Doc properties; the sections are page BLOCKS. Search is the
adapter's ``grep`` over rows (title and properties, as the live adapter defines it), read is ``read_section``
of the page (properties plus block text). A question answered only by a section body can be read but not
located, which is the real behaviour of a row-level index."""
import re

from quest_ai_runner.adapters.notion_adapter import NotionAdapter, static_token_provider

from retrieval import policy
from retrieval.fakes import NOTION_DB, network, notion_router
from retrieval.world import Lazy


def build(meter):  # noqa: ARG001
    def make(corpus):
        route, page_of = notion_router(corpus["docs"])
        adapter = NotionAdapter(static_token_provider("offline-token"), database_ids={"handbook": NOTION_DB})
        return route, page_of, {pid: doc for doc, pid in page_of.items()}, adapter
    state = Lazy(make)

    def retrieve(question, corpus):
        route, page_of, doc_of, adapter = state.get(corpus)

        def search(term):
            with network(route):
                obs = adapter.grep(term, max_hits=200)
            if obs.kind != "grep":
                return []
            return [doc_of[h["file"].split("/")[-1]] for h in obs.hits if h["file"].split("/")[-1] in doc_of]

        def read(doc_id):
            with network(route):
                return adapter.read_section(page_of[doc_id]).text
        return policy.gather(question, search, read)
    return retrieve
