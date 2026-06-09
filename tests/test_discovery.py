"""Discovery tools: the brain learns what sources/operations exist via list_*/describe_*.

These prove (1) discovery specs flow into the grounding through the same path as a read,
(2) several describe_* calls in one step fan out in parallel, (3) an adapter that predates
the discovery methods degrades gracefully (back-compat), and (4) the built-in FilesAdapter
enumerates real files + outlines.
"""
import tempfile
from pathlib import Path

from quest_ai_runner.adapters import FilesAdapter
from quest_ai_runner.core.adapters import Observation
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator

from .conftest import StubProvider, StubRetrieval


class DiscoveryStubRetrieval(StubRetrieval):
    """A retrieval adapter that records discovery calls and answers them."""

    def __init__(self, files=None):
        super().__init__(files)
        self.discovery_calls = []

    def list_sources(self):
        self.discovery_calls.append("list_sources")
        return Observation(kind="query", locator="list_sources",
                           text="- goals: the planning tree\n- quests: the user's quests")

    def describe_source(self, name, *, path=None):
        self.discovery_calls.append(f"describe_source:{name}")
        return Observation(kind="query", locator=f"describe_source({name})",
                           text=f"FIELDS OF {name}: id, title, completed")

    def list_operations(self):
        self.discovery_calls.append("list_operations")
        return Observation(kind="query", locator="list_operations",
                           text="- add_goal(...) creates a goal\n- get_insights(...) reads insights")

    def describe_operation(self, name):
        self.discovery_calls.append(f"describe_operation:{name}")
        return Observation(kind="query", locator=f"describe_operation({name})",
                           text=f"SIGNATURE OF {name}")


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


def test_list_sources_flows_into_grounding():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"list_sources": True}], "rationale": "what exists?"},
        {"action": "answer", "rationale": "now I know"},
    ])
    retrieval = DiscoveryStubRetrieval()
    res = _orch(provider, retrieval).run("what can you see?")
    assert res.kind == "answer"
    assert retrieval.discovery_calls == ["list_sources"]
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "the planning tree" in joined          # the listing reached the answer's grounding


def test_describe_specs_fan_out_in_parallel():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [
            {"describe_source": "goals"},
            {"describe_source": "quests"},
            {"list_operations": True},
        ], "rationale": "drill down"},
        {"action": "answer", "rationale": "have it"},
    ])
    retrieval = DiscoveryStubRetrieval()
    res = _orch(provider, retrieval).run("what fields do goals and quests have?")
    assert res.kind == "answer"
    assert set(retrieval.discovery_calls) == {
        "describe_source:goals", "describe_source:quests", "list_operations"}
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "FIELDS OF goals" in joined and "FIELDS OF quests" in joined


def test_describe_operation_drilldown():
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"list_operations": True}], "rationale": "catalog"},
        {"action": "read", "reads": [{"describe_operation": "get_insights"}], "rationale": "detail"},
        {"action": "answer", "rationale": "done"},
    ])
    retrieval = DiscoveryStubRetrieval()
    res = _orch(provider, retrieval).run("how do I get insights?")
    assert res.kind == "answer"
    assert retrieval.discovery_calls == ["list_operations", "describe_operation:get_insights"]
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "SIGNATURE OF get_insights" in joined


def test_discovery_on_legacy_adapter_degrades_gracefully():
    # A structural adapter with NO discovery methods (the back-compat case): a discovery spec
    # returns a benign "not supported" observation and the loop still answers — never raises.
    provider = StubProvider(decisions=[
        {"action": "read", "reads": [{"list_sources": True}], "rationale": "try discovery"},
        {"action": "answer", "rationale": "fallback"},
    ])
    retrieval = StubRetrieval({"README.md": "x"})   # plain stub, no list_sources
    res = _orch(provider, retrieval).run("what exists?")
    assert res.kind == "answer"
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "not supported" in joined


