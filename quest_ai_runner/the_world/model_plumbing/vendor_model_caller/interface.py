"""The interface of RoutedModelCalling: Protocols declared with ``@solution``. Importing this module runs nothing."""
from __future__ import annotations

import typing
from esoi import solution
from hubofhubs.api import ModelCalling


@solution("vendor-model-caller", kind="event", entry="answer",
          example={"messages": [{"role": "user", "content": "Reply with the single word: ok"}], "model": "claude-haiku"})
@typing.runtime_checkable
class RoutedModelCalling(ModelCalling, typing.Protocol):
    """The model seat as QAR fills it: the hub's ``vendor-model-caller`` interface (plan, answer, list models) over the router below, which
    sends each model id to the vendor that serves it. A consumer's own provider satisfies it the same way."""
