"""BASELINE: a command line that only knows the subcommand names. Anything is accepted, nothing is validated,
every invocation prints the usage line and exits 0, and no subcommand does its work. The floor a real parser,
exit codes and printed output must beat. Zero model calls."""

USAGE = "usage: quest-ai-runner {chat,quest,send,create-goal,context,poll,tools} ..."


def build(meter):  # noqa: ARG001
    def command(kind, args):
        if kind != "run_cli":
            raise ValueError(kind)
        return {"exit": 0, "text": USAGE + "\n"}
    return command
