"""The overlay arm's seed: a replay reads the specification edits its last saved replay made, so reuse of something kept can be proved with every write
blocked. SYNTHETIC records only.
Run:  python -m pytest evaluation/components/chat_quality/test_overlay_seed.py -q"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "impls"))

import prod_chat_overlay  # noqa: E402


def test_the_last_replays_applied_spec_edits_become_the_seed(tmp_path):
    folder = tmp_path / "poor_answers"
    folder.mkdir()
    edit = {"applied": True, "target": "sol-1", "field": "knowledge", "after": "[best guess] Deliverable rubric for x: a; b (source: sol-1, Quest AI, 2030-01-01)"}
    old = {"arm": "prod_chat_overlay", "reads_seen": [{"source": "tool apply_spec_edit {}", "text": "Done\n" + json.dumps(dict(edit, after="older"))}]}
    new = {"arm": "prod_chat_overlay", "reads_seen": [{"source": "tool read_spec_records {}", "text": "{}"},
                                                      {"source": "tool apply_spec_edit {}", "text": "Done: solutions sol-1\n" + json.dumps(edit)},
                                                      {"source": "tool apply_spec_edit {}", "text": "Not changed\n" + json.dumps({"applied": False, "reason": "locked"})}]}
    (folder / "X-1.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    env = {"QAR_EVAL_OUTPUT_DIR": str(tmp_path)}
    assert prod_chat_overlay.seed_from_last("X-1", env) == {"sol-1": {"knowledge": edit["after"]}}
    assert prod_chat_overlay.seed_from_last("X-2", env) == {}
