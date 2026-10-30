"""How the deep worker (``claude -p``) is launched: one command builder, one frozen settings value.

The one-shot chat provider (``adapters/claude_cli_provider.py``) launches Claude Code lean, with the flags that take its fixed
prompt overhead away. The deep runner never did: it launched with the CLI's defaults (every built-in tool schema, every settings
source, every ambient MCP server, no spend ceiling). Whether a lean launch costs the deep runner nothing in reliability is a
question for a measurement, so the flags are PARAMETERS of the one launch (``DeepLaunchSettings``) and the DEFAULT IS TODAY'S
BEHAVIOUR: every field unset adds no flag, and ``build_deep_argv`` returns byte for byte the argv the runner built before.

``build_deep_argv`` is a pure function (settings and the call's facts in, argv out) so the command is testable without a process,
and both launch paths (the one-shot ``-p --output-format json`` run and the ``stream-json`` run) share it. Core never reads the
environment: ``config_core_settings`` parses the ``QAR_DEEP_*`` names into this value, and ``SubprocessConfig.launch`` pins one.

Each flag, as ``claude --help`` (2.1.314) words it:
  --tools <tools...>                        "Specify the list of available tools from the built-in set. Use "" to disable all tools,
                                            "default" to use the default set, or specify tool names (e.g. "Bash,Edit,Read")."
                                            Unlike ``--allowed-tools`` (permission rules) it removes the schemas of the tools not listed.
  --setting-sources <sources>               "Comma-separated list of setting sources to load (user, project, local)."
  --strict-mcp-config                       "Only use MCP servers from --mcp-config, ignoring all other MCP configurations."
  --exclude-dynamic-system-prompt-sections  "Move per-machine sections (cwd, env info, memory paths, git status) from the system prompt
                                            into the first user message ... Only applies with the default system prompt (ignored with
                                            --system-prompt)." The deep runner never passes --system-prompt, so it applies here (the
                                            chat provider's comment says the opposite; it passes --system-prompt, where the help says
                                            it is ignored).
  --max-budget-usd <amount>                 "Maximum dollar amount to spend on API calls (only works with --print)."
  --max-turns <n>                           not listed by ``--help`` in 2.1.314 but already passed by this runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class DeepLaunchSettings:
    """The launch parameters of one deep worker. Every field's default adds no flag, so the default value IS today's launch.

    tools                     None: no ``--tools`` flag (the CLI's default tool set). A tuple of tool names: ``--tools "A,B"``,
                              the only built-in tools (and schemas) the worker gets. An empty tuple: ``--tools ""``, no tools.
    setting_sources           None: no flag. A string: ``--setting-sources <value>``; "" loads none of user, project, local.
    strict_mcp                True adds ``--strict-mcp-config`` (only the servers of ``--mcp-config``, none of the ambient ones).
    exclude_dynamic_sections  True adds ``--exclude-dynamic-system-prompt-sections`` (per-machine text moves out of the prefix).
    max_budget_usd            None: no flag. A positive number: ``--max-budget-usd <n>``, the worker's spend ceiling.
    max_turns                 None: the call's own turn budget stands. A positive number replaces it for every launch.
    """

    tools: Optional[Tuple[str, ...]] = None
    setting_sources: Optional[str] = None
    strict_mcp: bool = False
    exclude_dynamic_sections: bool = False
    max_budget_usd: Optional[float] = None
    max_turns: Optional[int] = None

    def is_default(self) -> bool:
        return self == DeepLaunchSettings()

    def describe(self) -> str:
        """The non-default parameters, as ``name=value`` words (an arm's label in a record); "default" when there are none."""
        parts = []
        if self.tools is not None:
            parts.append("tools=" + ("none" if not self.tools else "+".join(self.tools)))
        if self.setting_sources is not None:
            parts.append("setting_sources=" + (self.setting_sources or "none"))
        if self.strict_mcp:
            parts.append("strict_mcp=1")
        if self.exclude_dynamic_sections:
            parts.append("exclude_dynamic_sections=1")
        if self.max_budget_usd is not None:
            parts.append(f"max_budget_usd={self.max_budget_usd:g}")
        if self.max_turns is not None:
            parts.append(f"max_turns={self.max_turns}")
        return ",".join(parts) or "default"


def lean_flags(settings: DeepLaunchSettings) -> List[str]:
    """The flags the settings add; empty for the default."""
    flags: List[str] = []
    if settings.tools is not None:
        flags += ["--tools", ",".join(settings.tools)]
    if settings.setting_sources is not None:
        flags += ["--setting-sources", settings.setting_sources]
    if settings.strict_mcp:
        flags.append("--strict-mcp-config")
    if settings.exclude_dynamic_sections:
        flags.append("--exclude-dynamic-system-prompt-sections")
    if settings.max_budget_usd is not None and settings.max_budget_usd > 0:
        flags += ["--max-budget-usd", f"{settings.max_budget_usd:g}"]
    return flags


def build_deep_argv(binary: str, *, streaming: bool, skip_permissions: bool,
                    allowed_tools: Optional[Sequence[str]], disallowed_tools: Optional[Sequence[str]],
                    mcp_config_path: Optional[str], model: str, turns: int, goal_given: bool,
                    session_flag: str, session_id: str,
                    launch: Optional[DeepLaunchSettings] = None) -> List[str]:
    """The argv of one deep worker. Pure: the same inputs give the same list, and nothing is read from the process.

    ``streaming`` picks the ``stream-json`` form (the interruptible run) over the one-shot ``json`` form. ``model`` is the CLI-safe
    model the caller resolved. ``turns`` is the call's turn budget (``launch.max_turns`` replaces it when set) and is passed only
    when the goal is non-empty. ``session_flag`` is ``--resume`` or ``--session-id``. With ``launch`` None or default the result
    is exactly what the runner built before this function existed.
    """
    launch = launch or DeepLaunchSettings()
    if streaming:
        cmd = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--replay-user-messages"]
    else:
        cmd = [binary, "-p", "--output-format", "json"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if allowed_tools:
        cmd += ["--allowed-tools", ",".join(allowed_tools)]
    if disallowed_tools:
        cmd += ["--disallowed-tools", ",".join(disallowed_tools)]
    if mcp_config_path:
        cmd += ["--mcp-config", mcp_config_path]
    cmd += lean_flags(launch)
    cmd += ["--model", model]
    if goal_given:
        effective = launch.max_turns if launch.max_turns is not None and launch.max_turns > 0 else turns
        cmd += ["--max-turns", str(int(effective))]
    cmd += [session_flag, session_id]
    return cmd
