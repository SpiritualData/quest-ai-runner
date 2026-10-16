"""Whole-document web reads: ``full`` / ``part`` paging, the larger focused budget, cache sharing,
the planner/verifier views of a page read, and the read dispatch. Offline: a fake page fetcher."""
from types import SimpleNamespace
from typing import Any, Dict, Optional

from quest_ai_runner.adapters.web_cache import WebCache
from quest_ai_runner.adapters.web_research import (
    WebResearchAdapter,
    build_web_research_from_env,
    paginate_text,
)
from quest_ai_runner.adapters.web_search_backends import SearchResponse
from quest_ai_runner.core.orchestrator import VERIFY_WEB_EVIDENCE_MAX_CHARS
from quest_ai_runner.core.orchestrator_capabilities import describe_read_spec, read_spec_key
from quest_ai_runner.core.orchestrator_decisions import VERIFY_WEB_PAGE_EVIDENCE_MAX_CHARS
from quest_ai_runner.core.planner_context import (
    PLANNER_LINKS_HEAD,
    PLANNER_WEB_HEAD,
    PLANNER_WEB_PAGE_VIEW_CHARS,
    _render_gathered,
    _render_gathered_for_planner,
)

from .conftest import StubProvider
from .test_verify_web_evidence import verify_prompt
from .test_web_reads import FakeWeb, _orch


class FakeBackend:
    name = "fake"

    def search(self, query, *, max_results=5):
        return SearchResponse(hits=[], answer="")


def numbered_paragraphs(count: int) -> str:
    return "\n\n".join(
        f"Paragraph number {i:04d} says something distinct about topic {i:04d} and its details."
        for i in range(count))


def adapter_for(text: str, calls: Optional[Dict[str, int]] = None, **kwargs) -> WebResearchAdapter:
    def fetcher(url):
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        return SimpleNamespace(html=text, content_type="text/plain", status_code=200)

    return WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=fetcher, **kwargs)


URL = "https://example.com/long"


def test_defaults_are_the_larger_budgets(monkeypatch):
    monkeypatch.setenv("QAR_WEB_SEARCH_BACKEND", "searxng")
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    adapter = build_web_research_from_env({"QAR_WEB_SEARCH_BACKEND": "searxng",
                                           "SEARXNG_URL": "https://searx.example"})
    assert adapter._page_token_budget == 3000
    assert adapter._full_page_token_budget == 12000
    tuned = build_web_research_from_env({"QAR_WEB_SEARCH_BACKEND": "searxng",
                                         "SEARXNG_URL": "https://searx.example",
                                         "QAR_WEB_PAGE_TOKEN_BUDGET": "500",
                                         "QAR_WEB_PAGE_FULL_TOKEN_BUDGET": "2000"})
    assert (tuned._page_token_budget, tuned._full_page_token_budget) == (500, 2000)


def test_focused_read_is_larger_and_says_the_document_is_longer():
    text = numbered_paragraphs(2000)
    obs = adapter_for(text).fetch(URL, focus="topic 1500")
    assert "topic 1500" in obs.text
    assert len(obs.text) > 800 * 4  # more than the old 800 token budget could hold
    assert '"full": true' in obs.text


def test_short_document_focused_read_has_no_full_hint():
    obs = adapter_for(numbered_paragraphs(5)).fetch(URL, focus="topic 3")
    assert '"full": true' not in obs.text


def test_full_read_is_in_order_from_the_start_and_names_the_next_part():
    text = numbered_paragraphs(2000)
    adapter = adapter_for(text, full_page_token_budget=3000)
    obs = adapter.fetch(URL, full=True)
    assert obs.locator.startswith("web extract: ")
    assert "part 1 of" in obs.text
    assert '"part": 2' in obs.text
    body = obs.text.split("\n\n", 1)[1]
    assert body.startswith("Paragraph number 0000")
    assert len(body) <= 3000 * 4 + 50


def test_paging_reaches_the_end_and_covers_every_paragraph_once():
    text = numbered_paragraphs(1200)
    adapter = adapter_for(text, full_page_token_budget=2000)
    seen = []
    part = 1
    while True:
        obs = adapter.fetch(URL, part=part)  # part alone implies full
        seen.append(obs.text)
        if "(end of the document)" in obs.text:
            break
        assert f'"part": {part + 1}' in obs.text
        part += 1
        assert part < 100
    joined = "\n\n".join(t.split("\n\n", 1)[1] for t in seen)
    for i in (0, 599, 1199):
        assert f"Paragraph number {i:04d}" in joined
    assert joined.count("Paragraph number 0700") == 1
    assert len(seen) > 1


