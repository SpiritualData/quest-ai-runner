"""Folder review: which folders deserve cards, nested-repo and duplicate-folder detection, final verdicts.

Split out of ``file_context_store.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

from hubofhubs.recall.walk import is_claude_state_file, prune_dirnames
from .context_bootstrap_support import _BOOTSTRAP_MAX_BYTES, _SOURCE_EXTS, _extract_json_array, _log


# ---------------------------------------------------------------------------
# Folder relevance review: WHICH FOLDERS ARE WORTH INDEXING AT ALL
# ---------------------------------------------------------------------------
# Extension filtering decides whether a FILE is readable. It cannot decide whether a folder is
# knowledge. Measured on one real corpus of 81,465 "indexable" files: 79% of it was a single
# vendored tree present twice under different repos, and 66,000 of the files were .json/.yaml
# generated data. Indexing that is not merely wasteful -- it drowns the cards that matter, and
# every one of those files costs model calls to summarise and memory to embed.
#
# The unit of judgement is the FOLDER, not the file, because that is the unit a human would use
# ("that is a downloaded SDK", "that is a data dump") and because it collapses the problem from
# 15,423 directories to a few dozen decisions. The review is ADAPTIVE: shallow folders first, and
# only a folder that is both kept AND large is opened up a level. On the same corpus that is ~124
# folders reviewed in 3-4 batched calls, versus 15,423 directories.
#
# Verdicts are cached in the cards dir so the cost is paid once and a human can read, edit or
# delete the file to override any decision. ``QAR_FOLDER_REVIEW=0`` turns the whole thing off.

_FOLDER_REVIEW_FILE = "folder_review.json"


# Bumped when the MEANING of a cached verdict changes. v2 introduced "final" verdicts (see
# ``verdict_is_final``): before it, a deterministic exclusion could be re-opened and then vetoed
# by its own children, so one folder could hold two opposite verdicts at once.
_FOLDER_REVIEW_VERSION = 2


_FOLDER_REVIEW_BATCH = 40        # folders per LLM call


_FOLDER_REVIEW_MIN_FILES = 2000  # below this the skip list is enough; do not spend model calls


_FOLDER_REVIEW_START_DEPTH = 2   # first level judged, relative to the corpus root


_FOLDER_REVIEW_MAX_DEPTH = 6     # never descend past this


_FOLDER_REVIEW_EXPAND_OVER = 400 # a kept folder with more files than this gets opened up a level


# Risk signal kinds (``context_risk_gate``) that make a folder unsafe to index when the review
# cannot judge it. "large" is context for the model only: a big folder is not dangerous by itself.
HARD_RISK_KINDS = ("stores", "series", "copy")


_FOLDER_REVIEW_PROMPT = """You decide which folders of a knowledge corpus are worth indexing for
semantic search. The index is used to recall context for work on this project.

INDEX a folder when its files are things someone would want recalled: source the team writes,
documentation, notes, plans, specs, or configuration that describes the project.

SKIP a folder when its files are not knowledge:
- generated or derived data (datasets, dumps, exports, fixtures, snapshots, caches)
- third-party or vendored code the team did not write (downloaded packages, SDKs, plugin
  marketplaces, bundled libraries, toolchains)
- backups, archives, or a duplicate copy of another folder
- build or compile output
- another tool's internal state store

A folder marked [contains CLAUDE.md] (or AGENTS.md, .cursorrules, and so on) has a file someone
wrote to orient an assistant working there. Treat that as evidence the folder is curated knowledge
and lean toward INDEX. It is evidence, not proof: a vendored copy of someone else's project
carries their instruction file too, so it does not outweigh a folder that is plainly generated
data or a dependency checkout.

When a folder plainly holds BOTH kinds -- a workspace with real notes next to a vendored
dependency, say -- do not guess. Answer "mixed": true and it will be opened up and its children
judged individually. Prefer "mixed" over a confident SKIP whenever a folder is large and varied:
excluding it wholesale throws away everything beneath it.

