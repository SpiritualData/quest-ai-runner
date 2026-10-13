"""Publishing this runner's tools and connectors to Quest Workflows, and running the one action a
workflow step asks for (runner/action_catalog.py, runner/requested_action.py)."""
import json
from types import SimpleNamespace

from quest_ai_runner.core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.runner.action_catalog import (
    CatalogPublisher, build_actions, connector_actions, is_publishable_tool, params_schema_from_json_schema,
)
from quest_ai_runner.runner.quest_client import QuestClient
from quest_ai_runner.runner.requested_action import decode_json_params, run_requested_action

SEND_PARAMS = {
    "type": "object", "required": ["subject", "quest_id"],
    "properties": {
        "subject": {"type": "string", "description": "Mail subject"},
        "quest_id": {"type": "string"},
        "tone": {"type": "string", "enum": ["warm", "brief"]},
        "count": {"type": "integer"},
        "dry": {"type": "boolean"},
        "to": {"type": "array", "items": {"type": "string"}},
    },
}


def spec(name, mutates=True, **kw):
    calls = []

    def handler(args, ctx):
        calls.append((dict(args), ctx))
        return ToolResult(ok=True, text=f"{name} ok")

    s = ToolSpec(name=name, description=f"{name} description", handler=handler, when_to_use="when asked",
                 parameters=kw.pop("parameters", SEND_PARAMS), mutates=mutates, **kw)
    s.calls = calls
    return s


def test_the_rule_publishes_tools_that_act_and_withholds_read_only_ones():
    assert is_publishable_tool(spec("send_mail", mutates=True))
    assert not is_publishable_tool(spec("find_things", mutates=False))
    assert is_publishable_tool(spec("read_but_useful", mutates=False, workflow_step=True))
    assert not is_publishable_tool(spec("internal_write", mutates=True, workflow_step=False))


def test_actions_come_from_the_tool_definitions():
    registry = ToolRegistry([spec("send_mail", context_args={"quest_id": "quest_id"}), spec("find_things", mutates=False)])
    actions = build_actions(registry)
    assert [a["action_id"] for a in actions] == ["tool:send_mail"]
    action = actions[0]
    assert action["label"] == "Send mail" and action["actor_kinds"] == ["ai"]
    assert "send_mail description" in action["description"] and "Use when: when asked" in action["description"]
    assert action["description"].endswith("Changes data.")
    assert action["request"] == {"kind": "tool", "name": "send_mail"}
    names = [f["name"] for f in action["params_schema"]]
    assert "quest_id" not in names  # the runner fills it from the task, so the form never asks


def test_json_schema_maps_to_generic_param_fields():
    fields = {f["name"]: f for f in params_schema_from_json_schema(SEND_PARAMS)}
    assert fields["subject"]["required"] is True and fields["subject"]["help"] == "Mail subject"
    assert fields["tone"]["type"] == "enum" and fields["tone"]["enum"][0] == {"value": "warm", "label": "warm"}
    assert fields["count"]["type"] == "number" and fields["dry"]["type"] == "boolean"
    assert fields["to"]["type"] == "string" and "(JSON array)" in fields["to"]["help"]


def test_connector_operations_become_actions():
    ops = [{"name": "issues:create", "description": "Open an issue", "input_schema": {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}}]
    (action,) = connector_actions(ops)
    assert action["action_id"] == "connector:issues:create"
    assert action["request"] == {"kind": "connector_operation", "name": "issues:create"}
    assert action["description"].startswith("Connector issues: Open an issue")
    assert action["params_schema"][0]["name"] == "title" and action["params_schema"][0]["required"] is True


class FakeClient:
    def __init__(self, result=None):
        self.calls = []
        self.result = {"action_count": 1} if result is None else result

    def publish_action_catalog(self, owner_type, owner_id, publisher_id, actions, **kw):
        self.calls.append((owner_type, owner_id, publisher_id, len(actions), kw))
        return self.result


