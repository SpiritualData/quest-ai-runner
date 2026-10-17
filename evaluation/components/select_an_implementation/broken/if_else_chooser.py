"""BASELINE: the chooser a call site would hard-code. An ``if``/``elif`` over the model-provider names it knew
when written: a name outside the chain silently becomes the default instead of raising, a config override is
not read, every solution but the model provider is unknown, and no call site can be switched by data. Zero
model calls."""


def pick(solution, name):
    if solution != "call-a-model":
        return {"cls": None, "error": "KeyError"}
    if name == "anthropic":
        return {"cls": "AnthropicProvider", "error": None}
    if name == "gemini":
        return {"cls": "GeminiProvider", "error": None}
    if name == "openai":
        return {"cls": "OpenAIProvider", "error": None}
    return {"cls": "ClaudeCliProvider", "error": None}


def build(meter):  # noqa: ARG001
    def select(kind, args):
        if kind == "by_name":
            return pick(args["solution"], args["name"])
        if kind == "by_override":
            return pick(args["solution"], args.get("name"))  # overrides are not read
        if kind == "by_default":
            out = pick(args["solution"], None)
            out["default"] = "claude_cli"
            return out
        if kind == "typo":
            return {"error": None, "lists_choices": False}
        if kind == "integrity":
            return {"unresolvable": [], "bad_defaults": [], "solutions": 0}  # no table to check
        if kind == "live_switch":
            default = {"model_provider": "ClaudeCliProvider", "deep_runner": "SubprocessGoalRunner",
                       "search_backend": "SerperBackend"}[args["site"]]
            return {"cls": default, "error": None}  # nothing switches without editing the code
        raise ValueError(kind)
    return select