A folder marked [RISK: ...] tripped a cheap structural check: it holds several card stores of
its own, or it looks like a series of dated or numbered copies of one tree (daily backups,
snapshots, release archives), or it is very large. A series of copies and the card stores inside
them describe the same material many times over, so lean toward SKIP unless the folder plainly
holds the only copy of real, current knowledge.

Reply with ONLY a JSON array, one object per folder given, no prose:
[{"folder": "<exact folder string>", "index": true, "mixed": false, "reason": "<at most 8 words>"}]
"""


# Files a human wrote to orient an AI assistant in a folder. Their presence is the strongest
# cheap signal that a folder is CURATED KNOWLEDGE rather than incidental content: somebody cared
# enough about this directory to explain it. Deliberately not tied to one vendor -- a corpus does
# not stop being knowledge because the team uses a different assistant.
#
# It is a signal and NOT a verdict, which matters because the obvious failure is real: a vendored
# dependency clone carries its upstream's CLAUDE.md too, and that file is orientation for THAT
# project, not this corpus. So the deterministic safeguards (nested repository, fuzzy duplicate)
# are applied BEFORE this is ever consulted, and an instruction file cannot rescue a folder they
# have already excluded. What it does is tip the model toward keeping a folder it would otherwise
# be unsure about, and mark the folder as one whose contents deserve priority.
_AI_INSTRUCTION_FILES = (
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", "AGENT.md",
    ".cursorrules", ".windsurfrules", "copilot-instructions.md", "llms.txt",
)


def _ai_instruction_files_in(walk_root: Path, folder: str, limit: int = 400) -> List[str]:
    """Names of AI-orientation files at or under ``folder`` (deduped, capped). Never raises."""
    found: List[str] = []
    base = walk_root / folder if folder != "." else walk_root
    seen = set()
    try:
        checked = 0
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _NESTED_VCS_MARKERS
                           and d != "__pycache__"]
            for name in filenames:
                if name in _AI_INSTRUCTION_FILES and name not in seen:
                    seen.add(name)
                    found.append(name)
            checked += 1
            if checked >= limit or len(found) >= 4:
                break
    except Exception:  # noqa: BLE001
        pass
    return found


def _skip_nested_repos() -> bool:
    """Whether a directory with its own VCS metadata is excluded automatically (default yes)."""
    return os.getenv("QAR_SKIP_NESTED_REPOS", "1").strip().lower() not in ("0", "false", "no")


def _folder_review_enabled() -> bool:
    return os.getenv("QAR_FOLDER_REVIEW", "1").strip().lower() not in ("0", "false", "no")


def _dir_file_counts(walk_root: Path, skip_dirs: Set[str]) -> Dict[str, int]:
    """``{relative dir: indexable file count}`` for every dir holding indexable files."""
    counts: Dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(walk_root):
        prune_dirnames(dirnames, current=Path(dirpath), base_skip=skip_dirs)
        n = sum(1 for fn in filenames
                if not is_claude_state_file(fn) and Path(fn).suffix.lower() in _SOURCE_EXTS)
        if n:
            try:
                counts[Path(dirpath).relative_to(walk_root).as_posix()] = n
            except ValueError:
                continue
    return counts


def _roll_up_to_depth(counts: Dict[str, int], depth: int) -> Dict[str, int]:
    """Aggregate per-directory counts onto their ancestor at ``depth`` path segments."""
    rolled: Dict[str, int] = {}
    for rel, n in counts.items():
        parts = [p for p in rel.split("/") if p and p != "."]
        key = "/".join(parts[:depth]) if parts else "."
        rolled[key] = rolled.get(key, 0) + n
    return rolled


def _sample_filenames(walk_root: Path, folder: str, limit: int = 6) -> List[str]:
    """A few filenames from ``folder``, to give the judgement something concrete. Never raises."""
    names: List[str] = []
    base = walk_root / folder if folder != "." else walk_root
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            for fn in filenames:
                if not is_claude_state_file(fn) and Path(fn).suffix.lower() in _SOURCE_EXTS:
                    names.append(fn)
                    if len(names) >= limit:
                        return names
            if len(names) >= limit:
                break
    except Exception:  # noqa: BLE001
        pass
    return names


def _review_folder_batch(batch: List[Tuple[str, int]], walk_root: Path, provider,
                         model: Optional[str],
                         notes: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """One LLM call judging up to ``_FOLDER_REVIEW_BATCH`` folders. Never raises; {} on failure.

    ``notes`` maps a folder to the risk signals that put it in front of the model (see
    ``context_risk_gate``); they are shown beside the folder as ``[RISK: ...]``.
    """
    lines = []
    for folder, count in batch:
        samples = ", ".join(_sample_filenames(walk_root, folder)) or "(none)"
        guides = _ai_instruction_files_in(walk_root, folder)
        hint = f" [contains {', '.join(guides)}]" if guides else ""
        risk = f" [RISK: {notes[folder]}]" if notes and notes.get(folder) else ""
        size = f"{count} files" if count else "size not measured"
        lines.append(f'- "{folder}" ({size}){hint}{risk} e.g. {samples}')
    prompt = _FOLDER_REVIEW_PROMPT + "\n\nFOLDERS:\n" + "\n".join(lines)
    try:
        raw = provider.answer([{"role": "user", "content": prompt}], model=model)
    except Exception:  # noqa: BLE001 -- a review that fails must not stop the bootstrap
        _log.info("context index: folder review call failed; those folders default to INDEXED",
                  exc_info=True)
        return {}
    # _extract_json_array returns the array SUBSTRING (fences and prose stripped), not a value.
    blob = _extract_json_array(raw) if raw else ""
    try:
        parsed = json.loads(blob) if blob else None
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, list):
        _log.info("context index: folder review returned no usable JSON; those folders default "
                  "to INDEXED")
        return {}
    # Only the folders this batch ASKED about are accepted. The model is answering a question, so
    # a key it was not asked for is not an answer, and merging one writes a verdict for a folder
    # nothing judged. The case that matters: an unsolicited entry for a path inside a folder
    # already excluded for good would land in the cache for that pass, even though nothing under
    # a final exclusion is supposed to have a verdict at all. Cheaper and more honest to refuse
    # the key than to clean up after it.
    requested = {folder for folder, _ in batch}
    out: Dict[str, Dict[str, Any]] = {}
    unsolicited: List[str] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        folder = str(entry.get("folder") or "").strip()
        if not folder:
            continue
        if folder not in requested:
            unsolicited.append(folder)
            continue
        out[folder] = {"index": bool(entry.get("index", True)),
                       "mixed": bool(entry.get("mixed", False)),
                       "reason": str(entry.get("reason") or "")[:120]}
    if unsolicited:
        _log.info("context index: folder review returned %d verdict(s) for folder(s) it was not "
                  "asked about; ignored: %s", len(unsolicited),
                  ", ".join(sorted(unsolicited)[:5]))
    return out


# --- Safeguard 1: a nested repository is a dependency checkout, not your knowledge -------------
#
# Deterministic and cheap. A directory inside the corpus that carries its OWN ``.git`` is a
# separate repository someone checked out in place -- a vendored dependency, a sibling service, a
# downloaded tool. Its contents belong to that project and are recoverable from its own remote, so
# indexing them buries the corpus's real material under code nobody here wrote. This alone catches
# the case that motivated the whole review: two clones of one dependency repo accounted for 79% of
# a corpus, and neither was junk on disk -- both were working checkouts -- so no amount of "delete
# the duplicate" advice would have helped. They simply are not knowledge.
_NESTED_VCS_MARKERS = (".git", ".hg", ".svn")


def _nested_vcs_dirs(walk_root: Path, skip_dirs: Set[str]) -> Dict[str, str]:
    """``{relative dir: marker}`` for directories holding their own VCS metadata (root excluded)."""
    found: Dict[str, str] = {}
    for dirpath, dirnames, _filenames in os.walk(walk_root):
        prune_dirnames(dirnames, current=Path(dirpath), base_skip=skip_dirs)
        here = Path(dirpath)
        if here.resolve() == walk_root.resolve():
            continue
        for marker in _NESTED_VCS_MARKERS:
            if (here / marker).exists():
                try:
                    found[here.relative_to(walk_root).as_posix()] = marker
                except ValueError:
                    pass
                # Do not descend into a repository we have already classified whole.
                dirnames[:] = []
                break
    return found


# --- Safeguard 2: fuzzy duplicate detection ---------------------------------------------------
#
# Also deterministic, and sampled so it stays cheap. Two folders are compared on a SAMPLE of
# (relative path, content hash) pairs; the overlap is a percentage rather than a yes/no, because
# real duplicates in a working tree are never byte-identical -- they are the same tree at
# different commits, with different caches and generated files beside them. The pair that started
# this measured 19,222 differing entries and was still, unmistakably, the same thing twice.
_DUP_SAMPLE_FILES = 120


_DUP_MIN_OVERLAP = 0.70


def _folder_signature(walk_root: Path, folder: str, limit: int = _DUP_SAMPLE_FILES) -> Set[str]:
    """A sampled ``{relpath:sha1-prefix}`` set identifying a folder's content. Never raises."""
    import hashlib
    base = walk_root / folder if folder != "." else walk_root
    sig: Set[str] = set()
    try:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _NESTED_VCS_MARKERS
                           and d != "__pycache__"]
            for fn in sorted(filenames):
                if is_claude_state_file(fn) or Path(fn).suffix.lower() not in _SOURCE_EXTS:
                    continue
                fp = Path(dirpath) / fn
                try:
                    if fp.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                        continue
                    digest = hashlib.sha1(fp.read_bytes()).hexdigest()[:12]
                except OSError:
                    continue
                sig.add(f"{fp.relative_to(base).as_posix()}:{digest}")
                if len(sig) >= limit:
                    return sig
    except Exception:  # noqa: BLE001
        pass
    return sig


