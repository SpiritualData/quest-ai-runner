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
    evidence_text,
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
def test_an_auth_gated_page_counts_as_existing(status):
    guard = LinkGuard(APP, fetcher=fetcher_for({
        "https://api.example.com/x": (status, "", {"content_type": "text/html"})}))
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


# --- "verified" means a page a person can open: the HTML-vs-JSON 401/403 rule ----------------------

@pytest.mark.parametrize("status", [401, 403])
def test_a_gated_answer_that_is_html_is_a_page_and_stays(status):
    """A sign-in wall or a private document is something a person can open."""
    guard = LinkGuard(APP, fetcher=fetcher_for({
        "https://docs.example.com/private": (status, f"HTTP {status}", {"content_type": "text/html; charset=utf-8"})}))
    out, verdicts = guard.sanitize("[the doc](https://docs.example.com/private)")
    assert verdicts[0].verdict == OK
    assert "https://docs.example.com/private" in out


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("ctype", ["application/json", "text/plain", ""])
def test_a_gated_answer_that_is_not_html_is_not_a_page_and_is_stripped(status, ctype):
    guard = LinkGuard(APP, fetcher=fetcher_for({
        "https://api.example.com/api/tasks/1": (status, f"HTTP {status}", {"content_type": ctype})}))
    out, verdicts = guard.sanitize("[task](https://api.example.com/api/tasks/1)")
    assert verdicts[0].verdict == DEAD
    assert "not a page" in verdicts[0].reason
    assert "api.example.com" not in out
    assert "task (link removed: that address is not a web page)" in out


