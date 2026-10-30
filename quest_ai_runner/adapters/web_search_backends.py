"""web_search_backends -- QAR's names and selection over the hub's search backends.

The backends themselves moved to the hub package (``hubofhubs.websearch.backends`` and ``.gemini``); the hub's
``search-backend`` seat holds them. This module keeps what is QAR's: the ``search-the-web`` implementation rows (each class
below is the hub's, declared under QAR's solution so ``resolve("search-the-web", name)`` and the records are unchanged), the
``QAR_*`` setting names, the process environment as the default, and the Gemini key lookup. Names the old module defined
stay importable from here (CHANGELOG).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Mapping, Optional

from esoi import implementation, runtime
from hubofhubs.ports.search import SearchBackendError, SearchHit, SearchResponse  # noqa: F401
from hubofhubs.seats import SearchBackend  # noqa: F401
from hubofhubs.websearch import backends as hub_backends
from hubofhubs.websearch.backends import (  # noqa: F401
    ConfiguredJsonBackend,
    FallbackSearchBackend,
    HttpCallable,
    backend_class_for,
    dotted,
    fill,
    gemini_api_key_from_env,
    load_file_specs,
)
from hubofhubs.websearch.gemini import GeminiGroundingBackend as HubGeminiBackend

from ..core.resolve import SCOPE, resolve

logger = logging.getLogger("quest-ai-runner.web-search-backends")

QAR_KEYS = hub_backends.SelectionKeys(
    enabled="WEB_SEARCH_ENABLED",
    backend="QAR_WEB_SEARCH_BACKEND",
    apis_file="QAR_WEB_SEARCH_APIS_FILE",
    model="QAR_WEB_SEARCH_MODEL",
    provider_model="QAR_WEB_SEARCH_PROVIDER_MODEL",
)


@implementation("search-the-web", "serper")
class SerperBackend(hub_backends.SerperBackend):
    """The hub's Serper backend under QAR's ``search-the-web`` solution."""


@implementation("search-the-web", "brave")
class BraveBackend(hub_backends.BraveBackend):
    """The hub's Brave backend under QAR's ``search-the-web`` solution."""


@implementation("search-the-web", "tavily")
class TavilyBackend(hub_backends.TavilyBackend):
    """The hub's Tavily backend under QAR's ``search-the-web`` solution."""


@implementation("search-the-web", "searxng")
class SearxngBackend(hub_backends.SearxngBackend):
    """The hub's SearXNG backend under QAR's ``search-the-web`` solution."""


@implementation("search-the-web", "provider")
class ProviderNativeBackend(hub_backends.ProviderNativeBackend):
    """The hub's provider-native backend under QAR's ``search-the-web`` solution."""


@implementation("search-the-web", "gemini")
class GeminiGroundingBackend(HubGeminiBackend):
    """The hub's Gemini grounding backend; with no key given it looks one up in the process environment, as QAR always did."""

    def __init__(self, api_key: Optional[str] = None, *args: Any, **kwargs: Any) -> None:
        super().__init__(api_key or gemini_api_key_from_env(os.environ), *args, **kwargs)


def register_file_specs(env: Mapping[str, str]) -> List[str]:
    """Register the search APIs a deployment describes in the file named by ``QAR_WEB_SEARCH_APIS_FILE`` under
    ``search-the-web``; returns their names. A bad file registers nothing: web search never fails the turn."""
    classes = load_file_specs(env.get(QAR_KEYS.apis_file) or "")
    for name, backend_class in classes.items():
        runtime.register("search-the-web", name, backend_class, scope=SCOPE, replace=True)
    return list(classes)


def select_search_backend(env: Optional[Mapping[str, str]] = None, *, provider: Any = None) -> Optional[Any]:
    """The hub's selector with QAR's setting names (``QAR_WEB_SEARCH_BACKEND`` and friends), the process environment by
    default, and QAR's own resolution of names (``resolve("search-the-web", name)``)."""
    env = env if env is not None else os.environ
    extra = {name: resolve("search-the-web", name) for name in register_file_specs(env)}
    return hub_backends.select_search_backend(env, provider=provider, keys=QAR_KEYS,
                                              resolver=lambda name: resolve("search-the-web", name), extra=extra)