def _duplicate_folders(walk_root: Path, folders: List[str],
                       counts: Dict[str, int]) -> Dict[str, Tuple[str, float]]:
    """``{folder: (duplicate_of, overlap)}`` for folders that mirror a larger sibling.

    The SMALLER of a duplicate pair is the one reported, so the fuller copy survives. Never
    raises; returns {} if signatures cannot be built.
    """
    sigs: Dict[str, Set[str]] = {}
    for f in folders:
        sig = _folder_signature(walk_root, f)
        if len(sig) >= 20:          # too few files to judge similarity meaningfully
            sigs[f] = sig
    dupes: Dict[str, Tuple[str, float]] = {}
    names = sorted(sigs, key=lambda f: -_folder_file_count(counts, f))
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if b in dupes:
                continue
            inter = len(sigs[a] & sigs[b])
            overlap = inter / max(1, min(len(sigs[a]), len(sigs[b])))
            if overlap >= _DUP_MIN_OVERLAP:
                dupes[b] = (a, overlap)
    return dupes


def _is_excluded_folder(rel: str, excluded: Set[str]) -> bool:
    """Whether ``rel`` is, or sits under, an excluded folder. ``"."`` is never excluded here.

    The CORPUS ROOT is not an excludable folder, and an entry of ``"."`` in ``excluded`` must not
    be read as one: ``"."`` is a prefix of every relative path written with a leading ``./``, so
    one such entry excluded the whole corpus. Measured live: a real store's cached review held
    ``{".": {"index": false, "reason": "root mixes scripts, docs, state, credentials"}}`` and the
    bootstrap walk then collected 13 files out of 76,593, which looks exactly like a corpus with
    nothing in it. A verdict about the root is a verdict about its OWN loose files at most, and
    those are judged with the rest; it can never mean "index nothing".
    """
    if not rel or rel == ".":
        return False
    return any(rel == e or rel.startswith(e + "/")
               for e in excluded if e and e != ".")


