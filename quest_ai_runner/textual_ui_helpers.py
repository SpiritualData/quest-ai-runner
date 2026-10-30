"""Textual UI helpers: clipboard tool fallback, shallow and future context text, the missing-provider message.

Split out of ``textual_ui.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from typing import Any, List, Mapping, Optional, Sequence
from rich.text import Text

from esoi.metric_results import NOT_RECORDED_LINE, SOURCE_MEASURED, SOURCE_NOT_APPLICABLE, SOURCE_RECORDED
from .core.orchestrator_text import ellipsize, sentence  # noqa: F401  (the terminal's own name for it, used all over)


# The kind of record a source may name at the start of its title or a worker's bullet ("- collection: Pricing
# tiers", "Quest: Launch plan"): the store's word, so it is not shown.
STORE_KIND_PREFIX = re.compile(r"^(\s*(?:[-*\u2022]\s+)?)(?:collection|card|quest|goal|note|file)\s*:\s+", re.IGNORECASE)

_CLIPBOARD_COMMANDS = (
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["wl-copy"],
)


def _copy_to_clipboard_tool(text: str, which=shutil.which, run=None) -> tuple:
    """Try each known clipboard CLI tool; return ``(copied, message)``.

    Distinguishes two failure modes so the user knows what to do next:
      - no clipboard tool is installed at all -> tell them to install one
      - a tool IS installed but the call failed (e.g. no display/session) -> show that error
    ``which``/``run`` are injectable so this is testable without a real clipboard.
    """
    if run is None:
        import subprocess
        run = subprocess.run

    any_installed = False
    last_error: Optional[str] = None
    for cmd in _CLIPBOARD_COMMANDS:
        if which(cmd[0]) is None:
            continue
        any_installed = True
        try:
            run(cmd, input=text.encode(), check=True, capture_output=True, timeout=2)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            continue

    if not any_installed:
        return False, "(clipboard tool not found, install xclip or xsel)"
    return False, f"(clipboard tool found but copy failed: {last_error})"


def _build_shallow_context_bullets(cards: List[dict]) -> str:
    """Build the bullet lines behind Alt+C from a short answer's context sources, one per source.

    Mirrors the bullet format a deep run produces for the FutureContextPanel, so both reuse the same
    panel and key. A plain title, never the internal id or the kind of store it came from. Returns an
    empty string when there is nothing to show.
    """
    lines: List[str] = []
    for card in cards or []:
        lines.append(f"- {clean_title(card.get('title'))}")
    return "\n".join(lines)


# The transcript's context line names this many sources by title, then "and K more".
CONTEXT_TITLES_SHOWN = 2
CONTEXT_TITLE_CHARS = 50


# Columns a wrapped row is indented by, under a line's marker (``❯ ``, ``┃ ``) or its own leading space.
HANG_DEFAULT = 2
# A line is not wrapped with a hanging indent in a terminal narrower than the indent plus this many columns.
HANG_MIN_BODY = 8


class HangingText(Text):
    """A transcript line whose wrapped rows start under its words, not under its marker.

    ``❯ a long message`` wraps to a second row that begins two columns in. A line with a bar (``  ┃ a long
    question``) is made ``carry`` it: every row starts with the bar, so a wrapped question still reads as one block.
    The transcript wraps it (``rows``) before writing; anywhere else it is an ordinary ``Text``."""

    def __init__(self, *args: Any, hang: int = HANG_DEFAULT, carry: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hang = hang
        self.carry = carry

    def copy(self) -> "HangingText":
        clone = HangingText(self.plain, style=self.style, justify=self.justify, overflow=self.overflow,
                            no_wrap=self.no_wrap, end=self.end, tab_size=self.tab_size, hang=self.hang,
                            carry=self.carry)
        clone._spans[:] = self._spans
        return clone

    def rows(self, width: int, console: Any) -> List[Text]:
        """This line as rows of at most ``width`` columns, every row after the first indented by ``hang`` (or,
        with ``carry``, started with the same first ``hang`` characters, the bar included)."""
        hang = self.hang
        if width < hang + HANG_MIN_BODY or hang <= 0:
            return [self]
        out: List[Text] = []
        first_prefix: Optional[Text] = None
        for paragraph in self.split("\n", allow_blank=True):
            if first_prefix is None:
                first_prefix = paragraph[:hang]
                prefix, body = first_prefix, paragraph[hang:]
            else:   # a later paragraph of the same line starts under the words (or repeats the bar)
                prefix, body = (first_prefix if self.carry else Text(" " * hang, style=first_prefix.style)), paragraph
            pieces = body.wrap(console, width - hang)
            if not pieces:
                out.append(prefix.copy())
                continue
            for i, piece in enumerate(pieces):
                row = prefix.copy() if i == 0 or self.carry else Text(" " * hang, style=prefix.style)
                row.append_text(piece)
                out.append(row)
        return out


def notice_line(text: str, width: Optional[int] = None, indent: str = "  ", hang: Optional[int] = None) -> Text:
    """One dim transcript line: ``indent`` then ``text``, ellipsized to fit ``width`` when given.

    The one way the terminal writes a short quiet note (a warning, a hint), so each of them is a
    single line that never wraps; with no ``width`` a long one wraps under its own words.
    """
    body = ellipsize(text, max(width - len(indent), 1)) if width else text
    return HangingText(indent + body, style="dim", hang=len(indent) if hang is None else hang)


def message_count(history: Sequence[Sequence[str]]) -> int:
    """How many messages a conversation's history holds: each turn is the person's message and the AI's reply
    (a turn with no reply yet is one message)."""
    return sum(1 + bool(turn[1]) for turn in history)


def person_line(text: str) -> Text:
    """The person's own words in the transcript: ``❯`` and the text, in bold cyan.

    Their message when they send it, and each note they type while the AI works. A long one wraps under its
    own words, not under the marker."""
    return HangingText(f"❯ {text}", style="bold cyan", hang=2)


def indented_line(text: str, style: str = "", hang: int = HANG_DEFAULT, carry: bool = False) -> Text:
    """A transcript line that starts with ``hang`` spaces (or a marker that wide) and wraps under its words;
    ``carry`` repeats that start on every wrapped row instead (a question's bar)."""
    return HangingText(text, style=style, hang=hang, carry=carry)


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def working_notes_in(n: int) -> str:
    """The plain line for notes the person typed while the AI worked, once it has picked them up."""
    return "Working your note in now." if n == 1 else f"Working your {n} notes in now."


def card_score(card: dict) -> float:
    try:
        return float(card.get("relevance_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def source_count(card_meta: List[dict], sources: List[dict]) -> int:
    """How many sources the context holds: the cards, else the items of the source groups.

    "Sources" is the one word for the notes and files the AI looked at, in the context line, behind Alt+C
    (``Sources looked at``) and in the explanation, so the counts agree wherever they appear."""
    if card_meta:
        return len(card_meta)
    return sum(len(src.get("items") or []) for src in sources or [] if isinstance(src, dict))


def clean_title(title: Any) -> str:
    """A source's title for a person: one line, without the kind of record it came from (``Quest: ``,
    ``collection: ``), which is the store's word and not theirs. ``Untitled source`` when nothing is left."""
    text = " ".join(str(title or "").split())
    text = STORE_KIND_PREFIX.sub(r"\1", text).strip()
    return text or "Untitled source"


def context_block_lines(card_meta: List[dict], sources: List[dict]) -> List[Text]:
    """The transcript's note on the context gathered this turn: one dim sentence, or none.

    ``Looked at 6 sources: <title>, <title> and 4 more.`` The two best by retrieval score are named by title
    (never the internal id, the score or a file path; Alt+C lists them all). Nothing in the data says which
    of them the plan went on to use, so the order is the retrieval's own, the verb is "looked at" (never
    "used"), and no "most relevant to the plan" ranking is claimed. A long title is cut to
    ``CONTEXT_TITLE_CHARS`` at a word; the sentence wraps."""
    total = source_count(card_meta, sources)
    if not total:
        return []
    ranked = sorted(card_meta, key=card_score, reverse=True)
    titles = [ellipsize(clean_title(card.get("title")), CONTEXT_TITLE_CHARS) for card in ranked[:CONTEXT_TITLES_SHOWN]]
    more = total - len(titles)
    head = f"Looked at {plural(total, 'source')}"
    if not titles:
        return [indented_line(f"  {head}.", "dim")]
    if more > 0:
        named = ", ".join(titles) + f" and {more} more"
    else:
        named = " and ".join(titles)
    return [indented_line(f"  {head}: {named}.", "dim")]


# The transcript's metric block for one goal: this many lines, then how many more there are.
METRIC_LINES_SHOWN = 4
# Who vouches for a metric entry, in plain words (never the internal name of the meter).
METRIC_SOURCE_LABELS = {SOURCE_MEASURED: "measured automatically", SOURCE_RECORDED: "reported by the AI"}
# The most decimals a metric value shows; the data carries no unit, so none is added.
METRIC_DECIMALS = 3


def metric_number(value: Any) -> str:
    """A metric value the way a person reads it: at most ``METRIC_DECIMALS`` decimals, no trailing zeros.

    ``0.380`` is ``0.38``, ``1.0`` is ``1``, ``0.0004`` keeps its first digits instead of becoming ``0``, and a
    missing value is ``unknown``. The value's unit is not in the data, so none is shown or guessed."""
    if value is None:
        return "unknown"
    number = float(value)
    text = f"{number:.{METRIC_DECIMALS}f}".rstrip("0").rstrip(".")
    if text in ("0", "-0") and number != 0:
        text = f"{number:.2g}"
    return text or "0"


def metric_pair(entry: Mapping[str, Any]) -> str:
    return f"{metric_number(entry.get('before'))} → {metric_number(entry.get('after'))}"


def metric_groups(entries: Sequence[Mapping[str, Any]]) -> List[tuple]:
    """``(lead, other)`` per metric, in the order each first appears.

    A measured entry and the AI's own record of the same metric are two entries in the payload; they read as one
    line, led by the measured one, with the AI's numbers as ``other`` (None when there is no second entry).
    Nothing is dropped: an entry that has no partner stands alone."""
    groups: List[List[Any]] = []
    open_group: dict = {}
    for entry in entries:
        key = (entry.get("solution_id"), entry.get("metric_id"))
        source = entry.get("source")
        slot = open_group.get(key) if source in METRIC_SOURCE_LABELS and any(key) else None
        if slot is not None and slot[1] is None and (
                (slot[0].get("source") == SOURCE_MEASURED) != (source == SOURCE_MEASURED)):
            lead, other = (slot[0], entry) if slot[0].get("source") == SOURCE_MEASURED else (entry, slot[0])
            slot[0], slot[1] = lead, other
            continue
        slot = [entry, None]
        groups.append(slot)
        if source in METRIC_SOURCE_LABELS and any(key):
            open_group[key] = slot
    return [tuple(g) for g in groups]


def sentence_case(text: str) -> str:
    """``text`` with a lowercase first word capitalised, so it can open a sentence. A word with any capital in
    it (``iOS``, ``SQL``) is left as written."""
    word = text.split(" ", 1)[0] if text else ""
    return text[:1].upper() + text[1:] if word.islower() else text


def metric_entry_parts(lead: Mapping[str, Any], other: Optional[Mapping[str, Any]] = None) -> tuple:
    """One metric as ``(sentence, note)``: the sentence says the result, the note (``""`` when there is none)
    says what the other record of the same metric reported when the two differ.

    ``<name>: 0.38 → 0.91 (measured automatically)``. When the core flagged the two records as different
    (``disagrees``, which this never decides), or the other record has a number the lead lacks, the note is the AI's
    numbers, said neutrally: ``The AI reported 0.4 → 0.9.`` A declared not-applicable reads ``Nothing to measure. <reason>`` (a sentence of
    its own, so the reason's capital is right)."""
    if lead.get("source") == SOURCE_NOT_APPLICABLE:
        return f"Nothing to measure. {sentence(sentence_case(lead.get('reason') or 'No reason given'))}", ""
    name = lead.get("name") or lead.get("metric_id") or "Metric"
    how = METRIC_SOURCE_LABELS.get(lead.get("source"), str(lead.get("source")))
    text = f"{name}: {metric_pair(lead)} ({how})"
    # The other record also speaks when it holds a number the lead lacks (a metric that could only be read before the work),
    # so the person is not left with "unknown" while the AI reported a result.
    fills = other is not None and any(lead.get(end) is None and other.get(end) is not None for end in ("before", "after"))
    if not lead.get("disagrees") and not fills:
        return text, ""
    if other is not None:
        return text, f"The AI reported {metric_pair(other)}."
    return text, ("The AI reported different numbers." if lead.get("source") == SOURCE_MEASURED
                  else "The measured numbers differ.")


def natural_key(text: str) -> list:
    """A sort key that puts ``check 2`` before ``check 10``: digits compare as numbers, words ignoring case."""
    return [(0, int(part)) if part.isdigit() else (1, part.lower()) for part in re.split(r"(\d+)", text) if part]


def metric_order(group: tuple) -> tuple:
    """Where a metric line goes: by the metric's name (numbers inside it as numbers), a declared
    not-applicable last. The readings of one metric are already one group, so they stay together; the sort is
    stable, so metrics with the same name keep the order they were recorded in."""
    lead = group[0]
    return (lead.get("source") == SOURCE_NOT_APPLICABLE,
            natural_key(str(lead.get("name") or lead.get("metric_id") or "")))


def metric_line(lead: Mapping[str, Any], other: Optional[Mapping[str, Any]] = None) -> Text:
    """The transcript line of one metric: normal weight, and the note about a difference in the warning colour."""
    text, note = metric_entry_parts(lead, other)
    line = HangingText("  " + text, hang=2)
    if note:
        line.append(". ")
        line.append(note, style="yellow")
    return line


def metric_key(entry: Mapping[str, Any]) -> tuple:
    """What makes two metric entries the same result, so one is never written to the transcript twice."""
    return tuple(entry.get(key) for key in ("solution_id", "metric_id", "source", "reason", "before", "after"))


def union_entries(*sources: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The entries of several lists that carry the same results, each result once, in the order first seen.

    A turn reaches the terminal as a job's own entries, the turn's result and the explanation payload, and those
    are the same entries seen more than once. A result a source holds twice (two metrics that look alike) stays
    twice: the count of each result is the most any one source has, not the sum."""
    merged: List[Mapping[str, Any]] = []
    taken: Counter = Counter()
    for source in sources:
        seen: Counter = Counter()
        for entry in source if isinstance(source, (list, tuple)) else []:
            if not isinstance(entry, dict):
                continue
            key = metric_key(entry)
            seen[key] += 1
            if seen[key] > taken[key]:
                taken[key] += 1
                merged.append(entry)
    return merged


def metric_lines(entries: Sequence[Mapping[str, Any]], missing: bool = False,
                 limit: Optional[int] = METRIC_LINES_SHOWN, more_hint: str = "") -> List[Text]:
    """The transcript lines for a goal's metric results: one readable sentence per metric, in normal weight.

    Written from the payload entries and never by a model. At most ``limit`` of them (all when None), then
    a line counting the rest and ``more_hint`` (where to open them). A change that ended with no metric
    result recorded gets the one dim line saying so. ``[]`` when there is nothing to say."""
    groups = sorted(metric_groups(list(entries)), key=metric_order)
    shown = groups if limit is None else groups[:limit]
    lines = [metric_line(lead, other) for lead, other in shown]
    if len(groups) > len(shown):
        lines.append(notice_line(f"{plural(len(groups) - len(shown), 'more metric result')}. {more_hint}".strip()))
    if missing:
        lines.append(notice_line(NOT_RECORDED_LINE))
    return lines


def _missing_model_provider_message(env=None, which=shutil.which) -> Optional[str]:
    """Return a clear error message when no model provider can be reached, else ``None``.

    Zero-config first run: with no API-key env vars set, the config falls back to
    ``ClaudeCliProvider``, which needs the ``claude`` CLI on PATH. If it isn't installed, the
    session build hangs waiting on a subprocess that will never start. Detect that case up
    front so the user gets an actionable message instead of a blank alternate screen.
    A pure function (env/which injectable) so it is testable offline.
    """
    env = env if env is not None else os.environ
    backend = (env.get("QAR_MODEL_BACKEND") or "").strip().lower()
    has_key = bool(
        env.get("OPENAI_API_KEY") or env.get("GOOGLE_API_KEY") or env.get("ANTHROPIC_API_KEY")
    )
    if backend and backend != "claude_cli":
        return None  # an explicit non-CLI backend is configured; let it fail its own way
    if has_key:
        return None
    if which("claude") is not None:
        return None
    return (
        "No AI provider is configured. Set ANTHROPIC_API_KEY (or OPENAI_API_KEY / "
        "GOOGLE_API_KEY) to use a hosted model, or install the claude CLI and log in "
        "to use your Claude subscription."
    )




# The Alt+C panel's title: what it lists is the context the AI looked at; only a worker's own list of what it
# went on to rely on is called that.
PANEL_TITLE_LOOKED_AT = "Sources looked at"
PANEL_TITLE_RELIED_ON = "Sources it relied on"


def _build_future_context_text(bullets: str, relied: bool = False) -> Optional[Text]:
    """Build the Rich Text to display in the sources panel (Alt+C).

    ``Sources looked at (N)`` then one line per source, N being the number of lines, the same word and the
    same way of counting as the transcript's ``Looked at N sources``. ``relied`` is for a list the worker
    itself flagged as what it relied on (``Sources it relied on (N)``): the context a short answer gathered
    was only looked at, whatever the answer then did with it. Returns ``None`` when ``bullets`` is empty so
    callers can gate visibility. Pure function with no Textual dependencies, testable offline.
    """
    cleaned = bullets.strip()
    if not cleaned:
        return None
    lines = [STORE_KIND_PREFIX.sub(r"\1", ln) for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return None
    t = Text(no_wrap=False)
    title = PANEL_TITLE_RELIED_ON if relied else PANEL_TITLE_LOOKED_AT
    t.append(f"{title} ({len(lines)})\n", style="bold green")
    t.append("\n".join(f"  {ln}" for ln in lines) + "\n")
    t.append("  Alt+C to close")
    return t
