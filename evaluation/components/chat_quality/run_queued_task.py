"""Run ONE queued task the way the lane's production runner would, with every write confined: the lane's own configuration builds the runner, the task
executor runs the task (the brain, then the deep harness the lane selected), and a dry run (``QAR_DRY_RUN_WRITES``) refuses and records every write,
with the real deep worker kept (``QAR_DRY_RUN_DEEP=real``): its own tool processes inherit the dry run, so a Quest write it asks for is refused and
recorded there too, and it works in a scratch folder.

Used by the overlay replay (``impls/prod_chat_overlay.py``) after a chat turn queued background work: the turn's recorded task is run here, and what it
made (the run's report, a milestone deliverable's measurement and kept body, the refused writes) is judged with the turn.

    python run_queued_task.py --config LANE_TOML [--env-file LANE_ENV] --task TASK_JSON_FILE

Prints one line ``@@TASK@@ {json}`` with ``{status, result, measured, writes, model_calls, error}``.
"""
import argparse
import json
import os
import sys
import tempfile
import traceback


#: Hosts the replay may reach: the model providers, the embedding service, this machine, and Quest (whose writes the dry run refuses). Every other
#: host goes through a proxy that does not exist, so no script the worker runs can send mail, post to a chat or a social account, push to a git host or
#: write a spreadsheet. The worker's own web search runs at the model provider; fetching an arbitrary page is refused here (a replay limit).
REACHABLE = ("localhost", "127.0.0.1", "::1", ".anthropic.com", "anthropic.com", ".claude.ai", "claude.ai", "generativelanguage.googleapis.com",
             "api.voyageai.com")
DEAD_PROXY = "http://127.0.0.1:9"
#: Credentials an outward act would use, taken out of the replay's environment (the worker inherits it).
OUTWARD_UNSET = ("SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "SLACK_BOT_TOKEN", "DISCORD_TOKEN", "NOTION_API_KEY")


def no_outward_acts(scratch: str) -> dict:
    """The environment that keeps the replayed task from acting outside Quest: every host but the reachable ones behind a dead proxy, git unable to push
    (no SSH, no credential helper, no gh login), the mail key replaced by one that is refused, and the worker limited to the MCP servers the runner
    passes (none of the person's own connectors)."""
    quest_host = (os.environ.get("QUEST_BASE_URL") or "").split("://", 1)[-1].split("/", 1)[0]
    reachable = ",".join(REACHABLE + ((quest_host,) if quest_host else ()))
    return {"HTTPS_PROXY": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY, "https_proxy": DEAD_PROXY, "http_proxy": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
            "NO_PROXY": reachable, "no_proxy": reachable, "GIT_SSH_COMMAND": "false", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "", "GH_CONFIG_DIR": os.path.join(scratch, "gh"),
            "RESEND_API_KEY": "blocked-in-replay", "QAR_DEEP_STRICT_MCP": "1"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, help="the lane's configuration file (the production runner's)")
    parser.add_argument("--task", required=True, help="a JSON file holding the queued task document")
    parser.add_argument("--env-file", default="", help="the lane's KEY=VALUE file, loaded the way the lane loads it")
    args = parser.parse_args(argv)
    scratch = tempfile.mkdtemp(prefix="qar-task-replay-")
    out = {"status": None, "result": "", "measured": None, "writes": [], "model_calls": 0, "error": None}
    try:
        with open(args.task, encoding="utf-8") as handle:
            task = json.load(handle)
        if args.env_file:
            from quest_ai_runner.the_consciousness.request_intake.lane import load_env_file
            load_env_file(args.env_file)
        # After the lane's own file, so the confinement always wins over anything it sets.
        os.environ.update({"QAR_DRY_RUN_WRITES": "1", "QAR_DRY_RUN_DEEP": "real", "QAR_DRY_RUN_SCRATCH": scratch, "QAR_DEEP_WORKING_DIR": scratch,
                           "QAR_STATE_PATH": os.path.join(scratch, "state.json")})
        os.environ.update(no_outward_acts(scratch))
        for name in OUTWARD_UNSET:
            os.environ.pop(name, None)
        from quest_ai_runner import load_config
        from quest_ai_runner.the_consciousness.request_intake.poller import Poller
        from quest_ai_runner.the_world.tools import dry_run_writes
        dry_run_writes.install()
        poller = Poller(load_config(args.config), state_path=os.environ["QAR_STATE_PATH"])
        executor = poller.task_executor()
        # The runner's own client and tools hold the Quest key already (built above from a copy of the environment); the worker is started later with
        # this process's environment, so without the key it cannot reach Quest except through the runner's refused writes.
        os.environ.pop("QUEST_API_KEY", None)
        outcome = executor.execute(task)
        snap = dry_run_writes.RECORDER.snapshot()
        out.update(status=outcome.status, result=outcome.result, measured=getattr(executor, "milestone_measured", None),
                   writes=[{k: w.get(k) for k in ("kind", "what", "tool")} for w in snap["writes"] if not w.get("internal")], model_calls=snap["model_calls"])
    except Exception as error:  # noqa: BLE001  the replay reports what failed; the judge sees it
        out["error"] = f"{type(error).__name__}: {error}"
        traceback.print_exc(file=sys.stderr)
    print("@@TASK@@ " + json.dumps(out, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
