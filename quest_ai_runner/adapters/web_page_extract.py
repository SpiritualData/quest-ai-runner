"""web_page_extract -- SSRF guard, main-text HTML extraction, and focus-scored passage selection.

Moved to the hub package (``hubofhubs.websearch.page_extract``); this module re-exports its names so existing imports keep working (CHANGELOG).
"""
from hubofhubs.websearch.page_extract import (  # noqa: F401
    logger,
    Resolver,
    default_resolver,
    check_url_is_safe,
    extract_main_text,
    split_passages,
    estimate_tokens,
    select_passages,
)
