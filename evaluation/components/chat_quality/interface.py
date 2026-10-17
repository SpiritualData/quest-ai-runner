"""The chat-quality function. Every solution is ONE function with this signature:

    converse(case: dict) -> dict

``case`` follows ``datasets/schema.md`` (a user message or several turns, an expected routing, a rubric, pivot facts the
answer must use, declarative database writes it must or must not make, deterministic reply checks). The returned record is
what ``runner.run_case`` produces: ``passed`` (bool), ``score`` (0..1 or None when unjudged), ``precheck`` (the hard
deterministic failures), ``judged`` (the LLM judge's per-rubric verdict), ``error`` and ``seconds``.

This is the end-to-end metric for the chat solution (``answer-a-turn``): not which planner action is picked (that is
``classification`` and the dev arm of ``quest_ops``) but whether the whole reply on a rich realistic account is grounded,
correctly routed and free of unrequested side effects. It needs the disposable world on the dev Quest
(``runner.py setup``), so its graded arm has no offline form: ``impls/dev_quest_chat.py`` SKIPs when no dev Quest is configured. The offline arm
``impls/reference_replies.py`` checks the metric itself (schema, and pre-checks that accept a reference reply and reject none).
The full dataset (``datasets/``, validated offline by ``runner.py validate``) is run with ``runner.py``; the two smoke
cases in ``cases.json`` are what ``components/run.py`` scores.

An implementation module in ``impls/`` exposes ``build(meter) -> converse``.
"""
