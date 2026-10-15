"""OFFLINE ARM: cards written from the world by a deterministic extractor, read back through the real
``FileContextStore.assemble``. Each learned task becomes one card: keywords from its request, summary and future
context (``keywords_from_text``), one typed ``collection`` content item per collection the future context names,
and a stub resolver that returns identifiable fresh rows. The store keeps its library default
``confidence_threshold`` (a raw keyword score of 9.0): a paraphrased query that shares no rare word with a card
is not selected at all. No model, no embedder, no network. It measures the
persisted-card path (selection by keywords, fresh resolution, no leak), not the model's learning of cards."""
import json
import re
import tempfile
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.core.text_sampling import keywords_from_text

COLLECTION = re.compile(r"collection '([^']+)' \(id (\w+)\)")


class StubCollectionResolver:
    """Resolves a collection reference to FRESH, identifiable rows, so the view shows what was pulled in."""

    def resolve(self, locator, *, max_chars=2000):  # noqa: ARG002
        return f"[FRESH ROWS from collection '{locator.get('name', '?')}' ({locator.get('id', '?')})]: 3 recent entries."


def build(meter, threshold=None):  # noqa: ARG001
    state = {}

    def seed(world):
        cards_dir = Path(tempfile.mkdtemp(prefix="qar_card_quality_"))
        for i, item in enumerate(world["learn"]):
            text = " ".join([item["request"], item["executed"], item["future_context"]])
            refs = COLLECTION.findall(item["future_context"])
            card = {"id": f"learned-{i}", "keywords": sorted(set(keywords_from_text(text))),
                    "summary": item["executed"], "files": [],
                    "content": [{"id": f"c{j}", "type": "collection", "locator": {"name": name, "id": cid},
                                 "ts": 10.0 + i, "why": item["request"]} for j, (name, cid) in enumerate(refs)],
                    "conventions": [], "provenance": {"created_by_task": "", "model": "", "created_at": "",
                                                      "last_verified_at": ""},
                    "usage_count": 0, "last_outcome": "unknown"}
            (cards_dir / f"{card['id']}.json").write_text(json.dumps(card), encoding="utf-8")
        extra = {} if threshold is None else {"confidence_threshold": threshold}
        return FileContextStore(str(cards_dir), auto_bootstrap=False,
                                reference_resolvers={"collection": StubCollectionResolver()}, **extra)

    def select(query, world):
        if "store" not in state:
            state["store"] = seed(world)
        assembled = state["store"].assemble(query)
        return {"view": assembled.context_view or "",
                "titles": [c.get("title") or c.get("id") for c in (assembled.card_metadata or [])]}
    return select
