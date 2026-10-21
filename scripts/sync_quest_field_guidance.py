#!/usr/bin/env python3
"""Vendor the backend's quest field guidance YAML into Quest AI Runner.

The ONE source of truth for how an AI writes each quest field is the backend's
``app/prompts/quest_field_guidance.yaml``. Quest AI Runner carries a byte-identical copy at
``quest_ai_runner/core/quest_field_guidance.yaml`` and builds every field instruction from it
(``core/quest_field_instructions.py``). Never edit the copy: change the backend file, then run

    python scripts/sync_quest_field_guidance.py            # copy backend -> runner
    python scripts/sync_quest_field_guidance.py --check    # exit 1 when the copy has drifted

The backend path defaults to ``../quest-backend/app/prompts/quest_field_guidance.yaml`` relative to
the repo root; override it with a positional argument or ``QUEST_FIELD_GUIDANCE_SOURCE``.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "quest_ai_runner" / "core" / "quest_field_guidance.yaml"
DEFAULT_SOURCE = ROOT.parent / "quest-backend" / "app" / "prompts" / "quest_field_guidance.yaml"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", nargs="?", help="path to the backend YAML")
    parser.add_argument("--check", action="store_true", help="exit 1 when the vendored copy differs")
    args = parser.parse_args(argv)
    source = Path(args.source or os.environ.get("QUEST_FIELD_GUIDANCE_SOURCE") or DEFAULT_SOURCE)
    if not source.is_file():
        print(f"source not found: {source}", file=sys.stderr)
        return 2
    same = TARGET.is_file() and TARGET.read_bytes() == source.read_bytes()
    if args.check:
        print("in sync" if same else f"DRIFT: {TARGET} differs from {source}")
        return 0 if same else 1
    if same:
        print("already in sync")
        return 0
    shutil.copyfile(source, TARGET)
    print(f"copied {source} -> {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
