"""Direct tools (core/tools.py): the catalog, relevance selection, invocation, the TOML loader, the
shell CLI a deep run uses, and the orchestrator's "tool" action end to end with stub adapters.

The behavior under test is the one the feature exists for: a request a tool covers (send an email)
is DONE in-process, in one planner step, with no deep run, and the turn's execution record backs
the answer's claim that it happened.
"""
import json
import sys
import textwrap

import pytest

from .conftest import StubDeepRunner, StubProvider, StubRetrieval
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    decide_tool_for,
    normalize_decision,
)
from quest_ai_runner.core.tools import (
    SHOW_ALL_AT_OR_BELOW,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_tool_registry,
    command_args_as_flags,
    send_quest_email_spec,
)


def make_spec(name, *, handler=None, mutates=True, keywords=(), description=None, **kw):
    return ToolSpec(
        name=name, description=description or f"{name} does its thing",
        handler=handler or (lambda args, ctx: f"{name} ran with {sorted(args)}"),
        when_to_use=kw.pop("when_to_use", f"when you need {name}"),
        when_not_to_use=kw.pop("when_not_to_use", f"when {name} is the wrong tool"),
        mutates=mutates, keywords=keywords, **kw)


# ---------------------------------------------------------------------------------------------
# registry: selection, rendering, invocation
# ---------------------------------------------------------------------------------------------

def test_small_catalog_is_shown_whole():
    reg = ToolRegistry([make_spec("send_quest_email", keywords=("email",)), make_spec("make_chart")])
    assert [s.name for s in reg.select("email me the brief")] == ["send_quest_email", "make_chart"]


def test_large_catalog_is_narrowed_to_relevant_tools():
    specs = [make_spec(f"filler_{i}", description=f"does unrelated chore number {i}")
             for i in range(SHOW_ALL_AT_OR_BELOW + 4)]
    specs.append(make_spec("send_quest_email", keywords=("email", "mail", "send"),
                       description="Send an email through the quest mailer"))
    specs.append(make_spec("queue_email_for_review", keywords=("email", "review", "external"),
                       description="File an email draft for human review"))
    reg = ToolRegistry(specs)
    picked = [s.name for s in reg.select("send an email to me", k=3)]
    assert picked[0] == "send_quest_email"
    assert "queue_email_for_review" in picked
    assert not any(n.startswith("filler_") for n in picked)
    block = reg.render_planner_block("send an email to me", k=3)
    assert "more tool(s) not shown" in block and '{"tools":' in block


def test_render_shows_when_to_use_and_when_not():
    reg = ToolRegistry([make_spec("send_quest_email", when_to_use="internal mail",
                              when_not_to_use="external mail needing review")])
    block = reg.render_planner_block("email")
    assert "USE WHEN: internal mail" in block
    assert "DO NOT USE WHEN: external mail needing review" in block


def test_invoke_fills_context_then_defaults_and_validates():
    seen = {}

    def handler(args, ctx):
        seen.update(args)
        return ToolResult(ok=True, text="ok")

    spec = make_spec("send_quest_email", handler=handler,
                 parameters={"type": "object", "required": ["quest_id", "subject"],
                             "properties": {"quest_id": {"type": "string"},
                                            "subject": {"type": "string"},
                                            "to": {"type": "array", "items": {"type": "string"}}}},
                 context_args={"quest_id": "quest_id"}, defaults={"quest_id": "quest_default"})
    reg = ToolRegistry([spec])
    # context wins over the configured default
    assert reg.invoke("send_quest_email", {"subject": "s", "to": "a@b.c"},
                      ToolContext(quest_id="quest_ctx")).ok
    assert seen == {"quest_id": "quest_ctx", "subject": "s", "to": ["a@b.c"]}
    # no context: the default fills it
    seen.clear()
    assert reg.invoke("send_quest_email", {"subject": "s"}, ToolContext()).ok
    assert seen["quest_id"] == "quest_default"
    # missing required / unknown args / unknown tool fail as results, never raise
    spec.defaults = {}
    miss = reg.invoke("send_quest_email", {"subject": "s"}, ToolContext())
    assert not miss.ok and "quest_id" in miss.text
    bad = reg.invoke("send_quest_email", {"subject": "s", "quest_id": "q", "bogus": 1})
    assert not bad.ok and "bogus" in bad.text
    assert not reg.invoke("nope", {}).ok


