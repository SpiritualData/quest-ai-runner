"""The experience of the AI's work (metric ``metric-work-trace-experience``). Every arm is ONE function:

    experience(case: dict) -> dict          {"arm": name, "views": {variant name: {"live", "settled", "expanded"}}}

``case`` is one finished turn (``cases.json``): the person's message, the reply, the ``work_trace`` events the run emitted in arrival
order, the structural records of what its calls really did, and whether the thread is solo or a group. An arm decides what each
viewer of that turn is shown: nothing (the status quo), a plain list of every step, or the Trail. The shown elements come from
``quest-frontend/scripts/run-worktrace-view.js``, which runs the real rules of ``src/utils/workTrace.ts`` and the real English strings;
an arm that is not the Trail has its own composition there, so the three are measured by the same scorer on the same turns.

Scoring (``score.py``), the same for every arm:
  1. Truthfulness, a hard gate: the emitted events pass ``work_trace_check`` (counts not overstated, no private titles in a shared
     thread, every step terminal) AND what is shown holds: no number above what the calls did, no private title for a shared viewer,
     no URL, path, dash or internal word. One failure and the turn is not passed.
  2. Value: the share of shown elements a rubric judge says change what the person does next or build justified confidence in the
     answer. Nothing shown unless it earns its place. A turn with real work that shows nothing earns nothing (the person gets no
     confidence), and a turn without work that shows something earns nothing. Judged on turns captured from real runs only.
  3. Parity: the settled and expanded views are the same for the sender and for the other participants, solo and group, live (with a
     resumed stream repeating frames) and from history.
A turn is passed when 1 and 3 hold, something is shown when there was work to show, and (on real turns) every shown element earns its
place. ``cases.json`` marks each case ``stage``: ``offline`` (hand-built from the shapes real runs produced, no model call) or ``real``
(captured from a real run by ``live_turns.py``, judged by one Claude Code subscription call per case).
"""
