"""One probe per hub area: register a fake hub through the area's OWN configuration entry and report whether it worked with
zero edits to the library. An area with no probe reports ``holds`` false and says so: unprobed is not proven."""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCOPE = "quest_ai_runner"


@contextmanager
def registered(solution_id, name, target):
    """Register ``name`` for ``solution_id`` in the live registry for the block, then take it out again."""
    from esoi import runtime
    runtime.register(solution_id, name, target, scope=SCOPE, replace=True)
    try:
        yield
    finally:
        entry = runtime.entry_for(solution_id, None, SCOPE)
        entry.implementations.pop(name, None)
        entry.replaced.discard(name)


def web_search():
    from quest_ai_runner.adapters.web_search_backends import select_search_backend
    with registered("search-the-web", "fakehub", "hub_of_hubs.fake_hub:FakeSearch"):
        got = select_search_backend(env={"QAR_WEB_SEARCH_BACKEND": "fakehub", "FAKEHUB_URL": "http://hub.invalid"})
    return got is not None and type(got).__name__ == "FakeSearch", "selected by QAR_WEB_SEARCH_BACKEND"


def model_providers():
    from quest_ai_runner.core import model_family
    model_family.register_model_prefix("fakevendor", "fakevendor")
    try:
        name = model_family.provider_name_for_model("fakevendor-1", {"fakevendor", "anthropic"})
    finally:
        model_family.MODEL_PREFIX_PROVIDERS.pop("fakevendor", None)
    return name == "fakevendor", "model prefix routed by registered data"


def tools_mcp():
    from quest_ai_runner.config import RunnerConfig
    text = '[[mcp_servers]]\nalias = "fakehub"\ntransport = "http"\nurl = "http://hub.invalid/mcp"\n'
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "lane.toml"
        path.write_text(text, encoding="utf-8")
        cfg = RunnerConfig.from_file(path)
    return [s.alias for s in cfg.mcp_servers] == ["fakehub"], "a [[mcp_servers]] entry in the lane file"


def agent_harness():
    from quest_ai_runner.adapters.acp_deep_runner_config import resolve_agent_entry
    saved = os.environ.get("QAR_ACP_AGENT_COMMAND")
    os.environ["QAR_ACP_AGENT_COMMAND"] = "fakehub-acp-agent"
    try:
        got = resolve_agent_entry(None)
    finally:
        if saved is None:
            os.environ.pop("QAR_ACP_AGENT_COMMAND", None)
        else:
            os.environ["QAR_ACP_AGENT_COMMAND"] = saved
    return got == "fakehub-acp-agent", "QAR_ACP_AGENT_COMMAND names any ACP agent"


def context_assembly():
    from quest_ai_runner.core.resolve import resolve
    with registered("index-by-meaning", "fakehub", "hub_of_hubs.fake_hub:FakeVectorStore"):
        got = resolve("index-by-meaning", overrides={"index-by-meaning": "fakehub"})
    return got.__name__ == "FakeVectorStore", "chosen by cfg.selections for index-by-meaning"


def evals():
    from esoi import harness
    got = harness.load_impl("hub_of_hubs.fake_hub:build", "hub_of_hubs")
    return got(None)("registered", {})["holds"], "an evaluation arm named as module:function (esoi run --impl)"


def mcp_source():
    """Science sources arrive as MCP servers: the probe is the lane-file path, shared with tools_mcp."""
    return tools_mcp()


def world_hub():
    """The world is ONE connection to an aggregator: the shipped preset loads as exactly one MCP server, no per-app entry."""
    from quest_ai_runner.config import RunnerConfig
    cfg = RunnerConfig.from_file(HERE.parents[2] / "examples" / "lane-world-hub.toml")
    ok = [s.alias for s in cfg.mcp_servers] == ["world-hub"] and cfg.mcp_servers[0].transport == "http"
    return ok, "one aggregator entry in examples/lane-world-hub.toml, no per-app entries"


PROBES = {"web_search": web_search, "model_providers": model_providers, "tools_mcp": tools_mcp,
          "agent_harness": agent_harness, "context_assembly": context_assembly, "evals": evals,
          "world": world_hub, "science": mcp_source}


def run(area):
    probe = PROBES.get(area)
    if probe is None:
        return {"holds": False, "note": "no probe yet: a fake hub has not been registered for this area"}
    try:
        ok, how = probe()
    except Exception as exc:  # a probe that raises means the hub could not be added by configuration
        return {"holds": False, "note": f"{type(exc).__name__}: {exc}"}
    return {"holds": bool(ok), "note": how if ok else "not accepted: " + how}
