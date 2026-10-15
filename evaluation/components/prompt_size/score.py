"""Score one prompt_size case: build the fixed inputs, assemble the prompt, measure it.

A case passes when the prompt is within the case's ``ceiling_chars`` (the assembly point's budget) AND holds
zero duplicated blocks. A duplicated block is either a second (or later) context-updates block, or an
identical paragraph of at least ``MIN_BLOCK_CHARS`` characters repeated (whitespace-normalised). The
2026-10-06 incident had 16 duplicated context-update blocks in an 838K-character autopilot prompt.
"""
import re
from collections import Counter

MIN_BLOCK_CHARS = 200


def duplicate_blocks(text):
    """``(extra_update_blocks, extra_identical_paragraphs)`` in ``text``."""
    from quest_ai_runner.core.prompt_budget import count_blocks
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "")]
    seen = Counter(p for p in paras if len(p) >= MIN_BLOCK_CHARS)
    return max(0, count_blocks(text) - 1), sum(n - 1 for n in seen.values() if n > 1)


def score_case(fn, case):
    from prompt_size import fixtures
    text = fn(case["point"], fixtures.build_inputs(case))
    chars = len(text)
    blocks, paras = duplicate_blocks(text)
    dups = blocks + paras
    ceiling = case["ceiling_chars"]
    ok = chars <= ceiling and dups == 0
    note = (f"{chars} chars (ceiling {ceiling}), {dups} duplicated blocks "
            f"({blocks} context-update, {paras} identical paragraphs)")
    return dict(ok=ok, chars=chars, dups=dups, point=case["point"], got=f"{chars} chars", note=note)


def aggregate(rows):
    by = {}
    for r in rows:
        by.setdefault(r.get("point"), []).append(r)
    parts = [f"{p}: max {max(r.get('chars', 0) for r in rs)} chars, {sum(r.get('dups', 0) for r in rs)} dups"
             for p, rs in sorted(by.items(), key=lambda kv: str(kv[0]))]
    return "; ".join(parts)
