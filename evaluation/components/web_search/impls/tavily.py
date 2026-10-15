"""One production backend (`web_search_backends.TavilyBackend`) behind WebResearchAdapter, served the case's
tavily payload through the fixture HTTP seam. Zero model calls, zero network."""
from quest_ai_runner.adapters.web_search_backends import TavilyBackend  # noqa: F401  (built by _vendor.backend_for)
from web_search.impls._vendor import single

build = single("tavily")
