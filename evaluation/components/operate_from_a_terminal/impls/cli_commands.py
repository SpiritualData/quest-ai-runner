"""PACKAGE ARM: the package's own terminal member, ``quest_ai_runner.the_intelligence.mind.planner.area_take_in_the_world.CliCommands`` (the ``cli_commands`` arm of
``operate-from-a-terminal``), driven on the same cases as ``qar_main``: ``quest-ai-runner`` and ``python -m quest_ai_runner tools`` called in
process with a scrubbed environment and a scratch folder, output and exit code captured. Nothing is faked. Zero model calls."""
import tempfile
from pathlib import Path

from common import hermetic_env
from quest_ai_runner.the_intelligence.mind.planner.area_take_in_the_world import CliCommands


def build(meter):  # noqa: ARG001
    terminal = CliCommands()

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
                ran = terminal.operate(argv, args["entry"])
        return {"exit": ran.code, "text": ran.output.replace(root, "{dir}")}
    return command
