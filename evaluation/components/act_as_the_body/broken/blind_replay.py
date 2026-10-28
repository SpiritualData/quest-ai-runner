"""BASELINE: the shortcut a first draft would write. Replays the script literally against the account: every read runs
(a repeat included), every write runs (a repeat included), a write whose operation fails is applied anyway, and
a near-miss lookup is not looked at. There is no planner and so no record of what already ran. Zero model calls."""


def build(meter):  # noqa: ARG001
    def run_script(case):
        status, reads, writes = "draft", 0, []
        for step in case["script"]:
            if step.get("read") and step.get("path", "account.md") == "account.md":
                reads += 1
            elif "write" in step:
                writes.append({"value": step["write"]})
                status = step["write"]
        return {"handler_calls": writes, "reads": reads, "final_prompt": f"ACCOUNT status is {status}"}
    return run_script
