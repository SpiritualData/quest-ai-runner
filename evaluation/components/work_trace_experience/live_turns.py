"""Capture REAL turns for ``cases.json``: the real orchestrator on the Claude Code subscription, the real web search and the real
scholarly search, a seeded fictional card store for the person's records. Writes one case per turn with the real emitted ``work_trace``
events and the structural records of what the calls returned. Nothing here calls the paid API: ``QAR_MODEL_BACKEND=claude_cli``.

    python3 evaluation/components/work_trace_experience/live_turns.py --turn records --out /tmp/records.json

Turns: ``web`` (a current-facts question), ``records`` (a question about the person's own quests), ``papers`` (a scholarly question),
``unrelated`` (a coding question with the records seeded: no records step should show), ``group`` (the records question in a shared thread,
``audience`` shared). Each case also records which selected cards the answer used and which returned works the reply cited, computed
from the run's own planner facts and reply by ``quest_ai_runner.core.work_trace`` (a quote found in the card, a DOI or title in the reply). The web search needs ``GOOGLE_AI_API_KEY`` (or another
backend ``web_search`` knows) and the scholarly search ``OPENALEX_API_KEY`` in the environment; they are read by name and never
printed. The environment is rebuilt from an allowlist so no deployment's card store, tools file or keys leak into the run, and the
Quest URL is unroutable, so a write tool can reach nothing."""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2]))

KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR", "QAR_CLAUDE_PATH", "GOOGLE_AI_API_KEY", "OPENALEX_API_KEY")
KNEE = "I have a sore knee again. Given my current quests, what should I change this week?"
LOGGED = "How many runs have I logged for my marathon so far, and how are my long runs going?"
TURNS = {
    "web": dict(kind="web", surface="solo", audience="owner", message="What is the latest stable release of Python right now, and what are the headline changes in it?"),
    "rust": dict(kind="web", surface="solo", audience="owner", message="What is the latest stable Rust release, and what was the previous one?"),
    "go": dict(kind="web", surface="solo", audience="owner", message="What is the newest stable Go release and what changed in it?"),
    "records": dict(kind="records", surface="solo", audience="owner", message=KNEE),
    "logged": dict(kind="records", surface="solo", audience="owner", message=LOGGED),
    "spanish": dict(kind="records", surface="solo", audience="owner", message="Which of my quests is slipping most right now, and what is one small step for it?"),
    "papers": dict(kind="papers", surface="solo", audience="owner",
                   message="What does published research say about whether running volume or hill training raises knee injury risk in recreational marathon trainees? Please cite a few papers."),
    "papers2": dict(kind="papers", surface="solo", audience="owner",
                    message="What does the research say about spaced repetition for learning a second language? Please cite a few papers."),
    "unrelated": dict(kind="records", surface="solo", audience="owner",
                      message="In Python, what is the difference between a list and a tuple, and when should I use each?"),
    "unrelated2": dict(kind="records", surface="solo", audience="owner", message="Explain what a hash map is in two short paragraphs."),
    "greeting": dict(kind="none", surface="solo", audience="owner", message="Thanks, that is all for now."),
    "group": dict(kind="group", surface="group", audience="shared", message=KNEE),
}
#: A fictional person's records written as the cards the context store holds: ``(id, title, details, keywords)``. The title is the short name
#: a person gave the quest (the card's ``summary``, which is what the store shows as its title); the details are a note item the answer can read.
CARDS = [("marathon-april-quest", "Run a marathon in April",
          "Week 6 of 16. Left knee sore after hills, physio visit booked for Thursday. Swimming twice a week as cross-training. Long run of 24 km planned for Sunday.",
          ["marathon", "knee", "sore", "run", "long", "physio", "swim", "training", "week", "runs", "logged"]),
         ("morning-pages-habit", "Morning pages habit",
          "Skipping pages on travel days. Goal is 20 days in a row; current streak is 4 days.", ["morning", "pages", "habit", "writing", "travel", "streak"]),
         ("candle-shop-launch", "Launch the candle shop",
          "Wax shipment slipped by a week. Soft launch moved to 3 Nov. Budget 2000 USD, 1400 USD spent.", ["candles", "shop", "launch", "wax", "budget"]),
         ("spanish-practice", "Conversational Spanish",
          "25 minutes of daily practice, stuck on past tenses. Tutor session every second Tuesday.", ["spanish", "practice", "tenses", "language", "tutor"]),
         ("esoi-framework-rollout", "ESOI framework rollout",
          "Connects the ESOI framework and Quest product intent to the competitive alternatives. Owner Payal, review on Friday.", ["esoi", "framework", "product", "intent", "alternatives", "rollout"])]


