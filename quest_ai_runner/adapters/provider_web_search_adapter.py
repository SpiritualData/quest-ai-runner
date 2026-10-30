"""ProviderWebSearchAdapter -- search through the model provider's own web-search tool. Moved to the hub package
(``hubofhubs.websearch.provider_adapter``); kept here: the ``search-the-web`` implementation row."""
from __future__ import annotations

from esoi import implementation
from hubofhubs.websearch.provider_adapter import ProviderWebSearchAdapter as HubProviderWebSearchAdapter


@implementation("search-the-web", "provider_web_search")
class ProviderWebSearchAdapter(HubProviderWebSearchAdapter):
    """The hub's provider-native retrieval adapter, declared under QAR's ``search-the-web`` solution."""
