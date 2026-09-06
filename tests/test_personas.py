"""personas — the configurable persona resolver: registry shapes, the policy steps, precedence.

Offline: the only "model" is a scripted stand-in provider that records every call, and the only
"Quest client" is an in-memory stub. Two real-world policies are proven here side by side:

  * a STRUCTURAL lane (no provider, no cards): the task names its owner in a field, an unknown id
    is auto-registered as a real skill or parked in a per-id cache dir;
  * a CHARACTER lane: a persona activates only when asked for — a structured field, an LLM judging
    an explicit ask, or dominant domain cards. The load-bearing rule proven in several tests below
    is that a BARE NAME MENTION never activates anybody: naming somebody is not asking them.
"""
import json

import pytest

from quest_ai_runner.runner.personas import (
    DEFAULT_ASSIGNMENT_FIELDS,
    PersonaEntry,
    PersonaRegistry,
    PersonaResolverConfig,
    build_persona_resolver,
    card_activation,
    slugify,
    unique_slug,
)


class RecordingProvider:
    """A ModelProvider stand-in: replays one scripted verdict and records every call it got."""

    def __init__(self, verdict=None, raises=False):
        self.verdict = verdict
        self.raises = raises
        self.calls = []

    def list_models(self):
        return ["claude-haiku", "claude-sonnet"]

    def answer(self, messages, model=None, **kwargs):
        self.calls.append({"messages": messages, "model": model})
        if self.raises:
            raise RuntimeError("provider is down")
        return self.verdict


class StubQuestClient:
    """The two Quest surfaces the resolver touches: profile lookup and linked-quest names."""

    def __init__(self, profiles=None, quests=None, raises=False):
        self.profiles = dict(profiles or {})
        self.quests = dict(quests or {})
        self.raises = raises
        self.profile_calls = []

    def get_ai_profile(self, user_id, *, team_id=None):
        self.profile_calls.append((user_id, team_id))
        if self.raises:
            raise RuntimeError("quest api is down")
        return dict(self.profiles.get(user_id) or {})

    def get_my_quest(self, quest_id):
        if self.raises:
            raise RuntimeError("quest api is down")
        return dict(self.quests.get(quest_id) or {})


RICH_REGISTRY = {
    "sage": {"rep_id": "rep_sage_1", "skill": "sage", "display_name": "Sage the Guide",
             "aliases": ["the guide"]},
    "scout": {"rep_id": "rep_scout_2", "skill": "scout"},
}

FLAT_REGISTRY = {"u_alpha": "alpha-rep", "u_beta": "beta-rep"}