def test_invoke_turns_a_raising_handler_into_a_failed_result():
    def boom(args, ctx):
        raise RuntimeError("email is not enabled for this quest")
    res = ToolRegistry([make_spec("t", handler=boom)]).invoke("t", {})
    assert not res.ok and "not enabled" in res.text


def test_invoke_times_out():
    import time
    spec = make_spec("slow", handler=lambda a, c: time.sleep(2), timeout_seconds=0.2)
    res = ToolRegistry([spec]).invoke("slow", {})
    assert not res.ok and "timed out" in res.text


def test_command_flags_rendering():
    params = {"properties": {"to": {"type": "array", "flag": "--to"},
                             "subject": {"type": "string"},
                             "dry_run": {"type": "boolean"},
                             "absent": {"type": "string"}}}
    argv = command_args_as_flags(params, {"to": ["a@x", "b@x"], "subject": "Hi", "dry_run": True})
    assert argv == ["--to", "a@x", "--to", "b@x", "--subject", "Hi", "--dry-run"]


# ---------------------------------------------------------------------------------------------
# the standard email tool and the TOML loader
# ---------------------------------------------------------------------------------------------

class FakeQuestClient:
    def __init__(self):
        self.calls = []

    def send_quest_email(self, quest_id, *, subject, body, rep_id=None, task_id=None,
                         recipients=None):
        self.calls.append(dict(quest_id=quest_id, subject=subject, body=body, rep_id=rep_id,
                               task_id=task_id, recipients=recipients))
        return {"persona": "Zee's AI"}


def test_standard_send_quest_email_calls_quest_client():
    client = FakeQuestClient()
    spec = send_quest_email_spec({}, client_factory=lambda: client)
    reg = ToolRegistry([spec])
    res = reg.invoke("send_quest_email", {"subject": "Brief", "body": "Hello", "to": ["j@x.org"]},
                     ToolContext(quest_id="quest_1", task_id="t9"))
    assert res.ok and "Sent" in res.text and "j@x.org" in res.text
    assert client.calls == [dict(quest_id="quest_1", subject="Brief", body="Hello", rep_id=None,
                                 task_id="t9", recipients=["j@x.org"])]


def test_build_registry_standard_tool_needs_quest_credentials():
    assert build_tool_registry({}).names() == []
    env = {"QUEST_BASE_URL": "https://api.example.org", "QUEST_API_KEY": "qsk_x"}
    assert build_tool_registry(env).names() == ["send_quest_email"]
    assert build_tool_registry({**env, "QAR_STANDARD_TOOLS": "0"}).names() == []


