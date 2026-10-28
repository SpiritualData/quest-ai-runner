"""CURRENT: the library's persona resolver (``runner/personas.build_persona_resolver``), unchanged: a structured
assignment field first, then domain-card dominance with names excluded, then a structural fallback. The model-judged
explicit-ask step is off (it needs a provider), so this arm measures the deterministic precedence only. Zero model calls."""
import json
import shutil
import tempfile
from pathlib import Path

from quest_ai_runner.runner.personas import PersonaRegistry, PersonaResolverConfig, build_persona_resolver


def build(meter):  # noqa: ARG001
    def select(case):
        work = Path(tempfile.mkdtemp(prefix="qar_select_rep_"))
        try:
            cards = work / "cards"
            cards.mkdir()
            for name, card in (case.get("cards") or {}).items():
                (cards / f"{name}.json").write_text(json.dumps(card), encoding="utf-8")
            cfg = PersonaResolverConfig(
                skills_root=str(work / "skills"), registry=PersonaRegistry.from_mapping(case["registry"]),
                card_activation=bool(case.get("card_activation")), cards_dir=str(cards))
            found = build_persona_resolver(cfg)(dict(case["task"]))
            return {"rep_id": found[0] if found else None}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return select
