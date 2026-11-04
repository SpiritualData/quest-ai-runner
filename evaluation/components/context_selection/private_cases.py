"""Private real inputs as the third case source of the one context-selection metric.

The cases are real requests with a labelled candidate list (notes, goal updates, decisions, cards, conversation turns, quest state fields), kept
OUTSIDE this repository in the folders named by ``QAR_PRIVATE_CONTEXT_DATASET`` (one folder, or several separated by ``os.pathsep``; each holds
``cases.jsonl`` and ``manifest.json``). A folder without ``cases.jsonl`` is skipped, and a case id already loaded from an earlier folder is skipped
in a later one. With no folder present the source contributes no case and the metric runs on the file and topic cases alone; nothing fails.
Nothing from the data is copied here: a row records only how many cases of each source were present, and the digest of the cases records the
sha256 of every present folder's two private files (or ``private:absent``).

Cases of the richer schema carry metadata on each item (source kind, field, author, channel, age, relation to the quest, status, path, need role)
and on the request (the quest and goal titles). The item card starts with that metadata, so an arm can use source and recency; an item with a
path is written under that relative path (sanitized) so file-aware arms see a real path; the task an arm receives starts with a
"Quest: ... Goal: ..." line when the case has those titles. Older cases without these fields work unchanged.

A private case is scored like any other: the arm is a builder, and the fixed selector reads the case's own candidate items as its corpus. Each
item becomes one file of a throwaway corpus (one card per item: its type, then its text), the arm ranks that corpus for the request, and the first
``budget`` items it returns are the selection. Accuracy is the harmonic mean of needed-item recall and (1 - noise), where noise is the share of
selected items labelled irrelevant (helpful items are neutral); a self-contained case (no needed item) scores 1 only when nothing is selected, and
falls with each item selected. The case passes at ``PASS_ACCURACY``. Beside it the row reports ``edit_recall`` (the share of needed items whose need role is
``edit_target`` that were selected) and ``cross_quest_recall`` (the share of needed items whose ``quest_relation`` is neither ``same_quest`` nor
``same_goal`` that were selected; a selector that fences items by quest scope scores 0 there, which is what it measures), each only when the case
has such items; neither changes the accuracy. The size of what was selected is charged as cost (the share of
``SIZE_BUDGET_CHARS`` it fills, times the cost budget of ``[balance.context_selection]``), beside the store charge of ``store_cost.py``; time is
the seconds of the case. The weights of ``[balance.context_selection]`` fold these into the one number.

SCOPE MODES (flag ``QAR_PRIVATE_SCOPE_MODE``, or a ``scope_mode`` attribute on the arm; default ``none``). The same selector is scored three ways
over the same cases, which is how the quest scope mode of the product is chosen: ``none`` (no scope, today's scoring), ``fence`` (an item whose
quest is not the request's is removed from the corpus before ranking, as the hard quest fence does), and ``affinity`` (every item stays; the
ranking is re-ordered by the hub's soft affinity factor, so an item of the request's quest ranks first and the others stay reachable). Each item card
carries ``affinity_tags`` from its ``quest_relation`` (``same_quest`` / ``same_goal``: the request's tag ``TURN_TAG``; else a tag from the item's
``quest_title``, or ``quest:other``), and the request carries ``TURN_TAG``. Compare the three by running the metric with the flag set to each
value; ``cross_quest_recall`` against noise shows what each mode costs and gains. The weights are ``quest_ai_runner.core.scope_tags``
``AFFINITY_HOME_BOOST`` / ``AFFINITY_AWAY_FACTOR``.

The mix of private input kinds is set here, in ``KIND_MIX``, and nowhere else; the share of this source in the one number is in ``esoi.toml``.
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

# The weight of this source in the one number is not set here: it is the share the hub's retrieval metric leaves (esoi.toml [builds_on.context_selection.*]).
# The kinds of private input, as shares of the private cases. These are the committed fallback; when the dataset's manifest states its own
# input-kind proportions they replace these, so the mix follows how real inputs occur (self-contained 24%, one needed item 52%, several 24%).
KIND_MIX = dict(none=0.24, single=0.52, several=0.24)
PASS_ACCURACY = 0.5            # a case passes at or above this accuracy, the same half the topic cases use
SIZE_BUDGET_CHARS = 8000       # about 2000 tokens of selected context fills the size budget
TOP_K = 3
SCOPE_ENV = "QAR_PRIVATE_SCOPE_MODE"
SCOPE_MODES = ("none", "fence", "affinity")
TURN_TAG = "quest:request"       # the tag every request carries in the fence and affinity modes
HOME_RELATIONS = ("same_quest", "same_goal")


def dataset_folders(environ=None):
    """The folders named by the variable (separated by ``os.pathsep``), in order, whether or not they exist."""
    environ = os.environ if environ is None else environ
    folders = []
    for raw in (environ.get(ENV) or "").split(os.pathsep):
        if raw.strip():
            path = Path(raw.strip()).expanduser()
            folders.append(path.parent if path.is_file() else path)
    return folders


def present_folders(environ=None):
    """The named folders that hold a ``cases.jsonl``."""
    return [f for f in dataset_folders(environ) if (f / "cases.jsonl").is_file()]


def digest_parts(environ=None):
    """The recorded digest part of the private source: ``private:absent`` or ``private:<cases sha>/<manifest sha or none>`` for one folder; for
    several, those parts sorted and joined with ``+`` (so the order the folders are named in does not matter)."""
    parts = []
    for folder in present_folders(environ):
        manifest = folder / "manifest.json"
        manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else "none"
        parts.append(f"{hashlib.sha256((folder / 'cases.jsonl').read_bytes()).hexdigest()}/{manifest_sha}")
    return f"private:{'+'.join(sorted(parts))}" if parts else ABSENT


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


def task_text(raw):
    """The request an arm receives: the input text, led by one "Quest: ... Goal: ..." line when the case names its quest or goal."""
    request = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    parts = [f"{label}: {request[key]}." for label, key in (("Quest", "quest_title"), ("Goal", "goal_title")) if request.get(key)]
    text = raw.get("input_text", "")
    return f"{' '.join(parts)}\n{text}" if parts else text


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
        cases.append(dict(id=f"PRIV-{raw['case_id']}", area="real", task=task_text(raw), candidates=cands,
                          kind=kind_of(raw), heldout=False))
    return cases


def manifest_of(folder):
    try:
        return json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))["input_kind_mix"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def shares_of(mix_counts):
    total = sum(mix_counts.values())
    return {k: v / total for k, v in mix_counts.items()} if total else None


def kind_shares(folders, cases=None):
    """The shares of the three kinds: the manifests' ``input_kind_mix`` counts summed over every present folder when each has them, else (one
    folder) its proportions, else the shares of the loaded ``cases``, else ``KIND_MIX``."""
    folders = [folders] if isinstance(folders, (str, Path)) else list(folders or [])
    mixes = [manifest_of(f) for f in folders]
    try:
        if folders and all(isinstance(m, dict) and isinstance(m.get("counts"), dict) for m in mixes):
            total = dict(none=0, single=0, several=0)
            for m in mixes:
                c = m["counts"]
                total["none"] += c.get("none", 0)
                total["single"] += c.get("single", 0)
                total["several"] += c.get("multi_same_type", 0) + c.get("multi_source", 0) + c.get("several", 0)
            if shares_of(total):
                return shares_of(total)
        if len(folders) == 1 and isinstance(mixes[0], dict):
            p = mixes[0]["proportions"]
            return dict(none=p["none"], single=p["single"], several=p.get("multi_same_type", 0) + p.get("multi_source", 0))
    except (KeyError, TypeError, ValueError):
        pass
    if cases:
        return shares_of({k: sum(1 for c in cases if c["kind"] == k) for k in KIND_MIX})
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


def load_all(folders):
    """Every case of every folder, in folder order; a case id already loaded is skipped."""
    seen, cases = set(), []
    for folder in folders:
        for case in parse_cases((folder / "cases.jsonl").read_text(encoding="utf-8")):
            if case["id"] not in seen:
                seen.add(case["id"])
                cases.append(case)
    return cases


def load_cases(environ=None):
    """The private cases: none when no dataset folder is present, else every case, taken in the manifests' kind proportions. Their weight beside
    the hub's cases is the share ``esoi.toml`` gives them, not their number."""
    publish_digest(environ)
    folders = present_folders(environ)
    if not folders:
        return []
    cases = load_all(folders)
    return mix(cases, kind_shares(folders, cases), len(cases))


