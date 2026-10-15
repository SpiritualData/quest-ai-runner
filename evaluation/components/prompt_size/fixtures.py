"""Deterministic offline inputs for each assembly point (no network, no randomness, no model).

``build_inputs(case)`` turns a case's ``point`` and ``fixture`` into the inputs the live code path takes.
A fixture is either ``healthy`` (a normal day) or ``incident`` (the 2026-10-06 shape: past runs that quote
earlier briefs, each carrying its own context-updates block, plus bulk retrieval). The text is generated
from a seed, so it is stable and carries nothing private.
"""
from datetime import datetime, timezone

_WORDS = ("garden thesis chapter outline review budget donor sponsor sync draft interview coaching "
          "dataset estimate figure survey schedule ritual practice evidence method result summary "
          "decision pilot metric roadmap lesson reflection habit milestone report archive").split()


def prose(seed, chars):
    """About ``chars`` characters of stable, seed-unique prose (so two seeds never collide)."""
    out, n, i = [], 0, seed * 7919
    while n < chars:
        i = (i * 1103515245 + 12345) % 2147483648
        w = _WORDS[i % len(_WORDS)]
        out.append(w)
        n += len(w) + 1
    return f"[note {seed}] " + " ".join(out)[:chars]


def update_bundle(n, body_chars, verbatim=True):
    from quest_ai_runner.runner.context_update_bundle import ContextUpdates
    from quest_ai_runner.runner.context_update_types import ContextUpdate
    ups = [ContextUpdate(source="quest_notes", kind="note", item_id=f"n{i}", title=f"Note {i}",
                         body=prose(100 + i, body_chars), author="person",
                         occurred_at=datetime(2026, 10, 1 + i % 14, tzinfo=timezone.utc),
                         location="Quest", ref=f"U{i + 1}", excerpt=prose(200 + i, 60),
                         verbatim=verbatim, needs_response=(i % 3 == 0)) for i in range(n)]
    return ContextUpdates(card_id="q1", card_label="Fixture quest", updates=ups)


def _task(i, result):
    return dict(id=f"t{i}", task_kind="autopilot", status="done", result=result,
                worked_at=f"2026-10-0{1 + i % 9}T08:00:00+00:00", text=f"pass {i}")


def build_inputs(case):
    point, fx = case["point"], case["fixture"]
    big = fx == "incident"
    if point == "context_update_bundle":
        return dict(bundle=update_bundle(20 if big else 8, 6000 if big else 400))
    if point == "planner":
        turns = 30 if big else 6
        transcript = "\n".join(f"{'user' if t % 2 == 0 else 'assistant'}: {prose(300 + t, 500)}"
                               for t in range(turns))
        context = "\n\n".join(prose(400 + k, 2500) for k in range(6 if big else 2))
        gathered = [dict(kind="read", rel_path=f"notes/{k}.md", ok=True, step=0,
                         content=prose(500 + k, 3000)) for k in range(6 if big else 2)]
        return dict(user_message="Help me plan next week around the thesis outline.",
                    transcript=transcript, context_view=context, gathered=gathered)
    if point == "autopilot_batch":
        block = update_bundle(3, 600).as_prompt_block()
        quoted = ("Earlier pass result.\n\n" + block + "\n\n" + prose(600, 2000)) if big else prose(600, 2000)
        # the 16 quoted blocks of the incident: past runs nested one inside the next
        past = [_task(i, quoted) for i in range(16 if big else 1)]
        from quest_ai_runner.runner.autopilot_briefs import render_last_run_output, select_last_run_output
        last = render_last_run_output(select_last_run_output(past))
        adopted = [dict(id=f"a{i}", text=("Do the weekly review.\n\n" + block) if big else "Do the weekly review.")
                   for i in range(6 if big else 1)]
        return dict(quest_outcome="Finish the thesis outline", persona="a careful research partner",
                    scope_label="week of October 12", adopted_tasks=adopted,
                    next_steps=prose(700, 40000 if big else 1500), last_run=last,
                    reflection=prose(710, 3000), insights=prose(720, 3000),
                    context_updates=update_bundle(10 if big else 3, 4000 if big else 300).as_prompt_block(),
                    experiments=prose(730, 1500), asked=prose(740, 1500))
    if point == "deep_prompt":
        block = update_bundle(3, 600).as_prompt_block()
        if big:
            doctrine = "=== CONTEXT DOCTRINE ===\n" + prose(800, 3000)
            blocks = [f"=== RELEVANT PAST CONVERSATIONS ===\n{block}\n{prose(810 + i, 20000)}" for i in range(16)]
            preamble = "\n\n".join([doctrine] + blocks + [block])
            brief = "Compose the weekly pass.\n\n" + block + "\n\n" + prose(820, 30000)
        else:
            preamble = "=== CONTEXT DOCTRINE ===\n" + prose(800, 3000) + "\n\n" + block
            brief = "Compose the weekly pass.\n\n" + prose(820, 3000)
        return dict(goal="Produce this week's plan and flag what slipped.", brief=brief, preamble=preamble)
    raise ValueError(f"unknown assembly point: {point}")
