"""A name nothing placed keeps the turn iterating while the hub's stop decision says continue: each pass uses a route the run had not used, a blocked claim is never accepted
with routes untried, and only the decision ends it (stop_met when placed, ask when every route was tried)."""
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.critical_thinking import names_pass


def run(terms, deep=None, wider_places=False, monkeypatch=None):
    calls = []

    def fake_ground(request, root, glossary, search=True, seconds=4.0, levels_up=5, depth_down=3):
        calls.append(("search", seconds))
        return [dict(t, grounded=True, basis="files", expansion="Zonal Queue Exchange", complete=True) if wider_places else dict(t) for t in terms]

    monkeypatch.setattr(names_pass, "ground_terms", fake_ground)
    orchestrator = SimpleNamespace(cfg=SimpleNamespace(private_path_roots=["/nonexistent-root"]), _has_deep_execution_capability=lambda: deep is not None)
    rs = SimpleNamespace(user_message="what is WVTQ", text="I do not know.", terms=[dict(t) for t in terms])
    out = names_pass.iterate_on_names(orchestrator, rs, None, deep_search=deep)
    return out, rs, calls


CUT = [{"term": "WVTQ", "grounded": False, "complete": False, "unsearched": ["big/"]}]
UNDEFINED = [{"term": "WVTQ", "grounded": False, "complete": True, "unsearched": []}]


def test_a_cut_search_is_not_proof_of_absence_so_the_wider_search_runs_and_can_place_the_name(monkeypatch):
    out, rs, calls = run(CUT, wider_places=True, monkeypatch=monkeypatch)
    assert out["placed"] and out["decision"]["decision"] == "stop_met" and calls and calls[0][1] == names_pass.WIDER_SECONDS
    assert rs.terms[0]["grounded"]


def test_every_route_is_tried_before_the_hub_lets_the_run_ask_the_person(monkeypatch):
    out, rs, calls = run(UNDEFINED, monkeypatch=monkeypatch)
    assert [p["route"] for p in out["passes"]] == [names_pass.WIDER]
    assert not out["placed"] and out["decision"]["decision"] == "ask" and out["decision"]["code"] == "blocked_exhausted"


def test_the_deep_search_is_a_route_the_run_must_try_when_it_can_run_and_its_own_verified_goal_places_the_name(monkeypatch):
    out, rs, calls = run(UNDEFINED, deep=lambda names: {"met": True, "text": "WVTQ is defined in docs/glossary.md as the weekly review queue."}, monkeypatch=monkeypatch)
    assert [p["route"] for p in out["passes"]] == [names_pass.WIDER, names_pass.DEEP]
    assert out["placed"] and rs.terms[0]["basis"] == "deep-run" and out["decision"]["decision"] == "stop_met"


def test_a_deep_search_that_did_not_meet_its_goal_places_nothing_and_the_run_then_asks(monkeypatch):
    out, rs, _ = run(UNDEFINED, deep=lambda names: {"met": False, "text": "found nothing"}, monkeypatch=monkeypatch)
    assert not out["placed"] and out["decision"]["decision"] == "ask" and not rs.terms[0]["grounded"]


def test_available_routes_name_the_deep_search_only_when_it_can_run():
    assert names_pass.DEEP in names_pass.available_routes(True) and names_pass.DEEP not in names_pass.available_routes(False)
