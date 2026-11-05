"""The production assemblers of the ``select-context`` seat: real ``ContextAssembler`` classes the seat can build, one ranking method that the
evaluation arms share. No model call, no network, no embedding model."""
from hubofhubs.the_body.internal_context.context import ContextAssembler

from quest_ai_runner.select_context.retrieval_assembler import BlindGrepAssembler, FeatureRerankAssembler, read_folder

FILES = {"net.py": "def retry_delay(attempt):\n    return 2 ** attempt\n", "ui.py": "def draw():\n    pass\n"}


def test_blind_grep_ranks_files_by_distinct_task_tokens_and_ties_by_path():
    arm = BlindGrepAssembler(files=FILES)
    assert arm.ranked_files("where is the retry delay computed") == ["net.py"]
    assert arm.ranked_files("zzz unknown") == []


def test_assemble_renders_ranked_files_as_one_view_with_their_ids():
    context = BlindGrepAssembler(files=FILES).assemble("retry delay")
    assert context.card_ids == ["net.py"]
    assert "net.py" in context.context_view and context.sources[0]["items"] == ["net.py"]
    assert BlindGrepAssembler(files=FILES).assemble("zzz").context_view == ""


def test_the_corpus_is_read_from_a_folder_when_none_is_handed_in(tmp_path):
    (tmp_path / "a.py").write_text("def tokenizer_budget(): pass\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "b.py").write_text("tokenizer\n")
    assert list(read_folder(tmp_path)) == ["a.py"]
    assert BlindGrepAssembler(root=str(tmp_path)).ranked_files("tokenizer budget") == ["a.py"]


def test_record_is_a_no_op_and_both_arms_are_context_assemblers():
    assert BlindGrepAssembler(files=FILES).record("task", {}) is None
    assert issubclass(BlindGrepAssembler, ContextAssembler) and issubclass(FeatureRerankAssembler, ContextAssembler)


def test_the_seat_builds_its_default_selection():
    from quest_ai_runner import hub
    from esoi.assemble import AssemblyConfig
    built = hub.child().assemble(AssemblyConfig(solutions=("select-context",)))
    assert isinstance(built["select-context"], ContextAssembler)
