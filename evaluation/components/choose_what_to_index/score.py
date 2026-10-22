"""Score one choose-what-to-index case: build the fixture corpus, install the solution at the gate seam, run the
library's real bootstrap, then count what the lane took in.

Counted per case:
  series_cards  cards imported from the snapshot series (copy mode), or snapshot stores mounted (mount mode)
  series_open   snapshot folders the walk may enter (not excluded by the gate the bootstrap was handed)
  nested_ok     the legitimate nested project's store was imported or mounted
  docs_ok       the ordinary docs folder is not excluded
  review_calls  folder review calls the reviewer answered (one call judges up to forty folders, never a file)

The reviewer a case names decides what the folder review hears back: ``none`` (no provider), ``off``
(QAR_FOLDER_REVIEW=0), ``failing`` (every review call raises), ``skip`` (a reviewer that judges correctly: skip
the snapshots, index the rest) or ``index`` (index every folder). A case passes when the series is kept out
(``block_series``: no series card and no snapshot folder open) or, where a reviewer or a person said index, is let
in (the verdict is honoured, not overruled), AND the nested store and the docs folder are still taken in. A case
marked ``known_gap`` is a weakness of every arm today, kept so that a fix shows up as a passing case."""
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from choose_what_to_index import fixtures
from quest_ai_runner.adapters import context_store_bootstrap, context_store_bootstrap_walk
from quest_ai_runner.adapters.file_context_store import FileContextStore

SEAMS = (context_store_bootstrap, context_store_bootstrap_walk)


class Reviewer:
    """Answers the folder review prompt by policy; any other prompt gets no topics, so no card is derived."""

    def __init__(self, policy):
        self.policy = policy
        self.calls = 0

    def list_models(self):
        return []

    def answer(self, messages, model=None, **kw):  # noqa: ARG002
        text = messages[0]["content"]
        if "FOLDERS:" not in text:
            return "[]"
        self.calls += 1
        if self.policy == "failing":
            raise RuntimeError("review unavailable")
        folders = re.findall(r'^- "([^"]+)"', text, re.M)
        return json.dumps([{"folder": f, "mixed": False, "reason": "fixed verdict",
                            "index": self.policy == "index" or not f.startswith(fixtures.SERIES_DIR)}
                           for f in folders])


@contextmanager
def installed(fn, seen):
    def recording(*args, **kw):
        gate = fn(*args, **kw)
        seen.append(gate)
        return gate
    old = [m.folder_gate for m in SEAMS]
    old_env = os.environ.get("QAR_FOLDER_REVIEW")
    try:
        for m in SEAMS:
            m.folder_gate = recording
        yield
    finally:
        for m, o in zip(SEAMS, old):
            m.folder_gate = o
        if old_env is None:
            os.environ.pop("QAR_FOLDER_REVIEW", None)
        else:
            os.environ["QAR_FOLDER_REVIEW"] = old_env


def score_case(fn, case):
    work = Path(tempfile.mkdtemp(prefix="qar_choose_index_"))
    mount = case.get("mode") == "mount"
    seen = []
    try:
        corpus = work / "corpus"
        cards_dir = corpus / ".quest-context" if mount else work / "lane_cards"
        fixtures.build(corpus, cards_dir, case)
        policy = case["reviewer"]
        provider = None if policy == "none" else Reviewer(policy)
        with installed(fn, seen):
            if policy == "off":
                os.environ["QAR_FOLDER_REVIEW"] = "0"
            store = FileContextStore(str(cards_dir), repo_root=str(corpus), auto_bootstrap=False,
                                     **({"mount_mode": "mount", "confidence_threshold": 0.0} if mount else {}))
            store.bootstrap(root=str(corpus), provider=provider)
        series_root = (corpus / fixtures.SERIES_DIR).resolve()
        if mount:
            roots = [Path(getattr(m, "root", "") or "").resolve() for m in store._mounts]
            series_cards = sum(1 for r in roots if series_root in r.parents)
            nested_ok = (corpus / fixtures.NESTED_DIR).resolve() in roots
        else:
            ids = list(store._load_all())
            series_cards = sum(1 for i in ids if "snap" in i)
            nested_ok = any(fixtures.NESTED_CARD in i for i in ids)
        gate = seen[-1] if seen else None
        snaps = [corpus / fixtures.SERIES_DIR / d for d in fixtures.dates(case.get("dates", 4))]
        series_open = sum(1 for s in snaps if gate is None or not gate.excludes_dir(s))
        docs_ok = gate is None or not gate.excludes_dir(corpus / fixtures.DOCS_DIR)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    problems = []
    if case["block_series"] and (series_cards or series_open):
        problems.append(f"series taken in: {series_cards} {'mounts' if mount else 'cards'}, "
                        f"{series_open} snapshot folders open to the walk")
    if not case["block_series"] and not series_cards:
        problems.append("a reviewer's or a person's index verdict was overruled")
    if not nested_ok:
        problems.append("the legitimate nested store was not taken in")
    if not docs_ok:
        problems.append("the ordinary docs folder was excluded")
    return dict(ok=not problems, series_cards=series_cards, series_open=series_open, nested_ok=nested_ok,
                docs_ok=docs_ok, reviewer=policy, review_calls=provider.calls if provider else 0, block=case["block_series"], known_gap=bool(case.get("known_gap")),
                got=f"series {series_cards}/{series_open} open, nested {nested_ok}, docs {docs_ok}",
                note="ok" if not problems else "; ".join(problems))


def aggregate(rows):
    def taken(policy):
        rs = [r for r in rows if r.get("reviewer") == policy and r.get("block")]
        return sum(r.get("series_cards", 0) for r in rs), sum(r.get("series_open", 0) for r in rs)
    parts = []
    for policy in ("none", "off", "failing", "skip"):
        cards, open_ = taken(policy)
        parts.append(f"{policy} {cards}/{open_}")
    lost_nested = sum(1 for r in rows if "nested_ok" in r and not r["nested_ok"])
    lost_docs = sum(1 for r in rows if "docs_ok" in r and not r["docs_ok"])
    overruled = sum(1 for r in rows if "block" in r and not r["block"] and not r.get("series_cards"))
    gaps = sum(1 for r in rows if r.get("known_gap") and not r.get("ok"))
    calls = sum(r.get("review_calls", 0) for r in rows)
    return (f"series taken in where it should be kept out (imports or mounts / snapshot folders open), by reviewer: "
            f"{', '.join(parts)}; nested store lost {lost_nested}; docs folder excluded {lost_docs}; "
            f"index verdicts overruled {overruled}; known-gap cases failing {gaps}; folder review calls {calls}")
