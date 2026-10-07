"""web_search_backends -- pluggable SERP backends behind one small ``SearchBackend`` protocol.

Why several backends instead of one: dedicated SERP APIs (Serper, Brave, Tavily, a self-hosted
SearXNG) return real title+url+snippet results in well under a second and are cheap at volume;
Gemini's Google Search grounding tool needs no separate key beyond a Gemini key already paid for
elsewhere, so it is the key-free-beyond-an-LLM-key fallback. Rough cost per 1,000 searches (check
each provider's current pricing before relying on these):

  * Serper            ~$1/1k, down to ~$0.30/1k at volume.
  * Brave Search API   $5/1k, with a $5/month free credit.
  * Tavily             usage-based; see tavily.com/pricing.
  * SearXNG            self-hosted, no per-call cost (your own infra bill instead).
  * Gemini 2.5 Flash-Lite grounding: 1,500 grounded prompts/day free, then ~$35/1k.
  * Gemini 3.x grounding: 5,000 grounded prompts/month free, then ~$14/1k.

``select_search_backend`` picks one (or a fallback chain) from environment config; see its
docstring for the exact env vars and the "auto" selection order.

All HTTP goes through one small injectable callable per backend (``http: (method, url, **kwargs)
-> response``), defaulting to a thin wrapper over ``httpx.request``, so tests can run fully
offline with fake response objects (just ``.status_code``, ``.json()``, ``.headers``).
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol

import httpx

logger = logging.getLogger("quest-ai-runner.web-search-backends")

_DEFAULT_TIMEOUT = 8.0
_REDIRECT_TIMEOUT = 3.0
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"

HttpCallable = Callable[..., Any]


def _default_http(method: str, url: str, **kwargs: Any) -> Any:
    kwargs.setdefault("timeout", _DEFAULT_TIMEOUT)
    return httpx.request(method, url, **kwargs)


# ---------------------------------------------------------------------------
# Result shapes + errors
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    date: str = ""


@dataclass
class SearchResponse:
    hits: List[SearchHit]
    answer: str = ""
    queries_issued: List[str] = field(default_factory=list)


class SearchBackendError(Exception):
    """Raised by a backend's ``search()`` on any failure (HTTP error, bad shape, no results)."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse: ...


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------


def _raise_for_status(resp: Any, backend: str) -> None:
    status = getattr(resp, "status_code", 200)
    if status == 429:
        raise SearchBackendError(f"{backend}: HTTP 429 rate limited", status_code=429)
    if status in (401, 403):
        raise SearchBackendError(f"{backend}: HTTP {status} unauthorized", status_code=status)
    if status >= 400:
        raise SearchBackendError(f"{backend}: HTTP {status}", status_code=status)


def _json(resp: Any, backend: str) -> Dict[str, Any]:
    try:
        return resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        raise SearchBackendError(f"{backend}: invalid JSON response: {exc}") from exc


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text or "")


# ---------------------------------------------------------------------------
# 1. Serper (Google SERP proxy)
# ---------------------------------------------------------------------------


class SerperBackend:
    name = "serper"

    def __init__(self, api_key: str, http: HttpCallable = _default_http) -> None:
        self.api_key = api_key
        self.http = http

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        try:
            resp = self.http(
                "POST",
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
            )
        except SearchBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SearchBackendError(f"serper: request failed: {exc}") from exc
        _raise_for_status(resp, "serper")
        data = _json(resp, "serper")

        hits = [
            SearchHit(
                title=item.get("title", "") or "",
                url=item.get("link", "") or "",
                snippet=item.get("snippet", "") or "",
                date=item.get("date", "") or "",
            )
            for item in (data.get("organic") or [])[:max_results]
        ]

        answer = ""
        answer_box = data.get("answerBox") or {}
        if answer_box:
            answer = answer_box.get("answer") or answer_box.get("snippet") or ""
        if not answer:
            kg = data.get("knowledgeGraph") or {}
            answer = kg.get("description", "") or ""

        if not hits and not answer:
            raise SearchBackendError(f"serper: no results for {query!r}")
        return SearchResponse(hits=hits, answer=answer, queries_issued=[query])


