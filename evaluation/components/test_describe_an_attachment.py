"""Offline tests for describe_an_attachment: the cases name their sources and render to real images, the real prepare_attachments runs
in both arms, the floor names the image and asks no vision model, the current arm hands the description to the answering model, the
scorer reads the labelled answer, and the broken baseline fails every case. A stub stands in for ``claude -p``; no model call, no
network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_describe_an_attachment.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
import claude_judge  # noqa: E402
from describe_an_attachment import arms, score  # noqa: E402

CASES = json.loads((HERE / "describe_an_attachment" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


class Answerer:
    """The text-only answering model: it answers 12.5 only when its notes hold it."""

    def __init__(self):
        self.asked = []
        self.tokens_in = self.tokens_out = 0
        self.cost_usd = 0.0

    def answer(self, messages, *, model=None, system=None, step=None):  # noqa: ARG002
        text = messages[-1]["content"]
        self.asked.append(text)
        return "The longest run was 12.5 km." if "12.5" in text else "I cannot see the image."


class Describer:
    def __init__(self):
        self.calls = 0
        self.supports_native_images = True

    def answer(self, messages, *, model=None, system=None):  # noqa: ARG002
        self.calls += 1
        assert any(b.get("type") == "image" and b["source"]["data"] for b in messages[0]["content"])
        return "A table of runs. Longest: Aug 12, 12.5 km."


def arm(name, answerer, monkeypatch, describer=None):
    monkeypatch.setattr(claude_judge, "cli_provider", lambda meter, model: answerer)
    if describer is not None:
        monkeypatch.setattr(arms, "VisionCli", lambda **kw: describer)
    return common.load_impl(name, "describe_an_attachment")(common.CallMeter(max_calls=40))


def test_every_case_names_its_source_and_renders_to_a_png():
    assert len(BY_ID) == len(CASES) >= 10
    assert {c["kind"] for c in CASES} == {"needs_image", "control"}
    assert all(len(c["source"]) > 40 and c["expect_any"] for c in CASES)
    assert all(arms.render(c["image"])[:8] == b"\x89PNG\r\n\x1a\n" for c in CASES)


def test_the_floor_names_the_image_and_the_answer_cannot_use_it(monkeypatch):
    answerer = Answerer()
    out = arm("skip_images", answerer, monkeypatch)(BY_ID["DAI-01"])
    assert "no describer is configured" in out["grounding"] and not out["described"]
    assert not score.score_case(lambda case: out, BY_ID["DAI-01"])["ok"]


def test_the_current_arm_gives_the_answering_model_the_description(monkeypatch):
    describer, answerer = Describer(), Answerer()
    out = arm("vision_description", answerer, monkeypatch, describer)(BY_ID["DAI-01"])
    assert describer.calls == 1 and "12.5 km" in out["grounding"] and out["described"]
    assert score.score_case(lambda case: out, BY_ID["DAI-01"])["ok"]


def test_a_control_is_answered_without_the_image_and_the_description_is_waste(monkeypatch):
    answerer = Answerer()
    out = arm("skip_images", answerer, monkeypatch)(BY_ID["DAI-10"])
    assert not out["described"]
    row = score.score_case(lambda case: {**out, "reply": "OK", "described": True}, BY_ID["DAI-10"])
    assert row["ok"] and "controls where the message alone answers 1" in score.aggregate([dict(row, kind="control")])


def test_the_labelled_answers_match_numbers_with_separators_and_either_order():
    assert score.score_case(lambda c: {"reply": "That is 1,870 words.", "grounding": "", "described": True}, BY_ID["DAI-02"])["ok"]
    assert score.score_case(lambda c: {"reply": "380 dollars, owned by Mia.", "grounding": "", "described": True}, BY_ID["DAI-03"])["ok"]
    assert not score.score_case(lambda c: {"reply": "About 1,780.", "grounding": "", "described": True}, BY_ID["DAI-02"])["ok"]


def test_the_broken_baseline_fails_every_case():
    broken = common.load_impl("describe_an_attachment.broken.guess_the_image:build", "describe_an_attachment")(common.CallMeter())
    assert not any(score.score_case(broken, c)["ok"] for c in CASES)
