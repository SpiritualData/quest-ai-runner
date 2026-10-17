"""The contract scenarios, shared by every implementation: each takes a ``make_store(root)`` factory and a
scratch ``root`` and returns what it observed. The implementations differ only in which store they build."""
import time

from quest_ai_runner.core.anticipation_learner import Pattern, Prediction


def pattern(pid, text="what is due today"):
    return Pattern(pattern_id=pid, scope="s", hour_bucket=9, dow=1, is_weekend=False, canonical_text=text,
                   keywords=["due", "today"], weight=0.5, hits=2, misses=1, last_seen_ts=10.0, created_ts=5.0)


def prediction(pid, ttl=3600, text="what is due today"):
    now = time.time()
    return Prediction(prediction_id=pid, text=text, confidence=0.7, created_ts=now, expires_ts=now + ttl,
                      context_card_ids=["c1"])


def roundtrip(make, root, args):
    store = make(root)
    store.save_patterns("k", [pattern("p1"), pattern("p2")])
    store.save_predictions("k", [prediction("d1")])
    return {"patterns": sorted(p.pattern_id for p in store.load_patterns("k")),
            "predictions": [p.prediction_id for p in store.load_predictions("k")],
            "weight": store.load_patterns("k")[0].weight}


def isolation(make, root, args):
    store = make(root)
    store.save_patterns("a", [pattern("pa")])
    store.save_predictions("a", [prediction("da")])
    return {"other_patterns": len(store.load_patterns("b")), "other_predictions": len(store.load_predictions("b")),
            "own_patterns": len(store.load_patterns("a"))}


def replace(make, root, args):
    store = make(root)
    store.save_patterns("k", [pattern("p1")])
    store.save_predictions("k", [prediction("d1"), prediction("d2")])
    store.save_predictions("k", [prediction("d3")])
    store.save_patterns("k", [pattern("p1"), pattern("p2")])
    return {"predictions": [p.prediction_id for p in store.load_predictions("k")],
            "patterns": len(store.load_patterns("k"))}


def expiry(make, root, args):
    store = make(root)
    store.save_predictions("k", [prediction("live"), prediction("dead", ttl=-60)])
    return {"predictions": [p.prediction_id for p in store.load_predictions("k")]}


def view(make, root, args):
    store = make(root)
    store.save_predictions("k", [prediction("d1"), prediction("d2")], views={"d1": "bundle text"})
    return {"view": store.load_view("k", "d1"), "no_view": store.load_view("k", "d2"),
            "unknown": store.load_view("k", "zzz")}


def clear(make, root, args):
    store = make(root)
    store.save_patterns("k", [pattern("p1")])
    store.save_predictions("k", [prediction("d1")])
    store.clear_predictions("k")
    return {"predictions": len(store.load_predictions("k")), "patterns": len(store.load_patterns("k"))}


def log(make, root, args):
    store = make(root)
    for i in range(args.get("n", 3)):
        store.append_log({"i": i, "hit": i % 2 == 0})
    return {"logged": count_log(store)}


def count_log(store):
    path = getattr(store, "log_path", None)
    if path is not None:
        return len(path.read_text().splitlines()) if path.exists() else 0
    return len(getattr(store, "log_records", []))


def restart(make, root, args):
    make(root).save_patterns("k", [pattern("p1")])
    first = make(root)
    first.save_predictions("k", [prediction("d1")], views={"d1": "v"})
    second = make(root)
    return {"patterns": len(second.load_patterns("k")), "predictions": len(second.load_predictions("k")),
            "view": second.load_view("k", "d1")}


def tolerance(make, root, args):
    store = make(root)
    out = {"patterns": len(store.load_patterns("never")), "predictions": len(store.load_predictions("never")),
           "view": store.load_view("never", "x")}
    store.clear_predictions("never")
    store.append_log({"ok": True})
    out["raised"] = False
    return out


KINDS = dict(roundtrip=roundtrip, isolation=isolation, replace=replace, expiry=expiry, view=view, clear=clear,
             log=log, restart=restart, tolerance=tolerance)
