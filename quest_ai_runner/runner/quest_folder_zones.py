"""quest_folder_zones — make "who decided this?" legible inside a quest's working folder.

A folder synced to a quest accumulates two very different kinds of material: what the PERSON
said, and what the AI came up with. Six months in they are indistinguishable. Every file is
markdown, every file is confident, and nothing on the page records which of the two it is.

The failure that follows is specific and it is not hypothetical. An AI run analyses the work and
produces, say, a list of gaps. The next run reads that list, treats it as the brief, and resolves
them. The run after that builds on the resolutions. Ten runs later the whole plan rests on a
premise the person never agreed to -- and when they finally read it they say, correctly, "all of
that is from AI, you should be confirming every point with me". Nothing malfunctioned. The record
simply never distinguished a proposal from an instruction.

THREE ZONES, one rule each. ``ensure_folder_zones`` scaffolds them and writes the rule into the
folder's own ``CLAUDE.md``, so it is in front of every future run whether or not that run went
through this library:

* ``human_context/`` -- the person's own words and their own materials. An AI run may write here
  in exactly two cases: it is recording human input VERBATIM (a prompt, an email reply, feedback,
  quoted exactly, not summarised), or it was explicitly asked to. Never its own interpretation,
  never a tidied-up version.
* ``ai_driven/`` -- the AI's own workspace: self-documentation, analyses it decided to run,
  scratch files, designs nobody asked for yet. Everything here is a PROPOSAL until the record says
  otherwise. A run may write here freely.
* everything else -- collaborative work product, the thing the two are actually making together.

THE LEDGER (``ai_driven/provenance_ledger.md``) is the fourth piece, and the one that answers the
question the zones alone cannot: of the things the AI proposed, which has the person actually
seen? A folder can tell you a file is AI-authored; only a ledger can tell you the person read it,
on what date, and what they said back. Its statuses are deliberately ordered by how much weight a
run may put on a row: ``ai_proposed`` (they have not seen it) -> ``surfaced`` (put in front of
them, no answer yet) -> ``approved`` / ``rejected`` / ``superseded``. Only ``approved`` is
settled. A run that builds on anything else is doing what the failure above describes.

Nothing here is enforced by the filesystem, and that is on purpose: a run can always write
anywhere. What this module guarantees is that the rule is SCAFFOLDED (the directories exist, so
the choice of where to put a file is a real one), STATED (in the folder's CLAUDE.md, and in the
run context via :func:`folder_zones_contract`), and PRE-POPULATED with the one class of human
material a run should never have to transcribe by hand -- the person's own Quest notes, captured
verbatim by :func:`capture_human_input` on every pull.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ._managed_sections import replace_between

log = logging.getLogger("quest-ai-runner.quest_folder_zones")

AI_DRIVEN_DIR = "ai_driven"
HUMAN_CONTEXT_DIR = "human_context"

# Inbound human material captured from Quest lands in its own subfolder rather than loose in
# human_context/, so the person's OWN files (things they wrote or dropped in themselves) never sit
# in a directory an automated writer also appends to.
INBOUND_DIR = "from_quest"

LEDGER_NAME = "provenance_ledger.md"
GUIDE_FILE = "CLAUDE.md"

_ZONES_START = "<!-- QAR:MANAGED:folder_zones START -->"
_ZONES_END = "<!-- QAR:MANAGED:folder_zones END -->"

#: Ordered by how much weight a run may place on the row. Only ``approved`` is settled.
LEDGER_STATUSES = ("ai_proposed", "surfaced", "approved", "rejected", "superseded")

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
    ledger: str
    guide: str
    created: List[str]

    @property
    def scaffolded(self) -> bool:
        """Whether this call created anything (vs. finding an already-conforming folder)."""
        return bool(self.created)


def _render_zones_guide() -> str:
    """The managed block written into the folder's ``CLAUDE.md``.

    Addressed to whoever reads the folder next, human or AI, and phrased as rules rather than
    description: a future run acts on what this says, so an ambiguous sentence here becomes an
    ambiguous decision there.
    """
    return f"""## How this folder is organised (three zones)

Work here is a collaboration, and the record has to say which half of it came from whom. Three
zones, one rule each. They are not enforced by the filesystem — they are enforced by whoever is
writing, which includes you.

**`{HUMAN_CONTEXT_DIR}/` — the person's own words and materials.**
Write here in exactly two cases: you are recording human input VERBATIM (a prompt, an email
reply, feedback — quoted exactly, not summarised, not tidied), or you were explicitly asked to
put something here. Never your own interpretation of what they meant. Never a condensed version
"for convenience". If you find yourself editing a sentence in this zone, stop: the thing that
makes it useful is that it is unaltered. Their Quest notes and email replies are captured here
automatically under `{HUMAN_CONTEXT_DIR}/{INBOUND_DIR}/`.

