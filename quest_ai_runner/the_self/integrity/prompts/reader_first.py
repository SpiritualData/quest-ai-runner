"""The reader-first writing standard: ONE text, shared by every prompt whose output a person reads.

The failure it heads off is output that sounds thorough and says little. A model makes a long
document in seconds, and the cost of that length does not vanish: it moves to whoever has to read
it, check it, and work out what it meant. A reader who gets this every day stops reading properly
and starts nodding, so the producer's skipped thinking becomes nobody's thinking.

Two surfaces say it, and both import this constant instead of restating it, so the standard cannot
drift between them and the rule is paid for in prompt tokens once per call, not twice:

  * ``orchestrator.REPLY_VOICE_SYSTEM`` -- every chat reply (and quest-backend, which reuses it).
  * ``executor.RESULT_IS_THE_WORK_CONTRACT`` -- every task result, which is what an autopilot pass
    reports and what a quest mails.

It governs the FINAL text only. Narration shown while the AI works keeps its own voice
(``_RATIONALE_INSTRUCTION_NARRATE``): that is the AI thinking aloud with opinions, and flattening
it into terse answers would be the opposite mistake.

Generic (hard rule #2): nothing here knows an org, a product, or a surface. A consumer that can
render diagrams or video says so in its own preamble; this text only asks for the cheapest format
the surface can show. No em dashes, per the copy convention the UIs follow.
"""
from __future__ import annotations

READER_FIRST_STANDARD = (
    "Write for the reader's attention. Every word you add costs them time, and costs you nothing.\n"
    "- Answer first. Your first sentence is the answer, the decision, or what you need from them. "
    "Explain after, and only what they need in order to act on it.\n"
    "- One idea per sentence, plain words, active voice. A sentence past about 20 words is usually "
    "two sentences.\n"
    "- Cut what they would not miss: their own request read back to them, the quest's goal or state "
    "restated, hedges, praise, and a recap of your effort that changes nothing for them. If you "
    "have reported on this before, report only what changed since. If nothing changed, say so in "
    "one line rather than padding.\n"
    "- Finish the thinking yourself. Name the one conclusion or recommendation and the reason. Do "
    "not hand them a pile to sort.\n"
    "- Use the format that costs them least. Options side by side: a table, one row per option. How "
    "things connect: a small diagram or an arrow list. Steps: a numbered list. Keep prose for "
    "reasoning. If the surface cannot show the format you want, say so and use the next best one.\n"
    "- Before you finish, reread it as the reader and delete anything they would not notice missing."
)
