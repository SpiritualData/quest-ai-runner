# Arms in impls

*ESOI arms kept in `evaluation/components/esoi_behavior/impls`, generated from the records of the intents that own them by `esoi readmes`. Edit those records, not this page.*

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| In-situ prompt without the instruction layer | `behave-by-esoi:insitu_plain` | [`evaluation/components/esoi_behavior/impls/insitu_plain.py`](insitu_plain.py) |  |
| Shipped deep-run prompt with standing instructions | `behave-by-esoi:insitu_standing_instructions` | [`evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py`](insitu_standing_instructions.py) | selected |
| Chat-completion baseline without instructions | `behave-by-esoi:plain` | [`evaluation/components/esoi_behavior/impls/plain.py`](plain.py) |  |
| Chat-completion ablation with instructions | `behave-by-esoi:standing_instructions` | [`evaluation/components/esoi_behavior/impls/standing_instructions.py`](standing_instructions.py) |  |

## Where to go next

- Owning intent: [Integrity](../../../../quest_ai_runner/the_self/integrity/README.md)
- Repository: [top README](../../../../README.md)
