"""Quest operations for top intents and their hidden ESOI specification.

Role: six registry tools (see ``tools_quest_ops`` for how the registry shows an operation to a run).
``list_intents`` and ``get_spec`` read; ``refresh_spec`` asks the server to write or update a
specification (one hash-gated call, so a run whose inputs did not change writes nothing);
``edit_spec_record`` changes one record the AI wrote; ``place_quest`` puts a quest under an intent (a top intent or a sub-intent);
``create_sub_intent`` asks for a new sub-intent under an implementation record of an intent.

The server owns every rule: it refuses a field the person edited (FIELD_LOCKED) and holds a rename,
a vision change or a move of a quest the person placed as an ask for them (HTTP 202). The tools only
report which of those happened, in words, so a run never says "done" for a change that was refused
or is waiting for a person's yes.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .tools import STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec, quest_client_from_env
from .tools import quest_credentials_present
from .tools_status import ToolStatus
from .tools_quest_ops import NO_QUEST, QUEST_OPERATION_BUILDERS, read_failure_reason, trim

SUBJECT_TYPES = ("intent", "quest")
REFRESH_REASONS = ("first_open", "autopilot", "onboarding", "chat", "task", "manual")
OWNER_TYPES = ("user", "team", "org")
OPPORTUNITY_MAX = 400
REVEAL_LEVELS = ("private", "existence", "name", "full")

OWNER_PARAMS = {
    "owner_type": {"type": "string", "enum": list(OWNER_TYPES),
                   "description": "Whose intents: user (default), team or org."},
    "owner_id": {"type": "string", "description": "The team or org id; omit for the person's own."},
}
SUBJECT_PARAMS = {
    "subject_type": {"type": "string", "enum": list(SUBJECT_TYPES),
                     "description": "intent, or quest (default when this run is on a quest)."},
    "subject_id": {"type": "string",
                   "description": "The intent id, or the quest id (defaults to this run's quest)."},
}


def subject_of(args: Mapping[str, Any], ctx: ToolContext):
    """The (type, id) a call is about; a run on a quest defaults to that quest."""
    subject_id = str(args.get("subject_id") or "").strip()
    subject_type = str(args.get("subject_type") or "").strip().lower()
    subject_type = subject_type or "quest"
    if subject_type not in SUBJECT_TYPES:
        return None, None
    if not subject_id and subject_type == "quest":
        subject_id = str(ctx.quest_id or "")
    return subject_type, subject_id or None


def refusal_result(result: Mapping[str, Any], what: str) -> ToolResult:
    """A write the server declined, or one that was sent and never answered (``outcomeUnknown``): the second is not a refusal."""
    if result.get("outcomeUnknown"):
        return ToolResult(ok=False, status=ToolStatus.UNKNOWN, text=(
            f"Outcome unknown ({what}): the request was sent but Quest did not answer, so it may or may not have been "
            f"applied. Read it again before trying again."))
    return ToolResult(ok=False, status=ToolStatus.REFUSED, text=refusal_text(result, what))


def refusal_text(result: Mapping[str, Any], what: str) -> str:
    """Why a write did not land, in words a run can pass on. Never reads as success."""
    code = str(result.get("code") or "")
    if code == "FIELD_LOCKED":
        fields = ", ".join(result.get("lockedFields") or []) or "that field"
        return (f"Not changed: the person edited {fields}, so it is theirs and you may not write it. "
                f"Leave it; if you think it should change, say so in your reply as text.")
    if code == "REVISION_CONFLICT":
        return (f"Not changed: the record changed since you read it ({what}). Read it again with "
                f"get_spec and decide whether the edit still applies.")
    return f"Not changed ({result.get('status') or 'no answer'} {code}): {trim(result.get('message'), 200)}"


def pending_text(result: Mapping[str, Any], what: str) -> str:
    return (f"Held for the person: {what} needs their yes, so it is now an ask for them "
            f"(decision {result.get('decisionId')}). It is NOT done. Do not repeat it or report it as done.")


def spec_line(record: Mapping[str, Any]) -> str:
    fields = record.get("fields") or {}
    label = fields.get("name") or fields.get("problem") or fields.get("description") or ""
    if record.get("type") == "solution":   # a solution reads as a strategy: name + approach (purpose until it has one)
        statement = str(fields.get("approach") or fields.get("purpose") or fields.get("scoped_purpose") or "").strip()
        label = f"{label}: {statement}" if label and statement else label or statement
    locked = sorted(k for k, m in (record.get("fieldMeta") or {}).items() if (m or {}).get("editedByUser"))
    lock = f" [person edited: {', '.join(locked)}]" if locked else ""
    derived = " (from the quest)" if record.get("derived") else ""
    return (f"- {record.get('recordId')} {record.get('type')} rev {record.get('revision')}"
            f"{derived}: {trim(label, 100)}{lock}")


def parent_names(intents: Any) -> Dict[str, str]:
    return {str(i.get("intentId")): str(i.get("name") or "") for i in intents}


def intent_line(i: Mapping[str, Any], parents: Mapping[str, str]) -> str:
    """One intent for the model; a sub-intent names its parent and the implementation it arises from."""
    arises = i.get("arisesFrom") or {}
    sub = ""
    if arises:
        parent = parents.get(str(arises.get("intentId")), "") or str(arises.get("intentId"))
        sub = f" [sub-intent of {trim(parent, 60)}, record {arises.get('recordId')}]"
    return (f"- {i.get('intentId')} {trim(i.get('name'), 80)}{sub}: {trim(i.get('visionStatement'), 120)} "
            f"({i.get('questCount', len(i.get('quests') or []))} quests)")


def list_intents_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        client = factory()
        overview = client.list_intents(owner_type=str(args.get("owner_type") or "user"),
                                       owner_id=args.get("owner_id") or None)
        if overview is None:
            return ToolResult(ok=False, text=(
                f"The intents could not be read just now ({read_failure_reason(client)}). Do not assume "
                f"there are none."))
        intents = overview.get("intents") or []
        unsorted = overview.get("unsorted") or []
        if not intents:
            return ToolResult(ok=True, data=overview, text=(
                f"No top intents yet; {len(unsorted)} quests are unsorted. Never create a top intent "
                f"yourself: they belong to the person."))
        parents = parent_names(intents)
        subs = sum(1 for i in intents if i.get("arisesFrom"))
        lines = [intent_line(i, parents) for i in intents]
        kinds = f"{len(intents) - subs} top intents" + (f" and {subs} sub-intents" if subs else "")
        return ToolResult(ok=True, data=overview, text=(
            f"{kinds}, {len(unsorted)} unsorted quests.\n" + "\n".join(lines)))

    return ToolSpec(
        name="list_intents",
        handler=handler,
        description=("List the person's (or a team's or organization's) intents: id, name, vision "
                     "statement and how many quests sit under each, plus how many quests are unsorted. "
                     "A sub-intent shows its parent's name and the implementation record it arises from."),
        when_to_use=("You need the real top intents before placing a quest or refreshing a "
                     "specification, or the person asks what their intents are."),
        when_not_to_use="Creating or renaming an intent (the person does that). Never list from memory.",
        parameters={"type": "object", "properties": dict(OWNER_PARAMS)},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("intent", "intents", "top", "vision", "life", "priorities", "areas", "sorted"),
        timeout_seconds=30.0)


def get_spec_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        subject_type, subject_id = subject_of(args, ctx)
        if not subject_type or not subject_id:
            return ToolResult(ok=False, text="Needs subject_type (intent or quest) and subject_id.")
        client = factory()
        spec = client.get_spec(subject_type, subject_id)
        if spec is None:
            return ToolResult(ok=False, text=(
                f"The specification could not be read just now ({read_failure_reason(client)}). Do not "
                f"assume it is empty."))
        records = spec.get("records") or []
        if not records:
            return ToolResult(ok=True, data=spec, text=(
                f"The {subject_type} {subject_id} has no specification yet. refresh_spec writes one."))
        return ToolResult(ok=True, data=spec, text=(
            f"{len(records)} records for {subject_type} {subject_id} (digest {spec.get('digest')}):\n"
            + "\n".join(spec_line(r) for r in records)))

    return ToolSpec(
        name="get_spec",
        handler=handler,
        description=("Read the hidden ESOI specification of a top intent or a quest: its problems, ideals, "
                     "solutions, implementations and metrics, each with its revision and which fields "
                     "the person edited (those are theirs)."),
        when_to_use=("Before editing a specification record, or to judge whether a quest or intent has "
                     "a gap (no solution, no implementation) worth raising."),
        when_not_to_use="Answering ordinary questions about a quest: use the quest's own fields.",
        parameters={"type": "object", "properties": dict(SUBJECT_PARAMS)},
        mutates=False, workflow_step=False, origin="standard",
        keywords=("spec", "specification", "esoi", "problem", "ideal", "solution", "implementation",
                  "metric", "records", "intent"),
        timeout_seconds=30.0)


def refresh_spec_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        subject_type, subject_id = subject_of(args, ctx)
        if not subject_type or not subject_id:
            return ToolResult(ok=False, text="Needs subject_type (intent or quest) and subject_id.")
        reason = str(args.get("reason") or "task").strip().lower()
        if reason not in REFRESH_REASONS:
            return ToolResult(ok=False, text=f"reason must be one of {', '.join(REFRESH_REASONS)}.")
        client = factory()
        result = client.refresh_spec(subject_type, subject_id, reason=reason, force=bool(args.get("force")))
        if result.get("refused"):
            return refusal_result(result, f"{subject_type} {subject_id}")
        if result.get("pending"):
            return ToolResult(ok=False, status=ToolStatus.HELD, data=result, text=pending_text(result, "this specification change"))
        if result.get("skipped") == "digest_unchanged":
            return ToolResult(ok=True, status=ToolStatus.NOOP, data=result, text=(
                "Nothing changed since the last refresh, so nothing was written. Do not call it again "
                "this run."))
        applied = result.get("applied") or []
        asks = result.get("asks") or []
        lines = [f"- {a.get('recordId')} {a.get('action')}: {', '.join(a.get('fields') or [])}" for a in applied]
        lines += [f"- ask {a.get('decisionId')} ({a.get('kind')}) is waiting for the person's yes" for a in asks]
        return ToolResult(ok=True, data=result, text=(
            f"Specification refreshed: {len(applied)} records written, {len(asks)} asks raised.\n"
            + "\n".join(lines)).rstrip())

    return ToolSpec(
        name="refresh_spec",
        handler=handler,
        description=("Write or update the ESOI specification of a top intent or a quest from what it "
                     "holds now. One call, hash-gated: if nothing changed since the last refresh it "
                     "writes nothing. It never touches a field the person edited and raises an ask "
                     "before a rename, a vision change or moving a quest the person placed."),
        when_to_use=("The person asks you to write or refresh a specification. The backend also refreshes "
                     "a top intent by itself when a quest is placed, moved, completed or archived."),
        when_not_to_use=("Every turn, or right after creating a quest. Calling it again after it "
                         "answered nothing changed."),
        parameters={"type": "object", "properties": {
            **SUBJECT_PARAMS,
            "reason": {"type": "string", "enum": list(REFRESH_REASONS),
                       "description": "Why: autopilot, chat, task, first_open, onboarding or manual."},
            "force": {"type": "boolean", "description": "Skip the unchanged-inputs gate. Rarely right."}}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("spec", "specification", "esoi", "refresh", "intent", "problem", "solution", "gap"),
        timeout_seconds=120.0)


def edit_spec_record_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        record_id = str(args.get("record_id") or "").strip()
        fields = args.get("fields")
        if not record_id or not isinstance(fields, dict) or not fields:
            return ToolResult(ok=False, text="Needs record_id and fields (a column to new text map).")
        revision = args.get("expected_revision")
        client = factory()
        if revision in (None, ""):
            subject_type, subject_id = subject_of(args, ctx)
            spec = client.get_spec(subject_type, subject_id) if subject_type and subject_id else None
            row = next((r for r in (spec or {}).get("records") or [] if r.get("recordId") == record_id), None)
            if row is None:
                return ToolResult(ok=False, text=(
                    "Not changed: pass expected_revision (from get_spec), or the subject the record "
                    "belongs to so its revision can be read."))
            revision = row.get("revision")
        result = client.edit_spec_record(record_id, fields, int(revision))
        if result.get("refused"):
            return refusal_result(result, record_id)
        return ToolResult(ok=True, data=result, text=(
            f"Record {record_id} updated to revision {result.get('revision')}: {', '.join(sorted(fields))}."))

    return ToolSpec(
        name="edit_spec_record",
        handler=handler,
        description=("Change fields of one specification record you wrote. Columns are the record "
                     "type's own names (a solution: name, description, purpose, approach, out_of_scope, tags; "
                     "a metric: name, purpose, record, disadvantages, cascading_conditionals). A solution's strategy "
                     "statement is name + approach (purpose until it has an approach). A metric's disadvantages hold "
                     "what it cannot see or could reward wrongly. A costly metric's cascading_conditionals list its "
                     "stages cheapest first, as `name [key=value | key=value]` joined by ` > ` (esoi cascade). A "
                     "field the person edited is "
                     "refused; derived columns cannot be written."),
        when_to_use="A record you wrote is wrong or out of date and refresh_spec did not fix it.",
        when_not_to_use=("A field the person wrote or edited. Rewriting a record that is already "
                         "right. Rows marked from the quest (edit the quest field instead)."),
        parameters={"type": "object", "required": ["record_id", "fields"], "properties": {
            **SUBJECT_PARAMS,
            "record_id": {"type": "string", "description": "The record (esr_...) from get_spec."},
            "fields": {"type": "object", "description": "Column name to new text (a list of ids for id columns; a solution's tags "
                                                                      "are a list of {tag, risk_ids} objects, each naming the problem ids it mitigates)."},
            "expected_revision": {"type": "integer",
                                  "description": "The revision you read; looked up from the subject when omitted."}}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("spec", "specification", "record", "edit", "esoi", "intent"),
        timeout_seconds=45.0)


def place_quest_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        quest_id = str(args.get("quest_id") or ctx.quest_id or "").strip()
        if not quest_id:
            return ToolResult(ok=False, text=NO_QUEST.text)
        intent_id: Optional[str] = str(args.get("intent_id") or "").strip() or None
        client = factory()
        result = client.place_quest(quest_id, intent_id,
                                    implementation_id=str(args.get("implementation_id") or "").strip() or None,
                                    owner_type=str(args.get("owner_type") or "user"),
                                    owner_id=args.get("owner_id") or None)
        if result.get("refused"):
            return refusal_result(result, quest_id)
        if result.get("pending") is True:
            return ToolResult(ok=False, status=ToolStatus.HELD, data=result, text=pending_text(result, "this move"))
        errors = result.get("errors") or []
        if errors:
            return ToolResult(ok=False, status=ToolStatus.REFUSED, data=result, text=(
                "Not moved: " + "; ".join(f"{e.get('code')} {trim(e.get('message'), 120)}" for e in errors)))
        held = result.get("pending") or []
        if held:
            return ToolResult(ok=False, status=ToolStatus.HELD, data=result, text=pending_text(
                {"decisionId": held[0].get("decisionId")}, "moving a quest the person placed"))
        where = f"under intent {intent_id}" if intent_id else "back to unsorted"
        return ToolResult(ok=True, data=result, text=f"Quest {quest_id} placed {where}.")

    return ToolSpec(
        name="place_quest",
        handler=handler,
        description=("Put a quest under one of the person's intents, a top intent or a sub-intent (or back "
                     "to unsorted by leaving intent_id empty). Pick the most specific intent the quest "
                     "serves: a sub-intent when it fits, else its parent. Moving a quest the person placed themselves is held as an ask "
                     "for them."),
        when_to_use="Only when the person asks you to sort or move a quest.",
        when_not_to_use=("On your own initiative: moving a quest is the person's call, and the app does it "
                         "without an AI. When no intent clearly fits, leave it unsorted. Never create an "
                         "intent to fit a quest. Never re-sort quests the person has already placed."),
        parameters={"type": "object", "properties": {
            "quest_id": {"type": "string", "description": "The quest (defaults to this run's quest)."},
            "intent_id": {"type": "string", "description": "Any intent id from list_intents, sub-intents included; empty = unsorted."},
            "implementation_id": {"type": "string",
                                  "description": "The implementation record this quest carries out, if known."},
            **OWNER_PARAMS}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("place", "move", "sort", "intent", "quest", "group", "unsorted"),
        context_args={"quest_id": "quest_id"}, timeout_seconds=45.0)


def create_sub_intent_spec(env: Mapping[str, str], client_factory=None) -> ToolSpec:
    factory = client_factory or (lambda: quest_client_from_env(env))

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        record_id = str(args.get("record_id") or "").strip()
        name = str(args.get("name") or "").strip()
        opportunity = str(args.get("opportunity") or "").strip()
        if not record_id or not name or not opportunity:
            return ToolResult(ok=False, text=(
                "Needs record_id (an implementation record of the parent intent, from get_spec), name and "
                "opportunity (why it earns its own intent)."))
        if len(opportunity) > OPPORTUNITY_MAX:
            return ToolResult(ok=False, text=f"opportunity is {len(opportunity)} characters; keep it under {OPPORTUNITY_MAX}.")
        reveal = str(args.get("reveal") or "").strip() or None
        if reveal is not None and reveal not in REVEAL_LEVELS:
            return ToolResult(ok=False, text=f"reveal must be one of {', '.join(REVEAL_LEVELS)} (default private).")
        client = factory()
        result = client.create_sub_intent(record_id, name, opportunity, reveal=reveal,
                                          vision_statement=str(args.get("vision_statement") or "").strip())
        if result.get("refused"):
            return refusal_result(result, record_id)
        if result.get("pending") is True:
            return ToolResult(ok=False, status=ToolStatus.HELD, data=result, text=pending_text(result, f"the sub-intent '{trim(name, 60)}'"))
        child = result.get("intent") or {}
        return ToolResult(ok=True, data=result, text=(
            f"Sub-intent {child.get('intentId')} '{trim(child.get('name') or name, 80)}' created "
            f"(link {result.get('status')})."))

    return ToolSpec(
        name="create_sub_intent",
        handler=handler,
        description=("Ask for an implementation record of an intent to become its own sub-intent, with a "
                     "name, a vision statement and the opportunity line saying why it earns its own "
                     "intent. The server holds it as an ask for the person, so it is never created until "
                     "they say yes."),
        when_to_use=("The person asks you to split something out, or an implementation of an intent has "
                     "opportunities of its own that the parent does not serve (ESOI: When to split into "
                     "sub-intents)."),
        when_not_to_use=("An implementation that only carries out the parent, with no opportunity of its "
                         "own. A top intent (never create those). Repeating it while an ask is waiting."),
        parameters={"type": "object", "required": ["record_id", "name", "opportunity"], "properties": {
            "record_id": {"type": "string",
                          "description": "The parent's implementation record (esr_...) from get_spec."},
            "name": {"type": "string", "description": "The sub-intent's name."},
            "vision_statement": {"type": "string", "description": "Its vision statement, in the person's voice."},
            "reveal": {"type": "string", "enum": list(REVEAL_LEVELS),
                       "description": "How much of the new intent other owners may see; default private. Leave unset unless the person asked."},
            "opportunity": {"type": "string",
                            "description": f"Why it earns its own intent, one or two sentences (max {OPPORTUNITY_MAX} characters)."}}},
        mutates=True, workflow_step=False, origin="standard", on_request_only=True,
        keywords=("sub", "intent", "split", "implementation", "opportunity", "nested", "child"),
        timeout_seconds=45.0)


def with_credentials(build):
    return lambda env: build(env) if quest_credentials_present(env) else None


QUEST_INTENT_BUILDERS = {
    "list_intents": with_credentials(list_intents_spec),
    "get_spec": with_credentials(get_spec_spec),
    "refresh_spec": with_credentials(refresh_spec_spec),
    "edit_spec_record": with_credentials(edit_spec_record_spec),
    "place_quest": with_credentials(place_quest_spec),
    "create_sub_intent": with_credentials(create_sub_intent_spec),
}
STANDARD_TOOL_BUILDERS.update(QUEST_INTENT_BUILDERS)
QUEST_OPERATION_BUILDERS.update(QUEST_INTENT_BUILDERS)
