"""The reference-resolution function. Every solution is ONE function with this signature:

    resolve(kind: str, args: dict) -> dict

A context card holds typed items: notes that are their own text, and pointers (a file, a collection, a
conversation, a query, a chat thread) that are resolved fresh each time the card is used. The contract:
every item resolves through the resolver registered for its type (the consumer's resolver wins over a
built-in), a pointer that resolves to nothing or fails still shows what it points at as an
"(unresolved)" line instead of vanishing or breaking the card, one failing resolver never costs the
other items, an abridged summary says so and names the read that fetches the full source, the character
budget and the item cap hold, and only files may be handed on as a pointer. The fakes are the consumer's
data layer: a dict of file texts and scripted resolver callables.

Kind ``render``, args:
  items      card content items [{id, type, locator, why, ts}]
  files      {path: text} the built-in file resolver reads
  consumer   {type: {"mode": "text"|"empty"|"raise", "text": ..., "style": "object"|"callable"}}
  max_ref_chars   optional total character budget
Returns {"n": items rendered, "types": [...], "text": all rendered text joined, "pointer": [pointer_eligible per item]}.

An implementation module in ``impls/`` exposes ``build(meter) -> resolve``. No solution here calls a model.
"""