# ---------------------------------------------------------------------------
# 2. Brave Search API
# ---------------------------------------------------------------------------


class BraveBackend:
    name = "brave"

    def __init__(self, api_key: str, http: HttpCallable = _default_http) -> None:
        self.api_key = api_key
        self.http = http

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        try:
            resp = self.http(
                "GET",
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                params={"q": query, "count": max_results},
            )
        except SearchBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SearchBackendError(f"brave: request failed: {exc}") from exc
        _raise_for_status(resp, "brave")
        data = _json(resp, "brave")

        web = data.get("web") or {}
        hits = [
            SearchHit(
                title=item.get("title", "") or "",
                url=item.get("url", "") or "",
                snippet=_strip_tags(item.get("description", "") or ""),
                date=item.get("age") or item.get("page_age") or "",
            )
            for item in (web.get("results") or [])[:max_results]
        ]
        if not hits:
            raise SearchBackendError(f"brave: no results for {query!r}")
        return SearchResponse(hits=hits, answer="", queries_issued=[query])


# ---------------------------------------------------------------------------
# 3. Tavily
# ---------------------------------------------------------------------------


class TavilyBackend:
    name = "tavily"

    def __init__(self, api_key: str, http: HttpCallable = _default_http) -> None:
        self.api_key = api_key
        self.http = http

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        try:
            resp = self.http(
                "POST",
                "https://api.tavily.com/search",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": True,
                },
            )
        except SearchBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SearchBackendError(f"tavily: request failed: {exc}") from exc
        _raise_for_status(resp, "tavily")
        data = _json(resp, "tavily")

        hits = [
            SearchHit(
                title=item.get("title", "") or "",
                url=item.get("url", "") or "",
                snippet=item.get("content", "") or "",
                date=item.get("published_date", "") or "",
            )
            for item in (data.get("results") or [])[:max_results]
        ]
        answer = data.get("answer", "") or ""
        if not hits and not answer:
            raise SearchBackendError(f"tavily: no results for {query!r}")
        return SearchResponse(hits=hits, answer=answer, queries_issued=[query])


# ---------------------------------------------------------------------------
# 4. SearXNG (self-hosted)
# ---------------------------------------------------------------------------


class SearxngBackend:
    name = "searxng"

    def __init__(self, base_url: str, http: HttpCallable = _default_http) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.http = http

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        try:
            resp = self.http(
                "GET",
                f"{self.base_url}/search",
                params={"q": query, "format": "json"},
            )
        except SearchBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SearchBackendError(f"searxng: request failed: {exc}") from exc
        _raise_for_status(resp, "searxng")
        data = _json(resp, "searxng")

        hits = [
            SearchHit(
                title=item.get("title", "") or "",
                url=item.get("url", "") or "",
                snippet=item.get("content", "") or "",
                date=item.get("publishedDate", "") or "",
            )
            for item in (data.get("results") or [])[:max_results]
        ]
        if not hits:
            raise SearchBackendError(f"searxng: no results for {query!r}")
        return SearchResponse(hits=hits, answer="", queries_issued=[query])


# ---------------------------------------------------------------------------
# 5. Gemini Google Search grounding (key-free beyond an existing Gemini key)
# ---------------------------------------------------------------------------


def _gemini_api_key_from_env(env: Optional[Mapping[str, str]] = None) -> str:
    env = env if env is not None else os.environ
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY"):
        value = env.get(name)
        if value:
            return value
    return ""


def _location_header(resp: Any) -> str:
    headers = getattr(resp, "headers", None) or {}
    try:
        return headers.get("Location") or headers.get("location") or ""
    except Exception:  # noqa: BLE001
        return ""