_SCRATCH = []


def scratch_root():
    if not _SCRATCH:
        _SCRATCH.append(tempfile.mkdtemp(prefix="qar_private_items_"))
        atexit.register(shutil.rmtree, _SCRATCH[0], True)
    return Path(_SCRATCH[0])


def item_metadata(item):
    """What the item is and where it came from, for the start of its card: ``describe`` when present, else built from the fields that exist
    ("<source or type>[: field] by <author> via <channel>, <age>, <relation>, <status>"); empty when the item has none of them."""
    title = item.get("quest_title")
    described = item.get("describe")
    if described:
        return f"{described}, quest {title}" if title and title not in described else str(described)
    head = item.get("source_kind") or item.get("type") or ""
    if head and item.get("field"):
        head += f": {item['field']}"
    if item.get("author"):
        head += f"{' ' if head else ''}by {item['author']}"
    if item.get("channel"):
        head += f"{' ' if head else ''}via {item['channel']}"
    relation = item.get("quest_relation")
    if relation and title:
        relation = f"{relation} ({title})"
    return ", ".join(str(p) for p in (head, item.get("age_label"), relation or title, item.get("status")) if p)


def safe_rel(path, used):
    """A relative corpus path from an item's ``path``: no drive, root, ``.`` or ``..`` part, and not one already used; None when nothing is left."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p not in ("", ".", "..") and not p.endswith(":")]
    if not parts:
        return None
    rel = "/".join(parts)
    if rel in used or any(u.startswith(rel + "/") or rel.startswith(u + "/") for u in used):
        return None
    return rel


def scope_mode_of(fn=None, environ=None):
    """The scope mode in force: the arm's ``scope_mode`` attribute, else the flag, else ``none`` (an unknown value is ``none``)."""
    environ = os.environ if environ is None else environ
    mode = str(getattr(fn, "scope_mode", None) or environ.get(SCOPE_ENV) or "none").strip().lower()
    return mode if mode in SCOPE_MODES else "none"


