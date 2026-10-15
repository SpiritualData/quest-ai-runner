"""BASELINE: one provider, no routing, no fallback, no retry, one flat price. Every model name goes to the
primary provider, a quota error is final, a transient error is not retried, the dry run counts input only
and every model costs the same $3 per million input tokens. The floor the multi-provider stack must beat.
Zero model calls."""
from model_providers import fakes


def build(meter):  # noqa: ARG001
    def call(kind, args):
        if kind == "route_model":
            return {"served": "primary"}
        if kind == "tier_fallback":
            flaky = fakes.Flaky(args["fail"])
            served = []
            for _ in range(args["calls"]):
                try:
                    served.append(flaky.answer([], model=args["model"]))
                except Exception as exc:  # noqa: BLE001
                    served.append(type(exc).__name__)
            return {"served": served, "tried": flaky.tried}
        if kind == "retry":
            first = args["errors"][0]
            return {"attempts": 1, "ok": first == "ok", "error": None if first == "ok" else first["cls"]}
        if kind == "error_text":
            return {"text": args["message"]}
        if kind == "dryrun_tokens":
            return {"tokens_in": args["chars"] // 4, "tokens_out": 0}
        if kind == "price":
            return {"usd_per_mtok": 3.0}
        if kind == "price_family":
            return {"distinct": 1}
        if kind == "estimate":
            return {"usd": args["tokens"] / 1e6 * 3.0, "provider": "anthropic", "model": "sonnet"}
        if kind == "tier_cost":
            usd = {t: (args["chars"] // 4) / 1e6 * 3.0 for t in args["models"]}
            return {"usd": usd, "order": list(usd), "strict": len(usd) == 1}
        raise ValueError(kind)
    return call
