"""BASELINE: believe whatever the model returned. No validation of the signal, no hint trimming, a digest
cut from the tail, every reach verdict stamped into the prompt, the quest judge's pick used even when it
is not a listed quest, and every abridged item reported pending whether or not it was fetched. The floor
the production guards must beat. Zero model calls."""
import json


def build(meter):  # noqa: ARG001
    def judge(kind, args):
        raw = args.get("raw")
        if kind == "overseer_parse":
            if raw == "__raise__":
                raise RuntimeError("provider down")      # no degrade-to-proceed: the turn would break
            return dict(signal=str(raw.get("signal")), hint=str(raw.get("hint") or ""),
                        reason=str(raw.get("reason") or ""), degraded=False, text=str(raw.get("hint") or ""))
        if kind == "overseer_digest":
            parts = [f"CURRENT USER REQUEST: {args['user_message']}"]
            parts += list(args.get("recent_conversation") or []) + list(args.get("operations") or [])
            parts.append(f"DRAFT ANSWER: {args.get('draft_answer') or ''}")
            return {"text": "\n".join(parts)[:args.get("char_budget", 1600)]}   # tail (the draft) is what is lost
        if kind == "reach_parse":
            try:
                obj = raw if isinstance(raw, dict) else json.loads(raw)
                return {"verdict": {"reach": obj["reach"], "covered_by": obj.get("covered_by")}}
            except Exception:  # noqa: BLE001
                return {"verdict": None}
        if kind == "reach_block":
            return {"text": f"reach: {args['verdict']}"}
        if kind == "quest_rank":
            if raw == "__raise__":
                raise RuntimeError("judge down")
            rows = {int(r["item"]) - 1: r for r in raw.get("results", [])}
            return {"results": [[rows[i]["quest_id"] or None, rows[i]["ranking"]] if i in rows else [None, []]
                                for i in range(len(args["items"]))]}
        if kind == "sufficiency":
            labels = [b.get("why") or c.get("id") for c in args["cards"] for b in c.get("items", [])
                      if (b.get("locator") or {}).get("full_ref")]
            return {"pending": labels, "notice": bool(labels)}
        raise ValueError(kind)
    return judge