def write_registry(tmp_path, data, name="registry.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


# --- registry normalization -----------------------------------------------------

def test_rich_registry_shape_takes_id_from_rep_id_and_slug_from_skill(tmp_path):
    reg = PersonaRegistry.from_file(write_registry(tmp_path, RICH_REGISTRY))
    entry = reg.by_id("rep_sage_1")
    assert entry is not None
    assert entry.slug == "sage"
    assert entry.display_name == "Sage the Guide"
    # The entry KEY is the persona's name, so it stays matchable even beside a display_name.
    assert reg.match("sage") is entry
    assert reg.match("the guide") is entry


def test_rich_registry_infers_display_name_from_the_key_when_omitted(tmp_path):
    reg = PersonaRegistry.from_file(write_registry(tmp_path, RICH_REGISTRY))
    assert reg.by_id("rep_scout_2").display_name == "scout"


def test_flat_registry_shape_keys_are_ids_and_values_are_slugs(tmp_path):
    reg = PersonaRegistry.from_file(write_registry(tmp_path, FLAT_REGISTRY))
    assert set(reg.entries) == {"u_alpha", "u_beta"}
    assert reg.by_id("u_alpha").slug == "alpha-rep"
    assert reg.by_id("u_alpha").display_name == ""
    assert reg.match("u_beta").slug == "beta-rep"


def test_registry_from_file_never_raises_on_a_missing_or_invalid_file(tmp_path):
    assert not PersonaRegistry.from_file(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all", encoding="utf-8")
    assert not PersonaRegistry.from_file(bad)
    listed = tmp_path / "list.json"
    listed.write_text('["not", "a", "mapping"]', encoding="utf-8")
    assert not PersonaRegistry.from_file(listed)
    assert not PersonaRegistry.from_file(None)


def test_registry_skips_unusable_rows_but_keeps_the_good_ones(tmp_path):
    path = write_registry(tmp_path, {"good": {"rep_id": "r1", "skill": "good"},
                                     "bad": 17, "": "orphan"})
    reg = PersonaRegistry.from_file(path)
    assert list(reg.entries) == ["r1"]


# --- matching is EXACT, never a substring ---------------------------------------

def test_match_is_exact_and_case_insensitive_never_substring(tmp_path):
    reg = PersonaRegistry.from_file(write_registry(tmp_path, RICH_REGISTRY))
    assert reg.match("SAGE").slug == "sage"
    assert reg.match("Sage the Guide").slug == "sage"
    # A name mentioned inside a sentence is NOT a match — this is the load-bearing rule.
    assert reg.match("please ask sage about the roadmap") is None
    assert reg.match("sagebrush") is None
    assert reg.match("") is None
    assert reg.match(None) is None


# --- step 1: structured assignment ----------------------------------------------

def test_structured_field_resolves_without_any_model_call(tmp_path):
    provider = RecordingProvider(verdict='{"persona": "scout"}')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=provider)
    assert resolver({"assignee_rep_id": "rep_sage_1", "text": "write the brief"}) == (
        "rep_sage_1", str(tmp_path / "skills" / "sage"))
    assert provider.calls == []          # step 1 short-circuits before the judge


def test_structured_field_accepts_any_identifier_of_the_persona(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY)))
    for field, value in (("persona", "sage"), ("character", "Sage the Guide"),
                         ("handled_by", "the guide"), ("assignee", "rep_sage_1")):
        assert resolver({field: value})[0] == "rep_sage_1"


def test_assignment_fields_are_read_in_order(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY)))
    # assignee_user_id (the rep the task is FOR) beats user_id (whoever filed it).
    assert DEFAULT_ASSIGNMENT_FIELDS.index("assignee_user_id") < \
        DEFAULT_ASSIGNMENT_FIELDS.index("user_id")
    got = resolver({"assignee_user_id": "u_beta", "user_id": "u_alpha"})
    assert got == ("u_beta", str(tmp_path / "skills" / "beta-rep"))


# --- step 2: the LLM-judged explicit ask ----------------------------------------

def test_explicit_ask_judged_by_the_model_resolves_a_persona(tmp_path):
    provider = RecordingProvider(verdict='{"persona": "sage"}')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=provider)
    got = resolver({"text": "have sage draft the quarterly note"})
    assert got == ("rep_sage_1", str(tmp_path / "skills" / "sage"))
    assert len(provider.calls) == 1


def test_a_bare_name_mention_activates_nobody(tmp_path):
    """The judge says null for a mere mention, and nothing downstream rescues it."""
    provider = RecordingProvider(verdict='{"persona": null}')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=provider)
    assert resolver({"text": "summarise what sage said in the meeting"}) is None


def test_judge_verdict_is_parsed_out_of_a_markdown_fence(tmp_path):
    provider = RecordingProvider(verdict='```json\n{"persona": "scout"}\n```')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=provider)
    assert resolver({"text": "scout, please map the options"})[0] == "rep_scout_2"


def test_judge_gets_the_linked_quest_names_as_context(tmp_path):
    provider = RecordingProvider(verdict='{"persona": null}')
    client = StubQuestClient(quests={"q1": {"name": "Autumn planning"}})
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=provider, quest_client=client)
    resolver({"text": "draft the plan", "goal_id": "q1"})
    assert "Autumn planning" in provider.calls[0]["messages"][0]["content"]


