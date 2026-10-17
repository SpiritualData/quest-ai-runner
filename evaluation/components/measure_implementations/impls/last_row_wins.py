"""BASELINE: the naive harness. The last row recorded is the winner, no case file hash is compared, nothing is
flagged as unverified, a record carries no commit or hash, and run flags such as ``--only`` and
``--max-calls`` are not honoured. Zero model calls."""


def build(meter):  # noqa: ARG001
    def measure(kind, args):
        if kind == "rank":
            order = [r["impl"] for r in reversed(args["rows"])]
            return {"ranked": order, "beats": [order[0]] if order[0] != args["selected"] else [], "unverified": []}
        if kind == "board":
            last = args["rows"][-1]["impl"]
            return {"exit": 0, "text": f"{last} wins", "beats": [last] if last != args["selected"] else [],
                    "incomparable": []}
        if kind == "record":
            return {"git_sha": False, "dirty": False, "cases_sha_matches": False, "cases_sha_changes": False, "rows": 1
                    + (1 if args.get("edit_cases") else 0)}
        if kind == "run":
            return {"cases": 3, "passed": 3 if args["impl"] != "bad" else 0, "error": None, "model_calls": 0}
        if kind == "validity":
            return {"cases": 3, "good_passed": 3, "bad_passed": 3}
        raise ValueError(kind)
    return measure
