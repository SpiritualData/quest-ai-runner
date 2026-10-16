"""Standing instructions: principles every deep run receives, whoever it acts as.

A deep run's preamble is replaceable by design: a rep run swaps the lane's org preamble for that
person's persona, and the budget cuts preamble blocks by priority. Principles that should govern
ALL work therefore cannot live there. They go in their own layer, composed into every deep prompt
as a required part (never cut by the budget, never replaced by a persona).

The library ships one default, ``prompts/evolvable_structure.md``: Joshua's Evolvable Structure
that Optimizes Intent (ESOI) principles, generated from his skill by
``scripts/sync_standing_instructions.py`` (never hand-edit it). A deployment changes the layer with ``QAR_DEEP_INSTRUCTIONS_FILES``:

* unset            -> the shipped default;
* empty            -> no standing instructions;
* a path list      -> those files, in order, joined by blank lines (``os.pathsep``-separated, so
                      ``a.md:b.md`` on POSIX). The token ``default`` stands for the shipped file,
                      so ``default:/srv/org/rules.md`` adds a deployment's own rules after it.

Files are read on every call, so an edit applies to the next deep run without a restart. A file
that cannot be read is logged and skipped; the run is never blocked by it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from .runtime_settings import core_settings

log = logging.getLogger("quest-ai-runner.standing_instructions")

DEEP_INSTRUCTIONS_ENV = "QAR_DEEP_INSTRUCTIONS_FILES"
DEFAULT_TOKEN = "default"
DEFAULT_INSTRUCTIONS_FILE = Path(__file__).resolve().parent.parent / "prompts" / "evolvable_structure.md"


def instruction_files() -> List[Path]:
    """The files the standing-instruction layer is read from, in order (see module docstring)."""
    raw = core_settings().deep_instructions_files
    if raw is None:
        return [DEFAULT_INSTRUCTIONS_FILE]
    files: List[Path] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        files.append(DEFAULT_INSTRUCTIONS_FILE if entry == DEFAULT_TOKEN else Path(entry).expanduser())
    return files


def load_standing_instructions() -> str:
    """The standing-instruction text for a deep run, or "" when the layer is empty."""
    texts: List[str] = []
    for path in instruction_files():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            log.warning("%s: could not read %s (%s); skipping it", DEEP_INSTRUCTIONS_ENV, path, e)
            continue
        if text:
            texts.append(text)
    return "\n\n".join(texts)
