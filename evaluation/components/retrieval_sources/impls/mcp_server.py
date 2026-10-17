"""The live ``MCPRetrievalAdapter`` over a scripted MCP session (the SDK seam ``open_mcp_session`` is the only
fake; the real ``MCPClient`` runs on its own event loop). Search is the SERVER's ``search_docs`` tool through
the adapter's allowlist-gated ``query``; read is ``resources/read`` through ``read_section``. The ranking of a
search belongs to the server, so this number measures the adapter's plumbing (namespacing, allowlist, tool
call, resource read, truncation), not a retrieval algorithm."""
import contextlib
import re

from quest_ai_runner.adapters import mcp_client
from quest_ai_runner.adapters.mcp_client import MCPServerSpec
from quest_ai_runner.adapters.mcp_retrieval_adapter import MCPRetrievalAdapter

from retrieval import policy
from retrieval.fakes import mcp_session
from retrieval.world import Lazy

ALIAS = "handbook"


@contextlib.contextmanager
def seam(open_session):
    original = mcp_client.open_mcp_session
    mcp_client.open_mcp_session = open_session
    try:
        yield
    finally:
        mcp_client.open_mcp_session = original


def build(meter):  # noqa: ARG001
    def make(corpus):
        open_session = mcp_session(corpus["docs"])
        spec = MCPServerSpec(alias=ALIAS, transport="stdio", command="handbook-server", timeout_s=10.0)
        return open_session, MCPRetrievalAdapter(alias=ALIAS, spec=spec, allowed_tools=["search_docs"])
    state = Lazy(make)

    def retrieve(question, corpus):
        open_session, adapter = state.get(corpus)

        def search(term):
            with seam(open_session):
                obs = adapter.query({"tool": f"{ALIAS}:search_docs", "args": {"query": term}})
            return re.findall(r"doc://(\S+)", obs.text or "") if obs.kind == "query" else []

        def read(doc_id):
            with seam(open_session):
                return adapter.read_section(f"{ALIAS}:doc://{doc_id}").text
        return policy.gather(question, search, read)
    return retrieve
