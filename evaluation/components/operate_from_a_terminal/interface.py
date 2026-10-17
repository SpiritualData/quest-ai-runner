"""The terminal-command function. Every solution is ONE function with this signature:

    command(kind: str, args: dict) -> dict

A person (or a deep run's shell) operates the runner from a terminal: ``quest-ai-runner <subcommand>`` and
``python -m quest_ai_runner.tools <list|show|call>``. This component scores that surface offline: the real
``main()`` is called with an argv, the process environment is scrubbed, and any file a case needs is written
into a scratch folder. Only non-interactive, no-network subcommands are cases (the chat UI and the poll loop
need a terminal and a server); what is measured is that arguments are parsed, a bad invocation is refused
with the usage exit code instead of acting, and the printed output says what the person needs to act.

Kinds and the dict a solution returns:
  run_cli   args {entry, argv, env, files}
            -> {"exit": exit code, "text": stdout and stderr together}
            ``entry`` is "qar" (the ``quest-ai-runner`` main) or "tools" (``quest_ai_runner.tools``); ``env`` is set
            on top of a scrubbed environment; ``files`` maps a path under the scratch folder to its text; ``{dir}``
            inside ``argv``, ``env`` or ``files`` is that scratch folder.

Exit codes under test: 0 success, 1 the command ran and failed, 2 a usage error (nothing was done).
An implementation module in ``impls/`` exposes ``build(meter) -> command``. No solution here calls a model.
"""