def clean_env(root):
    for key in [k for k in os.environ if k not in KEEP]:
        os.environ.pop(key)
    os.environ.update(QAR_MODEL_BACKEND="claude_cli", QAR_MODEL_FAST="haiku", QAR_MODEL_BALANCED="haiku", QAR_MODEL_QUALITY="haiku",
                      QAR_MODEL_BEST="haiku", QUEST_BASE_URL="http://quest.invalid", QUEST_API_KEY="offline", QAR_STANDARD_TOOLS="1",
                      QAR_CORPUS_ROOT=root, QAR_CARDS_BACKEND="local", QAR_CONVERSATION_SEARCH="false", QUEST_AI_RECENT_CONTEXT_DISABLED="1",
                      QUEST_AI_ANTICIPATION="off")
    store = Path(root) / ".quest-context"
    store.mkdir(parents=True, exist_ok=True)
    for card_id, title, details, keywords in CARDS:
        (store / f"{card_id}.json").write_text(json.dumps({"id": card_id, "keywords": keywords, "summary": title, "conventions": [],
                                                           "usage_count": 1, "origin": "seed", "item_types": {"quest": 1},
                                                           "content": [{"id": f"{card_id}-note", "type": "note", "locator": {"text": details}}]}))


def instrument(orch, calls):
    """Record what the calls really returned, from results and never from wording."""
    web = orch.web
    search, fetch = web.search, web.fetch

    def searched(*a, **k):
        obs = search(*a, **k)
        hits = [h for h in (getattr(obs, "hits", None) or []) if isinstance(h, dict)]
        calls.append({"kind": "web_search", "hits": [{"url": h.get("url")} for h in hits],
                      "held": [{"title": str(h.get("title") or "")[:120], "url": h.get("url"), "snippet": str(h.get("snippet") or "")[:300]} for h in hits]})
        return obs

    def fetched(*a, **k):
        obs = fetch(*a, **k)
        calls.append({"kind": "web_page", "url": a[0] if a else "", "ok": getattr(obs, "kind", "error") != "error"})
        return obs
    web.search, web.fetch = searched, fetched
    invoke = orch.tools.invoke

    def invoked(name, args, ctx):
        result = invoke(name, args, ctx)
        if name == "search_scientific_literature":
            data = getattr(result, "data", None)
            data = data if isinstance(data, dict) else {}
            calls.append({"kind": "papers", "works": list(data.get("works") or []), "total": data.get("total") or 0})
        return result
    orch.tools.invoke = invoked
    plan = orch._plan

    def planned(*a, **k):
        decision = plan(*a, **k)
        calls.append({"kind": "plan_facts", "facts": list(getattr(decision, "facts_read", None) or [])})
        return decision
    orch._plan = planned


def run_turn(name):
    spec = TURNS[name]
    root = tempfile.mkdtemp(prefix="qar_trace_")
    clean_env(root)
    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator
    from quest_ai_runner.core.adapters_sinks import StreamSink
    cfg = _config_from_env()
    cfg.deep_runner = None
    orch = build_orchestrator(cfg)
    calls, frames = [], []
    instrument(orch, calls)
    started = time.time()
    result = orch.run(spec["message"], sink=StreamSink(frames.append), context_meta={"work_trace": {"audience": spec["audience"]}})
    events = [f["data"] for f in frames if f.get("type") == "work_trace"]
    cards = {}
    for f in frames:
        if f.get("type") == "context":
            for card in (f.get("data") or {}).get("card_metadata") or []:
                if isinstance(card, dict) and card.get("id") and str(card.get("adapter") or "").lower() != "turn":
                    cards[str(card["id"])] = {"id": str(card["id"]), "title": str(card.get("name") or card.get("title") or ""), "relevance": card.get("relevance_score")}
    from quest_ai_runner.core import work_trace
    facts = [f for c in calls if c.get("kind") == "plan_facts" for f in c["facts"]]
    reply = str(getattr(result, "text", "") or "")
    details = {c[0]: c[2] for c in CARDS}
    for card_id, card in cards.items():
        card["text"] = details.get(card_id.split(":")[-1], "")
    took = {cid: [f for f in facts if work_trace.cards_cited([f], {cid: card})] for cid, card in cards.items()}
    # The reference definition of "used": the planner took it AND the finished reply draws on it (core/work_trace.py).
    for card_id, card in cards.items():
        card["used"] = bool(took[card_id]) and work_trace.reply_draws_on(card, took[card_id], reply)
    calls[:] = [c for c in calls if c.get("kind") != "plan_facts"] + [{"kind": "plan_facts", "facts": facts}]
    for call in calls:
        if call.get("kind") == "papers":
            call["works"] = [dict(w, cited=work_trace.paper_cited(w, reply)) for w in call["works"]]
    calls.append({"kind": "records", "cards": list(cards.values())})
    links = [c for c in (getattr(result, "link_checks", None) or []) if isinstance(c, dict)]
    if links:
        calls.append({"kind": "links", "verdicts": [{"verdict": str(c.get("verdict") or "")} for c in links]})
    return dict(id=f"TRACE-REAL-{name.upper()}", stage="real", stratum="a-real", kind=spec["kind"], surface=spec["surface"], message=spec["message"],
                reply=str(getattr(result, "text", "") or ""), events=events, calls=calls,
                private_titles=[c[1] for c in CARDS] if spec["surface"] == "group" else [],
                seconds=round(time.time() - started, 1),
                source="real run on the Claude Code subscription (haiku), real web and scholarly search, seeded fictional card store with realistic titles")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turn", required=True, choices=sorted(TURNS))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    case = run_turn(args.turn)
    Path(args.out).write_text(json.dumps(case, indent=1))
    print(f"{case['id']}: {len(case['events'])} trace events, {case['seconds']}s -> {args.out}")


if __name__ == "__main__":
    main()
