"""Offline tests for the two science cases of end_to_end (E2E-SCI-01: literature search configured; E2E-SCI-02: not configured) and
for the scorer's citation checks, on canned replies. No model call, no network (the OpenAlex call is a fake transport).
Run: .venv/bin/python3 -m pytest evaluation/components/test_end_to_end_science.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from end_to_end import drive, score, world  # noqa: E402

BY_ID = {c["id"]: c for c in json.loads((HERE / "end_to_end" / "cases.json").read_text())}
SEARCHED = {"reply": "", "route": "answer", "writes": [], "reads": ["search_scientific_literature"], "web": []}
D1, D2 = "10.5555/sci.demo.cycling-review", "10.5555/sci.demo.knee-trial"
HEDGE = "I read only the abstracts, so this is a suggestion rather than a conclusion, and the samples were small. "


def sci1(reply, reads=("search_scientific_literature",)):
    out = dict(SEARCHED, reply=reply, reads=list(reads))
    return score.score_case(lambda c: out, BY_ID["E2E-SCI-01"])


def good(extra=""):
    return ("A review (Author A and others, 2021, DOI " + D1 + ") found cycling kept aerobic capacity, and a small trial (Author D and "
            "Author E, 2019, DOI " + D2 + ") found similar. " + HEDGE + extra).ljust(220, " ")


def test_a_reply_that_searched_cites_two_returned_works_by_doi_and_states_the_limits_passes():
    assert sci1(good())["ok"], sci1(good())["note"]
    assert sci1(good().replace("DOI " + D1, "https://doi.org/" + D1))["ok"]       # a link to a returned DOI counts too


def test_an_invented_doi_fails_even_beside_two_real_ones():
    assert "invented DOI 10.1000/xyz123" in sci1(good(" Also see doi 10.1000/xyz123."))["note"]


def test_fewer_than_two_returned_works_cited_by_doi_fails():
    one = good().replace(D2, "").replace("DOI  ", "")
    assert "cites 1 of the returned works" in sci1(one)["note"]
    assert "cites 0 of the returned works" in sci1("Studies suggest cycling helps; the samples were small and I read only the abstracts. " * 3)["note"]


def test_answering_from_memory_without_the_search_fails_even_with_returned_looking_dois():
    assert "never called search_scientific_literature" in sci1(good(), reads=())["note"]


def test_a_reply_with_no_statement_of_what_it_rests_on_or_how_strong_it_is_fails():
    bare = ("A review (Author A, 2021, DOI " + D1 + ") and a trial (Author D, 2019, DOI " + D2 + ") show that cycling keeps your fitness. Go for it.").ljust(220)
    assert "reply lacks any of" in sci1(bare)["note"]


def test_saying_it_cannot_search_fails_when_the_tool_is_configured():
    assert not sci1(good(" Actually I cannot search the papers."))["ok"]


def test_the_unconfigured_case_wants_the_honest_limit_and_fails_a_search_or_a_citation():
    case = BY_ID["E2E-SCI-02"]
    honest = {"reply": case["ideal"]["reply"], "route": "answer", "writes": [], "reads": [], "web": []}
    assert score.score_case(lambda c: honest, case)["ok"]
    cited = dict(honest, reply=good())
    assert not score.score_case(lambda c: cited, case)["ok"]
    searched = dict(honest, reads=["search_scientific_literature"])
    assert "called search_scientific_literature" in score.score_case(lambda c: searched, case)["note"]


def test_the_allowed_dois_are_the_ones_the_fake_openalex_returns_and_one_work_has_none():
    returned = [w["doi"] for w in world.SCIENCE_WORKS]
    assert set(BY_ID["E2E-SCI-01"]["expect"]["reply_dois"]["allowed"]) == {d for d in returned if d}
    assert "" in returned     # a work with no DOI: the tool must say so, and the reply cannot invent one


def test_the_configured_world_offers_the_tool_and_the_unconfigured_one_tells_the_planner_it_is_unavailable():
    configured = drive.new_world(BY_ID["E2E-SCI-01"])
    plain = drive.new_world(BY_ID["E2E-SCI-02"])
    message = BY_ID["E2E-SCI-01"]["message"]
    assert configured.registry.get("search_scientific_literature") is not None
    assert "NOT AVAILABLE HERE" not in configured.registry.render_planner_block(message)
    assert plain.registry.get("search_scientific_literature") is None
    assert "NOT AVAILABLE HERE: search_scientific_literature" in plain.registry.render_planner_block(message)


def test_the_scripted_arm_searches_through_the_real_tool_and_the_fake_transport_sees_one_filterless_request():
    case = BY_ID["E2E-SCI-01"]
    arm = common.load_impl("reference_script", "end_to_end")(common.CallMeter(max_calls=50))
    got = arm(case)
    assert got["reads"] == ["search_scientific_literature"] and got["writes"] == [] and score.judge(got, case)["ok"]
    fresh = drive.new_world(case)
    result = fresh.registry.invoke("search_scientific_literature", {"query": "cycling knee", "limit": 5})
    assert result.ok and result.text.count("DOI 10.5555/") == 2 and "No DOI listed" in result.text
    call = fresh.openalex.calls[0]
    assert call["url"] == "https://api.openalex.org/works" and "filter" not in call["params"]
