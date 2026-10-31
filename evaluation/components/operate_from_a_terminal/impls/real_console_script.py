"""REAL CONSOLE (the single-case arm of ``operate-from-a-terminal``): the commands as an operator types them. Each case starts the
installed ``quest-ai-runner`` console script (the entry point the package declares; ``python -m quest_ai_runner.tools`` for the
tools entry) as a real child process in a scratch folder with the hermetic environment of the component (the variables esoi.toml scrubs are gone, the
case's are set, ``HOME`` is the scratch folder), so argument parsing, the process exit code and the real stdout and stderr streams are the ones a person gets. Nothing
is called in-process and nothing is faked. Zero model calls (a command that would call a model is not among the cases)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from common import hermetic_env

CONSOLE = Path(sys.executable).parent / "quest-ai-runner"
TIMEOUT_SECONDS = 120


def command_line(entry, argv):
    if entry == "qar":
        if CONSOLE.exists():
            return [str(CONSOLE), *argv]
        return [sys.executable, "-c", "import sys; from quest_ai_runner.cli import main; sys.exit(main(sys.argv[1:]))", *argv]
    if entry == "tools":
        return [sys.executable, "-m", "quest_ai_runner.tools", *argv]
    raise ValueError(entry)


def build(meter):  # noqa: ARG001
    def command(kind, args):
        if kind != "run_cli":
            raise ValueError(kind)
        with tempfile.TemporaryDirectory(prefix="qar_real_cli_") as root:
            for rel, text in args.get("files", {}).items():
                path = Path(root) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text.replace("{dir}", root))
            env_case = {k: v.replace("{dir}", root) for k, v in args.get("env", {}).items()}
            argv = [a.replace("{dir}", root) for a in args["argv"]]
            with hermetic_env(env_case):
                env = dict(os.environ, HOME=root, PYTHONDONTWRITEBYTECODE="1")
            done = subprocess.run(command_line(args["entry"], argv), env=env, cwd=root, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=TIMEOUT_SECONDS, check=False)
        return {"exit": done.returncode, "text": done.stdout.replace(root, "{dir}")}
    return command
