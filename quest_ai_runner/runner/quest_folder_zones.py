"""quest_folder_zones -- make "who decided this?" legible inside a quest's working folder.

WHY. A folder synced to a quest accumulates what the PERSON said and what the AI came up with, and
six months in they look identical: every file is markdown, every file is confident. The failure is
specific: a run produces a gap list, the next run treats it as the brief, the run after builds on
it, and ten runs later the plan rests on a premise the person never agreed to. Nothing
malfunctioned; the record never distinguished a proposal from an instruction. A second failure is
that nobody consolidates: the same topic is researched again and again into new dated files, and no
file is the current answer.

SHAPE. The folder's ``CLAUDE.md`` is always loaded, so it carries only a SHORT MAP (one line per
item: what it is, plus its single rule or a "read when" clause). The detail lives in the file each
rule governs, each under its own managed markers so a re-render never touches human or row content:

* ``human_context/`` -- the person's own words, verbatim only. Inbound Quest notes are captured
  under ``human_context/from_quest/`` by :func:`capture_human_input` on every pull.
* ``ai_driven/`` -- the AI's workspace; everything in it is a proposal until the ledger says so.
* ``ai_driven/provenance_ledger.md`` -- what the person has actually reviewed. Its managed header
  holds the status table and the add/approve rules; the rows below it are never touched.
* ``ai_driven/source_of_truth/`` -- the current answer per topic, refined in place. Its README
  holds the rules (no date in the filename, check here before researching, named headings).
* everything else -- joint work product.

Nothing is enforced by the filesystem, on purpose: a run can always write anywhere. This module
guarantees the rules are SCAFFOLDED (the directories exist), STATED (the map in ``CLAUDE.md``, the
detail in the governed files, a short reminder in the run context via
:func:`folder_zones_contract`), and that the person's own Quest notes are captured verbatim. The
wording of each rule exists once, in the constants below, and every rendering derives from it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ._managed_sections import replace_between, strip_managed_blocks

log = logging.getLogger("quest-ai-runner.quest_folder_zones")

AI_DRIVEN_DIR = "ai_driven"
HUMAN_CONTEXT_DIR = "human_context"

# Nested inside ai_driven/, not a fourth top-level zone: the subset of AI workspace meant to be
# THE current answer on a topic, refined in place, rather than a point-in-time research artifact.
# See the module docstring for why this exists.
SOURCE_OF_TRUTH_DIR = "source_of_truth"

# Inbound human material captured from Quest lands in its own subfolder rather than loose in
# human_context/, so the person's OWN files (things they wrote or dropped in themselves) never sit
# in a directory an automated writer also appends to.
INBOUND_DIR = "from_quest"

LEDGER_NAME = "provenance_ledger.md"
GUIDE_FILE = "CLAUDE.md"


_ZONES_START = "<!-- QAR:MANAGED:folder_zones START -->"
_ZONES_END = "<!-- QAR:MANAGED:folder_zones END -->"
_LEDGER_START = "<!-- QAR:MANAGED:ledger_rules START -->"
_LEDGER_END = "<!-- QAR:MANAGED:ledger_rules END -->"
_SOT_START = "<!-- QAR:MANAGED:source_of_truth_rules START -->"
_SOT_END = "<!-- QAR:MANAGED:source_of_truth_rules END -->"

SOURCE_OF_TRUTH_README = "README.md"

#: Ordered by how much weight a run may place on the row. Only ``approved`` is settled.
LEDGER_STATUSES = ("ai_proposed", "surfaced", "approved", "rejected", "superseded")

# --- the wording, once -------------------------------------------------------------------------
# Every rendering (CLAUDE.md map, ledger header, README block, run-context contract) is built from
# these, so a rule changed here changes everywhere.

# (status, what it means, may you build on it?) -- rows of the ledger header's table.
_STATUS_TABLE = (
    ("ai_proposed", "An AI came up with it. They have not seen it.", "No. Surface it and ask."),
    ("surfaced", "Put in front of them. No answer yet.", "No. Silence is not agreement."),
    ("approved", "They asked for it or said go. Their words are in the row.", "Yes."),
    ("rejected", "They said no.", "No, and do not re-propose without a new reason."),
    ("superseded", "Overtaken by a later decision.", "No. Follow the row that replaced it."),
)
assert tuple(r[0] for r in _STATUS_TABLE) == LEDGER_STATUSES

RULE_ONLY_APPROVED = "Only `approved` is settled."
RULE_ADD_ROW = ("Add a row when you propose something the person has not asked for, at "
                "`ai_proposed`.")
RULE_APPROVE_ROW = ("Move a row to `approved` only when you can quote them, in the row, with where "
                    "they said it (a Quest note id, an email date, a task id). Your own confidence "
                    "is not evidence, and neither is their silence.")

_SOT_RULES = (
    "No date in the filename. A dated name marks a snapshot; a file here is the current answer and "
    "is refined in place, not superseded.",
    "Check here before researching a topic. If a file covers it, update that file (and any "
    "document it mirrors) instead of writing a new one.",
    "Use named headings, not numbered ones, so sections can be reordered or split without "
    "renumbering.",
    "Everything here is still a proposal until the ledger says otherwise.",
)

_AI_LOC = f"{AI_DRIVEN_DIR}/"
_LEDGER_LOC = f"{AI_DRIVEN_DIR}/{LEDGER_NAME}"
_SOT_LOC = f"{AI_DRIVEN_DIR}/{SOURCE_OF_TRUTH_DIR}/"

# One line per folder item: what it is, then its single rule or "read when" clause.
MAP_HUMAN = (f"the person's own words, verbatim only. Write here only to record their input "
             f"exactly or when asked. Their Quest notes and replies arrive under "
             f"`{HUMAN_CONTEXT_DIR}/{INBOUND_DIR}/`.")
MAP_AI = "your workspace. Everything here is a proposal until the ledger says otherwise."
MAP_SOT = ("the current answer per topic, updated in place. Read its README before starting "
           "research on a topic.")
MAP_LEDGER = "read before building on anything an AI proposed. Only `approved` rows are settled."
MAP_OTHER = "joint work product. Say what you changed and why."

_SYNC_FILE = "QUEST_SYNC.md"
_SYNC_CLI = "quest-ai-runner sync-quest-folder"


def _clean_inline(value: Optional[str], limit: int = 120) -> str:
    """One line of caller-supplied text, safe to embed in a managed block."""
    text = " ".join(strip_managed_blocks(str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _render_zones_guide(quest_id: Optional[str] = None, quest_name: Optional[str] = None) -> str:
    """The managed block written into the folder's ``CLAUDE.md``: a short map, not the rules.

    The always-loaded file stays small; each rule's detail lives in the file it governs. The quest
    line appears only when the caller supplied an id (and the name when it supplied one).
    """
    qid = _clean_inline(quest_id, 80)
    name = _clean_inline(quest_name)
    lines: List[str] = []
    if qid:
        lines.append(f"This folder is the working folder of quest `{qid}`"
                     + (f' ("{name}").' if name else "."))
        lines.append("")
    lines.append("## What's here")
    lines.append("")
    lines.append(
        f"- `{_SYNC_FILE}`: the quest's live state, including its experiments (read-only here; carry "
        "them forward). Blocks marked `QAR:MANAGED` are overwritten on "
        "every pull, so never hand-edit inside them. Add bullets under \"Notes to push to Quest\" "
        f"to send findings up. Pull and push with `{_SYNC_CLI} {qid or '<quest_id>'} . "
        "--direction both`.")
    lines.append(f"- `{HUMAN_CONTEXT_DIR}/`: {MAP_HUMAN}")
    lines.append(f"- `{_AI_LOC}`: {MAP_AI}")
    lines.append(f"- `{_SOT_LOC}`: {MAP_SOT}")
    lines.append(f"- `{_LEDGER_LOC}`: {MAP_LEDGER}")
    lines.append(f"- everything else: {MAP_OTHER}")
    return "\n".join(lines)


def _render_ledger_rules() -> str:
    """The managed header of the ledger: the status table and the add/approve rules."""
    rows = "\n".join(f"| `{s}` | {m} | {b} |" for s, m, b in _STATUS_TABLE)
    return (
        "What the person has actually reviewed, and what is still only an AI's idea. Each row "
        "below is one proposal, decision, or initiative.\n\n"
        "| Status | What it means | May you build on it? |\n| --- | --- | --- |\n"
        f"{rows}\n\n"
        f"{RULE_ONLY_APPROVED} Anything else is a suggestion, however finished it looks.\n\n"
        f"{RULE_ADD_ROW}\n{RULE_APPROVE_ROW}"
    )


def _render_source_of_truth_rules() -> str:
    """The managed block of ``ai_driven/source_of_truth/README.md``."""
    return ("The current answer per topic, refined in place.\n\n"
            + "\n".join(f"- {r}" for r in _SOT_RULES))


def _render_ledger_scaffold() -> str:
    """The initial ledger: the managed rules header, then an empty table for the rows."""
    return (f"# Provenance ledger\n\n{_LEDGER_START}\n{_render_ledger_rules()}\n{_LEDGER_END}\n\n"
            "| Item | Raised | Origin | Status | Their words, and where | Where it lives |\n"
            "| --- | --- | --- | --- | --- | --- |\n")


def _with_ledger_header(existing: str) -> str:
    """Insert or replace ONLY the managed header; every other byte of an existing ledger stays.

    With no markers yet, the header goes right under a leading ``# `` title line (or at the very
    top when there is none), so rows and any hand-written prose below it are left exactly as found.
    """
    body = _render_ledger_rules()
    if _LEDGER_START in existing and _LEDGER_END in existing:
        return replace_between(existing, _LEDGER_START, _LEDGER_END, body)
    block = f"{_LEDGER_START}\n{body}\n{_LEDGER_END}\n"
    if existing.startswith("# "):
        title, _, rest = existing.partition("\n")
        return f"{title}\n\n{block}\n{rest.lstrip(chr(10))}"
    return f"{block}\n{existing}"


def _with_source_of_truth_rules(existing: str) -> str:
    """Create or update the README's managed block; text outside the markers is untouched."""
    if not existing:
        existing = "# Source of truth\n"
    return replace_between(existing, _SOT_START, _SOT_END, _render_source_of_truth_rules())


