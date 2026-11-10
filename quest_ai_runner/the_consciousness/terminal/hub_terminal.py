"""The binding of the hub's Terminal Experience to this package: the hub's own screen (the arm its metric selected, a full-screen Textual screen where Textual is installed,
the standard-library one where it is not) with what is specific here supplied through the hub's binding points.

The hub's screen runs a goal on a real harness while the person watches, stops, resumes and steers it, and takes each line typed after a run as a follow-up. What stays here is
the consumer's: the words (``terminal_words``), the person the AI acts for (the name form ``quest-ai-runner chat NAME``, resolved by the hub's Representatives), the folder the
work happens in and the ceilings from ``RunnerConfig``. The older chat screen (``textual_ui``) stays beside it as the other implementation of this solution: it holds the
conversation features the hub's screen has no counterpart for yet (saved conversations, the conversation picker, personas from the corpus, the model-tier menu).

Run it: ``quest-ai-runner chat --screen hub [NAME]``.
"""
from __future__ import annotations

import sys
from typing import Any, Mapping, Optional

from esoi import implementation

from quest_ai_runner.the_consciousness.terminal import terminal_words  # noqa: F401  (registers the words this package uses in the hub's blocks)


@implementation("terminal-session", "hub_terminal")
class HubTerminal:
    """The hub's terminal arm with this package's binding."""

    def build(self, *, folder: str = ".", person: str = "", terminal: str = "", environ: Optional[Mapping[str, str]] = None, plain: bool = False) -> Any:
        """The hub's ``WorkTerminal`` for ``folder``, drawn by the hub's selected terminal arm (or ``terminal`` when one is named)."""
        import hubofhubs
        from hubofhubs.api import WorkTerminal, default_harness, harness_builder, terminal_arm
        env = dict(environ) if environ is not None else dict(__import__("os").environ)
        arm, _name, why = terminal_arm(terminal, env)
        harness, harness_why = default_harness()
        holder: dict = {}
        build = harness_builder(folder=folder, environ=env, stop_inputs=lambda: holder["terminal"].stop_inputs())
        screen = WorkTerminal(build, sys.stdout, harness=harness, why=harness_why, arm=arm, person=person, command="quest-ai-runner chat --screen hub",
                              represent=lambda name: hubofhubs.run("the-representatives", name), live=None if not plain else False)
        holder["terminal"] = screen
        screen.arm_why = why
        return screen

    def start(self, *, folder: str = ".", person: str = "", goal: str = "", terminal: str = "", plain: bool = False) -> int:
        """Open the screen and return its exit code (0 for a clean end)."""
        screen = self.build(folder=folder, person=person, terminal=terminal, plain=plain)
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not plain
        if interactive and hasattr(screen.arm, "host_terminal"):
            return int(screen.arm.host_terminal(screen, goal))
        return int(screen.run(goal))


__all__ = ["HubTerminal"]
