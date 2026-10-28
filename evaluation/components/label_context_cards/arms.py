"""Shared machinery of the arms: build a card from a case, store it, read it back through the real store, and run the real placement."""
import json
import re
import tempfile
from pathlib import Path

import common  # noqa: F401  (puts the repository on the path)
from quest_ai_runner.adapters.card_metadata_generator import CardMetadataGenerator
from quest_ai_runner.adapters.context_card_updater import categorize_files_with_llm
from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.core.text_sampling import keywords_from_text


class StubCollectionResolver:
    """Resolves a collection reference to FRESH, identifiable rows, so the view shows which collection a card pulled in."""

    def resolve(self, locator, *, max_chars=2000):  # noqa: ARG002
        return f"[FRESH ROWS from collection '{locator.get('name', '?')}' ({locator.get('id', '?')})]: 3 recent entries."


class TierRegistry:
    """The model registry at the boundary: the fast tier is the model the arm pinned."""

    def __init__(self, model):
        self.model = model

    def resolve_tier(self, tier):  # noqa: ARG002
        return self.model


def source_text(sources):
    return CardMetadataGenerator._summarize_sources(sources)


def rule_metadata(card):
    """The floor's metadata: the title is the first source's own name, the keywords are the words of the sources, no model."""
    sources = card.get("sources") or []
    name = str((sources[0].get("id") or sources[0].get("path") or "Topic") if sources else "Topic")
    title = " ".join(w for w in re.split(r"[\W_]+", Path(name).stem if "/" in name or "." in name else name) if w).title() or "Topic"
    text = source_text(sources)
    return {"title": title, "description": text[:200], "keywords": sorted(set(keywords_from_text(text)))}


def stored_card(case, metadata):
    """The card as the store keeps it: the metadata as its name, summary and keywords, and one collection content item per collection."""
    return {"id": case["card_id"], "name": metadata.get("title", ""), "summary": metadata.get("description", ""),
            "keywords": list(metadata.get("keywords") or []), "files": [],
            "content": [{"id": f"c{j}", "type": "collection", "locator": dict(col), "ts": 10.0, "why": metadata.get("title", "")}
                        for j, col in enumerate(case["collections"])],
            "conventions": [], "provenance": {"created_by_task": "", "model": "", "created_at": "", "last_verified_at": ""},
            "usage_count": 0, "last_outcome": "unknown"}


def retrieve(case, metadata):
    folder = Path(tempfile.mkdtemp(prefix="qar_label_cards_"))
    (folder / f"{case['card_id']}.json").write_text(json.dumps(stored_card(case, metadata)), encoding="utf-8")
    for other in case.get("distractors") or []:
        (folder / f"{other['card_id']}.json").write_text(json.dumps(stored_card(
            {"card_id": other["card_id"], "collections": other["collections"]},
            {"title": "", "description": other["summary"], "keywords": other["keywords"]})), encoding="utf-8")
    store = FileContextStore(str(folder), auto_bootstrap=False, reference_resolvers={"collection": StubCollectionResolver()})
    hits, leaks = [], []
    own = {col["id"] for col in case["collections"]}
    for query in case["queries"]:
        view = store.assemble(query["text"]).context_view or ""
        if query["expect"]:
            hits.append(query["expect"] in view)
        else:
            leaks.append(any(col_id in view for col_id in own))
    return {"hits": hits, "leaks": leaks, "metadata": metadata}


def run_retrieve(case, provider, model):
    """Metadata from the real generator over ``provider``, or from the floor's rules when there is none."""
    card = {"id": case["card_id"], "sources": case["sources"]}
    if provider is None:
        out = retrieve(case, rule_metadata(card))
        out["fallback"] = False
        return out
    metadata = CardMetadataGenerator(provider, model).generate(card)
    out = retrieve(case, metadata)
    out["fallback"] = str(metadata.get("description", "")).startswith("Card with ")   # the generator's own no-model fallback
    return out


def placed_by(categorization, files):
    """The inverse of the updater's card -> files answer: file -> card id, None for a file no card took."""
    out = {f: None for f in files}
    for card_id, paths in (categorization or {}).items():
        for path in paths:
            out[path] = card_id
    return out


def run_place(case, provider, model):
    cards = [{"id": c["id"], "title": c["title"], "summary": c["summary"]} for c in case["cards"]]
    return {"placed": placed_by(categorize_files_with_llm(case["files"], cards, model_provider=provider, registry=TierRegistry(model)), case["files"])}


def rule_place(case):
    """The floor's placement: the card already holding a file in the same folder, else the card whose title or summary shares a word
    stem (five letters) with the file's path, else no card."""
    placed = {}
    for path in case["files"]:
        folder, tokens = str(Path(path).parent), {w[:5] for w in re.split(r"[\W_]+", path.lower()) if len(w) >= 5}
        best, best_score = None, 0
        for card in case["cards"]:
            s = 0
            if path in card["files"]:
                s += 10
            if any(str(Path(f).parent) == folder for f in card["files"]):
                s += 5
            words = {w[:5] for w in re.split(r"\W+", (card["title"] + " " + card["summary"]).lower()) if len(w) >= 5}
            s += len(tokens & words)
            if s > best_score:
                best, best_score = card["id"], s
        placed[path] = best
    return {"placed": placed}
