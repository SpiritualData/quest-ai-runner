"""Usage receipts and dispositions: the rules the model is shown and the parsing of its replies.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from ..core.managed_blocks import RECEIPT_RULES, block_spans  # noqa: F401  (block_spans re-exported)
from .context_update_types import BLOCK_END, BLOCK_START, USAGE_HEADING, _REF_RE


# ---------------------------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------------------------

# "not only in your result" used to end this paragraph, from when a note's only answer channel was
# a note. It is now false on any quest that mails, where the result IS the message the person
# opens; each item says where its own answer goes, and that line is the one to follow.
_OFFER_PREAMBLE = (
    "Things the people you work with have said or done since you last looked at this work, in "
    "their own words. This is not a task list: judge which of these bear on what you are doing "
    "now, act on those, and leave the rest. Anything marked \"needs an answer\" is a question "
    "someone is waiting on -- answer it where the item's own \"to respond\" line says, because "
    "that is the place they will actually read it."
)


# What a disposition is, and the whole list of them, said to the run that has to pick one.
#
# This is a CHOICE FROM A LIST, which is what makes reading it back legitimate: the run is asked
# for a structured decision and its answer is recorded, rather than its prose being scanned for
# words that look like completion. Anything outside the list records nothing at all, so an item
# can only ever fail to move, never move wrongly.
#
# "done" on a standing instruction is the failure worth naming here, because it is the natural
# thing to write on the day you comply with one, and it is how a rule stops being followed: the
# record says finished, nothing raises it again, and the third report quietly drops it.
def _disposition_rules() -> str:
    from .feedback_ledger import disposition_vocabulary
    return (
        "Each line starts with a DISPOSITION, then a colon, then what you actually did. The "
        "disposition is recorded and carried forward, so use one of these exactly:\n\n"
        f"{disposition_vocabulary()}\n\n"
        "The one that matters most: if what they wrote governs FUTURE work (\"from now on\", "
        "\"in every report\", \"always\"), it is standing, never done, even on the day you follow "
        "it. Marking a standing instruction done is how it stops being followed: nothing raises it "
        "again and it quietly drops out of the next piece of work.\n"
        "If one thing they wrote contains two asks (a fix to make now AND a rule from now on), "
        "give both on the same line, separated by a semicolon, e.g.\n"
        "  [U1] done: added the Status column; standing: report limitations work every time"
    )


# The receipt protocol, stated to the run that will produce the work (text in core.managed_blocks).
_RECEIPT_RULES = RECEIPT_RULES  # defined in core.managed_blocks (block_spans needs it)


def usage_receipt_gate(refs: Sequence[str] = ()) -> str:
    """The closing instruction, naming the exact refs this run was shown.

    Enumerating them rather than describing them is the point: a run given "one line per ref" and a
    generic example reliably answers for the ones it found interesting and drops the rest, and the
    dropped ones are precisely the material the person is checking on.
    """
    listed = [str(r) for r in (refs or []) if str(r)]
    if listed:
        example = "\n".join(
            f"  [{r}] " + ("<disposition>: <up to 8 words on what you did>" if i == 0 else "...")
            for i, r in enumerate(listed))
        which = f"one line for each of {', '.join(listed)}, in that order, none left out"
    else:
        example = ("  [U1] done: added the Status column\n"
                   "  [U2] not used")
        which = "one line per ref you were shown above, in order, none left out"
    return (f"BEFORE YOU FINISH, account for the context updates you were shown. End your result "
            f"with:\n\n{USAGE_HEADING}\n{example}\n\nThat is {which}.\n\n"
            f"{_disposition_rules()}\n\n{_RECEIPT_RULES}")


# ---------------------------------------------------------------------------------------------
# Receipt parsing and rendering
# ---------------------------------------------------------------------------------------------

def parse_manifest(text: str) -> List[str]:
    """Recover the offered manifest lines from a composed task text.

    This is what lets the receipt be rendered by whoever finishes a run, without the bundle object
    travelling with the task through a queue: the block was written into the task's own text, so
    the task text is the record of what was offered. Returns [] when the text carries no block.

    When the text holds more than one block (a thread whose earlier brief rode along with today's),
    the LAST one is read: it is the newest, and it is the one the deep prompt keeps (see
    ``core.prompt_budget.keep_last_block``), so the receipt describes what the run was shown.
    """
    body = str(text or "")
    start = body.rfind(BLOCK_START)
    if start < 0:
        return []
    end = body.find(BLOCK_END, start)
    chunk = body[start:end if end > start else len(body)]
    # Deduped on the ref, first occurrence wins. The block lists every ref once as an index line
    # and then repeats the ones it details in full, so without this the receipt would carry a
    # detailed update twice.
    seen: Dict[str, str] = {}
    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        m = _REF_RE.match(line)
        if m and m.group(1) not in seen:
            seen[m.group(1)] = line
    return list(seen.values())


def parse_usage_notes(text: str) -> Dict[str, str]:
    """The run's own account of what it used, as ``{"U1": "cited in method section"}``.

    Reads only the lines under the ``Context used:`` heading, and only lines shaped as the gate
    asked for. This parses the model's own STRUCTURED report to display it back; it never gates
    behavior on the words in it, and a run that writes nothing simply produces an empty mapping
    (the receipt then says "no note from the run" rather than asserting anything).
    """
    body = str(text or "")
    idx = body.rfind(USAGE_HEADING)
    if idx < 0:
        return {}
    out: Dict[str, str] = {}
    for line in body[idx + len(USAGE_HEADING):].splitlines():
        stripped = line.strip()
        if not stripped:
            continue                   # blank lines inside the block are tolerated
        m = _REF_RE.match(stripped)
        if not m:
            if out:
                break          # the block ended and ordinary prose resumed
            continue
        ref, note = m.group(1), " ".join(m.group(2).split())
        out[ref] = note.strip(" .-")
    return out


def split_disposition(note: str) -> List[Tuple[str, str]]:
    """One receipt line's ``"done: added the column; standing: report it every time"`` as pairs.

    Returns ``[(disposition, words), ...]`` keeping only the parts whose disposition this library
    knows. A line with no recognised disposition returns ``[]`` and therefore records nothing,
    which is the safe failure: an unrecorded answer costs one repeated question, a misrecorded one
    costs the request itself.
    """
    from .feedback_ledger import normalize_disposition
    out: List[Tuple[str, str]] = []
    for part in str(note or "").split(";"):
        head, sep, tail = part.partition(":")
        disposition = normalize_disposition(head if sep else part)
        if not disposition:
            continue
        out.append((disposition, " ".join(tail.split()).strip(" .-")))
    return out


def parse_dispositions(text: str) -> Dict[str, List[Tuple[str, str]]]:
    """Every receipt line's declared dispositions, as ``{"U1": [(disposition, words), ...]}``.

    The machine-readable half of ``parse_usage_notes``, which keeps returning the whole line for
    display. A ref the run wrote nothing recognisable for is absent rather than empty, so a caller
    cannot mistake "said nothing" for "said nothing changed".
    """
    out: Dict[str, List[Tuple[str, str]]] = {}
    for ref, note in parse_usage_notes(text).items():
        pairs = split_disposition(note)
        if pairs:
            out[ref] = pairs
    return out


def strip_usage_block(text: str) -> str:
    """The run's result with its raw ``Context used:`` lines removed.

    The rendered receipt replaces them, so leaving both would show the same information twice, once
    in whatever shape the model chose and once in the standard one.
    """
    body = str(text or "")
    idx = body.rfind(USAGE_HEADING)
    if idx < 0:
        return body
    head, tail = body[:idx], body[idx + len(USAGE_HEADING):]
    kept_tail: List[str] = []
    ended = False
    for line in tail.splitlines():
        if ended:
            kept_tail.append(line)
            continue
        stripped = line.strip()
        if not stripped or _REF_RE.match(stripped):
            continue
        ended = True
        kept_tail.append(line)
    return (head.rstrip() + ("\n" + "\n".join(kept_tail).strip() if any(
        ln.strip() for ln in kept_tail) else "")).rstrip()


def render_receipt(manifest_lines: Sequence[str], usage: Dict[str, str]) -> str:
    """The standard block appended to a result: one line per offered update, what came of it.

    Every offered ref appears, in the order it was offered, whether or not the run mentioned it.
    That is the whole value: a person scanning this can see at a glance that their comment was
    read, and can see just as clearly when it was not.
    """
    rows = [ln for ln in (manifest_lines or []) if _REF_RE.match(str(ln).strip())]
    if not rows:
        return ""
    out = ["Context updates taken into account:"]
    for line in rows:
        m = _REF_RE.match(str(line).strip())
        ref = m.group(1) if m else ""
        note = (usage or {}).get(ref, "")
        if not note:
            note = "no note from the run"
        out.append(f"{line.strip()} -> {note}")
    return "\n".join(out)


def append_receipt(reported: str, manifest_lines: Sequence[str], *,
                   run_output: Optional[str] = None) -> str:
    """``reported`` with its raw usage lines replaced by the rendered receipt.

    The one call a consumer needs at the end of a run: it reads the run's own account, renders the
    standard block, and returns the text to report. With no manifest it returns the text
    unchanged, so wiring it in cannot alter a run that carried no updates.

    ``run_output`` names where the run's own account is when that is not the text being reported:
    a deep run's summary is rewritten into a report before it is sent, and that rewrite is free to
    drop the usage lines, so they are read from the raw summary.
    """
    lines = list(manifest_lines or [])
    if not lines:
        return reported
    usage = parse_usage_notes(reported if run_output is None else run_output)
    receipt = render_receipt(lines, usage)
    if not receipt:
        return reported
    return strip_usage_block(reported).rstrip() + "\n\n" + receipt
