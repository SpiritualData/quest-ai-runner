"""Fake providers shared by the model-provider solutions. Nothing here touches a network or a model."""
from quest_ai_runner.core.adapters_bases import ModelProviderBase
from quest_ai_runner.core.model_registry import next_lower_tier


class Named(ModelProviderBase):
    """Answers with its own name, so a test can see which provider a call reached."""

    def __init__(self, name):
        super().__init__()
        self.name = name

    def plan(self, prompt, *, model, tool_schema, layers=None):  # noqa: ARG002
        return {"action": "answer", "provider": self.name}

    def answer(self, messages, *, model, system=None, layers=None):  # noqa: ARG002
        return self.name

    def list_models(self):
        return [self.name]


class Flaky(ModelProviderBase):
    """Fails the models named in ``fail`` ("quota" -> a 429 error, "other" -> a ValueError); any other model
    answers with its own name. ``tried`` records every model asked, in order."""

    def __init__(self, fail):
        super().__init__()
        self.fail, self.tried = dict(fail), []

    def _maybe_fail(self, model):
        self.tried.append(model)
        kind = self.fail.get(model)
        if kind == "quota":
            raise Exception("429 RESOURCE_EXHAUSTED: quota exceeded for this model")
        if kind == "other":
            raise ValueError("the model blocked this response")

    def plan(self, prompt, *, model, tool_schema, layers=None):  # noqa: ARG002
        self._maybe_fail(model)
        return {"action": "answer", "model": model}

    def answer(self, messages, *, model, system=None, layers=None):  # noqa: ARG002
        self._maybe_fail(model)
        return f"answered by {model}"

    def list_models(self):
        return list(self.fail)


class FixedRegistry:
    """The two methods MultiProvider asks of a ModelRegistry: a fixed tier -> model map."""

    def __init__(self, top):
        self._top = dict(top)

    def top_models(self):
        return dict(self._top)

    def resolve_tier(self, tier):
        return self._top.get(tier, self._top["balanced"])

    def lower_tier(self, tier):
        return next_lower_tier(tier)
