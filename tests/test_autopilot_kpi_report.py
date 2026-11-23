"""The end-of-run KPI report: the brief asks for it, the block parses, and it is posted with the run's task id."""
from quest_ai_runner.intelligence.proactivity import autopilot_kpi_report as kr

OUTPUT = """Did the outreach.

KPI REPORT
{"values": [{"name": "Replies", "value": 3, "blocker": "The inbox login expired."}, {"name": "Security", "value": 80}]}
END KPI REPORT"""


class Client:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def post_autopilot_kpi_report(self, *args):
        if self.fail:
            raise RuntimeError("503")
        self.calls.append(args)


def test_parse_reads_the_last_valid_block():
    data = kr.parse_report("KPI REPORT\nnot json\nEND KPI REPORT\n" + OUTPUT)
    assert [v["name"] for v in data["values"]] == ["Replies", "Security"]
    assert kr.parse_report("no block here") is None


def test_fenced_json_is_accepted():
    assert kr.parse_report('KPI REPORT\n```json\n{"values": []}\n```\nEND KPI REPORT') == {"values": []}


def test_report_run_posts_values_and_new_kpis():
    client = Client()
    text = 'KPI REPORT\n{"values": [{"name": "Cost", "value": 2, "x": 1}], "new_kpis": [{"name": "Cost", "kind": "count", "definition": "Dollars", "metric_id": ""}]}\nEND KPI REPORT'
    assert kr.report_run(client, "q1", "a1", "t1", text) is True
    assert client.calls == [("q1", "a1", "t1", [{"name": "Cost", "value": 2}], [{"name": "Cost", "kind": "count", "definition": "Dollars"}])]


def test_a_run_without_a_block_or_a_refused_post_reports_false_and_never_raises():
    assert kr.report_run(Client(), "q", "a", "t", "nothing") is False
    assert kr.report_run(Client(fail=True), "q", "a", "t", OUTPUT) is False


def test_brief_names_defined_kpis_or_asks_for_its_own():
    with_kpis = kr.brief_section([{"name": "Replies", "kind": "count", "definition": "Replies received", "metric_id": "esr_1"}])
    assert "Replies (count" in with_kpis and "esr_1" in with_kpis and "new_kpis" not in with_kpis.split("END KPI REPORT")[0].split("KPI REPORT")[-1]
    own = kr.brief_section([])
    assert "no KPI yet" in own and '"new_kpis"' in own and own.rstrip().endswith(kr.END)


def test_output_shows_kpis_as_plain_lines_not_the_json_block():
    shown = kr.with_readable_kpis(OUTPUT, "")
    assert "KPI REPORT" not in shown and "{" not in shown
    assert "- Replies: 3 (blocked: The inbox login expired.)" in shown and "- Security: 80" in shown
    # The final reply may lack the block while the deep output holds it.
    assert "- Replies: 3" in kr.with_readable_kpis("Did the outreach.", OUTPUT)
    assert kr.with_readable_kpis("plain", "") == "plain"


def test_feedback_from_the_sheet_reaches_the_next_run():
    brief = kr.brief_section([{"name": "Replies", "kind": "count", "definition": "Replies received"}],
                             {"last_run": {"task_id": "t1", "reaction": "not_helpful"}, "comments": [{"kpi": "Replies", "author": "Payal", "text": "Count only real replies."}]})
    assert "Not helpful" in brief and 'Payal on the Replies row: "Count only real replies."' in brief
    assert "Feedback" not in kr.brief_section([], None)


def test_a_value_without_a_number_is_left_out_and_unknown_names_make_it_false():
    client = Client()
    text = 'KPI REPORT\n{"values": [{"name": "Cost", "value": null}, {"name": "Replies", "value": 2}]}\nEND KPI REPORT'
    assert kr.report_run(client, "q", "a", "t", text) is True
    assert client.calls[0][3] == [{"name": "Replies", "value": 2}]

    class Unknown(Client):
        def post_autopilot_kpi_report(self, *args):
            return {"recorded": [], "unknown": ["Ghost"]}
    assert kr.report_run(Unknown(), "q", "a", "t", text) is False
