"""quest_folder_index — know every locally synced quest folder, and which one a message is about.

A quest that is synced to a local folder already carries everything a person usually asks first
in that folder's ``QUEST_SYNC.md``: the quest id, the goal, the current state and the standing
next steps. An attended chat session used to learn none of that unless the session happened to be
started INSIDE the folder (``session_next_steps``). Asked "what is the full path of the 1000
subscribers quest and what is next", the brain went looking for it (and the narration guessed a
path that did not exist) while the answer sat, fully written, in a file the runner can read in a
millisecond.

This module closes that gap without a model call:

    index = discover_quest_folders(corpus_root, quest_folder_map)   # session start, ~tens of ms
    match = match_quest_folder(message, index)                      # every turn, microseconds
    block = render_quest_folder_context(match)                      # prepended to the turn

Matching is lexical over the quest's own words (its goal title and folder name weigh most, its
current state a little), weighted by how rare each word is ACROSS the indexed quests, so "1000"
(shared by two quests here) counts for less than "subscribers" (only one). It reads the USER's
words only, never model output. It is deliberately conservative: no clear winner means no block,
because an unrelated quest's state in the prompt is worse than none.

Everything is best-effort and never raises: an unreadable folder is simply not indexed.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .quest_folder_sync import (
    SYNC_FILE_NAME,
    parse_goal_block,
    quest_id_in_folder,
    read_next_steps,
)

log = logging.getLogger("quest-ai-runner.quest_folder_index")

# How deep under the corpus root to look for sync files, and which directories never hold one.
# The scan is a plain directory walk, measured at ~30 ms for ~2k directories.
DISCOVERY_MAX_DEPTH = 4
# A session started somewhere broad (a home directory) must not stall on the walk: stop after this
# many directories or this many seconds, whichever comes first, keeping what was found so far.
DISCOVERY_MAX_DIRS = 20000
DISCOVERY_MAX_SECONDS = 1.0
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages", "dist", "build"})

# Ceilings for what one matched quest adds to EVERY turn that mentions it.
STATE_MAX_CHARS = 1500
NEXT_STEPS_MAX_CHARS = 1500
MAX_FILES_LISTED = 25

# A match needs a real hit on the quest's name (goal title or folder name) worth at least this much
# idf weight, and must beat the runner-up by this factor. Tuned so one rare shared word ("1000
# subscribers" -> the subscribers quest, not the downloads quest that also says 1000) decides, and
# a message that names no quest decides nothing.
MIN_NAME_SCORE = 1.0
MIN_MARGIN = 1.5
# A message that names no quest can still be about one: "did we fix the pricing page deep link"
# is the subscribers quest's current state, word for word. That needs more evidence than a name
# hit: at least two distinct rare words from the state, worth this much idf together.
MIN_STATE_WORDS = 2
MIN_STATE_SCORE = 4.0

STOPWORDS = frozenset("""
a an and are as at be but by can do does for from get go has have how i if in into is it its me my
of on or our so that the their them then there these this to up us was we what when where which who
why will with you your about all any just let like make need now please should tell thing things
want would could full path file filepath folder next quest quests did doing done going gone
today tomorrow yesterday week weekly month work working team know think look see use used time new
good well really also still yet been being got one two some more most very much many
""".split())

WORD_RE = re.compile(r"[a-z0-9]+")


def words(text: str) -> List[str]:
    """Lowercased content words, crudely singularized so 'subscribers' meets 'subscriber'."""
    out = []
    for w in WORD_RE.findall((text or "").lower()):
        if w in STOPWORDS or (len(w) < 3 and not w.isdigit()):
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


@dataclass
class QuestFolder:
    """One locally synced quest: where it lives and what its sync file says about it."""
    quest_id: str
    folder: str
    title: str = ""
    current_state: str = ""
    next_steps: str = ""
    name_words: frozenset = field(default_factory=frozenset)
    body_words: frozenset = field(default_factory=frozenset)

    @property
    def sync_file(self) -> str:
        return os.path.join(self.folder, SYNC_FILE_NAME)


def read_quest_folder(folder: str, quest_id: str = "") -> Optional[QuestFolder]:
    """Index one folder from its sync file. None when there is no readable sync file."""
    path = Path(folder) / SYNC_FILE_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    quest_id = quest_id or quest_id_in_folder(str(folder)) or ""
    if not quest_id:
        return None
    goal = parse_goal_block(text)
    title = goal.get("outcome") or ""
    state = goal.get("current_state") or ""
    try:
        next_steps = read_next_steps(str(folder)) or ""
    except Exception:  # noqa: BLE001 - a malformed block is "no next steps", not a crash
        next_steps = ""
    folder_name = Path(folder).name.replace("_", " ").replace("-", " ")
    return QuestFolder(
        quest_id=quest_id, folder=str(Path(folder).resolve()), title=title,
        current_state=state, next_steps=next_steps,
        name_words=frozenset(words(title) + words(folder_name)),
        body_words=frozenset(words(state)),
    )


def quest_without_folder(quest_id: str, title: str = "", current_state: str = "") -> QuestFolder:
    """A quest known only from the Quest API (no local folder), matchable and pinnable the same way."""
    return QuestFolder(quest_id=quest_id, folder="", title=title or "", current_state=current_state or "",
                       name_words=frozenset(words(title or "")),
                       body_words=frozenset(words(current_state or "")))


def reachable_quests(client, team_ids: Sequence[str]) -> List[QuestFolder]:
    """Every quest this Quest account can reach: each team's quests plus the account's own.

    Network calls, so callers run this off the UI thread. Failures shrink the list, never raise.
    """
    found: Dict[str, QuestFolder] = {}
    for team_id in [t for t in team_ids if t]:
        try:
            for q in client.list_quests(team_id=team_id) or []:
                qid = q.get("quest_id") or ""
                if qid and qid not in found:
                    found[qid] = quest_without_folder(qid, q.get("outcome") or "")
        except Exception:  # noqa: BLE001
            log.info("could not list quests for team %s", team_id, exc_info=True)
    try:
        for q in client.list_my_quests() or []:
            qid = q.get("quest_id") or ""
            state = q.get("state") or {}
            if qid:
                found[qid] = quest_without_folder(
                    qid, state.get("outcome") or (found[qid].title if qid in found else ""),
                    state.get("current_state") or "")
    except Exception:  # noqa: BLE001
        log.info("could not list the account's own quests", exc_info=True)
    return list(found.values())


def merge_quests(local: Sequence[QuestFolder], remote: Sequence[QuestFolder]) -> List[QuestFolder]:
    """Local synced folders first (they carry a path), then remote-only quests, each id once."""
    seen = {q.quest_id for q in local}
    return list(local) + [q for q in remote if q.quest_id not in seen]


def discover_quest_folders(corpus_root: Optional[str],
                           quest_folder_map: Optional[Dict[str, str]] = None,
                           max_depth: int = DISCOVERY_MAX_DEPTH) -> List[QuestFolder]:
    """Every synced quest folder this runner can see, one per quest id.

    The deployment's ``quest_folder_map`` is authoritative for the quests it names; the rest come
    from walking the corpus for ``QUEST_SYNC.md`` files that declare a ``quest_id``. When the same
    quest turns up twice (a copy in a backup tree, say), the mapped folder wins, then the shallowest.
    """
    found: Dict[str, QuestFolder] = {}
    for quest_id, folder in (quest_folder_map or {}).items():
        entry = read_quest_folder(folder, quest_id)
        if entry is not None:
            found[quest_id] = entry
    if corpus_root and os.path.isdir(corpus_root):
        root = os.path.abspath(corpus_root)
        candidates = []
        import time
        deadline = time.monotonic() + DISCOVERY_MAX_SECONDS
        seen_dirs = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                seen_dirs += 1
                if seen_dirs > DISCOVERY_MAX_DIRS or time.monotonic() > deadline:
                    log.info("quest folder discovery stopped after %d directories under %s",
                             seen_dirs, root)
                    break
                depth = dirpath[len(root):].count(os.sep)
                dirnames[:] = ([d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
                               if depth < max_depth else [])
                if SYNC_FILE_NAME in filenames:
                    candidates.append((depth, dirpath))
        except OSError:
            log.info("quest folder discovery stopped early under %s", root, exc_info=True)
        for _depth, dirpath in sorted(candidates):
            entry = read_quest_folder(dirpath)
            if entry is not None and entry.quest_id not in found:
                found[entry.quest_id] = entry
    return list(found.values())


def match_quest_folder(message: str, folders: Sequence[QuestFolder]) -> Optional[QuestFolder]:
    """The quest this message is clearly about, or None when no quest clearly wins.

    An explicit quest id in the message decides outright. Otherwise each message word scores its
    rarity across the indexed quests (idf) when it hits the quest's name, and a third of that when it
    only hits the quest's current state. The winner must either hit its name, or (the message never
    named it) hit its current state with MIN_STATE_WORDS words worth MIN_STATE_SCORE; and it must
    beat the runner-up clearly.
    """
    if not folders or not message:
        return None
    lowered = message.lower()
    for entry in folders:
        if entry.quest_id and entry.quest_id.lower() in lowered:
            return entry
    msg_words = set(words(message))
    if not msg_words:
        return None
    total = len(folders)

    def idf(word: str) -> float:
        df = sum(1 for e in folders if word in e.name_words or word in e.body_words)
        return math.log(1 + total / df) if df else 0.0

    weights = {w: idf(w) for w in msg_words}
    scored = []
    for entry in folders:
        name_score = sum(weights[w] for w in msg_words if w in entry.name_words)
        state_hits = [w for w in msg_words if w in entry.body_words and w not in entry.name_words]
        state_score = sum(weights[w] for w in state_hits)
        named = name_score >= MIN_NAME_SCORE
        about_state = len(state_hits) >= MIN_STATE_WORDS and state_score >= MIN_STATE_SCORE
        # Ranking: a name hit counts in full, a state hit a third, unless the state is the only
        # evidence and is strong on its own, in which case it ranks at full weight too.
        total = name_score + (state_score if about_state and not named else state_score / 3)
        scored.append((total, named or about_state, entry))
    scored.sort(key=lambda s: s[0], reverse=True)
    best_total, qualifies, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if not qualifies:
        return None
    if runner_up and best_total < runner_up * MIN_MARGIN:
        return None
    return best


def list_folder_files(folder: str, limit: int = MAX_FILES_LISTED) -> List[str]:
    """Top-level entries of the quest folder (dirs marked with a slash), hidden ones left out."""
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    except OSError:
        return []
    shown = [n + "/" if os.path.isdir(os.path.join(folder, n)) else n for n in names[:limit]]
    if len(names) > limit:
        shown.append(f"... and {len(names) - limit} more")
    return shown


def clip(text: str, limit: int, where: str) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n(truncated; the full text is in {where})"


QUEST_CARD_PREFIX = "quest-folder-"
QUEST_CARD_OWNER = "quest_folder_index"


def quest_digest(entry: QuestFolder) -> str:
    """What is known about a quest without reading anything else: id, full paths, state, next steps."""
    parts = [
        f"Quest: {entry.title or '(untitled)'}",
        f"Quest id: {entry.quest_id}",
    ]
    if entry.folder:
        parts += [f"Local folder (full path): {entry.folder}",
                  f"Sync file (full path): {entry.sync_file}"]
    else:
        parts.append("Local folder: none; this quest is not synced to a folder on this machine.")
    files = list_folder_files(entry.folder) if entry.folder else []
    if files:
        parts.append("Folder contents: " + ", ".join(files))
    if entry.current_state:
        parts.append("Current state:\n" + clip(entry.current_state, STATE_MAX_CHARS,
                                               entry.sync_file if entry.folder else "Quest"))
    if entry.next_steps:
        parts.append("Standing next steps (its first line says when and by whom it was refreshed):\n"
                     + clip(entry.next_steps, NEXT_STEPS_MAX_CHARS, entry.sync_file))
    elif entry.folder:
        parts.append("Standing next steps: none written yet in the sync file.")
    return "\n".join(parts)


def render_quest_folder_context(entry: Optional[QuestFolder]) -> str:
    """The matched quest as turn text, for a quest with no local card (not synced to a folder)."""
    if entry is None:
        return ""
    return (
        "QUEST MATCHED TO THIS MESSAGE (known, so do not search for it). The user's words matched "
        "this quest. Answer questions about it from this block. If the message turns out not to be "
        "about this quest, ignore this block entirely and do not mention the quest: never relate an "
        "answer back to it unless the message is actually about it.\n" + quest_digest(entry)
    )


def quest_card_id(quest_id: str) -> str:
    return QUEST_CARD_PREFIX + quest_id


def quest_card(entry: QuestFolder, corpus_root: Optional[str] = None) -> Dict[str, object]:
    """The context card for one locally synced quest, derived entirely from its sync file.

    Quests are cards (quest-backend keeps one per quest); this is the same idea for a quest synced
    to a folder on this machine, so card selection can pick it like any other card and a caller
    that knows the turn is about this quest can put it first (``priority_card_ids``). The digest is
    a ``note`` item, so the card carries the full paths, state and next steps itself; it also pins
    the sync file, so the store's quest-folder boost treats it as part of the quest's folder. Owned
    outright by this module (``managed_by``) and rebuilt from the sync file whenever it changes.
    """
    from ..core.recent_context import quest_scope_key
    sync_rel = entry.sync_file
    if corpus_root:
        try:
            sync_rel = Path(entry.sync_file).resolve().relative_to(
                Path(corpus_root).resolve()).as_posix()
        except (ValueError, OSError):
            sync_rel = entry.sync_file
    keywords = sorted(set(entry.name_words) | {"quest"})
    summary = f"Quest: {entry.title}" if entry.title else f"Quest {entry.quest_id}"
    note_text = (
        "This quest is synced to a local folder. Answer where it lives, its state, or what to do "
        "next from this card, giving the full paths exactly as written; name the sync file as the "
        "source.\n" + quest_digest(entry)
    )
    return {
        "id": quest_card_id(entry.quest_id),
        "name": summary,
        "summary": summary,
        "description": clip(entry.current_state, 300, entry.sync_file) if entry.current_state else summary,
        "keywords": keywords,
        "conventions": [],
        "files": [{"path": sync_rel, "why": "The quest's sync file: goal, state, notes, next steps"}],
        "content": [
            {"id": "quest-digest", "type": "note", "why": "Quest location, state and next steps",
             "locator": {"text": note_text}},
            {"id": "quest-sync-file", "type": "file", "why": "The quest's sync file",
             "locator": {"path": entry.sync_file}},
        ],
        "scope_tags": [quest_scope_key(entry.quest_id)],
        "managed_by": QUEST_CARD_OWNER,
        "managed_fields": ["name", "summary", "description", "keywords", "files"],
        "managed_items": ["quest-digest", "quest-sync-file"],
        "provenance": {"created_by_task": "quest folder sync", "model": "", "created_at": "",
                       "last_verified_at": ""},
    }


def card_writer(assembler):
    """The first context store under ``assembler`` that can store a whole card, or None."""
    if assembler is None:
        return None
    if callable(getattr(assembler, "write_card", None)) and callable(getattr(assembler, "get_card", None)):
        return assembler
    for member in getattr(assembler, "assemblers", None) or []:
        found = card_writer(member)
        if found is not None:
            return found
    return None


def sync_quest_cards(store, entries: Sequence[QuestFolder], corpus_root: Optional[str] = None) -> int:
    """Create or refresh the card of every quest with a local folder. Returns how many were written.

    Only a card whose content actually changed is rewritten, so an unchanged corpus costs reads.
    Usage history (``usage_count``) on an existing card is kept.
    """
    written = 0
    if store is None:
        return 0
    for entry in entries:
        if not entry.folder:
            continue
        card = quest_card(entry, corpus_root)
        try:
            existing = store.get_card(card["id"]) or {}
        except Exception:  # noqa: BLE001
            existing = {}
        if all(existing.get(k) == card[k] for k in ("name", "keywords", "content", "files",
                                                      "scope_tags", "description")):
            continue
        card["usage_count"] = existing.get("usage_count", 0)
        if store.write_card(card["id"], card):
            written += 1
    return written


def describe_match(entry: QuestFolder) -> str:
    """One short line for the UI: which quest was matched, and where it lives."""
    return f"quest: {entry.title or entry.quest_id}  ·  {entry.folder or 'not synced locally'}"


def ids(entries: Iterable[QuestFolder]) -> List[str]:
    return [e.quest_id for e in entries]
