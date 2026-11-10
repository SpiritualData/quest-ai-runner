"""The ESOI rules for a prompt that generates, restructures or proposes solutions (Joshua, 2026-11-10; principles/application_questions.md).

ONE text shared by every such prompt, so the rule cannot drift between them. Generic: it knows no org or product.
"""

SOLUTION_ORDER_RULE = (
    "List solutions in priority order for the intent: what the intent needs first comes first, and a solution others "
    "depend on comes before them. Place a new solution by that priority, never by appending it. Elaborate a solution "
    "only through a sub-intent (an implementation of it), never by linking solutions to a parent solution."
)

APPLICATION_QUESTIONS_RULE = (
    "Keep Application Questions simple (they ask for the choice and do not restate ideals or metrics) and complete "
    "together: once every question of an intent is answered with confidence, no further choice may remain."
)
