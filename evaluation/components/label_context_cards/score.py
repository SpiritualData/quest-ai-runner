"""Score one card-labelling case against the labels its source states, and weigh value against cost and time.

A ``retrieve`` case is ok when every labelled query resolved its collection and the unrelated query resolved nothing; ``hits`` and
``leaks`` count the queries so the run reports the card retrieval hit rate. A ``place`` case is ok when every file went to its
labelled card; ``right`` and ``files`` count the files so the run reports the share placed in the labelled card.

``aggregate`` reads the whole run: the retrieval hit rate and the file placement share (value), the leaks (a label that pulls a card into
an unrelated question), the model calls per case (cost) and that both prompts run on card creation or refresh and in the background after
a deep run, so no reply waits on them (time)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))


def score_case(fn, case):
    got = fn(case)
    if case["kind"] == "retrieve":
        hits, leaks = got["hits"], got["leaks"]
        ok = all(hits) and not any(leaks)
        return dict(ok=ok, hits=sum(hits), hit_total=len(hits), leaks=sum(leaks), leak_total=len(leaks), fallback=bool(got.get("fallback")),
                    got=f"{sum(hits)}/{len(hits)} hit, {sum(leaks)} leak", want="all hit, no leak", kind="retrieve",
                    note="ok" if ok else f"title {got['metadata'].get('title')!r}, keywords {list(got['metadata'].get('keywords') or [])[:6]}"
                    + ("; generator fell back" if got.get("fallback") else ""))
    wrong = {f: (got["placed"].get(f), want) for f, want in case["label"].items() if got["placed"].get(f) != want}
    return dict(ok=not wrong, right=len(case["label"]) - len(wrong), files=len(case["label"]), got=json.dumps(got["placed"])[:120],
                want="labelled cards", kind="place",
                note="ok" if not wrong else "; ".join(f"{f} to {h}, labelled {w}" for f, (h, w) in wrong.items()))


def aggregate(rows):
    n = len(rows) or 1
    ok = sum(1 for r in rows if r.get("ok"))
    calls = sum(r.get("calls", 0) for r in rows)
    ret = [r for r in rows if r.get("kind") == "retrieve"]
    plc = [r for r in rows if r.get("kind") == "place"]
    hits, hit_total = sum(r["hits"] for r in ret), sum(r["hit_total"] for r in ret)
    leaks = sum(r["leaks"] for r in ret)
    right, files = sum(r["right"] for r in plc), sum(r["files"] for r in plc)
    fallbacks = sum(1 for r in ret if r.get("fallback"))
    return (f"value {ok}/{n} cases as labelled; card retrieval hits {hits}/{hit_total} queries with {leaks} leak(s) into the unrelated query "
            f"({fallbacks} card(s) got the generator's no-model fallback); files placed in the labelled card {right}/{files}; cost "
            f"{calls / n:.2f} model calls per case; both prompts run on card creation or in the background after a run, so no reply waits "
            f"on them; labels are source fixtures from a learned-card world and a unit test")
