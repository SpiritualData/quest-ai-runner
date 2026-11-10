"""``choose-the-context:digest_by_need``: a large body of records reaches the prompt as a few cited findings, never as raw entries. The model is a stub that
records its calls; nothing here spends anything."""
import json
import re
import threading

import pytest

import quest_ai_runner
from quest_ai_runner.the_intelligence.subconscious.digest_by_need import DEFAULTS, DigestByNeed, windows_of
from quest_ai_runner.the_intelligence.subconscious.digest_sources import MemoryDigestCache, SourceGroup, Unit

ENTRIES = 60
PADDING = "Written at the end of a long day with plenty of other words around it to make the record a realistic length for a journal."
MATTER = {7: "KEY knee pain after every 10k run", 23: "KEY I sleep better after a rest day", 41: "KEY Tuesday intervals felt great"}
NEED = "What has this person already tried or noticed that bears on running a marathon without pain?"
METRIC_NEED = "What does this person already measure or record that could show whether the plan works?"


class StubProvider:
    """Cites the records marked KEY for a need about what was tried, or LOG for a need about what is measured; a group named in ``fail_on`` raises."""

    def __init__(self, fail_on=()):
        self.calls, self.fail_on, self.lock = [], list(fail_on), threading.Lock()

    supports_web_search = False

    def plan(self, prompt, *, model, tool_schema, layers=None):
        raise NotImplementedError

    def list_models(self):
        return []

    def web_search(self, *args, **kwargs):
        raise NotImplementedError

    def answer(self, messages, *, model, system=None, layers=None):
        prompt = messages[0]["content"]
        with self.lock:
            self.calls.append({"prompt": prompt, "layers": layers, "model": model})
        if any(name in prompt for name in self.fail_on):
            raise RuntimeError("The AI could not answer just now.")
        marker = "LOG" if "measure or record" in prompt else "KEY"
        hits = re.findall(rf"\[(entry:[^\]]+)\] [^\n]*{marker}[^\n]*", prompt)
        if not hits:
            return json.dumps({"relevant": False, "reason": "These records do not bear on the need.", "findings": []})
        return "```json\n" + json.dumps({"relevant": True, "findings": [{"text": f"Found {marker} records ({len(hits)}) \u2014 noted.", "source_ids": hits + ["entry:invented"], "quote": ""}]}) + "\n```"


def collection(name="Training log", count=ENTRIES, prefix="entry", text_of=None):
    def text(n):
        return text_of(n) if text_of else MATTER.get(n) or (f"LOG distance {n} km" if n % 20 == 3 else f"filler entry number {n} about the weather and errands")
    return SourceGroup(f"collection:{name}", name, [Unit(f"{prefix}:{n}", f"Entry {n}", f"{text(n)}. {PADDING}") for n in range(count)])


def call(provider, groups, need=NEED, cache=None, **meta):
    arm = DigestByNeed(provider, cache=cache)
    return arm.choose(need, meta={"source_groups": groups, **meta})


def test_sixty_entries_reach_the_prompt_as_three_cited_findings():
    provider = StubProvider()
    context = call(provider, [collection()])
    cited = set(re.findall(r"entry:\d+", context.context_view))
    assert cited == {f"entry:{n}" for n in MATTER} and "entry:invented" not in context.context_view
    assert "filler entry number" not in context.context_view and "knee pain" not in context.context_view and len(context.context_view) < 1500
    assert "\u2014" not in context.context_view and set(context.card_ids) == cited and {m["id"] for m in context.card_metadata} == cited and all(m["title"] for m in context.card_metadata)
    assert len(provider.calls) >= 2 and all(len(c["prompt"]) < 8500 for c in provider.calls)
    first = provider.calls[0]["layers"]
    assert first[0]["cache"] is True and first[-1]["cache"] is False and NEED in first[0]["text"] and "\u2014" not in provider.calls[0]["prompt"]
    group = [s for s in context.sources if s["label"] == "Training log"][0]
    assert group["status"] == "analysed" and group["units_read"] == group["units_total"] == ENTRIES and context.partial is False


