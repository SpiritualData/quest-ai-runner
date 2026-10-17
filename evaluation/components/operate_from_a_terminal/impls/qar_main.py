"""CURRENT: the live entry points. ``quest_ai_runner.cli.main`` (argparse tree, store commands, quest, send,
create-goal, context) and ``quest_ai_runner.tools.__main__.main`` (list, show, call), called in-process with a
scrubbed environment and a scratch folder; stdout and stderr are captured. Nothing is faked. Zero model calls."""
import contextlib
import io
import tempfile
from pathlib import Path

from common import hermetic_env
from quest_ai_runner import cli
from quest_ai_runner.tools import __main__ as tools_main

ENTRIES = {"qar": cli.main, "tools": tools_main.main}


def run_entry(entry, argv):
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = ENTRIES[entry](argv)
            code = 0 if result is None else int(result)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, out.getvalue()


def build(meter):  # noqa: ARG001
    def command(kind, args):
        if kind != "run_cli":
            raise ValueError(kind)
        with tempfile.TemporaryDirectory(prefix="qar_cli_") as root:
            for rel, text in args.get("files", {}).items():
                path = Path(root) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text.replace("{dir}", root))
            env = {k: v.replace("{dir}", root) for k, v in args.get("env", {}).items()}
            argv = [a.replace("{dir}", root) for a in args["argv"]]
            with hermetic_env(env):
                code, text = run_entry(args["entry"], argv)
        return {"exit": code, "text": text.replace(root, "{dir}")}
    return command