def ensure_folder_zones(folder: str, quest_id: Optional[str] = None,
                        quest_name: Optional[str] = None) -> FolderZones:
    """Scaffold the zone convention in ``folder``. Idempotent.

    Creates the zone directories if missing, then refreshes three managed blocks in place: the
    short map in the folder's ``CLAUDE.md`` (created if absent), the rules header of
    ``ai_driven/provenance_ledger.md`` (rows below it are never touched), and the rules block of
    ``ai_driven/source_of_truth/README.md``. Prose outside each marker pair is untouched.

    ``quest_id`` and ``quest_name`` are optional runtime data for the map's opening line and its
    sync command; with no id the line is omitted and the command shows a placeholder.

    The ledger is created EMPTY-BUT-HEADED rather than filled in: a scaffolded row would be the
    library inventing a decision, and this module's entire purpose is that decisions have to come
    from somewhere real.

    Never raises on a filesystem failure -- a folder that cannot be scaffolded is a degraded
    convention, not a reason to fail the run that triggered it. The failure is logged and the
    returned ``created`` list says what actually happened.
    """
    base = Path(folder).expanduser()
    created: List[str] = []
    ai_dir = base / AI_DRIVEN_DIR
    human_dir = base / HUMAN_CONTEXT_DIR
    source_of_truth_dir = ai_dir / SOURCE_OF_TRUTH_DIR
    ledger = ai_dir / LEDGER_NAME
    readme = source_of_truth_dir / SOURCE_OF_TRUTH_README
    guide = base / GUIDE_FILE

    def refresh(path: Path, render) -> None:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        rendered = render(existing)
        if rendered != existing:
            path.write_text(rendered, encoding="utf-8")
            created.append(str(path))

    try:
        for path in (ai_dir, human_dir, human_dir / INBOUND_DIR, source_of_truth_dir):
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                created.append(str(path))
        if not ledger.exists():
            ledger.write_text(_render_ledger_scaffold(), encoding="utf-8")
            created.append(str(ledger))
        else:
            refresh(ledger, _with_ledger_header)
        refresh(readme, _with_source_of_truth_rules)
        refresh(guide, lambda text: replace_between(
            text, _ZONES_START, _ZONES_END, _render_zones_guide(quest_id, quest_name)))
    except OSError as e:
        log.warning("could not scaffold folder zones in %s: %s", folder, e)

    if created:
        log.info("folder zones scaffolded in %s (%d path(s))", folder, len(created))
    return FolderZones(
        folder=str(base), ai_driven=str(ai_dir), human_context=str(human_dir),
        source_of_truth=str(source_of_truth_dir), ledger=str(ledger), guide=str(guide),
        created=created,
    )

