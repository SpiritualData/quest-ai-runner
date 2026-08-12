"""Retry utilities for transient provider errors (503, rate limits, timeouts).

Provides a provider-agnostic retry decorator that handles:
  * HTTP 503 (Service Unavailable) from Gemini, OpenAI, Anthropic
  * Rate-limit errors (429)
  * Timeout errors
  * Other transient SDK errors

Applies exponential backoff with jitter and retries up to max_retries times.
"""
from __future__ import annotations

import json
import logging
import random
import time
from functools import wraps
from typing import Any, Callable, Optional

_log = logging.getLogger("quest-ai-runner.retry")


def parse_json_with_retry(
    produce: Callable[[], Any],
    *,
    max_retries: int = 2,
    base_delay: float = 0.5,
    validate: Optional[Callable[[Any], bool]] = None,
    label: str = "json",
) -> Any:
    """Standard helper: call ``produce`` and parse its output as JSON, RETRYING ``produce`` when
    parsing (or an optional ``validate(obj)`` check) fails.

    Use this for ANY JSON produced by a model or worker (a structured planner/verdict, an LLM that
    returns a JSON object, a tool envelope) so a malformed shape gets another attempt instead of
    silently degrading to a fallback. ``produce`` should make a FRESH call each time and return
    either a raw string (parsed via ``json.loads``) or an already-decoded ``dict``/``list``
    (validated and returned as-is). Applies exponential backoff with jitter between attempts.

    Returns the parsed object. Raises the last parse/validation error after the final attempt, so
    the caller can apply its own fallback (e.g. a safe default) in one place.
    """
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            raw = produce()
            obj = raw if isinstance(raw, (dict, list)) else json.loads(raw)
            if validate is None or validate(obj):
                return obj
            last_err = ValueError(f"{label}: output failed validation")
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            last_err = e
        if attempt < max_retries:
            _log.warning("%s parse failed (attempt %d/%d): %s — retrying",
                         label, attempt + 1, max_retries + 1, last_err)
            time.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.2))
    raise last_err if last_err is not None else ValueError(f"{label}: parse failed")


def format_provider_error(exc: Exception) -> str:
    """Return a user-friendly error string for known provider errors, or the raw str() otherwise.

    Detects billing, auth, and quota errors by inspecting the exception type and message so callers
    can show actionable guidance instead of a raw SDK traceback.
    """
    exc_str = str(exc)
    exc_type = type(exc).__name__

    # Google Gemini billing / dunning block (403 PERMISSION_DENIED)
    if "ClientError" in exc_type and "403" in exc_str:
        if "dunning" in exc_str.lower() or "PERMISSION_DENIED" in exc_str:
            return (
                "Google Gemini billing issue: your Google Cloud project has been blocked "
                "(overdue payment or spending limit). "
                "Fix it at console.cloud.google.com/billing, then retry. "
                "If the block persists, update GOOGLE_API_KEY in your .env to a key from a "
                "project with active billing."
            )

    # Generic 403 from any Google SDK
    if "403" in exc_str and "PERMISSION_DENIED" in exc_str:
        return (
            "Permission denied by the AI provider (403). "
            "Check your API key and billing status, then retry."
        )

    # Rate limit / quota exhausted
    if "429" in exc_str or "quota" in exc_str.lower() or "RateLimitError" in exc_type:
        return (
            "Rate limit or quota exceeded. Wait a moment and try again, "
            "or switch to a model with higher quota."
        )

    return str(exc)


def is_transient_error(exc: Exception) -> bool:
    """Identify transient errors worth retrying."""
    exc_str = str(exc)
    exc_type = type(exc).__name__
    exc_module = getattr(type(exc), "__module__", "") or ""

    # Gemini SDK: google.genai.errors.ServerError with 503/429
    if "ServerError" in exc_type or "RateLimitError" in exc_type:
        if "503" in exc_str or "429" in exc_str or "overloaded" in exc_str.lower():
            return True

    # Generic timeout / connection errors
    if any(keyword in exc_type.lower() for keyword in ["timeout", "connectionerror", "httperror"]):
        return True

    # httpx / httpcore transport-layer errors — all are transient (server closed mid-stream,
    # network blip, RemoteProtocolError, ConnectError, ReadError, etc.)
    if exc_module.startswith("httpx") or exc_module.startswith("httpcore"):
        return True

    # Anthropic SDK: RateLimitError, APIStatusError with 429/503
    if "RateLimitError" in exc_type or "APIStatusError" in exc_type:
        if "429" in exc_str or "503" in exc_str:
            return True

    # OpenAI SDK: RateLimitError, APIError with 429/503
    if "RateLimitError" in exc_type or "APIError" in exc_type:
        if "429" in exc_str or "503" in exc_str:
            return True

    return False


def retry_transient(max_retries: int = 3, base_delay: float = 1.0) -> Callable:
    """Decorator: retry on transient provider errors with exponential backoff + jitter.

    Args:
        max_retries: number of retries after the initial attempt (total tries = max_retries + 1)
        base_delay: initial delay in seconds; doubles on each retry, plus jitter

    Returns:
        Decorator that wraps a provider method (plan, answer, list_models, etc.)

    Example:
        @retry_transient(max_retries=3, base_delay=1.0)
        def plan(self, prompt, *, model, tool_schema):
            # API call here
            return ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    if not is_transient_error(exc):
                        raise  # Not transient; fail immediately

                    if attempt >= max_retries:
                        # Last attempt; give up
                        _log.error(
                            f"{func.__name__} failed after {max_retries + 1} attempts: {type(exc).__name__}: {exc}"
                        )
                        raise

                    # Transient error and we have retries left; backoff and retry
                    delay = base_delay * (2 ** attempt)  # exponential: 1, 2, 4, 8, ...
                    jitter = random.uniform(0, delay * 0.1)  # ±10% jitter
                    total_delay = delay + jitter
                    # DEBUG, not INFO: a mid-flight retry succeeding is invisible/expected
                    # noise to an end user (surfaced by the terminal UI's default INFO
                    # console) -- only the final give-up (_log.error above) is something a
                    # user needs to see. Still available via -v/--verbose.
                    _log.debug(
                        f"{func.__name__} attempt {attempt + 1} failed ({type(exc).__name__}); "
                        f"retrying in {total_delay:.2f}s"
                    )
                    time.sleep(total_delay)

            # Should not reach here, but if we do, raise the last exception
            raise last_exc

        return wrapper
    return decorator
