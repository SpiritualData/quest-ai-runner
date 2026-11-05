"""Link provenance: a link or file address in a reply stays only if the turn held it or its host is trusted. Offline, no model."""
from __future__ import annotations

from quest_ai_runner.the_intelligence.critical_thinking.response_quality.link_provenance_pass import strip_unseen_links

CONTEXT = ("Readable file sources (read-only): team server.\n"
           "File: reports/launch-summary.md. Address (already checked): https://files.example.test/reports/launch-summary.md\n")


def test_an_address_the_context_listed_is_kept():
    text = "Here it is: [launch summary](https://files.example.test/reports/launch-summary.md)."
    out, removed = strip_unseen_links(text, CONTEXT)
    assert out == text and removed == []


def test_an_invented_address_on_an_untrusted_host_is_replaced_by_its_label():
    out, removed = strip_unseen_links("See [the file](https://files.example.test/launch-summary) now.", CONTEXT)
    assert out == "See the file now."
    assert removed == ["https://files.example.test/launch-summary"]


def test_a_seen_host_does_not_vouch_for_an_invented_path():
    out, removed = strip_unseen_links("Open https://files.example.test/other.md today.", CONTEXT)
    assert "files.example.test" not in out and removed


def test_a_bare_invented_address_is_dropped_and_its_sentence_stays():
    out, removed = strip_unseen_links("The summary is at https://nowhere.example.com/x.", CONTEXT)
    assert out == "The summary is at."
    assert removed == ["https://nowhere.example.com/x"]


def test_a_file_address_the_context_never_held_is_removed():
    out, removed = strip_unseen_links("Open [it](file:///srv/reports/launch-summary.md) or [the copy](reports/copy.md).", CONTEXT)
    assert out == "Open it or the copy."
    assert len(removed) == 2


def test_a_relative_file_path_the_context_listed_becomes_plain_words_and_is_not_reported():
    assert strip_unseen_links("See [the file](reports/launch-summary.md).", CONTEXT) == ("See the file.", [])
    assert strip_unseen_links("See [](reports/launch-summary.md).", CONTEXT) == ("See reports/launch-summary.md.", [])


def test_a_trusted_host_is_kept_without_being_in_the_material():
    text = "Docs: [guide](https://docs.example.org/guide/start)."
    out, removed = strip_unseen_links(text, CONTEXT, trusted_hosts=("docs.example.org",))
    assert out == text and removed == []


def test_comparison_ignores_case_scheme_www_fragment_and_trailing_slash():
    out, removed = strip_unseen_links("[a](HTTP://WWW.Files.Example.test/reports/launch-summary.md#top)", CONTEXT)
    assert removed == [] and "launch-summary" in out


def test_in_app_routes_and_mail_links_are_left_to_the_link_guard():
    text = "Go to [tasks](/profile/ai-tasks), [jump](#top) or [write](mailto:a@example.org)."
    assert strip_unseen_links(text, "") == (text, [])


def test_text_without_links_is_unchanged():
    assert strip_unseen_links("No links here.", CONTEXT) == ("No links here.", [])
    assert strip_unseen_links("", CONTEXT) == ("", [])


def test_the_orchestrator_removes_an_invented_address_even_with_no_link_policy():
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    reply = ("Real: [one](https://files.example.test/reports/launch-summary.md). "
             "Invented: [two](https://invented.example.com/page) and file:///srv/x.md.")
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}], answer_text=reply)
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.link_guard = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=cfg)
    res = orch.run("show me the file", context_view=CONTEXT)
    assert "https://files.example.test/reports/launch-summary.md" in res.text
    assert "invented.example.com" not in res.text and "file:///" not in res.text
    assert {c["url"] for c in res.link_checks} == {"https://invented.example.com/page", "file:///srv/x.md"}


def test_the_switch_leaves_the_reply_untouched():
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "chit-chat"}],
                            answer_text="See [x](https://invented.example.com/page).")
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.link_guard = False
    cfg.link_provenance = False
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=cfg)
    assert "invented.example.com" in orch.run("hi").text