def test_judge_is_never_called_when_the_step_is_off(tmp_path):
    provider = RecordingProvider(verdict='{"persona": "sage"}')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY)),
        provider=provider)
    assert resolver({"text": "have sage draft the quarterly note"}) is None
    assert provider.calls == []          # the structural lane pays nothing for a step it is not using


def test_judge_is_skipped_when_no_provider_was_passed(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True))
    assert resolver({"text": "have sage draft the quarterly note"}) is None


# --- step 3: domain-card activation ---------------------------------------------

def write_card(cards_dir, card_id, keywords, **extra):
    cards_dir.mkdir(parents=True, exist_ok=True)
    payload = {"id": card_id, "keywords": list(keywords)}
    payload.update(extra)
    (cards_dir / f"{card_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def build_card_registry():
    return PersonaRegistry.from_mapping({
        "sage": {"rep_id": "rep_sage_1", "skill": "sage"},
        "scout": {"rep_id": "rep_scout_2", "skill": "scout"},
    })


def test_dominant_domain_cards_activate_a_persona(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "persona-sage-domain", ["mentoring", "coaching", "accountability"])
    write_card(cards, "persona-scout-domain", ["mapping", "terrain"])
    reg = build_card_registry()
    assert card_activation(reg, "plan the coaching and accountability programme",
                           cards).id == "rep_sage_1"


def test_cards_do_not_activate_on_a_bare_name_mention(tmp_path):
    """A persona's own name/aliases are excluded from card scoring, so a mention scores zero."""
    cards = tmp_path / "cards"
    write_card(cards, "persona-sage-domain", ["sage", "coaching"])
    reg = build_card_registry()
    assert card_activation(reg, "sage sent a note yesterday", cards) is None


def test_cards_need_dominance_not_just_a_hit(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "persona-sage-domain", ["coaching", "budget"])
    write_card(cards, "persona-scout-domain", ["terrain", "budget"])
    reg = build_card_registry()
    # One hit each: neither reaches min_hits, and neither dominates the other.
    assert card_activation(reg, "the budget", cards) is None
    # Two hits each clears min_hits but ties, and a tie is not >= 2x the runner-up.
    assert card_activation(reg, "the budget and coaching and terrain", cards) is None


def test_a_card_may_name_its_persona_outright(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "domain-card-7", ["telemetry", "dashboards"], persona="scout")
    reg = build_card_registry()
    assert card_activation(reg, "the telemetry dashboards need work", cards).id == "rep_scout_2"


def test_card_activation_is_off_unless_both_the_flag_and_a_dir_are_set(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "persona-sage-domain", ["mentoring", "coaching", "accountability"])
    text = "plan the coaching and accountability programme"
    reg_file = write_registry(tmp_path, RICH_REGISTRY)
    off = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"), registry_file=reg_file, cards_dir=str(cards)))
    assert off({"text": text}) is None
    no_dir = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"), registry_file=reg_file, card_activation=True))
    assert no_dir({"text": text}) is None
    on = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"), registry_file=reg_file,
        card_activation=True, cards_dir=str(cards)))
    assert on({"text": text}) == ("rep_sage_1", str(tmp_path / "skills" / "sage"))


# --- precedence between the steps -----------------------------------------------

def test_structured_beats_judge_beats_cards(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "persona-scout-domain", ["mapping", "terrain", "options"])
    reg_file = write_registry(tmp_path, RICH_REGISTRY)
    provider = RecordingProvider(verdict='{"persona": "sage"}')
    cfg = PersonaResolverConfig(skills_root=str(tmp_path / "skills"), registry_file=reg_file,
                                llm_explicit_ask=True, card_activation=True,
                                cards_dir=str(cards))
    resolver = build_persona_resolver(cfg, provider=provider)
    task = {"text": "map the terrain and the mapping options"}

    # cards alone would pick scout; the judge says sage; a structured field says scout again.
    assert card_activation(PersonaRegistry.from_file(reg_file), task["text"],
                           cards).id == "rep_scout_2"
    assert resolver(dict(task))[0] == "rep_sage_1"                       # judge beats cards
    assert provider.calls                                                # ...and it was consulted
    provider.calls.clear()
    assert resolver(dict(task, rep_id="rep_scout_2"))[0] == "rep_scout_2"  # field beats judge
    assert provider.calls == []


