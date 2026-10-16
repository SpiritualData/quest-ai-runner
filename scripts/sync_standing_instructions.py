#!/usr/bin/env python3
"""Regenerate the ESOI outputs from Joshua's evolvable-structure skill (one source of truth).

Run from anywhere:  python scripts/sync_standing_instructions.py [--skill PATH] [--check]
Exit 0 = written (or, with --check, already in sync), 1 = --check found drift, 2 = skill not found.

Two derived copies of the skill's ``SKILL.md`` (Joshua's own words), each its opening, the first
Purpose paragraph, Definitions and Principles. The skill-only parts (sub-agent and
feedback-recording instructions, Background, Diving Deeper) are left out of both.

* ``quest_ai_runner/prompts/evolvable_structure.md``: the standing instructions every deep run
  receives, with one runner line added to the Purpose.
* ``docs/evolvable-structure.md``: the public, reader-facing page the Quest AI "Explain how I got
  this" panel links to, with a short reader introduction instead of the runner line.

The library ships the copies so it never depends on the skill's path; this script and
``tests/test_standing_instructions.py`` keep both from drifting.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

SKILL_ENV = "QAR_ESOI_SKILL"
REPO = Path(__file__).resolve().parent.parent
# Where the skill sits relative to this checkout in the maintainers' corpus; elsewhere set the env.
DEFAULT_SKILL = REPO.parent.parent / ".claude" / "skills" / "evolvable-structure" / "SKILL.md"
OUTPUT = REPO / "quest_ai_runner" / "prompts" / "evolvable_structure.md"
PUBLIC_OUTPUT = REPO / "docs" / "evolvable-structure.md"
TAG = "Evolvable Structure that Optimizes Intent"
RUNNER_LINE = ("In whatever work you do (unless the human tells you otherwise), ensure that ESOI is generally "
               "applied, resulting in the effective creation of collaborative innovation tools, and the "
               "effective use of preexisting tools for collective progress over time.")
PUBLIC_INTRO = """\
<!-- Generated from the evolvable-structure skill by scripts/sync_standing_instructions.py. Do not
edit by hand: change the skill and rerun the script. -->

# Evolvable Structure that Optimizes Intent (ESOI)

ESOI is the design philosophy behind quest-ai-runner and Quest AI. Every deep run receives the
principles below as standing instructions (`quest_ai_runner/prompts/evolvable_structure.md`, the
same text). Under a Quest AI answer, "Explain how I got this" has an Evolvable structure section
that says how these principles shaped that answer: the ideal it serves, a practical way to check
it, the structure it built on or left open to change, and what would change or improve it next
time. It is written by the AI after the answer, as a summary, not a record of every step.

The principles themselves follow, as written by their author.
"""


def skill_path(arg: str | None = None) -> Path:
    return Path(arg or os.environ.get(SKILL_ENV) or DEFAULT_SKILL).expanduser()


def _sections(body: str) -> tuple[str, dict[str, str]]:
    """The text before the first ``## `` heading, and each ``## `` section's text by heading."""
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.M)
    return parts[0], {parts[i]: parts[i + 1].strip() for i in range(1, len(parts), 2)}


def _parts(skill_text: str) -> dict[str, str]:
    """The four shared parts of a SKILL.md: opening, first Purpose paragraph, Definitions, Principles."""
    body = re.sub(r"\A---\n.*?\n---\n", "", skill_text, flags=re.S)
    intro, sections = _sections(body)
    principles_key = next(k for k in sections if k.startswith("Principles"))
    return {
        "opening": re.sub(r"^# .*\n", "", intro.strip()).strip(),
        "purpose": sections["Purpose"].split("\n\n")[0].strip(),
        "definitions": next(v for k, v in sections.items() if k.startswith("Definitions")),
        "principles_key": principles_key,
        "principles": sections[principles_key],
    }


def build(skill_text: str) -> str:
    """The standing-instruction text derived from a SKILL.md."""
    p = _parts(skill_text)
    return "\n\n".join([
        "Apply the below principles:",
        f"<{TAG}>\n{p['opening']}",
        f"## Purpose\n\n{p['purpose']}\n\n{RUNNER_LINE}",
        f"## Definitions\n{p['definitions']}",
        f"## {p['principles_key']}\n{p['principles']}\n</{TAG}>",
    ]) + "\n"


def build_public(skill_text: str) -> str:
    """The public reader-facing page derived from the same SKILL.md."""
    p = _parts(skill_text)
    # The skill writes each definition on its own line and indents the intent template terms;
    # GitHub would fold both into one paragraph, so they become paragraphs and a list.
    definitions = re.sub(r"\n(?=\*\*)", "\n\n", p["definitions"])
    definitions = re.sub(r":\n(?=    \*\*)", ":\n\n", definitions)
    definitions = re.sub(r"^    (\*\*[^*]+\*\*:)", r"- \1", definitions, flags=re.M)
    return "\n\n".join([
        PUBLIC_INTRO.strip(),
        p["opening"],
        f"## Purpose\n\n{p['purpose']}",
        f"## Definitions\n{definitions}",
        f"## {p['principles_key']}\n{p['principles']}",
    ]) + "\n"


def outputs(skill_text: str) -> dict[Path, str]:
    """Every derived file and the text it must hold."""
    return {OUTPUT: build(skill_text), PUBLIC_OUTPUT: build_public(skill_text)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skill", help=f"SKILL.md path (default: ${SKILL_ENV} or {DEFAULT_SKILL})")
    ap.add_argument("--check", action="store_true", help="report drift instead of writing")
    args = ap.parse_args()
    src = skill_path(args.skill)
    if not src.is_file():
        print(f"skill not found: {src}", file=sys.stderr)
        return 2
    expected = outputs(src.read_text(encoding="utf-8"))
    if args.check:
        stale = [p for p, text in expected.items()
                 if not p.is_file() or p.read_text(encoding="utf-8") != text]
        for p in stale:
            print(f"{p} is out of date with {src}; run scripts/sync_standing_instructions.py", file=sys.stderr)
        return 1 if stale else 0
    for p, text in expected.items():
        p.write_text(text, encoding="utf-8")
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
