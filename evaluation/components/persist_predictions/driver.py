"""Shared driver for the prediction-store component: runs the ops against whatever store an implementation
supplies and reports each result in order."""
import json
from pathlib import Path

from quest_ai_runner.core.anticipation_learner import Pattern, Prediction


def run_ops(args, store):
    results = []
    for op in args["ops"]:
        name = op[0]
        try:
            results.append(apply(name, op[1:], store))
        except Exception as exc:  # noqa: BLE001 - a store that raises fails the case, visibly
            results.append(f"raised {type(exc).__name__}")
    return {"results": results}


def apply(name, rest, store):
    if name == "save_patterns":
        store.save_patterns(rest[0], [Pattern(pattern_id=p["pattern_id"], scope=rest[0], hour_bucket=9, dow=1,
                                              is_weekend=False, canonical_text=p["canonical_text"]) for p in rest[1]])
        return None
    if name == "load_patterns":
        return [p.canonical_text for p in store.load_patterns(rest[0])]
    if name == "save_predictions":
        store.save_predictions(rest[0], [Prediction(prediction_id=p["prediction_id"], text=p["text"], confidence=0.8,
                                         created_ts=1.0, expires_ts=4102444800.0)
                                         for p in rest[1]], views=rest[2] if len(rest) > 2 else None)
        return None
    if name == "load_predictions":
        return [p.prediction_id for p in store.load_predictions(rest[0])]
    if name == "load_view":
        return store.load_view(rest[0], rest[1])
    if name == "clear":
        store.clear_predictions(rest[0])
        return None
    if name == "append_log":
        store.append_log(rest[0])
        return None
    if name == "log_lines":
        path = Path(store.log_path)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    raise ValueError(name)