def _folder_file_count(counts: Dict[str, int], folder: str) -> int:
    """Indexable files at or under ``folder``."""
    return sum(n for f, n in counts.items() if f == folder or f.startswith(folder + "/"))


def _has_kept_child(verdicts: Dict[str, Dict[str, Any]], folder: str) -> bool:
    """Whether any directly-judged descendant of ``folder`` was kept."""
    prefix = folder + "/"
    return any(f.startswith(prefix) and v.get("index", True) for f, v in verdicts.items())


# A verdict's SOURCE decides whether it can be argued with.
#
# The two deterministic safeguards (nested repository, fuzzy duplicate) answer a question the
# model cannot: "does this content belong to this corpus at all". A vendored checkout is not more
# indexable because the files inside it look like good documentation, and a duplicate tree is not
# more indexable because its children are individually fine. So those verdicts are FINAL: nothing
# under them is judged, re-opened, or allowed to veto them.
#
# The bug this closes, measured on a real corpus: a nested repository of 23k generated files was
# correctly excluded, then opened up anyway because it was large, its 1,211 children were judged
# individually and kept, and ``_has_kept_child`` read those keeps as "the parent's verdict was
# superseded" and let the whole subtree back in. 2,082 cards of generated subject-matter summaries
# followed. One folder, two opposite verdicts, from the same bytes.
#
# Only a MODEL skip may be re-reviewed, and only when the model itself asked for that by answering
# "mixed": that is the one case where the verdict is explicitly provisional.
_FINAL_VERDICT_SOURCES = ("nested_repo", "duplicate")