def test_cards_run_only_after_the_judge_declines(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "persona-scout-domain", ["mapping", "terrain", "options"])
    provider = RecordingProvider(verdict='{"persona": null}')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True, card_activation=True,
                              cards_dir=str(cards)),
        provider=provider)
    assert resolver({"text": "map the terrain and the mapping options"})[0] == "rep_scout_2"
    assert len(provider.calls) == 1


# --- step 4: the structural fallback --------------------------------------------

def test_unknown_id_lands_in_the_cache_dir_when_auto_register_is_off(tmp_path):
    resolver = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"),
        registry_file=write_registry(tmp_path, FLAT_REGISTRY),
        cache_dir=str(tmp_path / "cache")))
    assert resolver({"assignee_user_id": "u_unknown"}) == (
        "u_unknown", str(tmp_path / "cache" / "u_unknown"))
    # ...and nothing was written into the skills root.
    assert not (tmp_path / "skills").exists()


def test_unknown_id_without_cache_dir_or_auto_register_resolves_to_nothing(tmp_path):
    resolver = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"),
        registry_file=write_registry(tmp_path, FLAT_REGISTRY)))
    assert resolver({"assignee_user_id": "u_unknown"}) is None


def test_a_task_with_no_assignment_field_at_all_resolves_to_nothing(tmp_path):
    resolver = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"),
        registry_file=write_registry(tmp_path, FLAT_REGISTRY),
        cache_dir=str(tmp_path / "cache")))
    assert resolver({"text": "just some prose"}) is None


def test_auto_register_creates_a_valid_skill_and_persists_the_slug(tmp_path):
    reg_file = write_registry(tmp_path, FLAT_REGISTRY)
    client = StubQuestClient(profiles={"u_new": {"display_name": "River Stone"}})
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"), registry_file=reg_file,
                              auto_register=True),
        quest_client=client, team_id="team_x")

    ident, skill_dir = resolver({"assignee_user_id": "u_new"})
    assert ident == "u_new"
    assert skill_dir == str(tmp_path / "skills" / "river-stone")

    skill_md = (tmp_path / "skills" / "river-stone" / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\n")
    assert "name: river-stone" in skill_md          # a VALID Claude skill: name + description
    assert "description: Act as the River Stone AI representative." in skill_md
    assert "QAR:MANAGED:persona START" in skill_md  # ...ready for the next rep_sync pull

    assert json.loads((tmp_path / "registry.json").read_text())["u_new"] == "river-stone"
    assert client.profile_calls == [("u_new", "team_x")]


def test_auto_registered_persona_is_reused_without_re_registering(tmp_path):
    client = StubQuestClient(profiles={"u_new": {"display_name": "River Stone"}})
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY),
                              auto_register=True),
        quest_client=client)
    first = resolver({"assignee_user_id": "u_new"})
    second = resolver({"assignee_user_id": "u_new"})
    assert first == second
    assert len(client.profile_calls) == 1           # the second task hit the registry, not the API


def test_auto_register_falls_back_to_the_id_when_the_profile_lookup_fails(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY),
                              auto_register=True),
        quest_client=StubQuestClient(raises=True))
    ident, skill_dir = resolver({"assignee_user_id": "u_new"})
    assert (ident, skill_dir) == ("u_new", str(tmp_path / "skills" / "u-new"))
    assert (tmp_path / "skills" / "u-new" / "SKILL.md").exists()


def test_auto_register_keeps_a_rich_registry_file_rich(tmp_path):
    reg_file = write_registry(tmp_path, RICH_REGISTRY)
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"), registry_file=reg_file,
                              auto_register=True),
        quest_client=StubQuestClient(profiles={"rep_new": {"display_name": "Ivy"}}))
    resolver({"assignee_rep_id": "rep_new"})
    written = json.loads((tmp_path / "registry.json").read_text())
    assert written["ivy"] == {"rep_id": "rep_new", "skill": "ivy", "display_name": "Ivy"}
    assert written["sage"] == RICH_REGISTRY["sage"]          # existing rows untouched


