"""The escalation function. Every solution is ONE function with this signature:

    escalate(kind: str, args: dict) -> dict

When the brain meets a human-only step it must put the question in front of one named person with enough
detail to decide, and stop. The contract: every escalation reaches an addressed person (explicit
assignee, else the configured default, else the quest's owner; never an unaddressed ask), it carries its
kind, hold/proceed-on-silence default, deadline and review link, it lands on the quest's own team, the
same open ask is never filed twice, and a failing backend comes back as "not filed" instead of raising.
The one fake is the Quest HTTP client (the network edge): an in-memory decision list that records every
decision it is asked to create.

Kind ``escalate``, args:
  escalation   {summary, kind, quest_id, assignee, default_on_silence, deadline_hours, review_url}
  default_assignee, default_deadline_hours   sink configuration
  quest_owner  owner user id the fake quest reports (or absent), ``team`` the quest's owning team
  open_decisions   [{id, summary}] already open on the quest
  create_fails     the backend rejects the create; ``repeat`` how many times the same escalation is raised
Returns {"id": decision id or "", "filed": decisions created, "assignee", "kind", "default_on_silence",
"review_url", "deadline_set", "team_id"} (the last four from the last decision created).

An implementation module in ``impls/`` exposes ``build(meter) -> escalate``. No solution here calls a model.
"""
