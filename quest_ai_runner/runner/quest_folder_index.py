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
        try:
            for dirpath, dirnames, filenames in os.walk(root):
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


def render_quest_folder_context(entry: Optional[QuestFolder]) -> str:
    """The matched quest as known context for this turn, or "" when nothing matched."""
    if entry is None:
        return ""
    parts = [
        "QUEST MATCHED TO THIS MESSAGE (from the quest's locally synced folder; this is known, "
        "so do not search for it). The user's words matched this quest by name. Answer questions "
        "about where it lives, its state, or what to do next from this block, giving full "
        "absolute paths exactly as written here. Name the sync file as the source. If the message "
        "turns out not to be about this quest, ignore this block entirely and do not mention the "
        "quest: never relate an answer back to it unless the message is actually about it.",
        f"Quest: {entry.title or '(untitled)'}",
        f"Quest id: {entry.quest_id}",
        f"Local folder (full path): {entry.folder}",
        f"Sync file (full path): {entry.sync_file}",
    ]
    files = list_folder_files(entry.folder)
    if files:
        parts.append("Folder contents: " + ", ".join(files))
    if entry.current_state:
        parts.append("Current state:\n" + clip(entry.current_state, STATE_MAX_CHARS, entry.sync_file))
    if entry.next_steps:
        parts.append("Standing next steps (its first line says when and by whom it was refreshed):\n"
                     + clip(entry.next_steps, NEXT_STEPS_MAX_CHARS, entry.sync_file))
    else:
        parts.append("Standing next steps: none written yet in the sync file.")
    return "\n".join(parts)


def describe_match(entry: QuestFolder) -> str:
    """One short line for the UI: which quest was matched, and where it lives."""
    return f"Quest: {entry.title or entry.quest_id}  ·  {entry.folder}"


def ids(entries: Iterable[QuestFolder]) -> List[str]:
    return [e.quest_id for e in entries]