def test_auto_register_never_clobbers_a_human_authored_skill_file(tmp_path):
    (tmp_path / "skills" / "ivy").mkdir(parents=True)
    (tmp_path / "skills" / "ivy" / "SKILL.md").write_text("mine\n", encoding="utf-8")
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY),
                              auto_register=True),
        quest_client=StubQuestClient(profiles={"u_new": {"display_name": "Ivy"}}))
    _ident, skill_dir = resolver({"assignee_user_id": "u_new"})
    # The taken slug is sidestepped, so nothing existing is overwritten.
    assert skill_dir != str(tmp_path / "skills" / "ivy")
    assert (tmp_path / "skills" / "ivy" / "SKILL.md").read_text() == "mine\n"


def test_auto_register_is_a_no_op_persist_without_a_registry_file(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"), auto_register=True),
        quest_client=StubQuestClient(profiles={"u_new": {"display_name": "Ivy"}}))
    ident, skill_dir = resolver({"assignee_user_id": "u_new"})
    assert (ident, skill_dir) == ("u_new", str(tmp_path / "skills" / "ivy"))
    assert not (tmp_path / "registry.json").exists()


# --- never raising --------------------------------------------------------------

def test_a_raising_provider_yields_no_persona_not_an_exception(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=RecordingProvider(raises=True))
    assert resolver({"text": "sage, please handle this"}) is None


def test_a_raising_quest_client_yields_no_persona_not_an_exception(tmp_path):
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True),
        provider=RecordingProvider(verdict='{"persona": null}'),
        quest_client=StubQuestClient(raises=True))
    assert resolver({"text": "draft the plan", "goal_id": "q1"}) is None


def test_a_malformed_task_or_card_never_raises(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / "broken.json").write_text("{not json", encoding="utf-8")
    (cards / "persona-sage-domain.json").write_text('"a bare string"', encoding="utf-8")
    resolver = build_persona_resolver(PersonaResolverConfig(
        skills_root=str(tmp_path / "skills"),
        registry_file=write_registry(tmp_path, RICH_REGISTRY),
        card_activation=True, cards_dir=str(cards)))
    assert resolver({}) is None
    assert resolver({"text": None, "rep_id": 12345}) is None


def test_a_raising_on_resolved_callback_never_breaks_the_task(tmp_path):
    def boom(_payload):
        raise RuntimeError("consumer bug")

    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY)),
        on_resolved=boom)
    assert resolver({"rep_id": "rep_sage_1"})[0] == "rep_sage_1"


# --- the on_resolved seam -------------------------------------------------------

def test_on_resolved_fires_with_the_task_id_and_skill_dir(tmp_path):
    seen = []
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY)),
        on_resolved=seen.append)
    task = {"assignee_user_id": "u_alpha", "text": "do the thing"}
    resolver(task)
    assert seen == [{"task": task, "user_id": "u_alpha",
                     "skill_dir": str(tmp_path / "skills" / "alpha-rep")}]
    # The payload carries a COPY of the task, so a consumer stashing it cannot mutate the caller's.
    seen[0]["task"]["text"] = "mutated"
    assert task["text"] == "do the thing"


def test_on_resolved_fires_with_none_when_nothing_resolves(tmp_path):
    seen = []
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY)),
        on_resolved=seen.append)
    assert resolver({"text": "nobody is named here"}) is None
    assert resolver({"assignee_user_id": "u_unknown"}) is None
    assert seen == [None, None]


# --- the two real policies, end to end ------------------------------------------