# Author kinds that mean "a person wrote this". Matched against a note's ``author_kind``.
#
# An UNKNOWN kind is deliberately NOT in this set and is never treated as human. The whole point of
# the capture is that human_context/ holds only things the person actually said; one AI note filed
# there as their words reproduces the exact confusion this module exists to end, and it is
# unfalsifiable afterwards (it reads as a quote). A missed capture is recoverable -- the note is
# still in Quest and still in the sync file. A wrong one is not.
_HUMAN_AUTHOR_KINDS = frozenset({"human", "user", "person", "owner", "member"})

# A note that ARRIVED as mail is from the person by construction: the reply address is theirs and
# an AI run posting to the quest uses the API, never the mailbox. This is a second, independent
# signal for a backend that does not stamp author_kind on inbound mail.
_HUMAN_SOURCES = frozenset({"email", "sms", "reply"})

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class FolderZones:
    """Where the zones are on disk, and what ``ensure_folder_zones`` had to create."""

    folder: str
    ai_driven: str
    human_context: str
    source_of_truth: str
    ledger: str
    guide: str
    created: List[str]

    @property
    def scaffolded(self) -> bool:
        """Whether this call created anything (vs. finding an already-conforming folder)."""
        return bool(self.created)



def is_human_note(note: Dict[str, Any]) -> bool:
    """Whether a Quest note was written by the PERSON, on evidence rather than inference.

    Two independent signals, either sufficient: an explicit human ``author_kind``, or a ``source``
    that only a person can post through (mail, sms). An absent or unrecognised ``author_kind`` is
    NOT human -- see ``_HUMAN_AUTHOR_KINDS`` for why the unknown case has to fall this way.

    Note that ``author_name`` is not consulted at all. A note an AI run posts carries the account
    OWNER's display name, because the API key is theirs, so the name says nothing about who wrote
    it -- the same trap ``quest_folder_sync._render_notes_block`` documents.
    """
    kind = str(note.get("author_kind") or "").strip().lower()
    if kind in _HUMAN_AUTHOR_KINDS:
        return True
    if kind == "ai":
        return False
    return str(note.get("source") or "").strip().lower() in _HUMAN_SOURCES