def test_the_second_call_hits_the_cache_and_reads_nothing_again():
    provider, cache = StubProvider(), MemoryDigestCache()
    first = call(provider, [collection()], cache=cache)
    spent = len(provider.calls)
    again = call(provider, [collection()], cache=cache)
    assert len(provider.calls) == spent and again.context_view == first.context_view
    coverage = [s for s in again.sources if s["label"] == "coverage"][0]
    assert coverage["analyses"] == 0 and coverage["reused"] == spent
    assert [s for s in again.sources if s["label"] == "Training log"][0]["status"] == "cached"


def test_a_changed_record_is_read_again_and_the_unchanged_windows_are_not():
    provider, cache = StubProvider(), MemoryDigestCache()
    call(provider, [collection()], cache=cache)
    spent = len(provider.calls)
    def edited(n):
        return "KEY changed record" if n == 59 else MATTER.get(n) or (f"LOG distance {n} km" if n % 20 == 3 else f"filler entry number {n} about the weather and errands")
    context = call(provider, [collection(text_of=edited)], cache=cache)
    assert len(provider.calls) - spent == 1 and "entry:59" in context.context_view


def test_different_needs_make_different_digests_from_the_same_records():
    provider, cache = StubProvider(), MemoryDigestCache()
    tried = call(provider, [collection()], NEED, cache)
    spent = len(provider.calls)
    measured = call(provider, [collection()], METRIC_NEED, cache)
    assert tried.context_view != measured.context_view and "KEY" in tried.context_view and "LOG" in measured.context_view
    assert len(provider.calls) == 2 * spent


def test_a_failed_group_is_reported_and_the_call_still_answers():
    provider = StubProvider(fail_on=["BROKEN-GROUP"])
    broken = collection("Broken group", 12, "bg", text_of=lambda n: f"BROKEN-GROUP record {n} with enough words to count")
    context = call(provider, [collection(), broken])
    status = {s["source_group"]: s for s in context.sources if "source_group" in s}
    assert status["collection:Broken group"]["status"] == "failed" and status["collection:Broken group"]["reason"] == "The AI could not answer just now."
    assert status["collection:Training log"]["status"] == "analysed" and context.partial is True and "entry:" in context.context_view


def test_a_group_that_cannot_be_loaded_is_failed_without_failing_the_call():
    def loader():
        raise OSError("disk gone")
    context = call(StubProvider(), [collection(), SourceGroup("notes", "Notes", loader=loader)])
    status = {s["source_group"]: s for s in context.sources if "source_group" in s}
    assert status["notes"]["status"] == "failed" and "OSError" in status["notes"]["reason"] and "entry:7" in context.context_view


def test_the_budget_of_analyses_skips_the_rest_and_says_so():
    provider = StubProvider()
    context = call(provider, [collection()], max_analyses=1)
    group = [s for s in context.sources if s["label"] == "Training log"][0]
    assert len(provider.calls) == 1 and group["status"] == "skipped" and "budget" in group["reason"] and context.partial is True


def test_a_body_small_enough_is_handed_on_whole_and_costs_no_call():
    provider = StubProvider()
    context = call(provider, [collection(count=3)])
    assert provider.calls == [] and "[entry:1]" in context.context_view
    assert [s["status"] for s in context.sources if "source_group" in s] == ["direct"] and context.card_ids == ["entry:0", "entry:1", "entry:2"]
    off = call(provider, [collection(count=3)], direct_chars=0)
    assert len(provider.calls) == 1 and "[entry:1]" not in off.context_view


def test_windows_are_bounded_and_every_window_keeps_its_records_in_order():
    windows = windows_of(collection().units, DEFAULTS)
    assert len(windows) >= 2 and all(sum(len(u.text) for u in w) <= DEFAULTS["window_chars"] for w in windows)
    assert [u.source_id for w in windows for u in w] == [f"entry:{n}" for n in range(ENTRIES)]


def test_no_model_reports_every_unread_group_skipped():
    context = DigestByNeed(None).choose(NEED, meta={"source_groups": [collection()]})
    assert [s["status"] for s in context.sources if "source_group" in s] == ["skipped"] and context.context_view == "" and context.partial is True


def test_the_one_line_entry_reaches_the_arm_with_the_stubbed_model():
    provider = StubProvider()
    context = quest_ai_runner.run("choose-the-context", {"task_text": NEED, "meta": {"source_groups": [collection()]}}, environ={},
                                  arms={"choose-the-context": "digest_by_need"}, provided={"vendor-model-caller": provider})
    assert set(context.card_ids) == {f"entry:{n}" for n in MATTER} and provider.calls
