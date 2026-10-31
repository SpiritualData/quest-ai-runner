"""Per-turn helpers: the card cache and the progress emitter (the hub's) and the narrator (QAR's, with its prompt).

Split out of ``orchestrator.py`` by definition; ``orchestrator`` re-exports every name here.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

from .prompt_specs import PromptSpec, register, render_prompt

from hubofhubs.ports.model import STEP_NARRATION, answer_with_reasoning
from hubofhubs.ports.scope import ScopedThreadPoolExecutor as ThreadPoolExecutor
from hubofhubs.ports.events import EVENT_PARTIAL, EVENT_STATUS, Mode  # noqa: F401  (EVENT_STATUS and Mode stay importable from here)
from hubofhubs.orchestrate.turn import Emitter as HubEmitter, TurnCardCache  # noqa: F401
from hubofhubs.ports.deep import ProgressEvent


# The per-turn cache and the event router are the hub's (hubofhubs.orchestrate.turn); the old names stay importable.
Emitter = HubEmitter
_Emitter = HubEmitter


# Cap on how many prior-turn narration lines a consumer's ``prior_narration`` can carry into a
# Narrator: bounds prompt growth from a long voice conversation to the last handful of beats, which
# is enough to stop the ack repeating its own recent pattern without re-reading a whole session.
_MAX_PRIOR_NARRATION = 8
# How much of the user's message the first beat sees (it is one short sentence, so a long message costs little).
_NARRATION_MESSAGE_CHARS = 3000


class Narrator:
    """Conversational, single-train-of-thought progress narration for one turn.

    Unified, latency-free flow with TWO sources of beats, both emitted as EVENT_PARTIAL (shown live
    in the chat bubble, not persisted, and spoken on voice):

    * The FIRST beat (the instant ack) is one cheap call started with ``begin()`` and collected with
      ``flush_first()``, so it runs CONCURRENTLY with context assembly and adds no wall-clock latency
      to quick turns. It is grounded in the user's new message + recent conversation + persona, and
      claims no findings (nothing has been read yet), so it cannot fabricate.
    * Every later beat is the planner's OWN ``rationale``, written conversationally in the rep's
      voice in the planning call the orchestrator already makes (Approach B), and handed here via
      ``relay(line)``. ``relay`` does NO LLM call — it just emits the line — so per-step narration is
      free and never blocks the turn.

    Generation (for the ack only) uses the cheap planner tier and SPEAKS IN THE SELECTED REP'S
    PERSONA when one is given. HOW the ack narrates is overridable by the consumer via
    ``system_prompt`` (the persona is always layered on top). Every failure is swallowed: the turn
    never depends on narration.

    Cross-turn awareness (``prior_narration``): a fresh ``Narrator`` is built for every turn, so
    without help the ack has no memory of what it already said in EARLIER turns of the same
    conversation, only of ``transcript_tail`` (the actual message content). On a voice consumer that
    speaks every beat aloud, that gap is what makes consecutive turns each open with their own
    differently-worded but equally generic "let me look into that" / "searching for that now" line:
    each ack sounds fresh to the model, so it never learns to stop. The caller (a consumer that
    persists a short rolling history of what actually reached this conversation's audio) can pass
    those lines in via ``prior_narration``; they seed both the repeat-detector (``_is_repeat``) and
    the ack/relay prompts with an explicit instruction to go quiet on that pattern rather than vary
    the wording. ``_said`` (this turn's own beats) stays separate so a consumer reading it back after
    the turn (e.g. via ``OrchestratorResult.narration_said``) gets exactly what to persist forward,
    with no double-counting of the seed.
    """

    DEFAULT_SYSTEM = (
        "You are working on the user's request right now and you keep them company while you work, "
        "saying in the first person what you are about to do or what you just noticed. Each line "
        "continues the same thread as the earlier ones and reacts to whatever just came in. Keep each "
        "line to one short, natural, spoken sentence. No lists, no markdown, no greeting, no "
        "restating the request back, no sign-off. Stay light and human, never robotic status labels. "
        "Connect to what was last said or done with the user when it helps the moment feel "
        "continuous. Do NOT use em dashes; use a comma or a period. Only speak when you have a "
        "concrete, specific thing to name (an actual finding, a specific next step, a real detail) "
        "worth saying out loud; a generic line that just restates that you are working on it, "
        "searching, or checking something, without naming anything specific, is not worth saying "
        "twice in a row and is not worth saying again if you already said one like it earlier in "
        "this same conversation. If nothing fresh and specific is worth saying at this moment, reply "
        "with an empty string rather than repeat the shape of an earlier line."
    )

    def __init__(self, *, provider: Any, model: str, emit: "_Emitter",
                 persona: str = "", transcript_tail: str = "",
                 system_prompt: Optional[str] = None, enabled: bool = True,
                 prior_narration: Optional[List[str]] = None):
        self._provider = provider
        self._model = model
        self._emit = emit
        self._persona = (persona or "").strip()
        # Lines already spoken aloud in EARLIER turns of this conversation (consumer-supplied, most
        # recent last). Read-only here: used for repeat detection and shown to the model, but never
        # mutated or re-emitted, so this turn always answers "what have I ALREADY told this user, in
        # total" without re-narrating any of it.
        self._prior: List[str] = list(prior_narration or [])[-_MAX_PRIOR_NARRATION:]
        self._recent = (transcript_tail or "").strip()[-800:]
        self._system = (system_prompt or self.DEFAULT_SYSTEM).strip()
        self.enabled = bool(enabled and provider is not None)
        self._said: List[str] = []
        self._first_future: Optional[Any] = None
        self._executor: Optional[ThreadPoolExecutor] = None
        # Guards _said + the emit call so the background first beat (emitted from its own thread the
        # instant it is ready) and a main-thread relay() beat never interleave their append+emit.
        self._lock = threading.Lock()

    def _gen(self, moment: str) -> Optional[str]:
        msgs: List[Dict[str, str]] = [{"role": "system", "content": self._system}]
        if self._persona:
            msgs.append({"role": "system", "content": "Speak as this persona:\n" + self._persona})
        user = ""
        if self._recent:
            user += f"The conversation so far (recent):\n{self._recent}\n\n"
        if self._prior:
            user += (
                "Lines you already said out loud in EARLIER turns of this same conversation "
                "(do not say another line in this same generic shape; only speak now if you have "
                "something concretely new to name):\n"
                + "\n".join(f"- {s}" for s in self._prior) + "\n\n"
            )
        if self._said:
            user += "What you've already said out loud this turn:\n" + "\n".join(self._said) + "\n\n"
        user = render_prompt("narrate-a-turn.first-beat", context=user, moment=moment)
        msgs.append({"role": "user", "content": user})
        line = answer_with_reasoning(self._provider, msgs, model=self._model, step=STEP_NARRATION)
        # A provider swallows a model error (or a refusal) and hands back a stand-in apology string,
        # so the text alone cannot tell a real beat from a failure. The structural signal is the
        # provider's own per-step outcome (``step_outcomes``, set when it declares one): a failed
        # narration call yields no beat at all, never the stand-in.
        outcomes = getattr(self._provider, "step_outcomes", None)
        if isinstance(outcomes, dict) and outcomes.get(STEP_NARRATION):
            return None
        return line

    def _say(self, line: Optional[str]) -> None:
        if not line:
            return
        # Defensive brand cleanup: never speak em dashes even if the model slips one in.
        line = line.strip().replace("—", ", ").replace(" -- ", ", ")
        if not line:
            return
        # Emit with a trailing space so consecutive beats (ack, then each per-step beat) read as
        # separate sentences when a consumer streams them into one bubble, instead of running
        # together ("...this.I'm pulling up..."). The clean line (no trailing space) is what we
        # store for dedup. A consumer that replaces the bubble on `done` discards the spacing anyway.
        # Locked: the ack beat emits from the background thread while relay() beats emit from the
        # main thread; the lock keeps append+emit atomic so the two never interleave.
        with self._lock:
            self._said.append(line)
            try:
                self._emit.emit(ProgressEvent(type=EVENT_PARTIAL, text=line + " ",
                                              data={"narration": True}))
            except Exception:  # noqa: BLE001 — narration must never break the run
                pass

    def begin(self, user_message: str) -> None:
        """Start the first beat concurrently AND emit it the instant it is ready.

        The background task generates the one-sentence ack and emits it ITSELF (``_gen_and_say``),
        so the instant response goes out the moment the model returns, never sequenced behind the
        main pipeline's context search or guidance selection. Ordering vs later beats is preserved
        by ``flush_first()`` (a join barrier the orchestrator runs before the planner loop emits any
        relay beat), so the ack always precedes the planner's rationale.
        """
        if not self.enabled:
            return
        # The whole message when it is a normal one; a longer one is cut WITH a marker, because an unmarked cut
        # ends mid-sentence and the one-line ack then tells the user their message was cut off.
        from .planner_context import cap_section
        moment = ("the user just sent a new message: "
                  f"{cap_section(user_message or '', _NARRATION_MESSAGE_CHARS, keep='head')}")
        try:
            self._executor = ThreadPoolExecutor(max_workers=1)
            self._first_future = self._executor.submit(self._gen_and_say, moment)
        except Exception:  # noqa: BLE001
            self._first_future = None

    def _gen_and_say(self, moment: str) -> None:
        """Generate the first beat and emit it immediately from this background thread.

        Backstopped by ``_is_repeat`` against ``_prior`` (this is the one beat with no earlier
        beats THIS turn to compare against, so without ``prior_narration`` it would never be
        caught): the ack is exactly the beat that repeats a generic "let me look into that" shape
        turn after turn when the consumer has no cross-turn memory to give it.
        """
        try:
            line = self._gen(moment)
            clean = (line or "").strip()
            if clean and self._is_repeat(clean):
                return
            self._say(clean)
        except Exception:  # noqa: BLE001 — narration must never break the run
            pass

    def flush_first(self) -> None:
        """Join barrier: wait for the background first beat to finish emitting before the planner
        loop emits any later (relay) beat, so the ack always comes first. The beat emits ITSELF the
        instant it is ready (see ``begin`` / ``_gen_and_say``), so this no longer emits — it only
        joins and cleans up. A timeout just proceeds (a very slow beat still emits when ready)."""
        if self._first_future is None:
            return
        try:
            self._first_future.result(timeout=5.0)
        except Exception:  # noqa: BLE001 — timeout / cancelled / provider error: skip
            pass
        finally:
            try:
                if self._executor is not None:
                    self._executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
            self._first_future = None

    def relay(self, line: Optional[str]) -> None:
        """Emit a pre-composed beat (the planner's conversational rationale). No LLM call.

        Used for every beat after the instant ack: the planner already wrote this line in the
        rep's voice, so we just speak it. De-duplicated against what was already said this turn AND
        against ``_prior`` (earlier turns) so a planner that repeats itself across re-plan steps, or
        across separate turns of a longer voice conversation, doesn't echo.
        """
        if not self.enabled or not line:
            return
        clean = line.strip()
        if clean and self._is_repeat(clean):
            return
        self._say(clean)

    def _is_repeat(self, line: str) -> bool:
        """True if ``line`` repeats or near-repeats something already said, THIS turn or earlier.

        Catches not just exact echoes but paraphrases (the bug was six lines that all meant
        "looking up your marathon quest"): compare on a normalized word set and treat a high
        overlap as a repeat. Checked against ``_prior`` (earlier turns, consumer-supplied) as well
        as ``_said`` (this turn), since the same generic-filler repeat can happen either within one
        long turn or across the separate turns of one voice conversation. This is the backstop; the
        system/planner prompts (which are shown both lists) are the primary defense.

        A line that normalizes to NO content words at all (e.g. "Let me look into that for you",
        every word of which is stopworded away) is exactly the content-free "still searching/
        checking" filler users hear as "the same thing over and over": there is nothing left to
        word-overlap-compare, so without a special case it would never match anything and could
        repeat indefinitely. Treat it instead as a repeat the moment ANY earlier line (this turn or
        prior) was ALSO content-free, capping content-free filler to at most one per conversation.
        """
        all_prev = (*self._prior, *self._said)
        norm = self._norm(line)
        if not norm:
            return any(not self._norm(prev) for prev in all_prev)
        for prev in all_prev:
            p = self._norm(prev)
            if not p:
                continue
            if norm == p:
                return True
            shared = len(norm & p)
            # >=70% of the shorter line's words shared → same beat reworded.
            if shared and shared / min(len(norm), len(p)) >= 0.7:
                return True
        return False

    @staticmethod
    def _norm(line: str) -> frozenset:
        words = re.findall(r"[a-z0-9']+", line.lower())
        stop = {"the", "a", "an", "to", "of", "your", "you", "i", "i'm", "im", "and", "for",
                "on", "at", "is", "it", "this", "that", "let", "me", "now", "so", "what",
                "into", "look", "looking", "check", "checking", "see", "want"}
        return frozenset(w for w in words if w not in stop)


# The narrator's per-beat instruction (the system voice is ``Narrator.DEFAULT_SYSTEM``).
NARRATE_BEAT_SPEC = register(PromptSpec(
    id="narrate-a-turn.first-beat", solution="narrate-a-turn", metric="metric-narrate-a-turn",
    tier="caller", cache_layer="tail", inputs=("context", "moment"),
    template=("{context}What just happened: {moment}\n\nIn one short first-person sentence that "
              "starts with I (I'll, I'm, I found), say, in 20 words or fewer, what you are about to do or "
              "what you noticed, naming something specific from the message. "
              "Do not state today's date, a day count or any figure the person did not give. "
              "Reply with an empty string if nothing fresh is worth saying."),
))