class GeminiGroundingBackend:
    """Searches via Gemini's native Google Search grounding tool (``google.genai``).

    Uses its OWN client (not the planner's provider), so it works regardless of which provider
    is running the planner/answer calls. Resolves the ``vertexaisearch`` redirect URLs Gemini
    returns to their real destinations (concurrent HEAD, falling back to GET, 3s timeout each,
    per-instance cache) so citations point at the actual source page.
    """

    name = "gemini-grounding"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_GEMINI_MODEL,
        client: Any = None,
        http: HttpCallable = _default_http,
    ) -> None:
        self.api_key = api_key or _gemini_api_key_from_env()
        self.model = model
        self.http = http
        self._client = client
        self.tokens_in = 0
        self.tokens_out = 0
        self.calls = 0
        self._redirect_cache: Dict[str, str] = {}
        self._redirect_lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import google.genai as genai
        except ImportError as exc:
            raise SearchBackendError("gemini-grounding: google-genai is not installed") from exc
        if not self.api_key:
            raise SearchBackendError("gemini-grounding: no Gemini API key configured")
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        client = self._get_client()
        try:
            from google.genai import types

            tool = types.Tool(google_search=types.GoogleSearch())
            prompt = (
                "Search the web and answer concisely in at most 120 words with the key facts, "
                f"numbers and dates. Query: {query}"
            )
            response = self._generate(client, types, tool, prompt)
        except SearchBackendError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SearchBackendError(f"gemini-grounding: {exc}") from exc

        self.calls += 1
        meta = getattr(response, "usage_metadata", None)
        if meta:
            self.tokens_in += getattr(meta, "prompt_token_count", 0) or 0
            self.tokens_out += getattr(meta, "candidates_token_count", 0) or 0

        answer = getattr(response, "text", "") or ""
        hits, queries_issued = self._parse_grounding(response, max_results)
        if not hits and not answer:
            raise SearchBackendError(f"gemini-grounding: no results for {query!r}")
        return SearchResponse(hits=hits, answer=answer, queries_issued=queries_issued or [query])

    def _generate(self, client: Any, types: Any, tool: Any, prompt: str) -> Any:
        """One ``generate_content`` call; Gemini 3.x gets minimal thinking (retried once without
        it if the model rejects the config), matching ``gemini_provider.GeminiProvider._generate``.
        """
        if "gemini-3" in self.model:
            try:
                config = types.GenerateContentConfig(
                    tools=[tool], thinking_config={"thinking_level": "minimal"}
                )
                return client.models.generate_content(model=self.model, contents=prompt, config=config)
            except Exception as exc:  # noqa: BLE001
                if "think" not in str(exc).lower():
                    raise
        config = types.GenerateContentConfig(tools=[tool])
        return client.models.generate_content(model=self.model, contents=prompt, config=config)

    def _parse_grounding(self, response: Any, max_results: int) -> Any:
        queries_issued: List[str] = []
        chunks: List[Any] = []
        supports: List[Any] = []
        for cand in getattr(response, "candidates", None) or []:
            gm = getattr(cand, "grounding_metadata", None)
            if not gm:
                continue
            chunks = list(getattr(gm, "grounding_chunks", None) or [])
            supports = list(getattr(gm, "grounding_supports", None) or [])
            queries_issued = list(getattr(gm, "web_search_queries", None) or [])
            break  # first candidate carries the grounding metadata

        if not chunks:
            return [], queries_issued

        snippets_by_index: Dict[int, List[str]] = {}
        for support in supports:
            segment = getattr(support, "segment", None)
            text = getattr(segment, "text", "") if segment else ""
            if not text:
                continue
            for idx in getattr(support, "grounding_chunk_indices", None) or []:
                bucket = snippets_by_index.setdefault(idx, [])
                if text not in bucket:
                    bucket.append(text)

        raw_hits: List[Dict[str, str]] = []
        urls_to_resolve: List[str] = []
        for i, chunk in enumerate(chunks[:max_results]):
            web = getattr(chunk, "web", None)
            if web is None:
                continue
            uri = getattr(web, "uri", "") or ""
            title = getattr(web, "title", "") or ""
            snippet = " ".join(snippets_by_index.get(i, [])).strip()
            if len(snippet) > 300:
                snippet = snippet[:300].rsplit(" ", 1)[0] + "..."
            raw_hits.append({"title": title, "url": uri, "snippet": snippet})
            if uri:
                urls_to_resolve.append(uri)

        resolved = self._resolve_redirects(urls_to_resolve)
        hits = [
            SearchHit(title=h["title"], url=resolved.get(h["url"], h["url"]), snippet=h["snippet"])
            for h in raw_hits
        ]
        return hits, queries_issued

    def _resolve_redirects(self, urls: List[str]) -> Dict[str, str]:
        """Resolve grounding-redirect URLs to their real destination. Failures keep the original URL."""
        resolved: Dict[str, str] = {}
        to_resolve: List[str] = []
        with self._redirect_lock:
            for u in urls:
                cached = self._redirect_cache.get(u)
                if cached is not None:
                    resolved[u] = cached
                else:
                    to_resolve.append(u)
        if not to_resolve:
            return resolved

        def resolve_one(u: str) -> str:
            try:
                resp = self.http("HEAD", u, timeout=_REDIRECT_TIMEOUT, follow_redirects=False)
                loc = _location_header(resp)
                if loc:
                    return loc
                # HEAD answered but carried no usable Location (e.g. 405 Method Not Allowed, or
                # 200 with no redirect): a GET may behave differently, so fall through and try it.
            except httpx.TimeoutException:
                # HEAD timed out: keep the redirect URL as-is. Retrying with GET would double the
                # worst-case latency for a dead or slow redirector, for no better outcome.
                return u
            except Exception:  # noqa: BLE001
                # Some other HEAD failure (connection refused, TLS error, ...): a GET may still work.
                pass
            try:
                resp = self.http("GET", u, timeout=_REDIRECT_TIMEOUT, follow_redirects=False)
                loc = _location_header(resp)
                if loc:
                    return loc
            except Exception:  # noqa: BLE001
                pass
            return u

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(to_resolve))) as pool:
            futures = {pool.submit(resolve_one, u): u for u in to_resolve}
            for fut in concurrent.futures.as_completed(futures):
                u = futures[fut]
                try:
                    resolved[u] = fut.result()
                except Exception:  # noqa: BLE001
                    resolved[u] = u

        with self._redirect_lock:
            self._redirect_cache.update(resolved)
        return resolved