def test_toml_custom_command_tool_and_standard_override(tmp_path):
    script = tmp_path / "draft.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        args = sys.argv[1:]
        print(json.dumps({"id": 42, "argv": args}))
    """))
    toml = tmp_path / "tools.toml"
    toml.write_text(textwrap.dedent(f"""
        [[tool]]
        name = "queue_email_for_review"
        description = "File an email draft for review."
        when_to_use = "Mail to people outside the team."
        when_not_to_use = "Internal mail: use send_quest_email."
        command = ["{{python}}", "{script}", "create", "--json"]
        keywords = ["email", "review"]
        [tool.parameters]
        type = "object"
        required = ["to", "subject", "body"]
        [tool.parameters.properties.to]
        type = "array"
        items = {{type = "string"}}
        flag = "--to"
        [tool.parameters.properties.subject]
        type = "string"
        [tool.parameters.properties.body]
        type = "string"

        [standard.send_quest_email]
        defaults = {{quest_id = "quest_default"}}
        when_to_use_extra = "Use the default quest for internal mail."
    """))
    env = {"QUEST_BASE_URL": "https://api.example.org", "QUEST_API_KEY": "qsk_x",
           "QAR_TOOLS_FILE": str(toml)}
    reg = build_tool_registry(env)
    assert sorted(reg.names()) == ["queue_email_for_review", "send_quest_email"]
    std = reg.get("send_quest_email")
    assert std.defaults == {"quest_id": "quest_default"}
    assert "default quest" in std.when_to_use
    custom = reg.get("queue_email_for_review")
    assert custom.origin == "custom"
    assert "flag" not in json.dumps(custom.public_parameters())
    res = reg.invoke("queue_email_for_review",
                     {"to": ["donor@x.org"], "subject": "Hi", "body": "Body"})
    assert res.ok, res.text
    assert res.data["argv"] == ["create", "--json", "--to", "donor@x.org", "--subject", "Hi",
                                "--body", "Body"]


def test_toml_entry_without_when_not_to_use_is_rejected(tmp_path, caplog):
    toml = tmp_path / "tools.toml"
    toml.write_text('[[tool]]\nname = "x"\ndescription = "d"\nwhen_to_use = "w"\n'
                    'command = ["true"]\n')
    reg = build_tool_registry({"QAR_TOOLS_FILE": str(toml)})
    assert reg.names() == []          # a broken file is logged and skipped, never fatal
    assert "when_not_to_use" in caplog.text


def test_cli_list_and_call(tmp_path, monkeypatch, capsys):
    from quest_ai_runner.tools.__main__ import main
    mod = tmp_path / "echo_tool.py"
    mod.write_text("def run(args, ctx):\n    return f\"echo {args['text']} quest={ctx.quest_id}\"\n")
    toml = tmp_path / "tools.toml"
    toml.write_text(textwrap.dedent("""
        [[tool]]
        name = "echo"
        description = "Echo text back."
        when_to_use = "Testing."
        when_not_to_use = "Anything real."
        handler = "echo_tool:run"
        mutates = false
        context_args = {quest = "quest_id"}
        [tool.parameters]
        type = "object"
        required = ["text"]
        [tool.parameters.properties.text]
        type = "string"
        [tool.parameters.properties.quest]
        type = "string"
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("QAR_TOOLS_FILE", str(toml))
    monkeypatch.setenv("QAR_STANDARD_TOOLS", "0")
    assert main(["list", "--query", "echo"]) == 0
    assert "echo (custom, read-only)" in capsys.readouterr().out
    assert main(["call", "echo", "--args", '{"text": "hi"}', "--quest", "quest_7"]) == 0
    assert "echo hi quest=quest_7" in capsys.readouterr().out
    assert main(["call", "echo", "--args", "{}"]) == 1


# ---------------------------------------------------------------------------------------------
# planner schema + normalization
# ---------------------------------------------------------------------------------------------

def test_decide_schema_gains_tool_action_only_with_tools():
    plain = decide_tool_for(False, False)
    assert "tool" not in plain["input_schema"]["properties"]["action"]["enum"]
    assert "tool_calls" not in plain["input_schema"]["properties"]
    with_tools = decide_tool_for(False, False, tools=True)
    assert "tool" in with_tools["input_schema"]["properties"]["action"]["enum"]
    assert "tool_calls" in with_tools["input_schema"]["properties"]
    # the shared base schema is never mutated
    assert "tool" not in decide_tool_for(False, False)["input_schema"]["properties"]["action"]["enum"]


