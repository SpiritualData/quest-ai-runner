"""Command-line tools a deep run can invoke from its shell.

A deep run is a subprocess, not a Python caller, so anything it needs to DO through Quest has to
exist as a command. Each module here is one such command and goes through QuestClient, so a run
never reaches a side effect except by a path the runner owns and can bound.
"""
