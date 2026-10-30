"""WebCache -- thread-safe LRU cache (memory + optional on-disk) for web search/fetch results.

Moved to the hub package (``hubofhubs.websearch.cache``); this module re-exports its names so existing imports keep working (CHANGELOG).
"""
from hubofhubs.websearch.cache import (  # noqa: F401
    logger,
    normalize_query,
    WebCache,
)
