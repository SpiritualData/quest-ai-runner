"""The selected solution, live: the production ``WebResearchAdapter`` built by ``build_web_research_from_env`` from
the process environment (whichever backend or chain a deployment would get), measured through
``scripts/web_search_bench.py``. Every query bypasses the cache read, so it is a real paid call; the spend is
counted into the meter at the list price in ``pricing.py``. Opt-in: SKIPPED (exit 0) unless a backend is
configured and ``QAR_EVAL_LIVE_SEARCH`` is set."""
import os

import common  # noqa: F401  (sets sys.path for the repo)
from quest_ai_runner.adapters.web_research import build_web_research_from_env
from web_search import bench
from web_search_live.pricing import PER_1K_USD


def build(meter):
    if not os.environ.get("QAR_EVAL_LIVE_SEARCH"):
        print("web_search_live: SKIPPED (QAR_EVAL_LIVE_SEARCH is not set; a run spends real search calls)")
        raise SystemExit(0)
    adapter = build_web_research_from_env()
    if adapter is None:
        print("web_search_live: SKIPPED (no search backend is configured in the environment)")
        raise SystemExit(0)

    warned = {}

    def search(case):
        out = bench.run_adapter(adapter, case, fresh=True)
        meter.calls += out["backend_calls"]
        meter.cost_usd += out["backend_calls"] * PER_1K_USD.get(out["backend"], 0.0) / 1000.0
        if out["backend"] not in PER_1K_USD and not warned.get(out["backend"]):
            warned[out["backend"]] = True
            print(f"web_search_live: no list price for backend {out['backend']!r}; its cost is recorded as 0.0")
        return out
    return search
