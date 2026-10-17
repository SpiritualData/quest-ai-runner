"""CURRENT: the live runner. A real ``Poller`` and ``TaskExecutor`` over a fake Quest client and a scripted
model, ``build_persona_resolver``, ``pull_quest_to_folder`` / ``push_folder_to_quest`` / ``quest_for_path``,
``local_time.scheduled_moment``, ``lane.load_env_file`` and ``lane.build_arg_parser``. Only the Quest client
and the model are faked. Zero model calls."""
import os
import tempfile
from pathlib import Path

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.lane import build_arg_parser, load_env_file
from quest_ai_runner.runner.local_time import scheduled_moment
from quest_ai_runner.runner.personas import PersonaResolverConfig, build_persona_resolver
from quest_ai_runner.runner.poller import Poller
from quest_ai_runner.runner.quest_folder_sync import (
    QuestFolderSyncError, pull_quest_to_folder, push_folder_to_quest, quest_for_path,
)

from common import hermetic_env
from run_queued_tasks import fakes

QUEST_ID = "quest_000000000001"


def lifecycle(args):
    tasks = fakes.materialise(args["tasks"])
    client = fakes.FakeQuest(tasks, claim=args.get("claim", "ok"), configured=args.get("configured", True))
    deep = args.get("deep")
    cfg = RunnerConfig(
        quest_base_url="http://quest.invalid", quest_api_key="qsk_offline", team_id=args.get("team_id", "team1"),
        retrieval=fakes.FileMap({"README.md": "fact"}), model_provider=fakes.ScriptedModel(args["decisions"]),
        deep_runner=fakes.FixedDeep(**deep) if deep else None,
        escalation=fakes.FixedEscalation(args["escalation_id"]) if args.get("escalation_id") else None)
    handled = []
    with hermetic_env():
        poller = Poller(cfg, state_path=None, client=client)
        for _ in range(args.get("scans", 1)):
            handled += poller.run_once()
    # Tasks run concurrently, so completion order is not part of the contract: compare as sorted lists.
    return {"handled": sorted(handled), "claimed": sorted(client.claimed),
            "reports": sorted([r[0], r[1]] for r in client.reports),
            "decision_ids": sorted(r[3] for r in client.reports if r[3])}


def persona(args):
    from quest_ai_runner.runner.personas import PersonaRegistry
    cfg = PersonaResolverConfig(skills_root="/skills", registry=PersonaRegistry.from_mapping(args["registry"]),
                                cache_dir=args.get("cache_dir"))
    found = build_persona_resolver(cfg)(args["task"])
    return {"persona": found[0] if found else None, "skill": Path(found[1]).name if found else None}


def folder(args):
    client = fakes.FakeFolderClient(args["state"], args["notes"])
    out = {"results": [], "error": None}
    with tempfile.TemporaryDirectory(prefix="qar_folder_") as root:
        sync = Path(root) / "QUEST_SYNC.md"
        for step in args["script"]:
            try:
                if step == "pull":
                    r = pull_quest_to_folder(client, QUEST_ID, root, zones=False)
                    out["results"].append(r.notes_pulled)
                elif step == "push":
                    out["results"].append(push_folder_to_quest(client, QUEST_ID, root).notes_pushed)
                elif "append" in step:
                    sync.write_text(sync.read_text() + "\n" + step["append"] + "\n")
                elif "bullet" in step:
                    text = sync.read_text()
                    sync.write_text(text.rstrip("\n") + f"\n- {step['bullet']}\n")
            except QuestFolderSyncError as e:
                out["error"] = type(e).__name__
                break
        out["text"] = sync.read_text() if sync.exists() else ""
    return out


def owner(args):
    with tempfile.TemporaryDirectory(prefix="qar_owner_") as root:
        mapping = {qid: str(Path(root) / rel) for qid, rel in args["map"].items()}
        found = quest_for_path(mapping, Path(root) / args["path"])
    return {"quest": found[0] if found else None}


def clock(args):
    moment = scheduled_moment(args["date"], args.get("time"), args.get("zone"))
    return {"iso": moment.isoformat() if moment else None, "aware": bool(moment and moment.tzinfo)}


def env_file(args):
    keys = set(args["preset"]) | {line.partition("=")[0].strip() for line in args["lines"]}
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory(prefix="qar_env_") as root:
        path = Path(root) / "lane.env"
        path.write_text("\n".join(args["lines"]) + "\n")
        try:
            for k in keys:
                os.environ.pop(k, None)
            os.environ.update(args["preset"])
            load_env_file(path)
            loaded = {k: os.environ.get(k) for k in keys}
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
    return {"env": {k: v for k, v in loaded.items() if v is not None}}


def lane_args(args):
    ns = build_arg_parser("lane", "d").parse_args(args["argv"])
    return {"once": ns.once, "check": ns.check, "verbose": ns.verbose}


KINDS = {"task_lifecycle": lifecycle, "persona_select": persona, "folder_sync": folder, "folder_owner": owner,
         "lane_clock": clock, "lane_env_file": env_file, "lane_args": lane_args}


def build(meter):  # noqa: ARG001
    def run(kind, args):
        return KINDS[kind](args)
    return run