def test_files_adapter_discovery():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "guide.md").write_text("# Title\n\nintro\n\n## Setup\nsteps\n\n## Usage\nmore")
        (root / "notes.md").write_text("plain notes")
        fa = FilesAdapter(str(root))

        src = fa.list_sources()
        assert src.kind == "query"
        assert "guide.md" in src.text and "notes.md" in src.text

        desc = fa.describe_source("guide.md")
        assert "Setup" in desc.text and "Usage" in desc.text   # heading outline

        ops = fa.list_operations()
        assert "read_section" in ops.text and "grep" in ops.text

        # describe_operation: known op and unknown op both return query observations, never raise.
        read_op = fa.describe_operation("read_section")
        assert read_op.kind == "query" and "read_section" in read_op.text
        grep_op = fa.describe_operation("grep")
        assert grep_op.kind == "query" and "grep" in grep_op.text
        unknown = fa.describe_operation("nonexistent_op")
        assert unknown.kind == "query" and "nonexistent_op" in unknown.text

        # describe_source on a missing file returns a query observation, not an error.
        missing = fa.describe_source("no_such_file.md")
        assert missing.kind == "query" and "no_such_file.md" in missing.text


# ---------------------------------------------------------------------------
# CachedDbAdapter discovery
# ---------------------------------------------------------------------------

from quest_ai_runner.adapters.cached_db_adapter import CachedDbAdapter


def _make_db(rows_by_coll=None, *, sources=None, operations=None, describe=None):
    """Build a CachedDbAdapter whose fetch returns canned rows."""
    rows_by_coll = rows_by_coll or {}

    def fetch(coll, filt):
        return rows_by_coll.get(coll, [])

    return CachedDbAdapter(fetch, sources=sources, operations=operations, describe=describe)


def test_cached_db_list_sources_advertised():
    db = _make_db(sources={"goals": "the planning tree", "quests": "user quests"})
    obs = db.list_sources()
    assert obs.kind == "query"
    assert "goals" in obs.text and "the planning tree" in obs.text
    assert "quests" in obs.text


def test_cached_db_list_sources_from_cache():
    # When no sources are advertised, list_sources returns whatever collections the cache saw.
    db = _make_db({"insights": [{"id": 1}]})
    # Prime the cache by running a query first.
    db.query({"collection": "insights", "filter": {}})
    obs = db.list_sources()
    assert obs.kind == "query"
    assert "insights" in obs.text


def test_cached_db_list_sources_empty():
    # No advertised sources, no queries yet: returns a benign "no sources" message.
    db = _make_db()
    obs = db.list_sources()
    assert obs.kind == "query"
    assert obs.text  # non-empty, no exception


def test_cached_db_describe_source_with_custom_describe():
    describe_fn = lambda name: f"SCHEMA OF {name}: id str, title str"
    db = _make_db(describe=describe_fn)
    obs = db.describe_source("goals")
    assert obs.kind == "query"
    assert "SCHEMA OF goals" in obs.text


def test_cached_db_describe_source_infers_from_sample():
    # No custom describe fn: should infer fields from a sample row.
    db = _make_db({"plans": [{"id": "abc", "title": "my plan", "done": False}]})
    obs = db.describe_source("plans")
    assert obs.kind == "query"
    assert "id" in obs.text and "title" in obs.text and "done" in obs.text


def test_cached_db_describe_source_no_rows():
    # Empty collection: returns a benign "no rows" message.
    db = _make_db({"empty_coll": []})
    obs = db.describe_source("empty_coll")
    assert obs.kind == "query"
    assert "empty_coll" in obs.text


def test_cached_db_list_operations_default():
    db = _make_db()
    obs = db.list_operations()
    assert obs.kind == "query"
    assert "query" in obs.text


def test_cached_db_list_operations_custom():
    db = _make_db(operations="add_goal(title, notes) — creates a goal\nget_insights() — reads insights")
    obs = db.list_operations()
    assert obs.kind == "query"
    assert "add_goal" in obs.text and "get_insights" in obs.text


def test_cached_db_describe_operation():
    db = _make_db(operations="add_goal(title) — creates a goal")
    obs = db.describe_operation("add_goal")
    assert obs.kind == "query"
    # Points to the operations listing (no per-op registry by default).
    assert "add_goal" in obs.text


def test_cached_db_discovery_via_orchestrator():
    # Prove CachedDbAdapter discovery flows all the way through the orchestrator.
    provider = StubProvider(decisions=[
        {"action": "read",
         "reads": [{"list_sources": True}, {"list_operations": True}],
         "rationale": "discover"},
        {"action": "answer", "rationale": "done"},
    ])
    db = _make_db(
        sources={"goals": "user goals"},
        operations="add_goal(title) — creates a goal",
    )
    orch = _orch(provider, db)
    res = orch.run("what can I store and do?")
    assert res.kind == "answer"
    joined = "\n".join(m["content"] for m in provider.last_answer_messages)
    assert "user goals" in joined
    assert "add_goal" in joined