def verdict_is_final(verdict: Optional[Dict[str, Any]]) -> bool:
    """Whether ``verdict`` was decided deterministically and may not be argued with.

    An explicit ``final`` in the file wins, including ``false``: the cache has always been the
    surface a human edits to overturn a decision, and a rule that cannot be overturned there is
    not a rule, it is a wall.
    """
    if not isinstance(verdict, dict):
        return False
    if "final" in verdict:
        return bool(verdict.get("final"))
    return str(verdict.get("source") or "") in _FINAL_VERDICT_SOURCES


def verdict_is_pinned(verdict: Optional[Dict[str, Any]]) -> bool:
    """Whether a human pinned this verdict, so no automatic pass may overwrite or drop it.

    Set ``"pinned": true`` on a folder in ``folder_review.json`` to keep a decision whatever the
    deterministic safeguards would say about it, e.g. to index a nested repository that genuinely
    IS this corpus's own work rather than a dependency checkout.
    """
    return isinstance(verdict, dict) and bool(verdict.get("pinned"))


def final_exclusion_covering(verdicts: Dict[str, Dict[str, Any]], folder: str) -> Optional[str]:
    """The final-excluded folder at or above ``folder``, or None when there is none."""
    parts = [p for p in folder.split("/") if p and p != "."]
    for depth in range(1, len(parts) + 1):
        candidate = "/".join(parts[:depth])
        verdict = verdicts.get(candidate)
        if verdict is not None and not verdict.get("index", True) and verdict_is_final(verdict):
            return candidate
    return None


def drop_verdicts_under_final_exclusions(verdicts: Dict[str, Dict[str, Any]]) -> List[str]:
    """Remove every verdict sitting UNDER a final exclusion. Returns the dropped folders.

    This is the cache invalidation for the bug described above: a store written by the buggy path
    holds model verdicts for folders inside a final exclusion, and keeping them would re-open the
    subtree on every later pass. Dropping them in CODE (rather than asking somebody to hand-edit
    the cache) is what makes the fix stick for stores that already exist.
    """
    dropped = [f for f in list(verdicts)
               if not verdict_is_pinned(verdicts.get(f))
               and final_exclusion_covering(verdicts, f) not in (None, f)]
    for folder in dropped:
        verdicts.pop(folder, None)
    return dropped


def load_verdicts(cards_dir: Path) -> Dict[str, Dict[str, Any]]:
    """The cached ``folder_review.json`` verdicts, ``{}`` when missing or unreadable."""
    try:
        cache_path = Path(cards_dir) / _FOLDER_REVIEW_FILE
        if cache_path.exists():
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("folders"), dict):
                return loaded["folders"]
    except Exception:  # noqa: BLE001
        pass
    return {}


