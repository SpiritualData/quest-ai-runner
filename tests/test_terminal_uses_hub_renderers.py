"""QAR's terminal draws what the hub's renderers return; it does not decide the wording, the folding or the privacy itself.

The rules for the work summary, interim folding, asks and the stopped reason live in `hubofhubs.terminal` (generic, usable by any
consumer). This fails if QAR starts to carry its own copy of a sentence or a renderer.
"""
import ast
import re
from pathlib import Path

from hubofhubs.terminal import strings

PACKAGE = Path(__file__).resolve().parent.parent / "quest_ai_runner"
HUB_RENDERERS = {"live_text", "summary_text", "summary_sentences", "expanded_groups", "earlier_preview", "interim_parts", "stopped_reason",
                 "apply_event", "sanitize_event", "summary_clauses", "folded_row", "ask_block", "parse_answer", "record_title_of"}


def sources(terminal_only=False):
    files = [p for p in PACKAGE.rglob("*.py") if not terminal_only or p.name.startswith(("textual_ui", "cli_chat", "interactive_session"))]
    return {p: p.read_text() for p in files}


def test_no_hub_sentence_is_copied_into_qar():
    fixed = {k: max(re.split(r"\{\{\w+\}\}", v), key=len).strip() for k, v in strings.EN.items()}
    fixed = {k: v for k, v in fixed.items() if len(v) >= 24}   # a short phrase can match by chance ("Type your answer")
    assert fixed
    copies = [(p.name, k) for p, text in sources().items() for k, v in fixed.items() if v in text]
    assert not copies, f"sentences the hub owns appear in QAR: {copies}"


def test_qar_defines_none_of_the_hub_renderers():
    defined = {(p.name, node.name) for p, text in sources(terminal_only=True).items() for node in ast.walk(ast.parse(text))
               if isinstance(node, ast.FunctionDef) and node.name in HUB_RENDERERS}
    assert not defined, f"QAR re-implements hub renderers: {sorted(defined)}"


def test_the_terminal_imports_the_hub_renderers():
    text = (PACKAGE / "textual_ui_trail.py").read_text()
    assert "from hubofhubs.terminal import" in text
    assert "from hubofhubs.terminal import" in (PACKAGE / "textual_ui_blocks.py").read_text()
