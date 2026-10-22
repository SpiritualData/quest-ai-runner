"""Textual UI helpers: clipboard tool fallback, shallow and future context text, the missing-provider message.

Split out of ``textual_ui.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any, List, Mapping, Optional, Sequence
from rich.text import Text

from .core.metric_results import NOT_RECORDED_LINE, SOURCE_MEASURED, SOURCE_NOT_APPLICABLE, SOURCE_RECORDED


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
        title = " ".join(str(card.get("title") or "").split()) or "Untitled source"
        lines.append(f"- {title}")
    return "\n".join(lines)


# The transcript's context line names this many sources by title, then "and K more".
CONTEXT_TITLES_SHOWN = 2
CONTEXT_TITLE_CHARS = 50


def ellipsize(text: str, limit: int) -> str:
    """``text`` on one line, cut to ``limit`` characters with a trailing ellipsis when it is longer.

    The cut falls on a word boundary when one is near the end, so a title is not chopped mid-word.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 1)]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def notice_line(text: str, width: Optional[int] = None, indent: str = "  ") -> Text:
    """One dim transcript line: ``indent`` then ``text``, ellipsized to fit ``width`` when given.

    The one way the terminal writes a short quiet note (a warning, a hint), so each of them is a
    single line that never wraps.
    """
    body = ellipsize(text, max(width - len(indent), 1)) if width else text
    return Text(indent + body, style="dim")


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def working_notes_in(n: int) -> str:
    """The plain line for notes the person typed while the AI worked, once it has picked them up."""
    return "Working your note in now." if n == 1 else f"Working your {n} notes in now."


def waiting_note_line(text: str) -> Text:
    """A note typed while the AI works, as the person's own line (like their messages) with a small tag.

    The tag is the only thing that says it has not been read yet; the count of notes waiting is in the
    status strip and nowhere else."""
    line = Text(f"❯ {text}", style="bold cyan")
    line.append("  (waiting)", style="dim")
    return line


def card_score(card: dict) -> float:
    try:
        return float(card.get("relevance_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def source_count(card_meta: List[dict], sources: List[dict]) -> int:
    """How many sources the context holds: the cards, else the items of the source groups.

    "Sources" is the one word for the notes and files the AI looked at, in the context line, behind Alt+C
    and in the explanation, so the counts agree wherever they appear."""
    if card_meta:
        return len(card_meta)
    return sum(len(src.get("items") or []) for src in sources or [] if isinstance(src, dict))


def context_block_lines(card_meta: List[dict], sources: List[dict]) -> List[Text]:
    """The transcript's note on the context gathered this turn: one dim sentence, or none.

    ``Found 6 sources: <title>, <title> and 4 more.`` The two best by retrieval score are named by title
    (never the internal id, the score or a file path; Alt+C lists them all). Nothing in the data says which
    of them the plan went on to use, so the order is the retrieval's own and no "most relevant to the
    plan" ranking is claimed. A long title is cut to ``CONTEXT_TITLE_CHARS``; the sentence wraps."""
    total = source_count(card_meta, sources)
    if not total:
        return []
    ranked = sorted(card_meta, key=card_score, reverse=True)
    titles = [ellipsize(card.get("title") or "Untitled source", CONTEXT_TITLE_CHARS)
              for card in ranked[:CONTEXT_TITLES_SHOWN]]
    more = total - len(titles)
    head = f"Found {plural(total, 'source')}"
    if not titles:
        return [Text(f"  {head}.", style="dim")]
    if more > 0:
        named = ", ".join(titles) + f" and {more} more"
    else:
        named = " and ".join(titles)
    return [Text(f"  {head}: {named}.", style="dim")]


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
    return f"{metric_number(entry.get('before'))} -> {metric_number(entry.get('after'))}"


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


def metric_entry_text(lead: Mapping[str, Any], other: Optional[Mapping[str, Any]] = None) -> str:
    """One metric as one sentence, wrapped by the transcript and never cut.

    ``<name>: 0.38 -> 0.91 (measured automatically)``. When the core flagged the two records as different
    (``disagrees``, which this never decides), the AI's numbers follow, said neutrally:
    ``... (measured automatically). The AI reported 0.4 -> 0.9.`` A declared not-applicable reads
    ``Nothing to measure: <reason>``."""
    if lead.get("source") == SOURCE_NOT_APPLICABLE:
        return f"Nothing to measure: {lead.get('reason') or 'no reason given'}"
    name = lead.get("name") or lead.get("metric_id") or "Metric"
    how = METRIC_SOURCE_LABELS.get(lead.get("source"), str(lead.get("source")))
    text = f"{name}: {metric_pair(lead)} ({how})"
    if other is not None and lead.get("disagrees"):
        text += f". The AI reported {metric_pair(other)}."
    elif other is None and lead.get("disagrees"):
        text = text[:-1] + (", and the AI reported different numbers)" if lead.get("source") == SOURCE_MEASURED
                            else ", and the measured numbers differ)")
    return text


def metric_key(entry: Mapping[str, Any]) -> tuple:
    """What makes two metric entries the same result, so one is never written to the transcript twice."""
    return tuple(entry.get(key) for key in ("solution_id", "metric_id", "source", "reason", "before", "after"))


def metric_lines(entries: Sequence[Mapping[str, Any]], missing: bool = False,
                 limit: Optional[int] = METRIC_LINES_SHOWN, more_hint: str = "") -> List[Text]:
    """The transcript lines for a goal's metric results: one readable sentence per metric, in normal weight.

    Written from the payload entries and never by a model. At most ``limit`` of them (all when None), then
    a line counting the rest and ``more_hint`` (where to open them). A change that ended with no metric
    result recorded gets the one dim line saying so. ``[]`` when there is nothing to say."""
    groups = metric_groups(list(entries))
    shown = groups if limit is None else groups[:limit]
    lines = [Text("  " + metric_entry_text(lead, other)) for lead, other in shown]
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


# The kind of store a worker may name at the start of a bullet ("- collection: Pricing tiers"): internal, so not shown.
STORE_KIND_PREFIX = re.compile(r"^(\s*(?:[-*\u2022]\s+)?)(?:collection|card|quest|note|file)\s*:\s+", re.IGNORECASE)


def _build_future_context_text(bullets: str) -> Optional[Text]:
    """Build the Rich Text to display in the "sources used" panel (Alt+C).

    ``Sources used (N)`` then one line per source, N being the number of lines, the same word and the same
    way of counting as the transcript's ``Found N sources``. Returns ``None`` when ``bullets`` is empty so
    callers can gate visibility. Pure function with no Textual dependencies, testable offline.
    """
    cleaned = bullets.strip()
    if not cleaned:
        return None
    lines = [STORE_KIND_PREFIX.sub(r"\1", ln) for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return None
    t = Text(no_wrap=False)
    t.append(f"Sources used ({len(lines)})\n", style="bold green")
    t.append("\n".join(f"  {ln}" for ln in lines) + "\n")
    t.append("  Alt+C to close")
    return t