def _folder_review(walk_root: Path, skip_dirs: Set[str], provider, model: Optional[str],
                   cards_dir: Path) -> Set[str]:
    """Relative folders to EXCLUDE from indexing, judged by batched LLM review.

    Adaptive: judge the shallow level, then open up only the folders that were kept AND are large
    enough that their contents could still be mostly junk. Cached in the cards dir; delete or edit
    that file to redo or override. Returns an empty set on any failure -- an unreviewable corpus is
    indexed in full, exactly as before this existed. (Folders with a structural risk signal are the
    exception, and are handled by ``context_risk_gate.folder_gate``, which calls ``review_corpus``.)
    """
    return review_corpus(walk_root, skip_dirs, provider, model, cards_dir)[0]


def _review_risk_folders(verdicts: Dict[str, Dict[str, Any]], risk: Dict[str, List[Tuple[str, str]]],
                         counts: Dict[str, int], walk_root: Path, provider,
                         model: Optional[str], notes: Dict[str, str]) -> None:
    """Judge every folder that carries a HARD risk signal and has no verdict yet, in place.

    This is the one place a risk-signalled folder reaches the model, and it does so whatever the
    corpus size (the file-count floor does not apply). A verdict already in the cache, from a
    model or from a human, is never re-asked. A skip is ``final``: nothing beneath it is judged or
    re-opened. The model's answer is recorded with ``source: risk_signal`` and the signals that
    triggered it, so the file stays readable and editable like every other verdict.
    """
    pending = [(f, _folder_file_count(counts, f)) for f, sigs in risk.items()
               if any(kind in HARD_RISK_KINDS for kind, _ in sigs)
               and f not in verdicts and final_exclusion_covering(verdicts, f) is None]
    if provider is None or not pending:
        return
    for i in range(0, len(pending), _FOLDER_REVIEW_BATCH):
        judged = _review_folder_batch(pending[i:i + _FOLDER_REVIEW_BATCH], walk_root, provider,
                                      model, notes=notes)
        for folder, verdict in judged.items():
            verdict["mixed"] = False
            verdict["source"] = "risk_signal"
            verdict["signals"] = sorted({kind for kind, _ in risk.get(folder, [])})
            if not verdict.get("index", True):
                verdict["final"] = True
            verdicts[folder] = verdict


