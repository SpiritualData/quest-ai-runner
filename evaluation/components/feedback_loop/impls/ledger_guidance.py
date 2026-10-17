"""CURRENT: the live loop. A correction is observed in the real ``FeedbackLedger``; the run's receipt
(``record_run_account`` with the real ``guidance_writer_for`` and ``GuidanceCardManager``) turns a standing
one into a guidance card; the next turn goes through a real ``Orchestrator`` wired to the real
``UniversalGuidanceProvider``, and the guidance it was shown is read from the planner prompt it sent. The
only fake is the model (scripted, no network). Zero model calls."""
import os
import tempfile

import scripted_brain
from quest_ai_runner.adapters.guidance_card_manager import GuidanceCardManager
from quest_ai_runner.core.guidance_provider import UniversalGuidanceProvider
from quest_ai_runner.runner.feedback_ledger import FeedbackLedger, guidance_writer_for, record_run_account

MARKER = "--- APPLICABLE GUIDANCE ---"


def build(meter):  # noqa: ARG001
    def replay(steps):
        root = tempfile.mkdtemp(prefix="qar_feedback_loop_")
        cards_dir = os.path.join(root, "cards")
        os.makedirs(cards_dir)
        ledger = FeedbackLedger(os.path.join(root, "ledger.json"))
        writer = guidance_writer_for(GuidanceCardManager(cards_dir=cards_dir))
        provider = UniversalGuidanceProvider(cards_dir=cards_dir, manager_factory=GuidanceCardManager)
        turns, count = [], 0
        for step in steps:
            if step["op"] == "correct":
                count += 1
                item_id = f"n{count}"
                ledger.observe(card_id="quest_a", source="note", item_id=item_id, text=step["text"],
                               author="A person", location=step.get("location", ""))
                ref = f"r{count}"
                record_run_account(ledger, card_id="quest_a", offered=[(ref, "note", item_id)],
                                   dispositions={ref: [tuple(d) for d in step["dispositions"]]},
                                   guidance_writer=writer)
            else:
                brain = scripted_brain.ScriptedProvider([{"action": "answer", "rationale": "ok", "model_tier": "sonnet"}])
                scripted_brain.build_brain(brain, guidance=provider).run(step["message"], quest_id="quest_a")
                prompt = brain.plan_prompts[0] if brain.plan_prompts else ""
                shown = prompt[prompt.index(MARKER):] if MARKER in prompt else ""
                turns.append(shown.split("--- GATHERED SO FAR")[0])
        return {"turns": turns}
    return replay