def test_structural_only_lane_costs_nothing_beyond_a_lookup(tmp_path):
    """A lane with both optional steps off must never touch a provider or the filesystem."""
    provider = RecordingProvider(verdict='{"persona": "sage"}')
    stashed = {}
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, FLAT_REGISTRY),
                              cache_dir=str(tmp_path / "cache")),
        provider=provider, on_resolved=lambda p: stashed.update(current=p))
    assert resolver({"assignee_user_id": "u_alpha", "user_id": "u_filed_by"}) == (
        "u_alpha", str(tmp_path / "skills" / "alpha-rep"))
    assert provider.calls == []
    assert stashed["current"]["user_id"] == "u_alpha"


def test_character_lane_runs_the_three_asked_for_paths(tmp_path):
    cards = tmp_path / "cards"
    write_card(cards, "persona-scout-domain", ["mapping", "terrain", "options"])
    provider = RecordingProvider(verdict='{"persona": "sage"}')
    resolver = build_persona_resolver(
        PersonaResolverConfig(skills_root=str(tmp_path / "skills"),
                              registry_file=write_registry(tmp_path, RICH_REGISTRY),
                              llm_explicit_ask=True, card_activation=True,
                              cards_dir=str(cards), judge_tier="fast"),
        provider=provider)
    assert resolver({"persona": "scout"})[0] == "rep_scout_2"              # 1. structured
    assert resolver({"text": "sage, take this on"})[0] == "rep_sage_1"     # 2. explicit ask
    provider.verdict = '{"persona": null}'
    assert resolver({"text": "map the terrain, mapping the options"})[0] == "rep_scout_2"  # 3.
    # ...and a task that asks for nobody runs as the plain assistant.
    assert resolver({"text": "tidy the notes from yesterday"}) is None


# --- slug helpers ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("River Stone", "river-stone"),
    ("  Dr. Ada  ", "dr-ada"),
    ("already-a-slug", "already-a-slug"),
    ("", ""),
    ("!!!", ""),
])
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_unique_slug_sidesteps_existing_dirs_and_in_flight_names(tmp_path):
    (tmp_path / "ivy").mkdir()
    assert unique_slug("ivy", "u_abcdef123456", tmp_path) == "ivy-123456"
    assert unique_slug("ivy", "u_abcdef123456", tmp_path,
                       taken={"ivy-123456"}) == "ivy-2"
    assert unique_slug("", "u_abcdef123456", tmp_path) == "u-abcdef123456"


def test_persona_entry_identifiers_are_lowercased_and_deduped():
    entry = PersonaEntry(id="r1", slug="sage", display_name="Sage", aliases=("The Guide", ""))
    assert entry.identifiers() == {"r1", "sage", "the guide"}


def test_persona_card_is_found_even_when_the_store_dwarfs_the_scan_cap(tmp_path, caplog):
    """A big card store must not silently switch persona card activation off.

    Regression: the scan was ``sorted(glob("*.json"))[:MAX_CARD_FILES]``, so in a store of a few
    thousand cards a persona's card that sorted past the cap was never read. Activation stopped
    working with no error, and whether it worked depended on the names of UNRELATED cards.
    """
    from quest_ai_runner.runner import personas

    cards = tmp_path / "cards"
    cards.mkdir()
    # Far more filler than the cap, all sorting BEFORE the persona's card ("a..." < "zelda").
    for i in range(personas.MAX_CARD_FILES + 200):
        (cards / f"aaa-filler-{i:05d}.json").write_text(
            json.dumps({"keywords": ["unrelated"]}), encoding="utf-8")
    (cards / "zelda-domain.json").write_text(
        json.dumps({"keywords": ["ocarina", "hyrule"]}), encoding="utf-8")

    registry = PersonaRegistry.from_mapping({"zelda": "zelda-skill"})
    entry = personas.card_activation(
        registry, "please chart the ocarina and hyrule work", cards, min_hits=2)

    assert entry is not None and entry.id == "zelda", (
        "a persona's own card must be found by identity, not by alphabetical luck")
    assert any("exceeds the" in r.getMessage() for r in caplog.records), (
        "a store past the cap must SAY so rather than silently considering fewer cards")