def review_corpus(walk_root: Path, skip_dirs: Set[str], provider, model: Optional[str],
                  cards_dir: Path,
                  risk: Optional[Dict[str, List[Tuple[str, str]]]] = None,
                  counts: Optional[Dict[str, int]] = None,
                  persist: bool = True,
                  ) -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
    """The folder review proper: ``(folders to exclude, every verdict now held)``.

    ``risk`` maps a folder to its structural risk signals (see ``context_risk_gate``). Those
    folders are always sent to the model, below the file-count floor too, with the signal as
    context. ``counts`` lets the caller supply the per-directory file counts it already took.
    ``persist=False`` leaves ``folder_review.json`` untouched (a read-only report).
    """
    risk = risk or {}
    cache_path = cards_dir / _FOLDER_REVIEW_FILE
    verdicts: Dict[str, Dict[str, Any]] = load_verdicts(cards_dir)
    hard_risk = {f: sigs for f, sigs in risk.items()
                 if any(kind in HARD_RISK_KINDS for kind, _ in sigs)}
    notes = {f: "; ".join(text for _, text in sigs) for f, sigs in risk.items()}

    if counts is None:
        counts = _dir_file_counts(walk_root, skip_dirs)
    if not counts and not hard_risk:
        return set(), verdicts

    # Only worth doing on a corpus big enough for the answer to matter. A small project root has
    # nothing to prune that the skip list has not already caught, and spending model calls to
    # confirm that would make every ordinary bootstrap slower and dearer for no gain. The problem
    # this solves only appears at scale -- one corpus where 91% of 81,464 files were dependency
    # checkouts, generated data and build output. A folder with a structural risk signal is the
    # one exception: the floor is about ordinary folders, and a series of dated copies is small
    # in the files it shows and enormous in the cards it would make.
    if sum(counts.values()) < _FOLDER_REVIEW_MIN_FILES:
        if not hard_risk:
            return set(), verdicts
        _review_risk_folders(verdicts, risk, counts, walk_root, provider, model, notes)
        return _finish_review(verdicts, counts, cards_dir, cache_path, persist), verdicts

    # --- Safeguard 1: nested repositories, decided without a model call --------------------
    # Recorded as verdicts so they are visible and editable in the cache file like any other.
    if _skip_nested_repos():
        unstored: List[str] = []
        for rel, marker in _nested_vcs_dirs(walk_root, skip_dirs).items():
            # A nested repo is a separate project with its own history. Two kinds, and the
            # difference decides whether excluding it loses anything:
            #   - it has its OWN card store -> already imported wholesale by the reuse path, so
            #     excluding it from THIS walk costs nothing and avoids indexing it twice.
            #   - it has none -> excluding it would make it vanish from the index entirely, which
            #     is right for a vendored dependency and wrong for a project the person works in.
            #     Those are named in the log rather than silently dropped, so the choice is
            #     visible and a person can index them as their own corpus.
            if verdict_is_pinned(verdicts.get(rel)):
                continue
            has_own_store = (walk_root / rel / ".quest-context" / "bootstrap_meta.json").exists()
            # ASSIGNED, not setdefault: a deterministic verdict outranks whatever the model (or an
            # older, buggier pass) recorded for the same folder. Otherwise the first cached model
            # "index": true survives forever and the safeguard never actually applies. A verdict a
            # human PINNED is the one exception.
            verdicts[rel] = {
                "index": False, "mixed": False, "final": True, "source": "nested_repo",
                "reason": (f"nested {marker} repository; its own card store is imported instead"
                           if has_own_store
                           else f"nested {marker} repository (separate project, not this corpus)"),
            }
            if not has_own_store:
                unstored.append(rel)
        if unstored:
            _log.info(
                "context index: %d nested repositor(ies) have no card store of their own and are "
                "excluded from this corpus — bootstrap them separately to index them: %s",
                len(unstored), ", ".join(sorted(unstored)[:8]),
            )

    # --- Safeguard 2: fuzzy duplicates, also without a model call --------------------------
    # Only the biggest folders are compared: a duplicate small enough not to matter is not worth
    # the hashing, and the pair that motivates this is always among the largest.
    biggest = sorted(_roll_up_to_depth(counts, 4), key=lambda f: -_folder_file_count(counts, f))
    for folder, (dup_of, overlap) in _duplicate_folders(walk_root, biggest[:40], counts).items():
        if verdict_is_pinned(verdicts.get(folder)):
            continue
        verdicts[folder] = {
            "index": False, "mixed": False, "final": True, "source": "duplicate",
            "reason": f"{overlap:.0%} duplicate of {dup_of}",
        }

    # Cache invalidation: anything cached UNDER a final exclusion was recorded by a pass that let
    # the subtree be re-opened, and keeping it would re-open it again now. See
    # ``drop_verdicts_under_final_exclusions``.
    stale_under_final = drop_verdicts_under_final_exclusions(verdicts)
    if stale_under_final:
        _log.info(
            "context index: dropped %d cached folder verdict(s) that sat inside a folder excluded "
            "for good (a nested repository or a duplicate tree); their parent's verdict stands",
            len(stale_under_final),
        )

    # --- Safeguard 3a: folders with a structural risk signal, always asked ----------------
    _review_risk_folders(verdicts, risk, counts, walk_root, provider, model, notes)

    # --- Safeguard 3b: the model, for what the two above cannot see ------------------------
    if provider is not None:
        depth = _FOLDER_REVIEW_START_DEPTH
        pending = [(f, n) for f, n in _roll_up_to_depth(counts, depth).items()
                   if f not in verdicts and final_exclusion_covering(verdicts, f) is None]
        while depth <= _FOLDER_REVIEW_MAX_DEPTH:
            for i in range(0, len(pending), _FOLDER_REVIEW_BATCH):
                verdicts.update(_review_folder_batch(pending[i:i + _FOLDER_REVIEW_BATCH],
                                                     walk_root, provider, model, notes=notes))
            # Descend only into folders that survived AND are big enough to still hide junk.
            depth += 1
            if depth > _FOLDER_REVIEW_MAX_DEPTH:
                break
            deeper = _roll_up_to_depth(counts, depth)
            pending = []
            for folder, n in deeper.items():
                if folder in verdicts:
                    continue
                # Nothing beneath a final exclusion is judged at all, at any depth. Checking the
                # whole ancestor chain (not just the direct parent) is what makes it hold: the
                # child of a child of an excluded nested repository is still inside it.
                if final_exclusion_covering(verdicts, folder) is not None:
                    continue
                parent = "/".join(folder.split("/")[:-1])
                parent_v = verdicts.get(parent)
                if parent_v is None:
                    continue
                if not parent_v.get("index", True):
                    # A model SKIP is re-opened only when the model ASKED for that by answering
                    # "mixed": that is the one case where it said the folder holds both kinds and
                    # a per-child judgement is wanted. Size used to be enough on its own, which is
                    # how a deterministic exclusion of a 23k-file vendored tree got re-opened and
                    # then outvoted by its own children. A model skip of a large folder that was
                    # NOT called mixed stands; the way to overturn it is to edit the cache file.
                    if not parent_v.get("mixed"):
                        continue
                elif not (n > _FOLDER_REVIEW_EXPAND_OVER or parent_v.get("mixed")):
                    continue
                pending.append((folder, n))
            if not pending:
                break

    return _finish_review(verdicts, counts, cards_dir, cache_path, persist), verdicts