def _inbound_filename(note: Dict[str, Any]) -> str:
    """A stable, collision-free, filesystem-safe name for one captured note.

    Keyed on the note id so re-capturing is a no-op: the same note always maps to the same file,
    which is what makes :func:`capture_human_input` safe to call on every single pull.
    """
    nid = str(note.get("note_id") or note.get("id") or "").strip()
    created = str(note.get("created_at") or "")[:10]
    stem = "-".join(p for p in (created, nid or "note") if p)
    # Two separate hazards, so two steps: unsafe characters (a path separator turns a note id into
    # a traversal) and a leading dot (a captured note that lands hidden is a captured note nobody
    # finds). Note ids are server-generated, but the id is not the only thing that reaches here and
    # a filename is not the place to trust an upstream format.
    safe = _UNSAFE_NAME_RE.sub("_", stem).lstrip("._-")
    return f"{safe or 'note'}.md"


def _render_inbound_note(note: Dict[str, Any]) -> str:
    """One captured note: a header of provenance, then their words, untouched.

    The body is written EXACTLY as it arrived. No wrapping, no markdown normalisation, no
    summary line at the top. A file in ``human_context/`` whose text has been improved is no
    longer evidence of anything.
    """
    nid = str(note.get("note_id") or note.get("id") or "").strip()
    created = str(note.get("created_at") or "").strip()
    source = str(note.get("source") or "").strip()
    author = str(note.get("author_name") or "").strip()
    meta = [f"note_id: {nid or '(unknown)'}"]
    if created:
        meta.append(f"date: {created}")
    if source:
        meta.append(f"arrived_by: {source}")
    if author:
        meta.append(f"account: {author}")
    front = "\n".join(meta)
    return (
        f"---\n{front}\ncaptured_by: quest-ai-runner\n---\n\n"
        "<!-- Their words, verbatim. Do not edit, summarise, or reformat this file. -->\n\n"
        f"{note.get('text', '')}\n"
    )


