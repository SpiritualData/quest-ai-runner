"""Private helper for the live arms: the legacy script's CLI runner and prompts, loaded by path."""
import importlib.util
import shutil
import sys
import tempfile

import common

_MOD = None


def legacy():
    global _MOD
    if _MOD is None:
        spec = importlib.util.spec_from_file_location("eval_vs_claude_code_legacy", common.REPO / "evaluation" / "eval_vs_claude_code.py")
        _MOD = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = _MOD
        spec.loader.exec_module(_MOD)
    return _MOD


def runner(meter, warm):
    m = legacy()
    claude = m._find_claude()
    if not claude:
        raise SystemExit("the live arms need the claude CLI; use --impl idf_hint offline")
    copy = m._copy_repo(common.REPO)

    def locate(case):
        meter.record()
        if warm:
            from context_selection.corpus import corpus_for
            from context_selection.impls import idf_docstrings
            hint = case["wrong_hint"] or ", ".join(idf_docstrings.build(meter)(case["task"], corpus_for(common.REPO))[:3])
            prompt = m._warm_prompt(case["task"], hint)
        else:
            prompt = m._cold_prompt(case["task"])
        res = m._run_claude(claude, prompt, copy)
        if "error" in res:
            raise RuntimeError(res["error"])
        return {"answer": res["result"], "turns": res["num_turns"]}
    locate.cleanup = lambda: shutil.rmtree(copy.parent, ignore_errors=True)
    return locate