def item_affinity_tags(item):
    """The affinity tags of one candidate item: the request's tag when it is of the request's quest or goal, a tag from its quest title when
    it names another, ``quest:other`` for another quest without a title, and none when the item says nothing about a quest."""
    relation = item.get("quest_relation")
    if relation in HOME_RELATIONS:
        return [TURN_TAG]
    if not relation and not item.get("quest_title"):
        return []
    slug = "-".join(str(item.get("quest_title") or "").lower().split())[:40]
    return [f"quest:{slug}" if slug else "quest:other"]


def reorder_by_affinity(ranked, owner, candidates):
    """``ranked`` (file paths, best first) re-ordered by position score ``1/(rank+1)`` times the hub's affinity factor for the item's tags against
    the request's; ties keep the arm's order. Nothing is removed."""
    from hubofhubs.recall.affinity_tags import affinity_factor
    from quest_ai_runner.core.scope_tags import AFFINITY_AWAY_FACTOR, AFFINITY_HOME_BOOST
    by_id = {c["item_id"]: c for c in candidates}

    def score(pair):
        rank, rel = pair
        tags = item_affinity_tags(by_id.get(owner.get(rel), {}))
        return -(affinity_factor(tags, [TURN_TAG], home_boost=AFFINITY_HOME_BOOST, away_factor=AFFINITY_AWAY_FACTOR) / (rank + 1)), rank
    return [rel for _, rel in sorted(enumerate(ranked), key=score)]


