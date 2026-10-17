"""The evolvable-structure registry stays true: see docs/structure/README.md.

The checks are the ``esoi`` package's (``esoi rules`` lists them) plus QAR's own plugin, scripts/esoi_rules.py,
both configured by esoi.toml. They run in-process here (no network, no model). A new module that no component
claims, a core module importing an adapter, an oversized module or a stale generated doc fails here.
"""
from pathlib import Path

import esoi.check
import esoi.config

ROOT = Path(__file__).resolve().parent.parent


def test_repo_structure_has_no_violations():
    problems = esoi.check.check(esoi.config.load(ROOT))
    assert problems == [], "\n".join(problems)
