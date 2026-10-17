"""The live ``SessionFileConversationStore`` (the remember-conversations implementation) over synthetic session
files written to a temp directory: ``related_slices`` with the user's id in scope and ``max_convs`` 3, and the
conversation ids it reports in ``sources``. Nothing is faked: it is a local source."""
import json
import tempfile
from pathlib import Path

from quest_ai_runner.adapters.session_file_conversation_store import SessionFileConversationStore

from conversation_recall.interface import MAX_CONVS


def build(meter):  # noqa: ARG001
    state = {}

    def store_for(world):
        if "store" not in state:
            root = Path(tempfile.mkdtemp(prefix="qar_conversation_recall_"))
            for conv in world["conversations"]:
                (root / f"{conv['id']}.json").write_text(json.dumps(conv), encoding="utf-8")
            state["store"] = SessionFileConversationStore(sessions_dir=str(root))
        return state["store"]

    def recall(query, user, world):
        ctx = store_for(world).related_slices(query, {"user_id": user}, max_convs=MAX_CONVS)
        known = [c["id"] for c in world["conversations"]]
        ids = []
        for source in ctx.sources:
            tail = str(source.get("conv_id", ""))
            match = next((k for k in known if tail == k or tail.endswith(f"{k}.json") or tail.endswith(f"/{k}")), None)
            if match and match not in ids:
                ids.append(match)
        return {"ids": ids}
    return recall