def test_normalize_decision_tool_action():
    cfg = OrchestratorConfig()
    raw = {"action": "tool", "tool_calls": [{"name": "send_quest_email", "args": {"subject": "x"}},
                                            {"args": {}}, "junk"]}
    assert normalize_decision(raw, cfg).action == "answer"           # tools not enabled
    d = normalize_decision(raw, cfg, tools_enabled=True)
    assert d.action == "tool"
    assert d.tool_calls == [{"name": "send_quest_email", "args": {"subject": "x"}}]
    assert normalize_decision({"action": "tool"}, cfg, tools_enabled=True).action == "answer"


# ---------------------------------------------------------------------------------------------
# the orchestrator loop
# ---------------------------------------------------------------------------------------------

def make_orch(provider, tools, **kw):
    cfg = kw.pop("config", None) or OrchestratorConfig()
    cfg.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg, tools=tools, **kw)


def email_registry(calls):
    def handler(args, ctx):
        calls.append({**args, "_quest": ctx.quest_id})
        return ToolResult(ok=True, text=f"Sent \"{args['subject']}\" to the quest's recipients.")
    return ToolRegistry([make_spec(
        "send_quest_email", handler=handler, keywords=("email", "send"),
        parameters={"type": "object", "required": ["quest_id", "subject", "body"],
                    "properties": {"quest_id": {"type": "string"},
                                   "subject": {"type": "string"},
                                   "body": {"type": "string"}}},
        context_args={"quest_id": "quest_id"})])


def test_tool_action_sends_without_a_deep_run():
    calls = []
    provider = StubProvider(decisions=[
        {"action": "tool", "rationale": "the email tool covers this",
         "tool_calls": [{"name": "send_quest_email",
                         "args": {"subject": "Today's plan", "body": "Here it is."}}]},
        {"action": "answer", "rationale": "sent; report the receipt"},
    ])
    deep = StubDeepRunner(met=True)
    res = make_orch(provider, email_registry(calls), deep_runner=deep).run(
        "send me an email with today's plan", quest_id="quest_42")
    assert calls == [{"subject": "Today's plan", "body": "Here it is.", "quest_id": "quest_42",
                      "_quest": "quest_42"}]
    assert deep.calls == []                                   # no deep run at all
    assert res.kind == "answer"
    assert res.execution_record.any_success
    # the planner was shown the tool and offered the "tool" action
    assert "send_quest_email" in provider.plan_prompts[0]
    assert "tool" in provider.plan_tool_schemas[0]["input_schema"]["properties"]["action"]["enum"]
    # the second plan saw the receipt
    assert "TOOL send_quest_email SUCCEEDED" in provider.plan_prompts[1]


def test_repeated_successful_call_is_not_run_twice():
    calls = []
    call = {"name": "send_quest_email", "args": {"subject": "s", "body": "b"}}
    provider = StubProvider(decisions=[
        {"action": "tool", "rationale": "send", "tool_calls": [call]},
        {"action": "tool", "rationale": "send again (wrongly)", "tool_calls": [call]},
        {"action": "answer", "rationale": "done"},
    ])
    make_orch(provider, email_registry(calls)).run("email me", quest_id="q1")
    assert len(calls) == 1


def test_failed_tool_is_recorded_as_failure():
    def handler(args, ctx):
        raise RuntimeError("email is not enabled for this quest")
    reg = ToolRegistry([make_spec("send_quest_email", handler=handler)])
    provider = StubProvider(decisions=[
        {"action": "tool", "rationale": "send",
         "tool_calls": [{"name": "send_quest_email", "args": {}}]},
        {"action": "answer", "rationale": "report the failure"},
    ])
    res = make_orch(provider, reg).run("email me")
    assert res.execution_record.any_failure
    assert "not enabled" in provider.plan_prompts[1]


def test_budget_ending_on_tool_step_answers_instead_of_escalating_to_deep():
    calls = []
    provider = StubProvider(decisions=[
        {"action": "tool", "rationale": "send",
         "tool_calls": [{"name": "send_quest_email", "args": {"subject": "s", "body": "b"}}]},
    ])
    deep = StubDeepRunner(met=True)
    res = make_orch(provider, email_registry(calls), deep_runner=deep,
                config=OrchestratorConfig(max_steps=1)).run("send an email to me", quest_id="q")
    assert len(calls) == 1
    assert deep.calls == []
    assert res.kind == "answer"


