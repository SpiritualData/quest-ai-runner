"""The public-repo hygiene scan finds planted secrets, ignores documented placeholders, and never prints a whole secret."""
import subprocess

from quest_ai_runner.structure import hygiene


def patterns(*parts):
    return sorted({f.pattern for f in hygiene.scan_text("".join(parts))})


def test_planted_secrets_are_found():
    assert patterns("sk-an", "t-api03-", "AbCdEfGhIjKlMnOpQrStUv") == ["anthropic-key"]
    assert patterns("qs", "k_", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4") == ["quest-key"]
    assert patterns("/ho", "me/jane/", "x") == ["machine-path"]
    assert patterns("jane.doe@", "acme-corp.com") == ["email"]


def test_placeholders_are_not_findings():
    assert patterns("see qs", "k_... and /ho", "me/user/x and name@", "example.org") == []
    assert patterns("a commit 5dc9490bb35f9729ef2c", "95d00a19ccd30c26339c") == []
    assert patterns("@pytest.mark.parametrize") == []


def test_a_finding_redacts_the_secret():
    finding = hygiene.scan_text("sk-an" + "t-api03-AbCdEfGhIjKlMnOpQrStUv")[0]
    assert "AbCdEfGhIjKl" not in str(finding)


def test_command_exits_one_on_a_planted_secret(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "a.md").write_text("qs" + "k_a1b2c3d4e5f6a7b8c9d0e1f2a3b4")
    assert hygiene.main(["--root", str(tmp_path)]) == 1
    (tmp_path / "a.md").write_text("nothing here")
    assert hygiene.main(["--root", str(tmp_path)]) == 0