def item_corpus(case, scope_mode="none"):
    """The case's candidate items as a corpus: one text file and one card per item (the card starts with the item's metadata, then its text).
    An item with a ``path`` is written under that path (sanitized, unique), else as ``item_NNN.txt``. Returns ``(corpus, {file path: item id})``."""
    from context_selection.corpus import Corpus
    from hubofhubs.recall.context_card_extraction import SOURCE_FILE_WEIGHT
    root = Path(tempfile.mkdtemp(dir=scratch_root()))
    corpus, owner, used = Corpus(root=root), {}, set()
    for index, item in enumerate(case["candidates"]):
        if scope_mode == "fence" and item_affinity_tags(item) not in ([], [TURN_TAG]):
            continue   # another quest's item never reaches the selector
        text = item.get("text") or ""
        rel = safe_rel(item.get("path"), used) or f"item_{index:03d}.txt"
        while rel in used:
            rel = f"item_{index:03d}_{len(used)}.txt"
        try:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        except OSError:
            rel = f"item_{index:03d}.txt"
            (root / rel).write_text(text, encoding="utf-8")
        used.add(rel)
        flat = " ".join(text.split())
        meta = item_metadata(item) or item.get("type") or "item"
        corpus.files.append(rel)
        corpus.cards[f"item-{index:03d}"] = dict(
            id=f"item-{index:03d}", keywords=[], summary=f"{meta}: {flat[:200]}", description=flat[200:900],
            files=[dict(path=rel, symbols=[])], weight=SOURCE_FILE_WEIGHT)
        if scope_mode != "none":
            corpus.cards[f"item-{index:03d}"]["affinity_tags"] = item_affinity_tags(item)
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
    out = dict(recall=round(recall, 4), noise=round(noise, 4), accuracy=round(accuracy, 4), selected=n, size_chars=chars)
    edit = {i for i in needed if by_id[i].get("need_role") == "edit_target"}
    if edit:
        out["edit_recall"] = round(sum(1 for i in picked if i in edit) / len(edit), 4)
    cross = {i for i in needed if by_id[i].get("quest_relation") not in (None, "same_quest", "same_goal")}
    if cross:
        out["cross_quest_recall"] = round(sum(1 for i in picked if i in cross) / len(cross), 4)
    return out


def score_case(fn, case):
    mode = scope_mode_of(fn)
    corpus, owner = item_corpus(case, mode)
    ranked = fn(case["task"], corpus)
    if mode == "affinity":
        ranked = reorder_by_affinity(ranked, owner, case["candidates"])
    budget = getattr(fn, "budget", TOP_K)
    row = numbers([owner[r] for r in ranked[:budget] if r in owner], case["candidates"])
    meter = getattr(fn, "meter", None)
    if meter is not None:   # reading the case's candidate items is a source read (priced and bounded by [balance.context_selection.source_access])
        meter.read(records=len(case["candidates"]), bytes=sum(len(c.get("text") or "") for c in case["candidates"]), calls=1, scope="quest")
    if meter is not None:   # the size of what was selected is the cost of the context
        from context_selection.store_cost import cost_budget
        meter.cost_usd += cost_budget() * min(row["size_chars"] / SIZE_BUDGET_CHARS, 1.0)
    return dict(row, ok=row["accuracy"] >= PASS_ACCURACY, top1=False, top3=False, private=True, kind=case["kind"],
                got=f"{row['selected']} selected, recall {row['recall']}, noise {row['noise']}",
                note=f"{case['kind']} input, accuracy {row['accuracy']}"
                     + "".join(f", {k.replace('_', ' ')} {row[k]}" for k in ("edit_recall", "cross_quest_recall") if k in row))
