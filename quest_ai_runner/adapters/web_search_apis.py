"""Search APIs described as DATA, and the one generic backend that runs any such description.

A hosted search API that answers JSON over HTTP needs no class: an entry says which URL to call, how to send the key, what
to send, where the results are in the reply and which fields are the title, link, snippet and date. ``ConfiguredJsonBackend``
(``web_search_backends.py``) reads an entry and searches. Serper, Brave and Tavily ship as entries below; a deployment adds its
own with a JSON file named by ``QAR_WEB_SEARCH_APIS_FILE`` (a list of entries of this shape), with no code.

Entry fields:
  name          the backend name (``QAR_WEB_SEARCH_BACKEND`` value)
  method        "GET" or "POST"
  url           the endpoint
  key_env       environment variable names that may hold the key (the first one set is used)
  headers       sent as given; ``{key}`` is replaced by the key
  send          "json" (POST body) or "params" (query string)
  payload       what is sent; the value ``"{query}"`` becomes the query and ``"{max_results}"`` the integer count
  results_path  dotted path to the list of results in the reply
  fields        title / url / snippet / date: each a list of keys; the first non-empty one in a result is used
  strip_tags    fields whose HTML tags are removed
  answer_paths  dotted paths tried in order for a direct answer (empty list: none)
"""
from typing import Any, Dict, List

SPECS: List[Dict[str, Any]] = [
    {"name": "serper", "method": "POST", "url": "https://google.serper.dev/search", "key_env": ["SERPER_API_KEY"],
     "headers": {"X-API-KEY": "{key}", "Content-Type": "application/json"}, "send": "json",
     "payload": {"q": "{query}", "num": "{max_results}"}, "results_path": "organic",
     "fields": {"title": ["title"], "url": ["link"], "snippet": ["snippet"], "date": ["date"]}, "strip_tags": [],
     "answer_paths": ["answerBox.answer", "answerBox.snippet", "knowledgeGraph.description"]},
    {"name": "brave", "method": "GET", "url": "https://api.search.brave.com/res/v1/web/search",
     "key_env": ["BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"],
     "headers": {"X-Subscription-Token": "{key}", "Accept": "application/json"}, "send": "params",
     "payload": {"q": "{query}", "count": "{max_results}"}, "results_path": "web.results",
     "fields": {"title": ["title"], "url": ["url"], "snippet": ["description"], "date": ["age", "page_age"]},
     "strip_tags": ["snippet"], "answer_paths": []},
    {"name": "tavily", "method": "POST", "url": "https://api.tavily.com/search", "key_env": ["TAVILY_API_KEY", "WEB_SEARCH_API_KEY"],
     "headers": {"Authorization": "Bearer {key}", "Content-Type": "application/json"}, "send": "json",
     "payload": {"query": "{query}", "max_results": "{max_results}", "search_depth": "basic", "include_answer": True},
     "results_path": "results",
     "fields": {"title": ["title"], "url": ["url"], "snippet": ["content"], "date": ["published_date"]}, "strip_tags": [],
     "answer_paths": ["answer"]},
]
