"""A destructive eval step refuses any account but the hermetic eval identity."""
import pytest

import devclient as D


def test_the_eval_identity_passes(tmp_path):
    env = {"QUEST_TEAM_ID": D.EVAL_TEAM_ID, "QUEST_API_KEY": "qsk_eval"}
    assert D.eval_identity_problem(tmp_path / "eval-account.env", env) is None


def test_a_lane_env_file_is_refused(tmp_path):
    env = {"QUEST_TEAM_ID": D.EVAL_TEAM_ID, "QUEST_API_KEY": "qsk_x"}
    assert "not the eval identity's env file" in D.eval_identity_problem(tmp_path / ".env", env)


def test_another_team_is_refused(tmp_path):
    env = {"QUEST_TEAM_ID": "team_someone_else", "QUEST_API_KEY": "qsk_x"}
    assert "not the eval team" in D.eval_identity_problem(tmp_path / "eval-account.env", env)


def test_the_lane_key_is_refused(tmp_path):
    (tmp_path / ".env").write_text("QUEST_API_KEY=qsk_lane\n")
    env = {"QUEST_TEAM_ID": D.EVAL_TEAM_ID, "QUEST_API_KEY": "qsk_lane"}
    assert "dev lane's key" in D.eval_identity_problem(tmp_path / "eval-account.env", env)


def test_assert_exits_when_refused(monkeypatch):
    monkeypatch.setattr(D, "eval_identity_problem", lambda *a, **k: "wrong account")
    with pytest.raises(SystemExit):
        D.assert_eval_identity("teardown")
