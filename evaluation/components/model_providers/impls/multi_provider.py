"""CURRENT: production ``MultiProvider`` (prefix routing and quota tier fallback), ``retry_transient`` and
``format_provider_error`` (``adapters/retry_utils.py``), the real ``DryRunProvider`` token estimator, and the
``pricing.py`` table, driven with fake providers. Zero model calls, zero network."""

from model_providers import fakes
from quest_ai_runner import pricing
from quest_ai_runner.adapters.dryrun_provider import DryRunProvider
from quest_ai_runner.adapters.multi_provider import MultiProvider
from quest_ai_runner.adapters.retry_utils import format_provider_error, retry_transient
from quest_ai_runner.core import provider_outage

PLAN_SCHEMA = {"name": "plan", "input_schema": {"type": "object", "properties": {}}}


def make_error(spec):
    """An exception of the named class, optionally claiming to come from another module (httpx)."""
    if spec["cls"] == "FileNotFoundError":          # the builtin itself: retry_utils checks isinstance
        return FileNotFoundError(spec.get("msg", ""))
    cls = type(spec["cls"], (Exception,), {"__module__": spec.get("module") or "builtins"})
    return cls(spec.get("msg", ""))


def build(meter):  # noqa: ARG001
    def call(kind, args):
        if kind == "route_model":
            providers = {n: fakes.Named(n) for n in args["providers"]}
            multi = MultiProvider(fakes.Named("primary"), providers)
            return {"served": multi.answer([{"role": "user", "content": "hi"}], model=args["model"])}
        if kind == "tier_fallback":
            flaky = fakes.Flaky(args["fail"])
            multi = MultiProvider(flaky, {}, registry=fakes.FixedRegistry(args["top"]) if args.get("registry", True) else None)
            served = []
            for _ in range(args["calls"]):
                try:
                    served.append(multi.answer([{"role": "user", "content": "q"}], model=args["model"]))
                except Exception as exc:  # noqa: BLE001
                    served.append(type(exc).__name__)
            return {"served": served, "tried": flaky.tried}
        if kind == "retry":
            script = list(args["errors"])
            state = {"attempts": 0}

            @retry_transient(max_retries=args["max_retries"], base_delay=0.0)
            def flaky_call():
                state["attempts"] += 1
                item = script.pop(0)
                if item == "ok":
                    return "ok"
                raise make_error(item)
            try:
                flaky_call()
                return {"attempts": state["attempts"], "ok": True, "error": None}
            except Exception as exc:  # noqa: BLE001
                return {"attempts": state["attempts"], "ok": False, "error": type(exc).__name__}
            finally:
                provider_outage.reset_for_tests()    # a give-up records a process-wide outage; do not leak it
        if kind == "error_text":
            return {"text": format_provider_error(make_error({"cls": args["type"], "msg": args["message"]}))}
        if kind == "dryrun_tokens":
            dry = DryRunProvider(fakes.Named("wrapped"))
            if args["call"] == "plan":
                dry.plan("x" * args["chars"], model="m", tool_schema=PLAN_SCHEMA)
            else:
                body = ("json " if args.get("json") else "") + "x" * (args["chars"] - (5 if args.get("json") else 0))
                dry.answer([{"role": "user", "content": body}], model="m")
            return {"tokens_in": dry.tokens_in, "tokens_out": dry.tokens_out}
        if kind == "price":
            return {"usd_per_mtok": pricing.get_input_cost_per_mtok(args["provider"], args["model"])}
        if kind == "price_family":
            prices = {pricing.get_input_cost_per_mtok(args["provider"], m) for m in args["models"]}
            return {"distinct": len(prices)}
        if kind == "estimate":
            usd, provider, model = pricing.estimate_bootstrap_cost(args["tokens"], args["env"])
            return {"usd": usd, "provider": provider, "model": model}
        if kind == "tier_cost":
            usd = {}
            for tier, model in args["models"].items():
                dry = DryRunProvider(fakes.Named("wrapped"))
                dry.plan("x" * args["chars"], model=model, tool_schema=PLAN_SCHEMA)
                usd[tier] = dry.tokens_in / 1e6 * pricing.get_input_cost_per_mtok("anthropic", model)
            return {"usd": usd, "order": sorted(usd, key=lambda t: (usd[t], list(usd).index(t))),
                    "strict": len(set(usd.values())) == len(usd)}
        raise ValueError(kind)
    return call
