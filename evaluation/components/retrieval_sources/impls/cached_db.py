"""The live ``CachedDbAdapter`` over the handbook held as DB rows. The consumer-supplied ``fetch`` (the DB
edge) is the only fake: it filters rows by a regex on ``text`` or an exact ``id``. One ``query`` per question
term; ``read`` is a query filtered to the row id, as the adapter has no per-row read of its own."""
import re

from quest_ai_runner.adapters.cached_db_adapter import CachedDbAdapter

from retrieval import policy
from retrieval.world import Lazy, markdown

COLLECTION = "handbook"
ROW_ID = re.compile(r'"id":\s*"([^"]+)"')


def rows_for(docs):
    return [{"id": d["id"], "title": d["title"], "body": markdown(d)} for d in docs]


def make_fetch(docs):
    rows = rows_for(docs)

    def fetch(collection, filter_):
        if collection != COLLECTION:
            return []
        if "id" in filter_:
            return [r for r in rows if r["id"] == filter_["id"]]
        if "text" in filter_:
            rx = re.compile(filter_["text"], re.IGNORECASE)
            return [r for r in rows if rx.search(r["title"] + "\n" + r["body"])]
        return rows
    return fetch


def build(meter):  # noqa: ARG001
    state = Lazy(lambda corpus: CachedDbAdapter(make_fetch(corpus["docs"])))

    def retrieve(question, corpus):
        adapter = state.get(corpus)

        def search(term):
            obs = adapter.query({"collection": COLLECTION, "filter": {"text": term}})
            return ROW_ID.findall(obs.text or "") if obs.kind == "query" else []

        def read(doc_id):
            return adapter.query({"collection": COLLECTION, "filter": {"id": doc_id}}).text
        return policy.gather(question, search, read)
    return retrieve
