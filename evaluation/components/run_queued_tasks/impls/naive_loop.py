"""BASELINE: the loop a first draft would write. Every due task is run on every scan (no handled-set), a
failed claim is ignored, every outcome is reported done with the model's text, a persona is any registry
entry whose name appears anywhere in the task, the sync file is rewritten from scratch each pull and every
bullet is pushed every time, a scheduled time ignores its zone, and an env file overrides the process
environment. No Quest escalation, no failure status. Zero model calls."""
from datetime import datetime

from run_queued_tasks import fakes

QUEST_ID = "quest_000000000001"


def lifecycle(args):
    client = fakes.FakeQuest(fakes.materialise(args["tasks"]), claim=args.get("claim", "ok"))
    handled = []
    if not args.get("configured", True):
        return {"handled": [], "claimed": [], "reports": [], "decision_ids": []}
    for _ in range(args.get("scans", 1)):
        for task in client.discover_due(team_id=args.get("team_id", "team1")):
            client.claim(task["id"])
            client.report_done(task["id"], "scripted answer")
            handled.append(task["id"])
    return {"handled": sorted(handled), "claimed": sorted(client.claimed),
            "reports": sorted([r[0], r[1]] for r in client.reports), "decision_ids": []}


def persona(args):
    blob = " ".join(str(v) for v in args["task"].values()).lower()
    for key, spec in args["registry"].items():
        names = {key.lower()} | ({str(v).lower() for v in spec.values() if isinstance(v, str)} if isinstance(spec, dict)
                                 else {str(spec).lower()})
        if any(n in blob for n in names):
            slug = spec.get("skill", key) if isinstance(spec, dict) else spec
            ident = spec.get("rep_id", key) if isinstance(spec, dict) else key
            return {"persona": ident, "skill": slug}
    return {"persona": None, "skill": None}


def folder(args):
    notes = list(args["notes"])
    out = {"results": [], "error": None}
    text = ""
    for step in args["script"]:
        if step == "pull":
            if not args["state"]:
                out["error"] = "QuestFolderSyncError"
                break
            text = f"Goal: {args['state']['outcome']}\n" + "\n".join(f"- {n['text']}" for n in notes) + "\n## Notes to push to Quest\n"
            out["results"].append(len(notes))
        elif step == "push":
            if not text:
                out["error"] = "QuestFolderSyncError"
                break
            tail = text.split("## Notes to push to Quest\n", 1)[1]
            out["results"].append(len([ln for ln in tail.splitlines() if ln.startswith("- ")]))
        elif "append" in step:
            text += step["append"] + "\n"
        elif "bullet" in step:
            text += f"- {step['bullet']}\n"
    out["text"] = text
    return out


def owner(args):
    path = args["path"]
    for qid, rel in args["map"].items():
        if path.startswith(rel):
            return {"quest": qid}
    return {"quest": None}


def clock(args):
    try:
        moment = datetime.strptime(f"{args['date']} {(args.get('time') or '00:00')[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {"iso": None, "aware": False}
    return {"iso": moment.isoformat(), "aware": False}


def env_file(args):
    env = dict(args["preset"])
    for line in args["lines"]:
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip()
    return {"env": env}


def lane_args(args):
    argv = args["argv"]
    return {"once": "--once" in argv, "check": "--check" in argv, "verbose": "-v" in argv or "--verbose" in argv}


def build(meter):  # noqa: ARG001
    kinds = {"task_lifecycle": lifecycle, "persona_select": persona, "folder_sync": folder, "folder_owner": owner,
             "lane_clock": clock, "lane_env_file": env_file, "lane_args": lane_args}

    def run(kind, args):
        return kinds[kind](args)
    return run
