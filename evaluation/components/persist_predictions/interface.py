"""The prediction-persistence function. Every solution is ONE function with this signature:

    persist(kind: str, args: dict) -> dict

The anticipation engine learns a user's recurring asks and keeps live predictions per scope; it persists
through the ``PredictionStore`` port (patterns, predictions with precomputed views, an outcome log). Whatever
backs the port must give the same answers. Each kind is one contract scenario run against a fresh store, and the
dict returned is what the scenario observed:

  roundtrip    patterns and predictions saved for a scope come back equal
  isolation    two scopes never see each other's records
  replace      saving predictions replaces the previous set, and saving patterns keeps the live predictions
  expiry       an expired prediction is not loaded
  view         a precomputed context view is returned for its prediction id and "" for an unknown one
  clear        clear_predictions empties the scope but keeps its patterns
  log          appended outcome records are all kept
  restart      a second store object on the same storage sees what the first saved
  tolerance    unreadable or unknown scope data returns empty results and never raises

An implementation module in ``impls/`` exposes ``build(meter) -> persist``. No solution here calls a model.
"""