def test_publishing_skips_unchanged_content_until_the_republish_window_passes():
    now = [0.0]
    publisher = CatalogPublisher(clock=lambda: now[0])
    client = FakeClient()
    targets = [("team", "t1"), ("org", "o1")]
    actions = [{"action_id": "tool:a"}]
    assert publisher.publish(client, targets, actions, env_id="env1") == 2
    assert publisher.publish(client, targets, actions, env_id="env1") == 0  # unchanged, recent
    assert publisher.publish(client, targets, actions + [{"action_id": "tool:b"}]) == 2  # changed
    now[0] += 5 * 3600
    assert publisher.publish(client, targets, actions + [{"action_id": "tool:b"}]) == 2  # keep-alive
    assert client.calls[0] == ("team", "t1", "quest-ai-runner", 1, {"env_id": "env1", "publisher_label": "Quest AI Runner"})


def test_a_failed_publish_is_retried_next_time_and_never_raises():
    publisher = CatalogPublisher(clock=lambda: 0.0)
    failing = FakeClient(result={})
    assert publisher.publish(failing, [("team", "t1")], [{"action_id": "x"}]) == 0
    assert publisher.publish(FakeClient(), [("team", "t1")], [{"action_id": "x"}]) == 1


def test_the_client_puts_to_the_team_or_org_registration_route():
    client = QuestClient("https://quest.example", "key", team_id="t1")
    seen = {}
    client._request = lambda method, path, *, params=None, body=None: seen.update(method=method, path=path, body=body) or {"ok": 1}
    client.publish_action_catalog("team", "t9", "quest-ai-runner", [{"action_id": "a"}], env_id="e1", publisher_label="L")
    assert seen == {"method": "PUT", "path": "/api/teams/t9/action-catalogs/quest-ai-runner",
                    "body": {"actions": [{"action_id": "a"}], "env_id": "e1", "publisher_label": "L"}}
    client.publish_action_catalog("org", "o9", "quest-ai-runner", [])
    assert seen["path"] == "/api/orgs/o9/action-catalogs/quest-ai-runner"


# -- running the requested action ------------------------------------------------------------------

def task_for(kind, name, args, **extra):
    return {"task_id": "task_1", "user_id": "u1", "goal_id": "quest_9",
            "requested_action": {"publisher": "quest-ai-runner", "kind": kind, "name": name, "args": args}, **extra}


def test_a_tool_action_calls_exactly_that_tool_with_the_saved_args_and_task_context():
    send = spec("send_mail", parameters={"type": "object", "properties": {
        "subject": {"type": "string"}, "quest_id": {"type": "string"}, "to": {"type": "array"}, "count": {"type": "integer"}}},
        context_args={"quest_id": "quest_id"})
    other = spec("other_tool")
    outcome = run_requested_action(task_for("tool", "send_mail", {"subject": "Hi", "to": '["a@x.org"]', "count": "3"}), ToolRegistry([send, other]))
    assert outcome.ok and outcome.text == "send_mail ok"
    ((args, ctx),) = send.calls
    assert args["subject"] == "Hi" and args["to"] == ["a@x.org"] and args["count"] == 3
    assert args["quest_id"] == "quest_9" and ctx.task_id == "task_1" and ctx.user_id == "u1"
    assert other.calls == []


def test_an_unknown_tool_or_kind_fails_honestly():
    assert not run_requested_action(task_for("tool", "gone", {}), ToolRegistry([])).ok
    outcome = run_requested_action(task_for("teleport", "x", {}), ToolRegistry([]))
    assert not outcome.ok and "teleport" in outcome.text


def test_a_failing_tool_is_a_failed_outcome_with_its_reason():
    def boom(args, ctx):
        return ToolResult(ok=False, text="mail server said no")
    registry = ToolRegistry([ToolSpec(name="send_mail", description="d", handler=boom)])
    outcome = run_requested_action(task_for("tool", "send_mail", {}), registry)
    assert not outcome.ok and outcome.text == "mail server said no"


def test_a_connector_action_is_refused_unless_the_server_is_write_enabled():
    server = SimpleNamespace(alias="issues", writable_tools=[])
    assert not run_requested_action(task_for("connector_operation", "issues:create", {}), ToolRegistry([]), [server]).ok
    assert not run_requested_action(task_for("connector_operation", "nope:create", {}), ToolRegistry([]), []).ok


