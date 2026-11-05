"""The one stable command line of Quest AI Runner: ``python -m quest_ai_runner <command> [arguments]``.

Deep workers, skills, lane scripts and cron call these names, so a module can move without breaking them:

    cli           the interactive terminal and the ``quest`` subcommands (the ``quest-ai-runner`` console script)
    tools         the Quest tool catalog from a shell: list, show, call
    lane          the lane supervisor that keeps a lane running
    requirements  the hub requirement and lock check
"""
import importlib
import sys

COMMANDS = {
    "cli": "quest_ai_runner.the_consciousness.terminal.cli",
    "tools": "quest_ai_runner.the_consciousness.terminal.__main__",
    "lane": "quest_ai_runner.the_consciousness.request_intake.lane_supervisor",
    "requirements": "quest_ai_runner.the_self.assembler.hub_requirements",
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return importlib.import_module(COMMANDS[argv[0]]).main(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