def _finish_review(verdicts: Dict[str, Dict[str, Any]], counts: Dict[str, int], cards_dir: Path,
                   cache_path: Path, persist: bool = True) -> Set[str]:
    """Write the verdict file and return the folders those verdicts exclude."""
    # Written whether or not a model took part: with no provider this pass still re-applies the
    # deterministic safeguards and drops the verdicts that sat under them, and that correction
    # belongs on disk rather than being recomputed in memory forever. The file stays what it has
    # always been, the record a human can read and edit to override any decision.
    if verdicts and persist:
        try:
            cards_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"version": _FOLDER_REVIEW_VERSION, "folders": verdicts}, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass

    # A MIXED model skip that was opened up must not veto the children that were individually
    # kept: by answering "mixed" the model said its own verdict was provisional, so the
    # finer-grained judgements supersede it. Every other exclusion stands on its own, however
    # many of its children a later pass liked -- see ``verdict_is_final``.
    expanded = {"/".join(f.split("/")[:-1]) for f in verdicts if "/" in f}
    excluded = set()
    for folder, verdict in verdicts.items():
        if verdict.get("index", True):
            continue
        if not folder or folder == ".":
            # The root is not an excludable folder. Carried as a verdict for the record, dropped
            # here, and logged so the judgement is visible rather than silently ignored.
            _log.info("context index: folder review skipped the corpus ROOT (%s); the root is "
                      "never excluded, its own loose files are judged with everything else",
                      verdict.get("reason") or "no reason given")
            continue
        superseded = (
            not verdict_is_final(verdict)
            and bool(verdict.get("mixed"))
            and folder in expanded
            and _has_kept_child(verdicts, folder)
        )
        if not superseded:
            excluded.add(folder)
    if excluded:
        skipped_files = sum(n for f, n in counts.items()
                            if any(f == e or f.startswith(e + "/") for e in excluded))
        _log.info("context index: folder review excluded %d folder(s) covering %d file(s); "
                  "verdicts in %s", len(excluded), skipped_files, cache_path)
    return excluded


def _prune_dead_cards() -> bool:
    return os.getenv("QAR_PRUNE_DEAD_CARDS", "1").strip().lower() not in ("0", "false", "no")
