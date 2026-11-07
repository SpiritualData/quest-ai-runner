"""Quest's side of Resolve a Reference, offline: people and goals are tied to a phrase only when exactly one is meant, a look-alike is an abstain, and a match never licenses a write."""
import json

from quest_ai_runner.the_body.hands.quest_operations import goal_links
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import resolve_member
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import alternatives_flow
from quest_ai_runner.the_intelligence.mind.reference_resolver import quest_references as refs

MEMBERS = [{"user_id": "u1", "name": "Ann Lee", "email": "ann.lee@example.org"}, {"user_id": "u2", "name": "Joanne Park", "email": "jp@example.org"},
           {"user_id": "u3", "name": "Ann Marie Ortiz", "email": "amo@example.org"}, {"user_id": "u4", "name": "Dev Rao", "email": "dev@example.org"}]


def test_a_member_is_found_by_id_email_whole_name_or_the_words_of_one_name():
    assert [resolve_member(who, MEMBERS)[0] for who in ("u4", "jp@example.org", "jp", "Ann Lee", "park", "dev rao")] == ["u4", "u2", "u2", "u1", "u2", "u4"]


def test_a_first_name_two_people_share_a_part_of_a_name_and_an_unknown_name_name_nobody():
    for who in ("Ann", "Joan", "Priya", "Lee Park", "the content team", ""):
        user_id, why = resolve_member(who, MEMBERS)
        assert user_id is None and why, who
    assert "Ann Lee" in resolve_member("Ann", MEMBERS)[1]      # the reason lists the people it could have been


def test_the_goal_tool_resolves_the_assignee_through_the_same_solution():
    class Client:
        def list_assignable_members(self, quest_id):
            return MEMBERS
    assert goal_links.resolve_assignee(Client(), "q1", "Dev") == ("u4", None)
    user_id, why = goal_links.resolve_assignee(Client(), "q1", "Ann")
    assert user_id is None and why.startswith("assignee: ")


GOALS = [{"id": "g1", "title": "Newsletter subscribers import cleanup", "text": "Remove bounced addresses."}, {"id": "g2", "title": "Subscribers survey results", "text": "Summarise the survey."},
         {"id": "g3", "title": "Push post A", "text": "Post one in full"}]


def test_two_unrelated_goals_that_share_words_with_a_title_are_not_that_title():
    assert refs.goal_titled("Send the spring newsletter to subscribers", GOALS) is None
    assert refs.goal_titled("subscribers survey RESULTS", GOALS)["id"] == "g2"
    assert refs.goal_titled("Push post", GOALS) is None      # a part of a title is not the title


def test_an_id_the_model_was_not_offered_is_never_adopted():
    offered = [GOALS[2]]
    assert refs.goal_offered("g3", offered) == "g3" and refs.goal_offered("g1", offered) == "" and refs.goal_offered("", offered) == ""


def test_a_post_is_paired_only_when_the_model_names_a_goal_offered_and_never_on_an_unusable_answer():
    asked = []
    pick = lambda prompt: asked.append(prompt) or json.dumps({"id": "g3"})      # noqa: E731
    assert refs.post_goal("Post one in full", GOALS, pick)["id"] == "g3" and len(asked) == 1
    for answer in ('{"id": "NONE"}', '{"id": "g9"}', "no idea", ""):
        assert refs.post_goal("Post one in full", GOALS, lambda prompt, a=answer: a) is None
    assert refs.post_goal("x", [], pick) is None and len(asked) == 1      # no goal, no call


def test_same_posts_offers_a_goal_to_one_version_only_and_a_match_is_not_a_licence_to_write():
    alts = [alternatives_flow.Alternative("A", "Post one in full"), alternatives_flow.Alternative("B", "Post two in full")]
    paired = alternatives_flow.same_posts(alts, GOALS, lambda prompt: json.dumps({"id": "g3"}))
    assert list(paired) == [id(alts[0])] and paired[id(alts[0])]["id"] == "g3"      # the second version is not given the goal the first took
    assert goal_links.is_owned(GOALS[2]) is False      # the safety guard is the same: a paired goal that Quest AI does not keep is still never updated
    assert goal_links.is_owned({"title": "Push next step: week of 2026-11-02"}) and goal_links.is_owned({"description": goal_links.MARKER + " x"})
