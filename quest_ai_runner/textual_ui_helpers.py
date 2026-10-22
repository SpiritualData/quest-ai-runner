"""Textual UI helpers: clipboard tool fallback, shallow and future context text, the missing-provider message.

Split out of ``textual_ui.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
from typing import List, Optional
from rich.text import Text


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
    """Build "context it used" bullet lines from shallow-turn context cards.

    Mirrors the bullet format deep runs produce for the FutureContextPanel, so a shallow
    turn's context cards can reuse the same Alt+C panel and footer hint instead of being
    discarded once the turn ends. Returns an empty string when there is nothing to show.
    """
    lines: List[str] = []
    for card in cards or []:
        title = card.get("title") or card.get("id") or "(no title)"
        adapter = card.get("adapter") or ""
        label = f"{adapter}: {title}" if adapter else title
        lines.append(f"- {label}")
    return "\n".join(lines)


# The transcript's context block: a count line, this many cards, then "and K more".
CONTEXT_CARDS_SHOWN = 3
CONTEXT_TITLE_CHARS = 70


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

    The one way the terminal writes a short quiet note (a warning, a hint, the context summary), so
    each of them is a single line that never wraps.
    """
    body = ellipsize(text, max(width - len(indent), 1)) if width else text
    return Text(indent + body, style="dim")


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def card_score(card: dict) -> float:
    try:
        return float(card.get("relevance_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def context_block_lines(card_meta: List[dict], sources: List[dict], width: int = 80) -> List[Text]:
    """The calm transcript block for the context gathered this turn, no line wider than ``width``.

    A count line, the three best cards by title (never the internal card id, the score or file
    paths; those are in the live context panel), "and K more", and one line of source labels with
    their item counts. At most six lines, so a turn that gathered hundreds of items still reads as
    one quiet block instead of a wall.
    """
    lines: List[Text] = [notice_line(
        f"Context: {plural(len(card_meta), 'card')}, {plural(len(sources), 'source')}", width, indent="",
    )]
    ranked = sorted(card_meta, key=card_score, reverse=True)
    for card in ranked[:CONTEXT_CARDS_SHOWN]:
        title = ellipsize(card.get("title") or "Untitled card", min(CONTEXT_TITLE_CHARS, max(width - 4, 10)))
        line = Text("  ")
        line.append("●", style="cyan")
        line.append(f" {title}")
        lines.append(line)
    if len(ranked) > CONTEXT_CARDS_SHOWN:
        lines.append(notice_line(f"… and {len(ranked) - CONTEXT_CARDS_SHOWN} more", width))
    # A source with no file-level items (a recent or card match) has no count worth a label.
    counted = [f"{src.get('label') or src.get('adapter') or '?'} ({len(src['items'])})"
               for src in sources if src.get("items")]
    if counted:
        lines.append(notice_line("Sources: " + ", ".join(counted), width))
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


def _build_future_context_text(bullets: str) -> Optional[Text]:
    """Build the Rich Text to display in the "context it used" panel.

    Returns ``None`` when ``bullets`` is empty so callers can gate visibility.
    This is a pure function with no Textual dependencies — testable offline.
    """
    cleaned = bullets.strip()
    if not cleaned:
        return None
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    if not lines:
        return None
    body = "\n".join(f"  {ln}" for ln in lines)
    hint = "\x1b[2m  [Alt+C] close\x1b[0m"
    t = Text.from_ansi(f"\x1b[1;32mContext it used\x1b[0m\n{body}\n{hint}")
    t.no_wrap = False
    return t
