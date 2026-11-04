"""Score one private-context case, and fold every arm into ONE balanced number.

Per case, from the items the arm selected:

  needed recall   R = selected needed items / needed items            (a case with no needed item is self-contained)
  noise           N = selected irrelevant items / selected items      (0 when nothing is selected; helpful items are neutral)
  accuracy        A = harmonic mean of R and (1 - N)                  (0 when R is 0)
                  self-contained case: A = 1 if nothing was selected, else (1 - selected/3, never below 0) * (1 - N)
  size            Z = 1 - selected characters / SIZE_BUDGET_CHARS     (never below 0)
  time            T = 1 - seconds / SECONDS_BUDGET                    (never below 0)
  case score      S = WEIGHT_ACCURACY * A + WEIGHT_SIZE * Z + WEIGHT_TIME * T

The arm's balanced score is the mean of S over its cases: the ONE number that ranks the arms (accuracy, size as the cost of the
context, and time folded together). The weights and budgets below are fixed constants, written before any scoring, and a change to
them is a change of metric. Per-case recall, noise and size are details. ``ok`` (recall at least 0.5 and noise at most 0.5; a
self-contained case: at most one item selected) is a descriptive pass count only and never ranks."""
import time

from private_context_needs import dataset

WEIGHT_ACCURACY, WEIGHT_SIZE, WEIGHT_TIME = 0.75, 0.20, 0.05
SIZE_BUDGET_CHARS = 8000      # about 2000 tokens of selected context is a full size budget
SECONDS_BUDGET = 2.0
RECALL_PASS, NOISE_PASS = 0.5, 0.5


def load_cases():
    return dataset.load_cases()


def clamp(x):
    return max(0.0, min(1.0, x))


def case_numbers(selected, candidates, seconds):
    """The numbers of one case from the selected ids (unknown ids are ignored, duplicates counted once)."""
    by_id = {c["item_id"]: c for c in candidates}
    picked = [i for i in dict.fromkeys(selected) if i in by_id]
    needed = {i for i, c in by_id.items() if c["label"] == "needed"}
    irrelevant = {i for i, c in by_id.items() if c["label"] == "irrelevant"}
    n_sel = len(picked)
    noise = (sum(1 for i in picked if i in irrelevant) / n_sel) if n_sel else 0.0
    if needed:
        recall = sum(1 for i in picked if i in needed) / len(needed)
        precision = 1 - noise
        accuracy = 0.0 if recall == 0 or precision == 0 else 2 * recall * precision / (recall + precision)
        ok = recall >= RECALL_PASS and noise <= NOISE_PASS and n_sel > 0
    else:
        recall = 1.0 if n_sel == 0 else 0.0
        accuracy = 1.0 if n_sel == 0 else clamp(1 - n_sel / 3) * (1 - noise)
        ok = n_sel <= 1
    chars = sum(len(by_id[i].get("text") or "") for i in picked)
    size = clamp(1 - chars / SIZE_BUDGET_CHARS)
    speed = clamp(1 - seconds / SECONDS_BUDGET)
    score = WEIGHT_ACCURACY * accuracy + WEIGHT_SIZE * size + WEIGHT_TIME * speed
    return dict(ok=ok, recall=round(recall, 4), noise=round(noise, 4), accuracy=round(accuracy, 4), size_chars=chars,
                selected=n_sel, needed=len(needed), score=round(score, 4), self_contained=not needed)


def score_case(fn, case):
    shown = [{k: c.get(k) for k in ("item_id", "type", "text")} for c in case["candidates"]]   # labels never reach the arm
    t0 = time.perf_counter()
    selected = fn(case["task"], shown)
    row = case_numbers(selected, case["candidates"], time.perf_counter() - t0)
    row.update(got=f"{row['selected']} selected, recall {row['recall']}, noise {row['noise']}, {row['size_chars']} chars",
               note=f"balanced {row['score']}")
    return row


def mean(rows, key):
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def aggregate(rows):
    if not rows:
        return "no cases loaded (QAR_PRIVATE_CONTEXT_DATASET absent or empty); balanced score not computed"
    has = [r for r in rows if not r["self_contained"]]
    own = [r for r in rows if r["self_contained"]]
    return (f"balanced score {mean(rows, 'score'):.4f} over {len(rows)} cases (accuracy {WEIGHT_ACCURACY}, size {WEIGHT_SIZE}, time "
            f"{WEIGHT_TIME}); details: needed recall {mean(has, 'recall'):.3f}, noise {mean(has, 'noise'):.3f}, "
            f"selected size {mean(rows, 'size_chars'):.0f} chars, self-contained cases left clean {sum(r['ok'] for r in own)}/{len(own)}, "
            f"descriptive threshold passes {sum(r['ok'] for r in rows)}/{len(rows)} (recall >= {RECALL_PASS}, noise <= {NOISE_PASS})")