def test_brainstorm_holds_a_mutating_tool_call():
    calls = []
    provider = StubProvider(decisions=[
        {"action": "tool", "rationale": "send",
         "tool_calls": [{"name": "send_quest_email", "args": {"subject": "s", "body": "b"}}]},
    ])
    res = make_orch(provider, email_registry(calls),
                config=OrchestratorConfig(execution_mode="brainstorm")).run(
        "what if we emailed the team?", quest_id="q")
    assert calls == []
    assert res.kind == "answer"


def test_tools_catalog_read_searches_hidden_tools():
    specs = [make_spec(f"filler_{i}") for i in range(SHOW_ALL_AT_OR_BELOW + 2)]
    specs.append(make_spec("publish_newsletter", keywords=("newsletter",)))
    provider = StubProvider(decisions=[
        {"action": "read", "rationale": "look for a newsletter tool",
         "reads": [{"tools": "newsletter"}]},
        {"action": "answer", "rationale": "found it"},
    ])
    make_orch(provider, ToolRegistry(specs)).run("zzz qqq")
    assert "publish_newsletter" not in provider.plan_prompts[0]
    assert "(no tool matched this request)" in provider.plan_prompts[0]
    assert "TOOLS MATCHING YOUR SEARCH" in provider.plan_prompts[1]
    assert "publish_newsletter" in provider.plan_prompts[1]


def test_deep_brief_lists_relevant_tools_with_commands():
    calls = []
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Write the report and email it", "deep_brief": "write + email",
         "rationale": "real work"},
    ])
    deep = StubDeepRunner(met=True)
    make_orch(provider, email_registry(calls), deep_runner=deep).run(
        "write the weekly report and email it to me", context_meta={"quest_id": "quest_9"})
    assert deep.calls, "deep run expected"
    brief = deep.calls[0]["brief"]
    assert "TOOLS YOU CAN CALL" in brief
    assert f"{sys.executable}" in brief and "-m quest_ai_runner.tools call" in brief
    assert "--quest quest_9" in brief


def test_no_tools_means_no_tool_vocabulary():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "hi"}])
    make_orch(provider, None).run("hello")
    assert "tool" not in provider.plan_tool_schemas[0]["input_schema"]["properties"]["action"]["enum"]
    assert "DIRECT TOOLS" not in provider.plan_prompts[0]


def test_build_orchestrator_wires_tools_from_config_credentials(monkeypatch):
    from quest_ai_runner.config import RunnerConfig, resolve_tool_registry
    monkeypatch.delenv("QAR_STANDARD_TOOLS", raising=False)
    monkeypatch.delenv("QUEST_BASE_URL", raising=False)
    monkeypatch.delenv("QUEST_API_URL", raising=False)
    monkeypatch.delenv("QUEST_API_KEY", raising=False)
    cfg = RunnerConfig(quest_base_url="https://api.example.org", quest_api_key="qsk_test")
    assert resolve_tool_registry(cfg).names() == ["send_quest_email"]
    # a registry wired in code wins, including an empty one (= no tools)
    cfg.tool_registry = ToolRegistry()
    assert resolve_tool_registry(cfg).names() == []


def test_tools_block_comes_after_the_planner_body():
    """The tools block is the last instruction before the decision. Placed above the planner body,
    a live model filled tool_calls with the right call yet chose "answer", so nothing ran."""
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "nothing to do"}])
    make_orch(provider, email_registry([])).run("Please email me the weekly plan",
                                                quest_id="quest_42")
    prompt = provider.plan_prompts[0]
    assert prompt.index("DIRECT TOOLS") > prompt.index("Please email me the weekly plan")
    assert "Calls run ONLY when action is \"tool\"" in prompt