def capture_human_input(folder: str, notes: Iterable[Dict[str, Any]]) -> List[str]:
    """Write each human-authored note into ``human_context/from_quest/``, verbatim. Idempotent.

    This is the one thing an automated writer may put in the human zone unasked, and it earns the
    exception by being a transcription rather than a contribution: the person's own words, byte for
    byte, under a filename derived from the note's id so a repeated call rewrites nothing.

    Why it belongs on the sync path rather than in a run's own judgement: the person's input is the
    thing most likely to be paraphrased into oblivion, and a run summarising their email into "he
    approved the plan" is exactly how a rejection becomes an approval three runs later. Capturing
    it mechanically removes the judgement call.

    Returns the paths written this call (empty when everything was already captured).
    """
    base = Path(folder).expanduser() / HUMAN_CONTEXT_DIR / INBOUND_DIR
    written: List[str] = []
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("could not create %s: %s", base, e)
        return written

    for note in notes or []:
        if not is_human_note(note):
            continue
        if not str(note.get("text") or "").strip():
            continue
        path = base / _inbound_filename(note)
        if path.exists():
            continue
        try:
            path.write_text(_render_inbound_note(note), encoding="utf-8")
            written.append(str(path))
        except OSError as e:  # noqa: PERF203 -- one unwritable note never stops the rest
            log.warning("could not capture human note to %s: %s", path, e)

    if written:
        log.info("captured %d human note(s) into %s", len(written), base)
    return written


def folder_zones_contract(folder: Optional[str] = None) -> str:
    """Told to a run whose quest has a working folder, alongside the result/email contracts.

    Short on purpose, and built from the same constants as the folder's ``CLAUDE.md`` map, the
    ledger header and the source-of-truth README, so the wording exists once. The details live in
    those files; what this has to do is make the run LOOK, and name the one failure mode worth
    interrupting a run over.
    """
    where = f" ({folder})" if folder else ""
    return (
        f"This quest has a working folder{where}; its CLAUDE.md is a short map:\n"
        f"- `{HUMAN_CONTEXT_DIR}/`: {MAP_HUMAN}\n"
        f"- `{_AI_LOC}`: {MAP_AI}\n"
        f"- `{_SOT_LOC}`: {MAP_SOT}\n"
        f"- everything else: {MAP_OTHER}\n"
        f"Before you build on any analysis, gap list, plan or initiative that came from an AI run, "
        f"read `{_LEDGER_LOC}` for its status. {RULE_ONLY_APPROVED} If what you are about to build "
        "on is `ai_proposed` or `surfaced`, it is a suggestion they have not agreed to: say so in "
        "your result and ask, rather than treating it as the brief. "
        f"{RULE_ADD_ROW} {RULE_APPROVE_ROW}"
    )
