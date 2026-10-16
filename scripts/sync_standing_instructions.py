#!/usr/bin/env python3
"""Regenerate the shipped standing instructions from Joshua's evolvable-structure skill.

Run from anywhere:  python scripts/sync_standing_instructions.py [--skill PATH] [--check]
Exit 0 = written (or, with --check, already in sync), 1 = --check found drift, 2 = skill not found.

``quest_ai_runner/prompts/evolvable_structure.md`` is a derived copy of the skill's ``SKILL.md``
(Joshua's own words): its opening, the first Purpose paragraph, Definitions and Principles. The
skill-only parts (sub-agent and feedback-recording instructions, Background, Diving Deeper) are
left out, and one runner line replaces them. The library ships the copy so it never depends on the
skill's path; this script and ``tests/test_standing_instructions.py`` keep the copy from drifting.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SKILL_ENV = "QAR_ESOI_SKILL"
DEFAULT_SKILL = Path("/home/joshua/hq/stories/spiritual_data/product/.claude/skills/evolvable-structure/SKILL.md")
OUTPUT = Path(__file__).resolve().parent.parent / "quest_ai_runner" / "prompts" / "evolvable_structure.md"
TAG = "Evolvable Structure that Optimizes Intent"
RUNNER_LINE = ("In whatever work you do (unless the human tells you otherwise), ensure that ESOI is generally "
               "applied, resulting in the effective creation of collaborative innovation tools, and the "
               "effective use of preexisting tools for collective progress over time.")


def skill_path(arg: str | None = None) -> Path:
    return Path(arg or os.environ.get(SKILL_ENV) or DEFAULT_SKILL).expanduser()


def _sections(body: str) -> tuple[str, dict[str, str]]:
    """The text before the first ``## `` heading, and each ``## `` section's text by heading."""
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    return parts[0], {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def build(skill_text: str) -> str:
    """The standing-instruction text derived from a SKILL.md."""
    body = re.sub(r"\A---\n.*?\n---\n", "", skill_text, flags=re.S)
    intro, sections = _sections(body)
    opening = re.sub(r"^# .*\n", "", intro.strip()).strip()
    purpose = sections["Purpose"].split("\n\n")[0].strip()
    definitions = next(v for k, v in sections.items() if k.startswith("Definitions"))
    principles_key = next(k for k in sections if k.startswith("Principles"))
    return "\n\n".join([
        "Apply the below principles:",
        f"<{TAG}>\n{opening}",
        f"## Purpose\n\n{purpose}\n\n{RUNNER_LINE}",
        f"## Definitions\n{definitions}",
        f"## {principles_key}\n{sections[principles_key]}\n</{TAG}>",
    ]) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill", help=f"SKILL.md path (default: ${SKILL_ENV} or {DEFAULT_SKILL})")
    ap.add_argument("--check", action="store_true", help="report drift instead of writing")
    args = ap.parse_args()
    src = skill_path(args.skill)
    if not src.is_file():
        print(f"skill not found: {src}", file=sys.stderr)
        return 2
    text = build(src.read_text(encoding="utf-8"))
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") == text:
            return 0
        print(f"{OUTPUT} is out of date with {src}; run scripts/sync_standing_instructions.py", file=sys.stderr)
        return 1
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