def test_a_connector_action_runs_through_the_write_adapter(monkeypatch):
    from quest_ai_runner.adapters import mcp_write_adapter
    from quest_ai_runner.core.adapters import WriteResult
    seen = {}

    class FakeAdapter:
        def __init__(self, *, alias, spec, writable_tools):
            seen["init"] = (alias, list(writable_tools))

        def write_operation(self, name, args):
            seen["call"] = (name, args)
            return WriteResult(ok=True, rel_path=name, detail={})

    monkeypatch.setattr(mcp_write_adapter, "MCPWriteAdapter", FakeAdapter)
    server = SimpleNamespace(alias="issues", writable_tools=["create"])
    outcome = run_requested_action(task_for("connector_operation", "issues:create", {"title": "T"}), ToolRegistry([]), [server])
    assert outcome.ok and seen["call"] == ("issues:create", {"title": "T"}) and seen["init"] == ("issues", ["create"])


def test_decode_json_params_leaves_bad_json_for_the_tool_to_report():
    props = {"to": {"type": "array"}, "n": {"type": "integer"}}
    assert decode_json_params(props, {"to": "not json", "n": "x"}) == {"to": "not json", "n": "x"}


# -- the poller: a requested-action task is run directly and reported ------------------------------

class ReportingClient:
    configured = True

    def __init__(self):
        self.reports = []
        self.published = []

    def claim(self, task_id, handler=None):
        return {"task_id": task_id}

    def report_done(self, task_id, result, session_id=None):
        self.reports.append(("done", task_id, result))

    def report_failed(self, task_id, result, session_id=None):
        self.reports.append(("failed", task_id, result))

    def publish_action_catalog(self, owner_type, owner_id, publisher_id, actions, **kw):
        self.published.append((owner_type, owner_id, [a["action_id"] for a in actions], kw))
        return {"action_count": len(actions)}


def poller_with(tmp_path, registry, client, **cfg_kw):
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller
    from tests.conftest import StubProvider, StubRetrieval
    cfg = RunnerConfig(quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1", tool_registry=registry,
                       retrieval=StubRetrieval({}), model_provider=StubProvider(decisions=[]), **cfg_kw)
    return Poller(cfg, state_path=str(tmp_path / "state.json"), client=client)


def test_the_poller_runs_a_requested_action_directly_and_reports_done(tmp_path, monkeypatch):
    send = spec("send_mail", parameters={"type": "object", "properties": {"subject": {"type": "string"}}})
    client = ReportingClient()
    poller = poller_with(tmp_path, ToolRegistry([send]), client)
    monkeypatch.setattr("quest_ai_runner.runner.poller.TaskExecutor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no executor")))
    task = task_for("tool", "send_mail", {"subject": "Hi"}, text="x", status="queued")
    assert poller._handle_one(task) == "task_1"
    assert client.reports == [("done", "task_1", "send_mail ok")]
    assert send.calls[0][0] == {"subject": "Hi"}


def test_the_poller_reports_failure_for_an_action_it_cannot_run(tmp_path):
    client = ReportingClient()
    poller = poller_with(tmp_path, ToolRegistry([]), client)
    poller._handle_one(task_for("tool", "gone", {}, text="x", status="queued"))
    assert client.reports[0][0] == "failed" and "gone" in client.reports[0][2]


def test_the_heartbeat_publishes_the_catalog_to_each_team_and_org_once(tmp_path):
    client = ReportingClient()
    client.post_environment_heartbeat = lambda *a, **k: {}
    registry = ToolRegistry([spec("send_mail"), spec("find_things", mutates=False)])
    poller = poller_with(tmp_path, registry, client, org_id="org1", env_id="env7")
    poller._emit_heartbeat()
    poller._emit_heartbeat()  # unchanged and recent: not sent again
    assert client.published == [
        ("team", "team1", ["tool:send_mail"], {"env_id": "env7", "publisher_label": client.published[0][3]["publisher_label"]}),
        ("org", "org1", ["tool:send_mail"], {"env_id": "env7", "publisher_label": client.published[1][3]["publisher_label"]}),
    ]
