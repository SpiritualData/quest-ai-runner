"""Private real inputs as the third case source of the one context-selection metric.

The cases are real requests with a labelled candidate list (notes, goal updates, decisions, cards, conversation turns, quest state fields), kept
OUTSIDE this repository in the folder named by ``QAR_PRIVATE_CONTEXT_DATASET`` (``cases.jsonl`` and ``manifest.json``). With the variable absent the
source contributes no case and the metric runs on the file and topic cases alone; nothing fails. Nothing from the data is copied here: a row records
only how many cases of each source were present, and the digest of the cases records the sha256 of the two private files (or ``private:absent``).

A private case is scored like any other: the arm is a builder, and the fixed selector reads the case's own candidate items as its corpus. Each
item becomes one file of a throwaway corpus (one card per item: its type, then its text), the arm ranks that corpus for the request, and the first
``budget`` items it returns are the selection. Accuracy is the harmonic mean of needed-item recall and (1 - noise), where noise is the share of
selected items labelled irrelevant (helpful items are neutral); a self-contained case (no needed item) scores 1 only when nothing is selected, and
falls with each item selected. The case passes at ``PASS_ACCURACY``. The size of what was selected is charged as cost (the share of
``SIZE_BUDGET_CHARS`` it fills, times the cost budget of ``[balance.context_selection]``), beside the store charge of ``store_cost.py``; time is
the seconds of the case. The weights of ``[balance.context_selection]`` fold these into the one number.

The mix of case kinds is set here, in ``KIND_MIX`` and ``SOURCE_WEIGHTS``, and nowhere else.
"""
import atexit
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

ENV = "QAR_PRIVATE_CONTEXT_DATASET"
LABELS = ("needed", "helpful", "irrelevant")
HERE = Path(__file__).resolve().parent
SOURCES_FILE = HERE / "datasets" / "case_sources.json"
ABSENT = "private:absent"

# The three case sources are mixed with equal weight (file finding, topics, real inputs), fixed before any arm was scored. The private cases are
# taken in the number that gives them their weight beside the file and topic cases already present.
SOURCE_WEIGHTS = dict(file=1.0, topic=1.0, private=1.0)
# The kinds of private input, as shares of the private cases. These are the committed fallback; when the dataset's manifest states its own
# input-kind proportions they replace these, so the mix follows how real inputs occur (self-contained 24%, one needed item 52%, several 24%).
KIND_MIX = dict(none=0.24, single=0.52, several=0.24)
PASS_ACCURACY = 0.5            # a case passes at or above this accuracy, the same half the topic cases use
SIZE_BUDGET_CHARS = 8000       # about 2000 tokens of selected context fills the size budget
TOP_K = 3


