"""One production backend (`web_search_backends.SerperBackend`) behind WebResearchAdapter, served the case's
serper payload through the fixture HTTP seam. Zero model calls, zero network."""
from quest_ai_runner.the_world.tools.web_search.web_search_backends import SerperBackend  # noqa: F401  (built by _vendor.backend_for)
from web_search.impls._vendor import single

build = single("serper")
