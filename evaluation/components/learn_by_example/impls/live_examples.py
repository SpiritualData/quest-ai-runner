"""CURRENT: the shipped examples, driven for real. Each file is compiled and imported, run as a subprocess with a
scrubbed environment (``--help``, a not-configured run, the offline composite-retrieval demo), the e2e demo's
stub provider is called, ``qar.toml`` is loaded through ``RunnerConfig.from_file``, and the README and a secret
scan are applied to the file. Zero model calls, zero network."""
import importlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from quest_ai_runner.config import RunnerConfig

REPO = Path(__file__).resolve().parents[4]
EXAMPLES = REPO / "examples"
SECRET_PATTERNS = [r"sk-ant-[A-Za-z0-9]", r"qsk_[A-Za-z0-9]{8,}", "/" + "home" + r"/\w", r"\b[0-9a-f]{24}\b",
                   r"[\w.+-]+@(?!example\.)[\w-]+\.(?:com|org|net)\b"]


def run(args):
    with tempfile.TemporaryDirectory() as home:
        env = dict(args.get("env", {}), PATH=os.environ.get("PATH", ""), HOME=home, PYTHONPATH=str(REPO),
                   PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run([sys.executable, str(EXAMPLES / args["file"]), *args.get("argv", [])], cwd=home, env=env,
                              capture_output=True, text=True, timeout=120)
    return {"exit": done.returncode, "text": done.stdout + done.stderr}


def build(meter):  # noqa: ARG001
    def run_example(kind, args):
        if kind == "compile":
            compile((EXAMPLES / args["file"]).read_text(), args["file"], "exec")
            return {"compiles": True}
        if kind == "import":
            return {"imported": importlib.import_module(args["module"]) is not None}
        if kind == "run":
            return run(args)
        if kind == "stub":
            from examples.e2e_demo import HUMAN_ONLY_MARKER, StubProvider
            prompt = f"{HUMAN_ONLY_MARKER} approve the spend" if args["marker"] else "say hello"
            return {"action": StubProvider().plan(prompt, model="stub", tool_schema={})["action"]}
        if kind == "config_file":
            return {"runner_label": RunnerConfig.from_file(str(EXAMPLES / "qar.toml")).runner_label}
        if kind == "documented":
            return {"documented": args["file"] in (EXAMPLES / "README.md").read_text()}
        if kind == "clean":
            text = (EXAMPLES / args["file"]).read_text()
            return {"hits": [m.group(0) for pat in SECRET_PATTERNS for m in re.finditer(pat, text)]}
        raise ValueError(kind)
    return run_example
