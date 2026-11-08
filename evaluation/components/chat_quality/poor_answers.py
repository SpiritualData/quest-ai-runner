"""Private poorly answered inputs as a conditional case source of the one chat-quality metric.

The cases are real requests that production answered badly, each labelled with how it failed and how it should have been handled. They are kept
OUTSIDE this repository, in the file (or the folder holding ``poorly_answered_inputs.jsonl``) named by ``QAR_PRIVATE_POOR_ANSWERS_DATASET``
(several separated by ``os.pathsep``). With nothing present the source contributes no case and the metric runs on the dataset cases alone; nothing
fails. Nothing from the data is copied here: the digest of the cases records the sha256 of every present file (or ``private:absent``), written
into the untracked ``datasets/case_sources.local.json`` (only when a file is present) before the case files are hashed, so a row recorded with the set and a row recorded without it never compare.

A private case runs through the same chat arm as every other case (a fresh conversation, no quest pinned) and is judged by the same judge. Its
rubric is the case's own ``pass_criteria`` when it lists them, else built from its ``failure_kind`` and ``ideal_handling``: generic checks that the dev eval world can satisfy (the world holds no
data of the organization the request came from), so a case passes by honest, correctly routed, non-fabricating handling, never by facts the world
lacks. The ideal handling and what went wrong go to the judge as ``evidence_notes``.

The share of this source in the one number is set in ``esoi.toml`` (``source_shares`` of ``[balance.chat_quality]``), before any case is scored:
``weighted_rate`` folds the pass rate of the other cases and the pass rate of this source in that share, which is the headline ``score.aggregate``
prints and the number the combined QAR metric reads for the chat arm.
"""
import hashlib
import json
import os
from pathlib import Path

ENV = "QAR_PRIVATE_POOR_ANSWERS_DATASET"
FILE_NAME = "poorly_answered_inputs.jsonl"
SOURCE = "poor_answers"
ABSENT = "private:absent"
HERE = Path(__file__).resolve().parent
SOURCES_FILE = HERE / "datasets" / "case_sources.local.json"   # untracked (gitignored); the committed case_sources.json always says ``private:absent``

GENERIC_RUBRIC = (
    "Does not hand the work back to the person when the assistant could act, search or read for itself (no 'you could check' or 'please look it up' in place of doing it)",
    "States no invented fact, count, name, date or blocker: every specific claim is one the reply shows it found or read",
    "Takes a route that fits the request, which is {kind}: it answers, searches, asks one specific question or hands off, and does not stall or go silent",
    "Says plainly what it could not see or do instead of guessing, and still gives the person a usable next step",
)


def data_files(environ=None):
    """The poorly answered files present: a named file as is, a named folder through ``poorly_answered_inputs.jsonl`` inside it."""
    environ = os.environ if environ is None else environ
    found = []
    for raw in (environ.get(ENV) or "").split(os.pathsep):
        if not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        path = path / FILE_NAME if path.is_dir() else path
        if path.is_file():
            found.append(path)
    return found


def digest_part(environ=None):
    """``private:absent`` or ``poor_answers:<sha256 of each present file>`` (sorted and joined with ``+``, so the order does not matter)."""
    shas = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in data_files(environ))
    return f"{SOURCE}:{'+'.join(shas)}" if shas else ABSENT


def publish_digest(environ=None):
    """Write the digest where the harness hashes the case files (``datasets/``) when a private set is present, into the untracked
    ``case_sources.local.json`` beside the committed ``case_sources.json`` (which stays ``private:absent``), and remove that file when none is
    present: a run that saw the dataset never changes a tracked file, so no private digest can be committed."""
    part = digest_part(environ)
    if part == ABSENT:
        SOURCES_FILE.unlink(missing_ok=True)
        return
    text = json.dumps({SOURCE: part}) + "\n"
    if not SOURCES_FILE.exists() or SOURCES_FILE.read_text(encoding="utf-8") != text:
        SOURCES_FILE.write_text(text, encoding="utf-8")


def case_of(raw):
    """One chat_quality case from a poorly answered record, or None when it has no id or no input text."""
    case_id, text = raw.get("case_id"), (raw.get("input_text") or "").strip()
    if not case_id or not text:
        return None
    kind = str(raw.get("failure_kind") or "an ordinary request").strip()
    notes = [f"source: {SOURCE} (private). failure kind: {kind}.",
             f"what went wrong: {raw.get('what_went_wrong') or 'not recorded'}",
             f"ideal: {raw.get('ideal_handling') or 'not recorded'}",
             "The dev eval world holds no data of the organization this request came from, so a reply passes by honest, correctly routed, "
             "non-fabricating handling that follows the shape of the ideal, never by facts the world lacks."]
    return dict(id=str(case_id), dataset="implicit", source=SOURCE, private=True, area=f"poor answers: {kind}", category=kind,
                capability=str(raw.get("capability_tested") or ""), message=text, conversation_scope="none", expected_routing="any",
                rubric=[str(item) for item in raw["pass_criteria"]] if isinstance(raw.get("pass_criteria"), list) and raw["pass_criteria"]
                else [item.format(kind=kind) for item in GENERIC_RUBRIC],     # the case's own criteria when it states them forbid_writes=False, pivots_change_answer=False,
                precheck={"reply_regex": [r"\S"]}, evidence_notes=" ".join(notes),
                **{key: raw[key] for key in ("effect", "state_check", "metric_probe", "increment", "increment_kind", "group", "cue_resolves_to", "esoi", "speed_class", "speed_target_seconds", "token_target", "history") if raw.get(key)})   # optional: the writes the case allows on the bounded production arm and how to count its effect


def parse_cases(text):
    """Cases from jsonl text; a line that is not a record with an id and an input is skipped."""
    cases = []
    for line in text.splitlines():
        try:
            raw = json.loads(line) if line.strip() else None
        except ValueError:
            raw = None
        case = case_of(raw) if isinstance(raw, dict) else None
        if case:
            cases.append(case)
    return cases


def load_cases(environ=None):
    """Every case of every present file (a case id already loaded is skipped); none when nothing is present."""
    publish_digest(environ)
    seen, cases = set(), []
    for path in data_files(environ):
        for case in parse_cases(path.read_text(encoding="utf-8")):
            if case["id"] not in seen:
                seen.add(case["id"])
                cases.append(case)
    return cases


def share(table=None):
    """The share of this source in the one number, from ``[balance.chat_quality]`` (``source_shares``); ``table`` is for tests."""
    if table is None:
        import common
        table = common.CONFIG.balance.get("chat_quality") or {}
    value = (table.get("source_shares") or {}).get(SOURCE)
    if value is None or not 0 < float(value) < 1:
        raise ValueError(f"[balance.chat_quality] source_shares needs {SOURCE} between 0 and 1, got {value!r}")
    return float(value)


def weighted_rate(passed_other, total_other, passed_source, total_source, source_share):
    """The pass rate with this source at ``source_share`` of the number: the other cases carry the rest. With no case of this source (or of the
    others) the rate is the plain pass rate of the cases there are."""
    if not total_source:
        return passed_other / total_other if total_other else 0.0
    if not total_other:
        return passed_source / total_source
    return (1 - source_share) * passed_other / total_other + source_share * passed_source / total_source


def rate_of_rows(rows, source_share):
    other = [r for r in rows if r.get("source") != SOURCE]
    mine = [r for r in rows if r.get("source") == SOURCE]
    return weighted_rate(sum(bool(r["ok"]) for r in other), len(other), sum(bool(r["ok"]) for r in mine), len(mine), source_share)
