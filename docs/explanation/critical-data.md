# Critical data: reading what the answer depends on, and checking it

A fluent reply can be wrong in a way a claims check never sees: it can answer a different question. "What is the primary human blocker right now?" was answered with the quest's
goal counts, no ask read, and the turn ended "Goal met". Two causes: an information turn ships without the goal verifier (so no verdict existed, and no verdict was shown as met), and the
verifier judges support for claims, not whether the reply addresses the request.

There are two layers, and each can catch what the other misses.

**The subconscious reads what the request depends on.** The consumer offers sources (`context_meta["critical_sources"]`, each an id and one line saying what it holds). For a matched
quest the chat offers the quest's open asks, kept as one card per quest (`quest_ask_cards.py`, fenced to the quest by `scope_tags`, written from one read, a failed read leaves the old
card alone because a failed read is `None`, never "nothing is waiting"). One narrow fast-tier question (the hub's `declare`) answers which offered sources this request depends on,
as structure. Those are read into the top of the turn's context; the rest are not, so a question about a goal's due date brings no ask into the context and the reply cannot raise one.

**Critical thinking checks the turn.** After the answer, a turn the verifier never saw is judged by the hub's selected Goal Completion: data the request depends on that nobody consulted,
or a reply the check found not to address the request as understood (`judge_answer`, one more narrow question), is not met. The hub's stop decision then says what runs next: read the named
source, or rewrite the reply with the gap named; an unreadable source makes the reply say so and ask. Nothing reads the words of the request or the reply: both questions return structure.

There is no switch: a correctness gate that can be turned off is not a gate. Its cost is held down by structure: the two-question declaration runs only when the consumer offers a source for the turn (a matched quest), the judgement after the answer runs only when the goal verifier gave no verdict (a verdict exists then, and it already judges the reply against the request), both questions run on the fast tier, and a failed check rewrites at most twice.

A consumer whose records are not cards of the context store (Quest's embedded brain keeps its decisions in a database) offers a source with its own `read`: a callable returning the text, or None when it could not be read. The brain offers each quest in scope's open asks this way and the runner reads one only when the request depends on it.
