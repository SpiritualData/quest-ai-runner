# feedback_loop: does a correction stop being needed twice

One component, two solution ids (many-to-one, on purpose). `store-feedback` (the `FeedbackStore` port:
`runner/feedback_ledger.py` records what a person said and how far it got, and turns a standing rule into a
guidance card) and `guide-by-feedback` (the `GuidanceProvider` port: `core/guidance_provider.py` puts the
relevant cards in front of the next turn) are two halves of one loop, and neither is worth anything alone, so
the metric is the whole loop: after a correction is stored, does the guidance shown to the next relevant turn
contain it, and not the irrelevant ones. Both solution rows therefore carry
`evaluation/components/run.py feedback_loop --impl ledger_guidance` as their metric command.

* `ledger_guidance` (selected): the real `FeedbackLedger`, `record_run_account`, `guidance_writer_for`,
  `GuidanceCardManager`, `UniversalGuidanceProvider` and a real `Orchestrator`; the guidance read is what the
  planner prompt carries. Only the model is scripted.
* `no_memory` (floor) and `dump_all` (baseline) show what recall alone and precision alone are worth.

Cases are synthetic (no real data). `FL-06`, `FL-09`, `FL-10` and `FL-16` are known gaps of the live provider
kept on purpose: with no vector store and no model filter, a stored rule that does not bear on a turn is still
shown while the card limit has room, so precision is 7/14. Recall is 16/16.
