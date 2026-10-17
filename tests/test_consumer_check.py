"""The consumer-registry check: a conforming implementation passes; a drifted one, a bad id and a bare duplicate fail."""
import csv
import sys
import textwrap

from quest_ai_runner.structure.consumer_check import REQUIRED_COLUMNS, check_consumer, read_pin


def write_registry(tmp_path, rows):
    path = tmp_path / "reg.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(REQUIRED_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in REQUIRED_COLUMNS})
    return path


def make_module(tmp_path, name, body):
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body))
    sys.modules.pop(name, None)


BASE = {"selected_by": "hand-wired", "metric": "a live proof", "status": "done"}


def test_conforming_implementation_passes(tmp_path):
    make_module(tmp_path, "good_escalation", """
        from quest_ai_runner.core.adapters import EscalationSinkBase
        class Sink(EscalationSinkBase):
            def escalate(self, escalation):
                return None
    """)
    reg = write_registry(tmp_path, [{**BASE, "integration_id": "a", "module": "good_escalation", "symbol": "Sink",
                                     "qar_solution_id": "escalate-to-a-human", "qar_port": "EscalationSink",
                                     "kind": "implementation"}])
    report = check_consumer(reg, tmp_path)
    assert report.problems == [] and report.counts == {"implementation": 1}


def test_missing_method_unknown_solution_and_bare_duplicate_fail(tmp_path):
    make_module(tmp_path, "bad_sink", """
        class Sink:
            pass
    """)
    reg = write_registry(tmp_path, [
        {**BASE, "integration_id": "a", "module": "bad_sink", "symbol": "Sink", "qar_solution_id": "no-such-solution",
         "qar_port": "EscalationSink", "kind": "implementation"},
        {**BASE, "integration_id": "b", "module": "bad_sink", "kind": "duplicate"},
        {**BASE, "integration_id": "c", "module": "bad_sink", "kind": "call", "qar_symbol": "quest_ai_runner.core.nope:X"},
    ])
    text = "\n".join(check_consumer(reg, tmp_path).problems)
    assert "C2 a" in text and "missing method escalate" in text and "C5 b" in text and "C4 c" in text


def test_read_pin_parses_a_git_pin(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("quest-ai-runner @ git+https://example.com/x.git@61d93e6e4eb09ae926b23ee704c91912cefbc331\n")
    assert read_pin(req) == "61d93e6e4eb09ae926b23ee704c91912cefbc331"


def test_solutions_and_results_gate(tmp_path):
    from quest_ai_runner.structure.consumer_check import cases_hash
    (tmp_path / "t.py").write_text("x = 1\n")
    make_module(tmp_path, "good_escalation2", """
        from quest_ai_runner.core.adapters import EscalationSinkBase
        class Sink(EscalationSinkBase):
            def escalate(self, escalation):
                return None
    """)
    path = tmp_path / "reg.csv"
    cols = list(REQUIRED_COLUMNS) + ["solution_id"]
    with path.open("w", newline="") as handle:
        w = csv.DictWriter(handle, fieldnames=cols)
        w.writeheader()
        base = {**BASE, "module": "good_escalation2", "symbol": "Sink", "qar_solution_id": "escalate-to-a-human",
                "qar_port": "EscalationSink", "kind": "implementation"}
        w.writerow({**base, "integration_id": "a", "solution_id": "s1"})
        w.writerow({**base, "integration_id": "b", "solution_id": ""})
    sols = tmp_path / "sols.csv"
    sols.write_text("solution_id,selected,metric_files\ns1,Sink,t.py\n")
    res = tmp_path / "res.csv"
    res.write_text(f"solution_id,cases,passed,git_sha,cases_sha\ns1,3,3,abc1234,{cases_hash(['t.py'], tmp_path)}\n")
    problems = check_consumer(path, tmp_path, solutions=sols, results=res).problems
    assert problems == ["C9 b: the integration point sits in no solution (solution_id is empty)"]
    (tmp_path / "t.py").write_text("x = 2\n")
    assert any("C10 s1: stale" in p for p in check_consumer(path, tmp_path, solutions=sols, results=res).problems)