**`{AI_DRIVEN_DIR}/` — your workspace.**
Self-documentation, analyses you decided to run, scratch and temporary files, designs nobody has
asked for yet. Write here freely. Everything in this zone is a PROPOSAL until
`{AI_DRIVEN_DIR}/{LEDGER_NAME}` records otherwise — including work that is finished, verified,
and obviously correct. Finished is not the same as agreed.

**Everything else — the collaborative work product.**
The document, the code, the plan: the thing the two of you are actually making. Files here carry
joint authorship, so changing one means changing something the person may consider theirs. Say
what you changed and why, in your result.

### Before you build on anything, check the ledger

`{AI_DRIVEN_DIR}/{LEDGER_NAME}` records what the person has actually reviewed. Its statuses:

| Status | What it means | May you build on it? |
| --- | --- | --- |
| `ai_proposed` | You (or an earlier run) came up with it. They have not seen it. | **No.** Surface it and ask. |
| `surfaced` | Put in front of them. No answer yet. | **No.** Silence is not agreement. |
| `approved` | They asked for it, or said go. Their words are in the row. | Yes. |
| `rejected` | They said no. | No, and do not re-propose without new reason. |
| `superseded` | Overtaken by a later decision. | No — follow the row that replaced it. |

Only `approved` is settled. This matters most for the things that feel most like facts: a gap
analysis, a list of risks, a proposed re-sequencing, a new initiative. Those are the rows that
get treated as the brief three runs later, when nobody remembers they were a suggestion.

**Add a row whenever you propose something the person has not asked for**, at `ai_proposed`.
**Move a row to `approved` only when you can quote them**, in the row, with where they said it.
Your own confidence is not evidence. Neither is their silence.
"""


def _render_ledger_scaffold() -> str:
    """The initial ledger: the instructions, then an empty table for the rows."""
    statuses = " → ".join(f"`{s}`" for s in LEDGER_STATUSES[:2])
    return f"""# Provenance ledger

What the person has actually reviewed, and what is still only the AI's idea.

Every row is one proposal, decision, or initiative. Add a row the moment you propose something
they did not ask for, at `ai_proposed`. Move it along ({statuses} → `approved` / `rejected` /
`superseded`) only on evidence, and put the evidence IN the row: their own words, quoted, and
where they said them (a Quest note id, an email date, a task id).

Only `approved` is settled. Anything else is a suggestion, however finished it looks, and
building on it is how a plan ends up resting on a premise nobody agreed to.

| Item | Raised | Origin | Status | Their words, and where | Where it lives |
| --- | --- | --- | --- | --- | --- |
"""


def ensure_folder_zones(folder: str) -> FolderZones:
    """Scaffold the three-zone convention in ``folder``. Idempotent.

    Creates the two zone directories and the ledger if missing, and refreshes the managed block in
    the folder's ``CLAUDE.md`` (creating that file if it does not exist). Prose outside the markers
    is untouched, so a folder with its own CLAUDE.md keeps everything it already said.

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
    ledger = ai_dir / LEDGER_NAME
    guide = base / GUIDE_FILE

    try:
        for path in (ai_dir, human_dir, human_dir / INBOUND_DIR):
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                created.append(str(path))
        if not ledger.exists():
            ledger.write_text(_render_ledger_scaffold(), encoding="utf-8")
            created.append(str(ledger))
        existing = guide.read_text(encoding="utf-8") if guide.exists() else ""
        rendered = replace_between(existing, _ZONES_START, _ZONES_END, _render_zones_guide())
        if rendered != existing:
            guide.write_text(rendered, encoding="utf-8")
            created.append(str(guide))
    except OSError as e:
        log.warning("could not scaffold folder zones in %s: %s", folder, e)

    if created:
        log.info("folder zones scaffolded in %s (%d path(s))", folder, len(created))
    return FolderZones(
        folder=str(base), ai_driven=str(ai_dir), human_context=str(human_dir),
        ledger=str(ledger), guide=str(guide), created=created,
    )


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

    Short on purpose. The full rules live in the folder's own ``CLAUDE.md``, which the run reads
    when it gets there; repeating them in the prompt would spend context on something already
    written down. What this has to do is make the run LOOK -- and name the one failure mode worth
    interrupting a run over.
    """
    where = f" ({folder})" if folder else ""
    return (
        f"This quest has a working folder{where} organised in three zones, described in its "
        f"CLAUDE.md: `{HUMAN_CONTEXT_DIR}/` holds the person's own words (write there only to "
        f"record their input verbatim, or when asked), `{AI_DRIVEN_DIR}/` is your workspace and "
        "everything in it is a proposal, and everything else is collaborative work product.\n"
        f"Before you build on any analysis, gap list, plan or initiative that came from an AI run, "
        f"check `{AI_DRIVEN_DIR}/{LEDGER_NAME}` for its status. Only `approved` rows are settled, "
        "and an approved row quotes the person saying so. If what you are about to build on is "
        "`ai_proposed` or `surfaced`, it is a suggestion they have not agreed to: say so in your "
        "result and ask, rather than treating it as the brief. Add a row at `ai_proposed` for "
        "anything you propose that they did not ask for."
    )
