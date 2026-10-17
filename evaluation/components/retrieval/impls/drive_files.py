"""The live ``GoogleDriveAdapter`` over a scripted Drive REST API (``urlopen`` is the only fake). Drive has no
search behind this adapter (``grep`` is refused by design), so the policy is BROWSE: ``query`` lists the folder,
``read_section`` reads every file once (cached), and a term matches the text read. It measures that listing and
export work and that the right file is readable; its cost grows with the folder, which the other sources avoid."""
import json
import re

from quest_ai_runner.adapters.google_chat_adapter import static_token_provider
from quest_ai_runner.adapters.google_drive_adapter import GoogleDriveAdapter

from retrieval import policy
from retrieval.fakes import DRIVE_FOLDER, drive_router, network
from retrieval.world import Lazy


def build(meter):  # noqa: ARG001
    def make(corpus):
        return drive_router(corpus["docs"]), GoogleDriveAdapter(static_token_provider("offline-token"),
                                                                 root_folder_id=DRIVE_FOLDER), {}
    state = Lazy(make)

    def retrieve(question, corpus):
        route, adapter, texts = state.get(corpus)
        with network(route):
            if not texts:
                listing = adapter.query({"action": "list", "folder_id": DRIVE_FOLDER})
                files = json.loads(listing.text).get("files", []) if listing.kind == "query" else []
                for entry in files:
                    texts[entry["id"]] = adapter.read_section(entry["id"]).text or ""

        def search(term):
            rx = re.compile(term, re.IGNORECASE)
            return [fid for fid, text in texts.items() if rx.search(text)]

        return policy.gather(question, search, lambda doc_id: texts.get(doc_id, ""))
    return retrieve
