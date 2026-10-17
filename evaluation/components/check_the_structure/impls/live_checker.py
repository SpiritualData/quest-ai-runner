"""CURRENT: the live checkers run as their own commands over a synthetic fixture. ``repo`` cases run the COPY of
``scripts/check_structure.py`` (with the real ``env_vars``, ``build_tracker`` and ``leaderboard`` beside it)
inside the fixture repo; ``consumer`` cases run ``python -m quest_ai_runner.structure check-consumer``.
Nothing is reimplemented, only the repository under inspection is fake. Zero model calls."""
from check_the_structure import fixture
from quest_ai_runner import structure


def build(meter):  # noqa: ARG001
    def check(kind, args):
        root = fixture.fresh_repo(kind, args.get("edits", []))
        if kind == "repo":
            return fixture.report(root, [fixture.PY, "scripts/check_structure.py"])
        return fixture.report(root, fixture.consumer_command(root, structure.__name__))
    return check
