"""The reply-guard function. Every solution is ONE function with this signature:

    guard(kind: str, args: dict) -> dict

Three small engines sit between a model's words and the person reading them: the link guard (no reply
leaves with an address nobody checked), the recipe store (a request that repeats a known operation is
recognised, one that merely sounds similar is not), and the anticipation learner (recurring asks become
suggestion chips at the time they recur). All three are deterministic once their one outside seam (the HTTP
probe, the clock) is faked, so this component runs offline, with no model and no network.

Kinds and the dict a solution returns:
  link_sanitize   args {text, policy, fetch}   -> {"text": the reply as sent, "verdicts": {url: ok|dead|unknown}}
                  ``policy`` is the LinkPolicy JSON; ``fetch`` maps an external URL to [status, detail]
                  (a URL missing from it times out). Fabricated and real links are mixed on purpose.
  link_extract    args {text}                  -> {"urls": [...], "labels": [...]}
  recipe_match    args {learned, request, scope} -> {"tool": best recipe's tool | None, "n": candidates}
                  ``learned`` is [[request, tool, args], ...] taught in order.
  recipe_learn    args {learned}               -> {"recipes": how many recipes the store holds}
  anticipation_chips  args {asks, now, recent} -> {"top": text|None, "texts": [...], "n", "patterns"}
                  ``asks`` is [[text, iso time], ...] folded in order; chips are read at ``now``.
  anticipation_match  args {predictions, actual} -> {"served": prediction text | None}
  anticipation_weight args {w, scores}         -> {"w": final weight, "pruned": bool}

The live arm of the replay engine (does the recipe fast path answer a repeat with the right tool, through
a real model?) is the ``recipe_fast_path`` component and is waived in waivers.csv S10 (needs the claude CLI).

An implementation module in ``impls/`` exposes ``build(meter) -> guard``. No solution here calls a model.
"""