def dataset_folder(environ=None):
    environ = os.environ if environ is None else environ
    raw = (environ.get(ENV) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path.parent if path.is_file() else path


def digest_parts(environ=None):
    """The recorded digest part of the private source: ``private:absent`` or ``private:<cases sha>/<manifest sha or none>``."""
    folder = dataset_folder(environ)
    cases = folder / "cases.jsonl" if folder else None
    if not cases or not cases.is_file():
        return ABSENT
    manifest = folder / "manifest.json"
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else "none"
    return f"private:{hashlib.sha256(cases.read_bytes()).hexdigest()}/{manifest_sha}"


def publish_digest(environ=None):
    """Write the digest part where the harness hashes the case files (``datasets/``), and put the committed text back when the process ends, so
    the working tree is not left changed by a run that saw the dataset."""
    committed = SOURCES_FILE.read_text(encoding="utf-8") if SOURCES_FILE.exists() else None
    text = json.dumps(dict(private=digest_parts(environ))) + "\n"
    if committed != text:
        SOURCES_FILE.write_text(text, encoding="utf-8")
        atexit.register(lambda: SOURCES_FILE.write_text(committed, encoding="utf-8") if committed is not None else SOURCES_FILE.unlink(missing_ok=True))


def kind_of(raw):
    needed = sum(1 for c in raw["candidates"] if c.get("label") == "needed")
    return "none" if not needed else "single" if needed == 1 else "several"


def parse_cases(text):
    """Cases from jsonl text; a line with no id, no candidate list, a candidate without an id or a label outside LABELS is skipped."""
    cases = []
    for line in text.splitlines():
        try:
            raw = json.loads(line) if line.strip() else None
        except ValueError:
            raw = None
        cands = raw.get("candidates") if isinstance(raw, dict) else None
        if not raw or not raw.get("case_id") or not isinstance(cands, list) or not all(c.get("item_id") and c.get("label") in LABELS for c in cands):
            continue
        cases.append(dict(id=f"PRIV-{raw['case_id']}", area="real", task=raw.get("input_text", ""), candidates=cands,
                          kind=kind_of(raw), heldout=False))
    return cases


def kind_shares(folder):
    manifest = Path(folder) / "manifest.json" if folder else None
    if manifest and manifest.is_file():
        try:
            p = json.loads(manifest.read_text(encoding="utf-8"))["input_kind_mix"]["proportions"]
            return dict(none=p["none"], single=p["single"], several=p.get("multi_same_type", 0) + p.get("multi_source", 0))
        except (ValueError, KeyError, TypeError):
            pass
    return dict(KIND_MIX)


def mix(cases, shares, count):
    """``count`` private cases, split by kind in ``shares`` (sorted by id within a kind, so the same cases every time); a kind with fewer cases
    than its quota gives all it has."""
    chosen, left = [], count
    for position, (kind, share) in enumerate(shares.items()):
        quota = left if position == len(shares) - 1 else round(share * count)   # the last kind takes what rounding left, so the count is exact
        chosen += sorted((c for c in cases if c["kind"] == kind), key=lambda c: c["id"])[:quota]
        left -= quota
    return sorted(chosen, key=lambda c: c["id"])


def load_cases(other_cases, environ=None):
    """The private cases for a mix with ``other_cases`` (the file and topic cases): none when the dataset is absent, else as many as make their
    ``SOURCE_WEIGHTS`` share, in the manifest's kind proportions."""
    publish_digest(environ)
    folder = dataset_folder(environ)
    cases = folder / "cases.jsonl" if folder else None
    if not cases or not cases.is_file():
        return []
    others = SOURCE_WEIGHTS["file"] + SOURCE_WEIGHTS["topic"]
    count = round(SOURCE_WEIGHTS["private"] / others * len(other_cases))
    return mix(parse_cases(cases.read_text(encoding="utf-8")), kind_shares(folder), count)


_SCRATCH = []


def scratch_root():
    if not _SCRATCH:
        _SCRATCH.append(tempfile.mkdtemp(prefix="qar_private_items_"))
        atexit.register(shutil.rmtree, _SCRATCH[0], True)
    return Path(_SCRATCH[0])


def item_corpus(case):
    """The case's candidate items as a corpus: one text file and one card per item (the card says the item type, then starts with its text).
    Returns ``(corpus, {file path: item id})``."""
    from context_selection.corpus import Corpus
    from hubofhubs.recall.context_card_extraction import SOURCE_FILE_WEIGHT
    root = Path(tempfile.mkdtemp(dir=scratch_root()))
    corpus, owner = Corpus(root=root), {}
    for index, item in enumerate(case["candidates"]):
        rel, text = f"item_{index:03d}.txt", item.get("text") or ""
        (root / rel).write_text(text, encoding="utf-8")
        flat = " ".join(text.split())
        corpus.files.append(rel)
        corpus.cards[f"item-{index:03d}"] = dict(
            id=f"item-{index:03d}", keywords=[], summary=f"{item.get('type') or 'item'}: {flat[:200]}", description=flat[200:900],
            files=[dict(path=rel, symbols=[])], weight=SOURCE_FILE_WEIGHT)
        owner[rel] = item["item_id"]
    return corpus, owner


def numbers(selected, candidates):
    """The numbers of one case from the selected item ids (an id outside the case is ignored, a repeat counts once)."""
    by_id = {c["item_id"]: c for c in candidates}
    picked = [i for i in dict.fromkeys(selected) if i in by_id]
    needed = {i for i, c in by_id.items() if c["label"] == "needed"}
    n = len(picked)
    noise = sum(1 for i in picked if by_id[i]["label"] == "irrelevant") / n if n else 0.0
    if needed:
        recall = sum(1 for i in picked if i in needed) / len(needed)
        accuracy = 0.0 if recall == 0 or noise == 1 else 2 * recall * (1 - noise) / (recall + 1 - noise)
    else:
        recall = 1.0 if n == 0 else 0.0
        accuracy = 1.0 if n == 0 else max(0.0, 1 - n / TOP_K) * (1 - noise)
    chars = sum(len(by_id[i].get("text") or "") for i in picked)
    return dict(recall=round(recall, 4), noise=round(noise, 4), accuracy=round(accuracy, 4), selected=n, size_chars=chars)


def score_case(fn, case):
    corpus, owner = item_corpus(case)
    ranked = fn(case["task"], corpus)
    budget = getattr(fn, "budget", TOP_K)
    row = numbers([owner[r] for r in ranked[:budget] if r in owner], case["candidates"])
    meter = getattr(fn, "meter", None)
    if meter is not None:   # the size of what was selected is the cost of the context
        from context_selection.store_cost import cost_budget
        meter.cost_usd += cost_budget() * min(row["size_chars"] / SIZE_BUDGET_CHARS, 1.0)
    return dict(row, ok=row["accuracy"] >= PASS_ACCURACY, top1=False, top3=False, private=True, kind=case["kind"],
                got=f"{row['selected']} selected, recall {row['recall']}, noise {row['noise']}",
                note=f"{case['kind']} input, accuracy {row['accuracy']}")