# ---------------------------------------------------------------------------
# 6. Provider-native (e.g. AnthropicProvider.web_search)
# ---------------------------------------------------------------------------


class ProviderNativeBackend:
    """Wraps any ``ModelProvider`` exposing ``web_search(query, model=, max_results=)``."""

    name = "provider-native"

    def __init__(self, provider: Any, model: str) -> None:
        self.provider = provider
        self.model = model

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        try:
            result = self.provider.web_search(query, model=self.model, max_results=max_results)
        except Exception as exc:  # noqa: BLE001
            raise SearchBackendError(f"provider-native: {exc}") from exc

        hits = [
            SearchHit(
                title=r.get("title", "") or "",
                url=r.get("url", "") or "",
                snippet=r.get("snippet", "") or "",
            )
            for r in (result.get("results") or [])
        ]
        answer = result.get("answer", "") or ""
        if not hits and not answer:
            raise SearchBackendError(f"provider-native: no results for {query!r}")
        return SearchResponse(hits=hits, answer=answer, queries_issued=[query])


# ---------------------------------------------------------------------------
# 7. Fallback chain
# ---------------------------------------------------------------------------


class FallbackSearchBackend:
    """Tries each backend in order; moves on on error or an empty (no hits, no answer) response.

    After a 429/401/403 from a backend, that backend sits in a cooldown so a failing primary
    isn't hammered on every subsequent search.
    """

    def __init__(self, backends: List[Any], *, cooldown_seconds: float = 60.0) -> None:
        if not backends:
            raise ValueError("FallbackSearchBackend requires at least one backend")
        self.backends = backends
        self.name = ">".join(b.name for b in backends)
        self._cooldown_seconds = cooldown_seconds
        self._cooldown_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        errors: List[str] = []
        now = time.time()
        for backend in self.backends:
            with self._lock:
                until = self._cooldown_until.get(backend.name, 0.0)
            if until > now:
                errors.append(f"{backend.name}: in cooldown after a recent failure")
                continue
            try:
                resp = backend.search(query, max_results=max_results)
            except SearchBackendError as exc:
                errors.append(str(exc))
                if exc.status_code in (401, 403, 429):
                    with self._lock:
                        self._cooldown_until[backend.name] = time.time() + self._cooldown_seconds
                continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{backend.name}: {exc}")
                continue
            if not resp.hits and not resp.answer:
                errors.append(f"{backend.name}: empty response")
                continue
            return resp
        raise SearchBackendError("; ".join(errors) if errors else "all backends failed")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_log = logger


