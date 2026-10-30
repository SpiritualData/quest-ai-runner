"""Robust extraction of a web-search query from a planner-produced spec dict.

Moved to the hub package (``hubofhubs.websearch.query_spec``); this module re-exports its names so existing imports keep working (CHANGELOG).
"""
from hubofhubs.websearch.query_spec import (  # noqa: F401
    coerce_web_query,
)
