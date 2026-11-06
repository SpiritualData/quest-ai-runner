"""ProviderWebSearchAdapter -- search through the model provider's own web-search tool. The retrieval adapter is the hub's
(``hubofhubs.websearch.provider_adapter``); this class adds the ``web-search`` seat's ``search`` call by way of the hub's provider-native backend."""
from __future__ import annotations

from typing import Optional

from esoi import implementation
from hubofhubs.api import SearchResponse, backends as hub_backends
from hubofhubs.api import ProviderWebSearchAdapter as HubProviderWebSearchAdapter


@implementation("web-search", "provider_web_search")
class ProviderWebSearchAdapter(HubProviderWebSearchAdapter):
    """The hub's provider-native retrieval adapter, declared under QAR's ``search-the-web`` solution."""

    def search(self, query: str, *, max_results: int = 5, instruction: Optional[str] = None) -> SearchResponse:
        """One web search through the provider's native tool, as a ``SearchResponse``; raises ``SearchBackendError`` on failure."""
        backend = hub_backends.ProviderNativeBackend(self._provider, self._model)
        return backend.search(query, max_results=max_results or self._max_results, instruction=instruction)
