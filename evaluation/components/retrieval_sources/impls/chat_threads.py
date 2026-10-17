"""The live ``GoogleChatAdapter`` over a scripted Chat REST API (``urlopen`` is the only fake). Each handbook
document is one Chat thread of short messages. Search is the adapter's ``grep`` over the loaded conversations,
read is ``read_section`` of the thread (resolved by its tail id)."""
import time

from quest_ai_runner.adapters.google_chat_adapter import GoogleChatAdapter, static_token_provider

from retrieval import policy
from retrieval.fakes import CHAT_SPACE, chat_router, network
from retrieval.world import Lazy


def build(meter):  # noqa: ARG001
    def make(corpus):
        adapter = GoogleChatAdapter(static_token_provider("offline-token"), space_names=[CHAT_SPACE],
                                    lookback_days=30, max_messages_per_space=500)
        return chat_router(corpus["docs"], time.time()), adapter
    state = Lazy(make)

    def retrieve(question, corpus):
        route, adapter = state.get(corpus)

        def search(term):
            with network(route):
                obs = adapter.grep(term, max_hits=400)
            return [h["file"].rsplit("/", 1)[-1] for h in obs.hits] if obs.kind == "grep" else []

        def read(doc_id):
            with network(route):
                return adapter.read_section(doc_id).text
        return policy.gather(question, search, read)
    return retrieve
