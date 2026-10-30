"""CANDIDATE: the tidy through the repository port (``hubofhubs/recall/card_store_tidy.py``), run on a store that lives on a
backend. The fixture store is loaded into ``BackendShapedRepository`` (in memory, no quarantine folder, a server
revision on every write), planned with the library's ``plan_cards`` and its default reasons, and applied with
``RepositoryTidy`` (built through the ``repository_tidy`` arm QAR declares) exactly as a lane that opted into ``card_tidy = "apply"`` applies it: export first, keeper first,
then removal. The backend's cards are then written back over the folder so the score reads them (a card the
backend no longer holds moves into ``_quarantine/backend/``). Zero model calls."""
import json
import os
from pathlib import Path

from card_growth.backend_fake import BackendShapedRepository
from card_growth.score import indexed
from hubofhubs.recall import card_store_tidy as tidy
from quest_ai_runner.adapters.card_store_tidy import repository_tidy


def build(meter):  # noqa: ARG001
    def settle(cards_dir):
        cards_dir = Path(cards_dir)
        held = indexed(cards_dir)
        repo = BackendShapedRepository()
        for key, card in held.items():
            repo.seed(key, card)
        cards = repo.load_all()
        plan = tidy.plan_store(cards, cards_dir=cards_dir, reasons=tidy.DEFAULT_REASONS, repo=repo)
        run = repository_tidy(repo, cards_dir).apply(plan, cards)
        if run.failed:
            raise RuntimeError(run.failed)
        gone = cards_dir / "_quarantine" / "backend"
        for key in held:
            if key not in repo.cards:
                gone.mkdir(parents=True, exist_ok=True)
                os.replace(cards_dir / f"{key}.json", gone / f"{key}.json")
            elif repo.cards[key] != cards[key]:
                (cards_dir / f"{key}.json").write_text(
                    json.dumps(tidy.without_server_fields(repo.cards[key]), indent=2), encoding="utf-8")
    return settle