def test_http_probe_reports_the_content_type_of_a_gated_answer_from_a_real_server():
    """No stub: a stdlib server on loopback answers 401 as JSON and 403 as an HTML page."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from quest_ai_runner.core.link_guard import http_probe

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api"):
                body, ctype, code = b'{"detail":"Not authenticated"}', "application/json", 401
            else:
                body, ctype, code = b"<html>Sign in</html>", "text/html; charset=utf-8", 403
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        guard = LinkGuard(LinkPolicy(passes=1, timeout=3.0))
        assert guard.check_external(base + "/api/tasks/1")[0] == DEAD
        assert guard.check_external(base + "/signin")[0] == OK
    finally:
        server.shutdown()


# --- provenance: the AI must have SEEN the link, not invented it ------------------------------------

PROV = LinkPolicy(
    routes=("/quest/[questId]", "/profile/ai-tasks"),
    origins=("app.example.org",),
    passes=1,
    require_provenance=True,
)
REACHABLE = {
    "https://seen.example.com/page": (200, "HEAD"),
    "https://invented.example.com/page": (200, "HEAD"),
}


def test_a_reachable_link_the_turn_never_saw_is_stripped():
    fetch = fetcher_for(REACHABLE)
    guard = LinkGuard(PROV, fetcher=fetch)
    out, verdicts = guard.sanitize(
        "See [it](https://invented.example.com/page).", evidence="the user asked about tasks")
    assert verdicts[0].verdict == UNKNOWN
    assert "invented.example.com" not in out
    assert "it (link removed: I did not find this address in anything I read)" in out
    assert fetch.calls == []   # never fetched: unseen is decided before the network


def test_a_link_that_appeared_in_the_turns_material_passes():
    guard = LinkGuard(PROV, fetcher=fetcher_for(REACHABLE))
    evidence = evidence_text("user: look at", {"web": [{"url": "https://seen.example.com/page"}]})
    out, verdicts = guard.sanitize("See [it](https://seen.example.com/page).", evidence=evidence)
    assert verdicts[0].verdict == OK
    assert "https://seen.example.com/page" in out


def test_provenance_ignores_case_scheme_www_fragment_and_trailing_slash():
    guard = LinkGuard(PROV, fetcher=fetcher_for({"https://seen.example.com/page": (200, "HEAD")}))
    out, _ = guard.sanitize("[a](https://seen.example.com/page#top)",
                            evidence="Source: HTTP://WWW.Seen.Example.com/Page/")
    assert "https://seen.example.com/page#top" in out


def test_a_seen_host_does_not_vouch_for_an_invented_path_on_it():
    guard = LinkGuard(PROV, fetcher=fetcher_for({"https://seen.example.com/page/deeper": (200, "HEAD")}))
    out, _ = guard.sanitize("[a](https://seen.example.com/page/deeper)",
                            evidence="https://seen.example.com/page")
    assert "seen.example.com" not in out


def test_in_app_routes_are_exempt_from_provenance():
    guard = LinkGuard(PROV, fetcher=fetcher_for({}))
    out, verdicts = guard.sanitize(
        "[quest](/quest/abc123) and [tasks](https://app.example.org/profile/ai-tasks?taskId=1)",
        evidence="")
    assert [v.verdict for v in verdicts] == [OK, OK]
    assert "/quest/abc123" in out and "app.example.org/profile/ai-tasks" in out


def test_without_evidence_or_with_the_flag_off_nothing_is_gated():
    # evidence=None: the caller has no material to offer (a deep worker's output), so exempt.
    guard = LinkGuard(PROV, fetcher=fetcher_for(REACHABLE))
    out, _ = guard.sanitize("[a](https://invented.example.com/page)")
    assert "https://invented.example.com/page" in out
    # Flag off (the library default): evidence is ignored.
    off = LinkGuard(LinkPolicy(passes=1), fetcher=fetcher_for(REACHABLE))
    out, _ = off.sanitize("[a](https://invented.example.com/page)", evidence="nothing")
    assert "https://invented.example.com/page" in out
    assert LinkPolicy().require_provenance is False
    assert LinkPolicy.from_dict({"require_provenance": True}).require_provenance is True


def test_a_seen_but_unreachable_link_is_still_stripped():
    guard = LinkGuard(PROV, fetcher=fetcher_for({"https://seen.example.com/page": (404, "HTTP 404")}))
    out, verdicts = guard.sanitize("[a](https://seen.example.com/page)",
                                   evidence="https://seen.example.com/page")
    assert verdicts[0].verdict == DEAD


def test_a_seen_link_the_host_would_not_confirm_is_kept_but_an_unseen_one_is_not():
    # 429 and a timeout are unsettled (unknown), not absent: a source the turn read stays a link.
    seen = "https://seen.example.com/page"
    for answer in ((429, "HTTP 429"), (503, "HTTP 503"), (None, "timeout")):
        guard = LinkGuard(PROV, fetcher=fetcher_for({seen: answer}))
        out, verdicts = guard.sanitize(f"See [it]({seen}).", evidence=seen)
        assert verdicts[0].verdict == OK, answer
        assert f"[it]({seen})" in out
    guard = LinkGuard(PROV, fetcher=fetcher_for({"https://invented.example.com/page": (429, "HTTP 429")}))
    out, _ = guard.sanitize("[x](https://invented.example.com/page)", evidence=seen)
    assert "invented.example.com" not in out
    # Without provenance an unsettled probe still strips (the earlier, safe reading).
    off = LinkGuard(LinkPolicy(passes=1), fetcher=fetcher_for({seen: (429, "HTTP 429")}))
    out, _ = off.sanitize(f"[it]({seen})", evidence=seen)
    assert seen not in out


def test_the_orchestrator_hands_the_turns_own_material_to_the_guard():
    """End to end: a link from the user's message and one from a gathered read pass; an invented
    one is stripped. The evidence comes from the run, not from a global."""
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = ("From you: [one](https://seen.example.com/page). Invented: "
             "[two](https://invented.example.com/page). Mine: [q](/quest/abc).")
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    orch.link_guard_built = True
    orch.link_guard_instance = LinkGuard(PROV, fetcher=fetcher_for(REACHABLE))

    res = orch.run("please summarize https://seen.example.com/page for me")

    assert "https://seen.example.com/page" in res.text
    assert "invented.example.com" not in res.text
    assert "/quest/abc" in res.text


def test_a_policy_object_on_the_config_builds_the_guard_without_a_file():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "x"}], answer_text="hi")
    cfg = OrchestratorConfig()
    cfg.link_policy = PROV
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=cfg)
    guard = orch.resolve_link_guard()
    assert guard is not None and guard.policy is PROV


# --- provenance for deep runs: model output is never evidence ----------------------------------

def deep_turn(runner, message="show me my docs"):
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

    from .test_deep_run_status_honesty import deep_plan
    from .test_per_goal_context_iteration import ScriptedProvider
    from .conftest import StubRetrieval

    provider = ScriptedProvider(plans=[deep_plan()], verdicts=[{"met": True}])
    cfg = OrchestratorConfig(deep_model_ladder=["haiku", "sonnet", "opus"])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner, config=cfg)
    orch.link_guard_built = True
    orch.link_guard_instance = LinkGuard(
        LinkPolicy(passes=1, require_provenance=True),
        fetcher=fetcher_for({"https://seen.example.com/a": (200, "HEAD"),
                             "https://invented.example.com/b": (200, "HEAD")}))
    return orch.run(message)


def test_an_in_process_runners_invented_link_is_stripped_and_cannot_vouch_for_itself():
    from quest_ai_runner.core.adapters_values import DeepResult

    from .test_deep_run_status_honesty import ScriptedRunner

    out = ("Docs: [seen](https://seen.example.com/a) and [made up](https://invented.example.com/b)")
    runner = ScriptedRunner([DeepResult(met=True, output=out,
                                        read_evidence="row: https://seen.example.com/a")],
                            uses_model=False)
    res = deep_turn(runner)
    text = res.deep_results[0].output
    assert res.deep_results[0].model_written is True
    assert "https://seen.example.com/a" in text      # what the runner itself read survives
    assert "invented.example.com" not in text        # its own words never vouch for a link


def test_a_real_claude_code_workers_output_keeps_its_exemption():
    from quest_ai_runner.core.adapters_values import DeepResult

    from .test_deep_run_status_honesty import ScriptedRunner

    runner = ScriptedRunner([DeepResult(met=True, output="[b](https://invented.example.com/b)")])
    res = deep_turn(runner)
    assert res.deep_results[0].model_written is False
    assert "https://invented.example.com/b" in res.deep_results[0].output


def test_link_evidence_excludes_model_written_deep_output_but_keeps_what_it_read():
    from types import SimpleNamespace

    from quest_ai_runner.core.adapters_values import DeepResult
    from quest_ai_runner.core.orchestrator_judges import JudgesMixin

    res = SimpleNamespace(deep_results=[
        DeepResult(met=True, output="https://model.example.com/x", model_written=True,
                   read_evidence="https://read.example.com/y"),
        DeepResult(met=True, output="https://worker.example.com/z"),
    ])
    rs = SimpleNamespace(user_message="u", transcript="", context_view="", gathered=None,
                         attachments=None, rep_preamble="persona https://persona.example.com/p",
                         context_meta={"link_evidence": ["https://extra.example.com/e"]})
    text = JudgesMixin.link_evidence_for(SimpleNamespace(), rs, res)
    assert "model.example.com" not in text
    for kept in ("read.example.com", "worker.example.com", "persona.example.com", "extra.example.com"):
        assert kept in text


# --- google documents match by document id ------------------------------------------------------

@pytest.mark.parametrize("seen,written", [
    ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv/edit?usp=sharing",
     "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv"),
    ("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUv/edit#gid=0",
     "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUv/view"),
    ("https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOpQrStUv/edit",
     "https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOpQrStUv/"),
    ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUv/view",
     "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOpQrStUv"),
])
def test_a_google_document_matches_by_id_whatever_the_suffix(seen, written):
    guard = LinkGuard(PROV, fetcher=fetcher_for({written: (200, "HEAD")}))
    out, verdicts = guard.sanitize(f"[doc]({written})", evidence=seen)
    assert verdicts[0].verdict == OK and written in out


def test_a_different_google_document_id_is_not_vouched_for():
    written = "https://docs.google.com/document/d/ZZZZZZZZZZZZZZZZZZZZZZ/edit"
    guard = LinkGuard(PROV, fetcher=fetcher_for({written: (200, "HEAD")}))
    out, _ = guard.sanitize(f"[doc]({written})",
                            evidence="https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv/edit")
    assert "ZZZZ" not in out


# --- route parameter validation hook ------------------------------------------------------------

def policy_with(validator):
    return LinkPolicy(routes=("/quest/[questId]", "/quest/[questId]/chat", "/profile/ai-tasks"),
                      origins=("app.example.org",), passes=1, route_validator=validator)


def test_route_validator_receives_pattern_and_params_and_can_reject():
    seen = []

    def validator(pattern, params):
        seen.append((pattern, dict(params)))
        return None if params["questId"] == "mine" else ("dead", "not a page you can open")

    guard = LinkGuard(policy_with(validator), fetcher=fetcher_for({}))
    out, verdicts = guard.sanitize(
        "[a](/quest/mine) [b](https://app.example.org/quest/theirs/chat) [t](/profile/ai-tasks)")
    assert {v.url: v.verdict for v in verdicts} == {
        "/quest/mine": OK, "https://app.example.org/quest/theirs/chat": DEAD,
        "/profile/ai-tasks": OK}
    assert "/quest/mine" in out and "theirs" not in out
    assert "not a page you can open" in out
    assert ("/quest/[questId]", {"questId": "mine"}) in seen
    assert ("/quest/[questId]/chat", {"questId": "theirs"}) in seen
    assert all(p != "/profile/ai-tasks" for p, _ in seen)   # no params, nothing to validate


def test_route_validator_failure_is_unknown_and_unmatched_routes_never_reach_it():
    def boom(pattern, params):
        raise RuntimeError("db down")

    guard = LinkGuard(policy_with(boom), fetcher=fetcher_for({}))
    _, verdicts = guard.sanitize("[a](/quest/x) [b](/nope/x)")
    assert {v.url: v.verdict for v in verdicts} == {"/quest/x": UNKNOWN, "/nope/x": DEAD}
