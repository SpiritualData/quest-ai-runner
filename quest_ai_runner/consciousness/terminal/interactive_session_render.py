"""Terminal rendering for the session: ANSI helpers, skill front matter parsing, the stdout _Console and model labels.

Split out of ``interactive_session.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import sys
from typing import List, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# Optional rendering dependency: Markdown via rich when it is installed.
try:
    from rich.console import Console as _RichConsole
    from rich.markdown import Markdown as _RichMarkdown
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False


# ── ANSI helpers ──────────────────────────────────────────────────────────────

_RESET   = "\033[0m"
_BOLD    = "\033[1m"
_DIM     = "\033[2m"
_CYAN    = "\033[36m"
_GREEN   = "\033[32m"
_YELLOW  = "\033[33m"
_MAGENTA = "\033[35m"
_BLUE    = "\033[34m"
_GOLD    = "\033[38;5;220m"  # 256-color gold for opus
_BRIGHT_CYAN = "\033[96m"


def _a(code: str, s: str) -> str:
    return f"{code}{s}{_RESET}"


def _highlight_ansi(text: str) -> str:
    """Minimal ANSI syntax highlight for markdown code blocks and emphasis (non-rich fallback)."""
    import re
    lines = text.split("\n")
    out: List[str] = []
    in_block = False
    for ln in lines:
        if ln.startswith("```"):
            in_block = not in_block
            # Show fence in dim; language label in cyan
            lang = ln[3:].strip()
            if in_block:
                out.append(_a(_DIM, "```") + (_a(_CYAN, lang) if lang else ""))
            else:
                out.append(_a(_DIM, "```"))
            continue
        if in_block:
            # Code block lines: dim yellow
            out.append(_a(_YELLOW, ln))
            continue
        # Inline code: `foo` → cyan
        ln = re.sub(r"`([^`]+)`", lambda m: _a(_CYAN, "`" + m.group(1) + "`"), ln)
        # Bold: **foo** → bold
        ln = re.sub(r"\*\*(.+?)\*\*", lambda m: _a(_BOLD, m.group(1)), ln)
        # Italic: *foo* → dim
        ln = re.sub(r"\*(.+?)\*", lambda m: _a(_DIM, m.group(1)), ln)
        out.append(ln)
    return "\n".join(out)


def _bullet(text: str, indent: int = 0, color: Optional[str] = None) -> str:
    """Format a bullet point with optional color and indentation."""
    prefix = "  " * indent + "● "
    if color:
        return prefix + color + text + _RESET
    return prefix + text


def _parse_skill_frontmatter(skill_file: str) -> dict:
    """Parse SKILL.md YAML frontmatter and return metadata dict."""
    if not yaml:
        return {}
    try:
        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}
        return yaml.safe_load(parts[1]) or {}
    except Exception:
        return {}


# ── Console wrapper ───────────────────────────────────────────────────────────

class _Console:
    """Thin render surface — rich when available, ANSI otherwise, plain on non-TTY."""

    def __init__(self) -> None:
        self._rich = _RichConsole(highlight=False, soft_wrap=True) if _HAS_RICH else None
        self._color = sys.stdout.isatty()

    def write(self, s: str) -> None:
        sys.stdout.write(s); sys.stdout.flush()

    def line(self, s: str = "") -> None:
        sys.stdout.write(s + "\n"); sys.stdout.flush()

    def dim(self, s: str) -> None:
        if self._rich:    self._rich.print(s, style="dim", highlight=False)
        elif self._color: self.line(_a(_DIM, s))
        else:             self.line(s)

    def bullet(self, text: str, indent: int = 0, color: Optional[str] = None) -> None:
        """Print a bullet point with optional color."""
        if self._rich and color:
            self._rich.print(f"{'  ' * indent}[{color}]●[/] {text}", highlight=False)
        else:
            self.line(_bullet(text, indent, color))

    def sub_bullet(self, text: str, indent: int = 1) -> None:
        """Print a sub-bullet (tree-style indented)."""
        self.dim("  " * indent + "⎿  " + text)

    def markdown(self, text: str) -> None:
        """Render text as markdown with syntax-highlighted code blocks."""
        if self._rich:
            self._rich.print(_RichMarkdown(text, code_theme="monokai"), highlight=False)
        elif self._color:
            self.line(_highlight_ansi(text))
        else:
            self.line(text)

    def speaker(self, label: str, color: str, text: str) -> None:
        _ansi = {"cyan": _CYAN, "green": _GREEN, "yellow": _YELLOW,
                 "magenta": _MAGENTA, "blue": _BLUE}
        if self._rich:
            self._rich.print(f"[bold {color}]{label}[/]  {text}", highlight=False)
        elif self._color:
            self.line(f"{_BOLD}{_ansi.get(color, '')}{label}{_RESET}  {text}")
        else:
            self.line(f"{label}  {text}")

    def rule(self) -> None:
        w = self._width()
        s = "─" * w
        if self._rich:    self._rich.print(s, style="dim", highlight=False)
        elif self._color: self.line(_a(_DIM, s))
        else:             self.line(s)

    def _width(self) -> int:
        try:
            import shutil
            return max(8, min(shutil.get_terminal_size((80, 24)).columns, 100))
        except Exception:  # noqa: BLE001
            return 72


# ── Model labels and deep-run tracking ────────────────────────────────────────

def _model_label(model_id: Optional[str]) -> str:
    if not model_id:
        return ""
    m = model_id.lower()
    for tier in ("haiku", "sonnet", "opus", "fable"):
        if tier in m:
            return tier
    return model_id.split("-")[0]


def _model_display(model_id: Optional[str], resolved: Optional[str] = None) -> str:
    """The tier plus the full model id it resolved to, e.g. ``sonnet (claude-sonnet-4-5-2025...)``.

    ``resolved`` is the id the worker actually ran with; without it (or when it only repeats the
    tier) the id in ``model_id`` is shown as its tier label plus itself when it is a full id.
    """
    full = (resolved or model_id or "").strip()
    label = _model_label(model_id or resolved)
    if not full:
        return ""
    if not label or full.lower() == label.lower():
        return full
    return f"{label} ({full})"
