"""Shared driver for the mid-run input component: runs the LIVE Orchestrator with a given inbox (or none)
and reports what the planner saw. Implementations differ only in the inbox object they supply."""
import scripted_brain


def run_case(args, inbox):
    conv = args["conv"]
    pushes = args["pushes"]
    pushed_texts = [p["text"] for p in pushes if p["conv"] == conv and p["text"].strip()]

    def push_at(call_index):
        for p in pushes:
            if p["at"] == call_index and inbox is not None:
                inbox.push(p["conv"], p["text"])

    reads = [{"action": "read", "reads": [{"rel_path": f"file_{i}.md"}], "rationale": "look", "model_tier": "sonnet"}
             for i in range(args["reads"])]
    provider = scripted_brain.ScriptedProvider(reads + [{"action": "answer", "rationale": "ok", "model_tier": "sonnet"}],
                                               on_plan=push_at)
    push_at(-1)
    scripted_brain.build_brain(provider, input_inbox=inbox).run("do the work", quest_id=conv)
    prompts = provider.plan_prompts
    first_seen = {t: next((i for i, p in enumerate(prompts) if t in p), None) for t in pushed_texts}
    copies = {t: prompts[-1].count(t) if prompts else 0 for t in pushed_texts}
    pending = list(inbox.peek(conv)) if inbox is not None else []
    lost = [t for t in pushed_texts if first_seen[t] is None and t not in pending]
    others = [p["text"] for p in pushes if p["conv"] != conv and p["text"].strip()]
    leaked = [t for t in others if any(t in prompt for prompt in prompts)]
    return {"first_seen": first_seen, "copies": copies, "lost": lost, "leaked": leaked}


def inbox_ops(args, inbox):
    results = []
    for op in args["ops"]:
        name, conv, *rest = op
        if inbox is None:
            results.append(None if name == "push" else [])
        elif name == "push":
            inbox.push(conv, rest[0])
            results.append(None)
        else:
            results.append(list(getattr(inbox, name)(conv)))
    return {"results": results}
