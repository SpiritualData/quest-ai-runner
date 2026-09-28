"""Link guard: no reply leaves with a link nobody checked (core/link_guard.py).

Every test here is offline. The one network seam is ``LinkGuard(fetcher=...)``, so these run with
no sockets and no spend.
"""
from __future__ import annotations

import json

import pytest

from quest_ai_runner.core.link_guard import (
    DEAD,
    OK,
    UNKNOWN,
    LinkGuard,
    LinkPolicy,
    build_link_guard,
    route_to_regex,
    trim_url,
)


def fetcher_for(statuses):
    """A fake probe returning a canned (status, detail) per URL, and counting the calls."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        return statuses.get(url, (None, "timeout"))

    fetch.calls = calls
    return fetch


APP = LinkPolicy(
    routes=("/profile/ai-tasks", "/quest/[questId]/chat", "/quest", "/events/[slug]/[...rest]"),
    origins=("app.example.org",),
    trusted_hosts=("docs.example.org",),
    allowed_schemes=("app-task",),
    passes=2,
)


# --- route matching ---------------------------------------------------------------------------

def test_route_patterns_match_real_paths_and_reject_invented_ones():
    assert route_to_regex("/quest/[questId]/chat").match("/quest/abc123/chat")
    assert route_to_regex("/quest/[questId]/chat").match("/quest/abc123/chat/")
    assert not route_to_regex("/quest/[questId]/chat").match("/quest/abc123/chat/extra")
    assert route_to_regex("/events/[slug]/[...rest]").match("/events/webinar/a/b/c")
    assert route_to_regex("/").match("/")


def test_internal_path_that_exists_is_kept_and_one_that_does_not_is_stripped():
    guard = LinkGuard(APP, fetcher=fetcher_for({}))
    text = "See [your tasks](/profile/ai-tasks) and [the dashboard](/profile/dashboard)."
    out, verdicts = guard.sanitize(text)
    assert "[your tasks](/profile/ai-tasks)" in out
    assert "/profile/dashboard" not in out
    assert "the dashboard (link removed: that address does not exist)" in out
    assert {v.url: v.verdict for v in verdicts} == {
        "/profile/ai-tasks": OK, "/profile/dashboard": DEAD}


def test_absolute_url_on_the_apps_own_origin_is_judged_by_route_not_by_http():
    # A single-page app answers 200 for paths it has no screen for, so HTTP status proves nothing
    # about its own origin. The route table is the stronger check, and no fetch should happen.
    fetch = fetcher_for({})
    guard = LinkGuard(APP, fetcher=fetch)
    out, verdicts = guard.sanitize(
        "[tasks](https://app.example.org/profile/ai-tasks) and "
        "[nope](https://app.example.org/made/up/screen)")
    assert fetch.calls == []
    assert "https://app.example.org/profile/ai-tasks" in out
    assert "/made/up/screen" not in out
    assert [v.verdict for v in verdicts] == [OK, DEAD]


def test_query_string_and_fragment_do_not_break_a_valid_route():
    guard = LinkGuard(APP, fetcher=fetcher_for({}))
    out, _ = guard.sanitize("[task](/profile/ai-tasks?taskId=atask_2f875a184178#top)")
    assert "/profile/ai-tasks?taskId=atask_2f875a184178#top" in out


def test_with_no_route_table_an_internal_path_is_unverifiable_and_stripped():
    guard = LinkGuard(LinkPolicy(), fetcher=fetcher_for({}))
    out, verdicts = guard.sanitize("[tasks](/profile/ai-tasks)")
    assert "/profile/ai-tasks" not in out
    assert verdicts[0].verdict == UNKNOWN


# --- external URLs ----------------------------------------------------------------------------

def test_reachable_url_survives_and_a_404_is_stripped():
    fetch = fetcher_for({
        "https://real.example.com/a": (200, "HEAD"),
        "https://gone.example.com/b": (404, "HTTP 404"),
    })
    guard = LinkGuard(APP, fetcher=fetch)
    out, verdicts = guard.sanitize(
        "Read [the paper](https://real.example.com/a) and [the old one](https://gone.example.com/b).")
    assert "https://real.example.com/a" in out
    assert "gone.example.com" not in out
    assert "the old one (link removed: that address does not exist)" in out
    assert [v.verdict for v in verdicts] == [OK, DEAD]


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_gated_endpoint_counts_as_existing(status):
    guard = LinkGuard(APP, fetcher=fetcher_for({"https://api.example.com/x": (status, "")}))
    out, verdicts = guard.sanitize("[api](https://api.example.com/x)")
    assert verdicts[0].verdict == OK
    assert "https://api.example.com/x" in out


def test_a_host_that_does_not_resolve_is_dead():
    guard = LinkGuard(APP, fetcher=fetcher_for({"https://nope.invalid/x": (None, "dns")}))
    _, verdicts = guard.sanitize("[x](https://nope.invalid/x)")
    assert verdicts[0].verdict == DEAD
    assert verdicts[0].reason == "host does not resolve"


def test_a_trusted_host_is_accepted_without_any_network_call():
    fetch = fetcher_for({})
    guard = LinkGuard(APP, fetcher=fetch)
    out, verdicts = guard.sanitize("[docs](https://docs.example.org/guide)")
    assert fetch.calls == []
    assert verdicts[0].verdict == OK
    assert "https://docs.example.org/guide" in out


def test_an_unsettled_url_is_retried_across_passes_before_its_link_is_removed():
    # THE POINT OF MULTIPLE PASSES: one timeout must not delete a good link, so the guard reads the
    # same URL policy.passes times before the verdict sticks.
    fetch = fetcher_for({})  # every call times out
    guard = LinkGuard(LinkPolicy(passes=3), fetcher=fetch)
    out, verdicts = guard.sanitize("[slow](https://slow.example.com/x)")
    assert fetch.calls == ["https://slow.example.com/x"] * 3
    assert verdicts[0].verdict == UNKNOWN
    assert "slow (link removed: could not verify it)" in out


def test_a_verdict_is_cached_so_the_same_url_is_checked_once():
    fetch = fetcher_for({"https://real.example.com/a": (200, "HEAD")})
    guard = LinkGuard(APP, fetcher=fetch)
    guard.sanitize("[one](https://real.example.com/a)")
    guard.sanitize("[two](https://real.example.com/a)")
    assert fetch.calls == ["https://real.example.com/a"]


def test_slow_links_are_checked_concurrently_not_one_at_a_time():
    # THE POINT OF CONCURRENCY: a reply with several slow hosts must not take as long as checking
    # each of them back to back would.
    import time as _time

    def slow_fetch(url, timeout):
        _time.sleep(0.3)
        return 200, "HEAD"

    policy = LinkPolicy(passes=1, concurrency=8)
    guard = LinkGuard(policy, fetcher=slow_fetch)
    text = " ".join(f"[l{i}](https://h{i}.example.com/x)" for i in range(6))
    start = _time.monotonic()
    out, verdicts = guard.sanitize(text)
    elapsed = _time.monotonic() - start
    assert all(v.verdict == OK for v in verdicts)
    assert len(verdicts) == 6
    # Sequential would take ~1.8s (6 x 0.3s); concurrent finishes in about one link's time.
    assert elapsed < 1.0


def test_an_overall_time_budget_bounds_the_whole_sanitize_call():
    # A reply full of slow links must not hold up the turn for minutes: whatever has not settled
    # inside policy.overall_timeout is unknown, not waited on.
    import time as _time

    def slow_fetch(url, timeout):
        _time.sleep(0.5)
        return 200, "HEAD"

    policy = LinkPolicy(passes=1, concurrency=8, overall_timeout=0.05)
    guard = LinkGuard(policy, fetcher=slow_fetch)
    text = " ".join(f"[l{i}](https://h{i}.example.com/x)" for i in range(4))
    start = _time.monotonic()
    out, verdicts = guard.sanitize(text)
    elapsed = _time.monotonic() - start
    assert elapsed < 0.5
    assert all(v.verdict == UNKNOWN for v in verdicts)
    assert all("time limit" in v.reason for v in verdicts)
    assert "example.com" not in out


# --- what is and is not a link ------------------------------------------------------------------

def test_urls_inside_code_are_shown_not_offered_and_are_never_touched():
    guard = LinkGuard(APP, fetcher=fetcher_for({}))
    text = "Run `curl https://made.up.invalid/x` first.\n\n```\nGET https://also.made.up/y\n```\n"
    out, verdicts = guard.sanitize(text)
    assert out == text
    assert verdicts == []


def test_a_bare_url_is_checked_too_and_sentence_punctuation_is_not_part_of_it():
    fetch = fetcher_for({"https://real.example.com/a": (200, "HEAD")})
    guard = LinkGuard(APP, fetcher=fetch)
    out, _ = guard.sanitize("It is at https://real.example.com/a.")
    assert fetch.calls == ["https://real.example.com/a"]
    assert out == "It is at https://real.example.com/a."


def test_a_dead_bare_url_leaves_no_address_behind():
    guard = LinkGuard(APP, fetcher=fetcher_for({"https://gone.example.com/b": (404, "")}))
    out, _ = guard.sanitize("It is at https://gone.example.com/b, go look.")
    assert "gone.example.com" not in out
    assert "(link removed: that address does not exist)" in out


def test_trim_url_keeps_a_balanced_closing_paren():
    assert trim_url("https://e.com/a_(b)") == "https://e.com/a_(b)"
    assert trim_url("https://e.com/a),") == "https://e.com/a"


def test_an_in_app_scheme_the_consumer_declared_is_left_alone():
    guard = LinkGuard(APP, fetcher=fetcher_for({}))
    out, verdicts = guard.sanitize("[#atask_2f875a184178](app-task:atask_2f875a184178)")
    assert out == "[#atask_2f875a184178](app-task:atask_2f875a184178)"
    assert verdicts[0].verdict == OK


def test_a_mailto_address_is_kept_without_being_fetched():
    fetch = fetcher_for({})
    guard = LinkGuard(APP, fetcher=fetch)
    out, verdicts = guard.sanitize("Reach us at [support](mailto:support@example.org).")
    assert out == "Reach us at [support](mailto:support@example.org)."
    assert verdicts[0].verdict == OK
    assert fetch.calls == []


def test_a_tel_number_is_kept_without_being_fetched():
    # Quest AI often gives a person a number to call; tel: used to fall through to the catch-all
    # unrecognized-scheme branch and get stripped like a fabricated URL.
    fetch = fetcher_for({})
    guard = LinkGuard(APP, fetcher=fetch)
    out, verdicts = guard.sanitize("Call [the office](tel:+15551234567) any time.")
    assert out == "Call [the office](tel:+15551234567) any time."
    assert verdicts[0].verdict == OK
    assert fetch.calls == []


def test_a_label_that_is_itself_the_url_does_not_survive_as_text():
    guard = LinkGuard(APP, fetcher=fetcher_for({"https://gone.example.com/b": (404, "")}))
    out, _ = guard.sanitize("[https://gone.example.com/b](https://gone.example.com/b)")
    assert "gone.example.com" not in out


def test_an_angle_autolink_is_checked_like_any_other():
    guard = LinkGuard(APP, fetcher=fetcher_for({"https://gone.example.com/b": (404, "")}))
    out, verdicts = guard.sanitize("See <https://gone.example.com/b> for more.")
    assert verdicts[0].verdict == DEAD
    assert "gone.example.com" not in out


# --- rewrites: a known-wrong address becomes the right one ---------------------------------------

def test_a_rewrite_rule_redirects_a_known_wrong_address_instead_of_deleting_it():
    # The real incident this module exists for: the model linked the task-status API endpoint,
    # which 401s as JSON and is not a page. The right answer is the in-app task screen.
    policy = LinkPolicy(
        routes=("/profile/ai-tasks",),
        rewrites=((r"^https://ai\.example\.org/api/tasks/(atask_[0-9a-f]{12})$",
                   r"/profile/ai-tasks?taskId=\1"),),
    )
    guard = LinkGuard(policy, fetcher=fetcher_for({}))
    out, verdicts = guard.sanitize(
        "[Task](https://ai.example.org/api/tasks/atask_2f875a184178)")
    assert out == "[Task](/profile/ai-tasks?taskId=atask_2f875a184178)"
    assert verdicts[0].verdict == OK
    assert verdicts[0].replaced_with == "/profile/ai-tasks?taskId=atask_2f875a184178"


# --- whole-reply behaviour ------------------------------------------------------------------------

def test_the_sanitized_reply_contains_no_unverified_address_at_all():
    fetch = fetcher_for({
        "https://real.example.com/a": (200, "HEAD"),
        "https://gone.example.com/b": (404, ""),
    })
    guard = LinkGuard(APP, fetcher=fetch)
    reply = (
        "Here is what I found.\n\n"
        "- Source: [the paper](https://real.example.com/a)\n"
        "- Old: [removed page](https://gone.example.com/b)\n"
        "- Screen: [your tasks](/profile/ai-tasks)\n"
        "- Made up: [the settings hub](/settings/hub)\n"
        "- Bare: https://gone.example.com/b\n"
    )
    out, verdicts = guard.sanitize(reply)
    for bad in ("gone.example.com", "/settings/hub"):
        assert bad not in out
    for good in ("https://real.example.com/a", "/profile/ai-tasks"):
        assert good in out
    assert sum(1 for v in verdicts if v.verdict == OK) == 2


def test_max_urls_caps_the_work_and_the_extras_are_stripped_not_trusted():
    fetch = fetcher_for({})
    guard = LinkGuard(LinkPolicy(max_urls=2, passes=1), fetcher=fetch)
    text = " ".join(f"[l{i}](https://h{i}.example.com/x)" for i in range(5))
    out, verdicts = guard.sanitize(text)
    assert len(fetch.calls) == 2
    assert all(v.verdict != OK for v in verdicts)
    assert "example.com" not in out


def test_empty_and_link_free_text_is_returned_untouched():
    guard = LinkGuard(APP, fetcher=fetcher_for({}))
    assert guard.sanitize("") == ("", [])
    assert guard.sanitize("Nothing to click here.") == ("Nothing to click here.", [])


# --- policy loading --------------------------------------------------------------------------------

def test_policy_file_loads_routes_from_a_separate_generated_file(tmp_path):
    (tmp_path / "app-routes.json").write_text(
        json.dumps({"generated_from": "app/", "routes": ["/profile/ai-tasks"]}), encoding="utf-8")
    (tmp_path / "policy.json").write_text(json.dumps({
        "routes_file": "app-routes.json",
        "origins": ["app.example.org"],
        "rewrites": [{"match": "^http://x$", "replace": "https://x"}],
    }), encoding="utf-8")
    policy = LinkPolicy.from_file(str(tmp_path / "policy.json"))
    assert policy.routes == ("/profile/ai-tasks",)
    assert policy.origins == ("app.example.org",)
    assert policy.rewrites == (("^http://x$", "https://x"),)


def test_a_broken_policy_file_degrades_to_defaults_rather_than_raising(tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{not json", encoding="utf-8")
    assert LinkPolicy.from_file(str(bad)) is None
    guard = build_link_guard(str(bad))
    assert isinstance(guard, LinkGuard)
    assert guard.policy.routes == ()


def test_build_link_guard_returns_none_when_switched_off():
    assert build_link_guard("", enabled=False) is None


# --- wired into the orchestrator: the reply that LEAVES is the sanitized one -----------------------

def test_a_finished_answer_has_its_fabricated_link_stripped_before_it_is_emitted():
    """End to end through Orchestrator.run: the guard is the last thing to touch the words."""
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = ("Open [your tasks](/profile/ai-tasks), then check "
             "[the settings hub](/settings/hub).")
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    # Inject the guard directly: the policy file / env plumbing has its own tests above, and this
    # one is about finish() applying whatever guard it resolves.
    orch.link_guard_built = True
    orch.link_guard_instance = LinkGuard(APP, fetcher=fetcher_for({}))

    res = orch.run("where are my tasks?")

    assert res.kind == "answer"
    assert "/profile/ai-tasks" in res.text
    assert "/settings/hub" not in res.text
    assert "the settings hub (link removed: that address does not exist)" in res.text
    assert {c["url"]: c["verdict"] for c in res.link_checks} == {
        "/profile/ai-tasks": OK, "/settings/hub": DEAD}


def test_the_guard_can_be_switched_off_and_then_the_reply_is_untouched():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = "Check [the settings hub](/settings/hub)."
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.link_guard = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    res = orch.run("where?")
    assert "/settings/hub" in res.text
    assert res.link_checks == []


def test_http_probe_asks_for_html_like_the_browser_that_will_open_the_link(monkeypatch):
    """A proxied single-page app serves its screens only to a request that accepts HTML."""
    import urllib.request
    from quest_ai_runner.core import link_guard

    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["accept"] = req.get_header("Accept")
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert link_guard.http_probe("https://example.org/reports?x=1", 1.0) == (200, "HEAD")
    assert seen["accept"].startswith("text/html")