def test_part_out_of_range_is_named_not_an_error():
    adapter = adapter_for(numbered_paragraphs(5))
    obs = adapter.fetch(URL, part=9)
    assert obs.kind == "read"
    assert "does not exist" in obs.text
    assert "1 part" in obs.text


def test_short_document_full_read_is_one_part_with_end_marker():
    obs = adapter_for(numbered_paragraphs(5)).fetch(URL, full=True)
    assert "part 1 of 1" in obs.text
    assert "(end of the document)" in obs.text


def test_modes_and_parts_share_one_fetch_but_have_distinct_locators():
    calls: Dict[str, int] = {}
    adapter = adapter_for(numbered_paragraphs(1500), calls, full_page_token_budget=2000)
    focused = adapter.fetch(URL, focus="topic 0100")
    first = adapter.fetch(URL, full=True)
    second = adapter.fetch(URL, part=2)
    assert calls["n"] == 1
    assert len({focused.locator, first.locator, second.locator}) == 3


def test_paginate_text_cuts_at_paragraph_breaks_and_keeps_all_text():
    text = numbered_paragraphs(300)
    parts = paginate_text(text, 2000)
    assert len(parts) > 3
    assert all(len(p) <= 2000 for p in parts)
    assert all(p.startswith("Paragraph number") for p in parts)
    assert "".join(p.replace("\n", "").replace(" ", "") for p in parts) == text.replace(
        "\n", "").replace(" ", "")


def test_paginate_text_without_breaks_still_splits():
    parts = paginate_text("x" * 5000, 2000)
    assert [len(p) for p in parts] == [2000, 2000, 1000]


def test_dispatch_passes_full_and_part_only_when_asked():
    class Web(FakeWeb):
        def fetch(self, url, *, focus=None, fresh=False, full=False, part=None):
            self.fetch_calls.append({"url": url, "focus": focus, "fresh": fresh,
                                     "full": full, "part": part})
            return super().fetch(url, focus=focus, fresh=fresh)

    web = Web()
    orch = _orch(StubProvider([]), web=web)
    orch._exec_one_read({"web_page": "https://x.example/1", "focus": "a"})
    orch._exec_one_read({"web_page": "https://x.example/1", "full": True})
    orch._exec_one_read({"web_page": "https://x.example/1", "full": True, "part": 3})
    own = [c for c in web.fetch_calls if "full" in c]  # FakeWeb.fetch records a second entry
    assert own[0]["full"] is False and own[0]["part"] is None
    assert own[1]["full"] is True and own[1]["part"] is None
    assert own[2]["part"] == 3


def test_repeat_guard_treats_each_part_as_a_different_read():
    one = {"web_page": URL, "full": True}
    two = {"web_page": URL, "full": True, "part": 2}
    assert read_spec_key(one) != read_spec_key(two)
    assert describe_read_spec(two) != describe_read_spec(one)


def test_schema_and_prompts_expose_full_and_part():
    from quest_ai_runner.core.orchestrator_decide_tool import _WEB_READ_SPEC_FIELDS
    assert {"full", "part"} <= set(_WEB_READ_SPEC_FIELDS)
    assert '"full": true' in PLANNER_WEB_HEAD
    assert "summarize" in PLANNER_WEB_HEAD
    assert '"full": true' in PLANNER_LINKS_HEAD


def long_page_obs(chars: int, tail: str = "TAIL_FACT_999") -> Dict[str, Any]:
    return {"kind": "read", "rel_path": URL, "locator": f"web extract: {URL} (full, part 1 of 2)",
            "text": "A" * chars + tail}


def test_planner_view_shortens_a_page_but_the_answer_view_does_not():
    obs = long_page_obs(40000)
    planner = _render_gathered_for_planner([obs], recent_full=3, compress_over=5)
    answer = _render_gathered([obs])
    assert "planner view shortened" in planner
    assert len(planner) < PLANNER_WEB_PAGE_VIEW_CHARS + 600
    assert "TAIL_FACT_999" in answer and "planner view shortened" not in answer


def test_planner_view_leaves_other_reads_alone():
    obs = {"kind": "read", "rel_path": "notes.md", "locator": "notes.md", "text": "B" * 20000}
    assert "B" * 20000 in _render_gathered_for_planner([obs], recent_full=3, compress_over=5)


def test_verifier_sees_the_end_of_a_page_read():
    assert VERIFY_WEB_PAGE_EVIDENCE_MAX_CHARS > VERIFY_WEB_EVIDENCE_MAX_CHARS
    prompt = verify_prompt([long_page_obs(30000)])
    assert "TAIL_FACT_999" in prompt
    search_only = {"kind": "query", "rel_path": "web_search:q", "text": "C" * 20000 + "SEARCH_TAIL"}
    assert "SEARCH_TAIL" not in verify_prompt([search_only])
