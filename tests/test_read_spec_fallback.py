"""A read spec naming a file and a search falls back to the file when the search half errors."""
from quest_ai_runner.core.adapters_values import Observation
from quest_ai_runner.core.orchestrator_reads import ReadsMixin


class Retrieval:
    def grep(self, pattern, scope=None):
        return Observation(kind="error", pattern=pattern, scope=scope, error="no such scope")

    def read_section(self, rel_path, **kw):
        return Observation(kind="read", rel_path=rel_path, text="file text")


class Cfg:
    max_gather_chars = 1000


class Fake(ReadsMixin):
    retrieval = Retrieval()
    cfg = Cfg()
    tools = None


def run(spec):
    return Fake()._exec_one_read(spec)


def test_file_half_answers_when_the_search_half_fails():
    obs = run({"rel_path": "a:CLAUDE.md", "grep": "dash", "scope": "a:nowhere"})
    assert obs.kind == "read" and obs.text == "file text"


def test_a_readable_file_wins_over_a_wandering_search():
    class Both(Retrieval):
        def grep(self, pattern, scope=None):
            return Observation(kind="grep", pattern=pattern, hits=[], text="noise")

    class Other(Fake):
        retrieval = Both()

    obs = Other()._exec_one_read({"rel_path": "a:CLAUDE.md", "grep": "dash", "scope": "a:"})
    assert obs.kind == "read"


def test_a_search_alone_keeps_its_error():
    assert run({"grep": "dash", "scope": "a:nowhere"}).kind == "error"