def select_search_backend(
    env: Optional[Mapping[str, str]] = None, *, provider: Any = None
) -> Optional[Any]:
    """Pick a ``SearchBackend`` (or a ``FallbackSearchBackend`` chain) from environment config.

    Env vars:
      WEB_SEARCH_ENABLED        -- "false"/"0"/"off"/"no" disables web search entirely (default on).
      QAR_WEB_SEARCH_BACKEND    -- force exactly one: serper|brave|tavily|searxng|gemini|provider.
                                    If its key/config is missing, logs a warning and returns None
                                    rather than silently falling back to another backend.
      QAR_WEB_SEARCH_MODEL      -- Gemini grounding model (default "gemini-2.5-flash-lite").
      QAR_WEB_SEARCH_PROVIDER_MODEL -- model id for provider-native search (required for "provider").
      SERPER_API_KEY
      BRAVE_SEARCH_API_KEY / BRAVE_API_KEY
      TAVILY_API_KEY / WEB_SEARCH_API_KEY (legacy name)
      SEARXNG_URL
      GEMINI_API_KEY / GOOGLE_API_KEY / GOOGLE_AI_API_KEY

    "auto" (no ``QAR_WEB_SEARCH_BACKEND``): collects every backend whose config is present, in
    this order -- serper, brave, tavily, searxng, gemini, provider-native -- and wraps more than
    one in a ``FallbackSearchBackend``. ``provider`` (a ``ModelProvider``, usually the
    ``MultiProvider``) is only used for the "provider" option, and only when
    ``QAR_WEB_SEARCH_PROVIDER_MODEL`` is also set and the provider reports
    ``supports_web_search()`` for that model. Returns ``None`` when nothing is configured.
    """
    env = env if env is not None else os.environ
    enabled = (env.get("WEB_SEARCH_ENABLED", "") or "").strip().lower()
    if enabled in ("false", "0", "off", "no"):
        return None

    model = env.get("QAR_WEB_SEARCH_MODEL") or _DEFAULT_GEMINI_MODEL
    provider_model = env.get("QAR_WEB_SEARCH_PROVIDER_MODEL") or ""

    def build(name: str) -> Optional[Any]:
        if name == "serper":
            key = env.get("SERPER_API_KEY", "")
            return SerperBackend(api_key=key) if key else None
        if name == "brave":
            key = env.get("BRAVE_SEARCH_API_KEY", "") or env.get("BRAVE_API_KEY", "")
            return BraveBackend(api_key=key) if key else None
        if name == "tavily":
            key = env.get("TAVILY_API_KEY", "") or env.get("WEB_SEARCH_API_KEY", "")
            return TavilyBackend(api_key=key) if key else None
        if name == "searxng":
            base_url = env.get("SEARXNG_URL", "")
            return SearxngBackend(base_url=base_url) if base_url else None
        if name == "gemini":
            key = _gemini_api_key_from_env(env)
            return GeminiGroundingBackend(api_key=key, model=model) if key else None
        if name == "provider":
            if provider is None or not provider_model:
                return None
            fn = getattr(provider, "supports_web_search", None)
            try:
                if not (callable(fn) and fn(provider_model)):
                    return None
            except Exception:  # noqa: BLE001
                return None
            return ProviderNativeBackend(provider, provider_model)
        return None

    explicit = (env.get("QAR_WEB_SEARCH_BACKEND", "") or "").strip().lower()
    if explicit:
        backend = build(explicit)
        if backend is None:
            _log.warning(
                "QAR_WEB_SEARCH_BACKEND=%s requested but its key/config is missing; "
                "web search disabled", explicit,
            )
        return backend

    order = ["serper", "brave", "tavily", "searxng", "gemini", "provider"]
    available = [b for b in (build(name) for name in order) if b is not None]
    if not available:
        return None
    if len(available) == 1:
        return available[0]
    return FallbackSearchBackend(available)
