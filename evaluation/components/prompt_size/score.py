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


def score_sweep(fn, case):
    """A production case runs ``sweep`` samples from a light day to the heaviest; the ceiling must hold for all."""
    from prompt_size import fixtures
    sweep = case["sweep"]
    rows = [score_case(fn, dict(case, sweep=sweep, sample=i, sweep_run=True)) for i in range(sweep)]
    sizes = [r["chars"] for r in rows]
    dups = sum(r["dups"] for r in rows)
    ceiling = case["ceiling_chars"]
    ok = max(sizes) <= ceiling and dups == 0
    note = (f"{sweep} production samples: median {sorted(sizes)[len(sizes) // 2]}, p95 {p95(sizes)}, max {max(sizes)} "
            f"chars (ceiling {ceiling}), {dups} duplicated blocks")
    return dict(ok=ok, chars=max(sizes), p95_chars=p95(sizes), dups=dups, point=case["point"],
                got=f"max {max(sizes)} chars", note=note)


def score_case(fn, case):
    from prompt_size import fixtures
    if case.get("sweep") and not case.get("sweep_run"):
        return score_sweep(fn, case)
    text = fn(case["point"], fixtures.build_inputs(case))
    chars = len(text)
    blocks, paras = duplicate_blocks(text)
    dups = blocks + paras
    ceiling = case["ceiling_chars"]
    ok = chars <= ceiling and dups == 0
    note = (f"{chars} chars (ceiling {ceiling}), {dups} duplicated blocks "
            f"({blocks} context-update, {paras} identical paragraphs)")
    return dict(ok=ok, chars=chars, dups=dups, point=case["point"], got=f"{chars} chars", note=note)


def p95(values):
    """Nearest-rank 95th percentile of ``values`` (the max when there are fewer than 20)."""
    vals = sorted(values)
    if not vals:
        return 0
    return vals[min(len(vals) - 1, max(0, -(-95 * len(vals) // 100) - 1))]


def aggregate(rows):
    """Per assembly point: the max and p95 of the fixed fixtures, and of the production sweep on its own line."""
    by = {}
    for r in rows:
        key = f"{r.get('point')}{' (production sweep)' if 'p95_chars' in r else ''}"
        by.setdefault(key, []).append(r)
    parts = []
    for p, rs in sorted(by.items()):
        sizes = [r.get("chars", 0) for r in rs]
        p95s = [r["p95_chars"] for r in rs if "p95_chars" in r] or [p95(sizes)]
        parts.append(f"{p}: max {max(sizes)} chars, p95 {max(p95s)} chars, {sum(r.get('dups', 0) for r in rs)} dups")
    return "; ".join(parts)
