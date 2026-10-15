"""The call-a-model function. Every solution is ONE function with this signature:

    call(kind: str, args: dict) -> dict

Compares ways of calling a model on what does not need a model: which provider serves a model name, what
happens when a tier is out of quota or a provider errors, which errors are retried and how often, what a
call costs per provider and tier, and the arithmetic of the pricing table. Providers are fakes: a recording
fake per vendor, a flaky fake that fails named models, and the real ``DryRunProvider`` (it estimates tokens
from prompt length and calls nothing). No network, no key, no model.

Kinds and the dict a solution returns:
  route_model    args {model, providers}            -> {"served": name of the provider that handled it}
  tier_fallback  args {top, fail, model, calls, registry} -> {"served": [answer or error class per call],
                                                      "tried": [model per attempt, in order]}
  retry          args {errors, max_retries}          -> {"attempts", "ok", "error": last class | None}
  error_text     args {type, message}               -> {"text": the user-facing message}
  dryrun_tokens  args {call, chars, json}           -> {"tokens_in", "tokens_out"}
  price          args {provider, model}             -> {"usd_per_mtok"}
  price_family   args {provider, models}            -> {"distinct": distinct input prices across the spellings}
  estimate       args {tokens, env}                 -> {"usd", "provider", "model"}
  tier_cost      args {models, chars}               -> {"usd": {tier: input cost of one plan call}, "order": tiers cheapest first,
                                                      "strict": True when every tier has a different price}

What this does NOT measure: latency and pass rate of a real model per tier. That needs live calls at each
tier (``QAR_EVAL_MODEL`` sweeps through the classification and quest_ops components) and is waived in
waivers.csv S10 (call-a-model:unmeasured live arm). The pricing table is INPUT tokens only by design
(its module says so); output cost is not modelled.

An implementation module in ``impls/`` exposes ``build(meter) -> call``. No solution here calls a model.
"""
