"""``quest_collections``: every collection that belongs to a quest, found without a spec, read incrementally.

THE PROBLEM. ``CollectionEntriesSource`` reads one collection a spec NAMES. A quest's real footprint in a
person's data is wider and nobody should have to list it: the habits linked to the quest, the journals
and trackers filed under its category, and (for a quest that manages the person's whole schedule) every
habit they keep. A quest that waits to be told which collections matter is a quest that misses the
journal entry written an hour ago.

WHAT IT FINDS, each collection once:

1. collections whose ``linkedQuestIds`` include the quest, plus the ids on the quest record
   (``linked_collection_ids``, ``habit_collection_ids``);
2. collections sharing one of the quest's top-level categories (``category_ids`` / ``category_id`` on the
   quest record, ``categoryId`` on the collection);
3. what the spec names: ``all_habits`` (every enabled habit the person keeps: the quest manages their
   whole schedule) and ``include`` (collections by ``name`` or ``id``, linked or not, each optionally with
   a ``categories`` filter on its entries, e.g. an Insights list filtered to one tag).

Spec shape::

    {"source": "quest_collections",
     "all_habits": true,
     "include": [{"name": "Dream Journal"},
                 {"name": "Insights custom list", "categories": ["Psychic Abilities"],
                  "category_field": "categories"}],
     "discover": true}                        # false = only what is named

INCREMENTAL. A collection whose ``lastEntryAt`` is older than the watermark is never downloaded. For the
rest, entries are paged newest first and the paging stops once a whole page is older than the watermark;
an entry counts when it was created OR edited (``updatedAt``) after it.

EVERY FIELD, BY NAME. Each entry is rendered with its fields' own names (from the collection's
``customFields``), skipping empty values and bookkeeping keys, in full: what the person wrote is never
summarised here. Habit collections are folded into ONE update so a person with forty habits does not
spend the bundle's cap on them; every other collection is its own update.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from quest_ai_runner.body.intake_sources.context_sources_base import _BaseSource
from quest_ai_runner.body.intake_sources.context_update_state import CollectRequest
from quest_ai_runner.body.intake_sources.context_update_types import ContextUpdate, _as_utc, log

# Entry keys that are a habit's own bookkeeping, not something the person wrote.
BOOKKEEPING_KEYS = frozenset({
    "period", "period_start", "period_end", "last_activity_date", "entry_date", "completed",
    "sessions", "habit_timer", "acted_on", "id", "_id"})
_PAGE = 50
_MAX_PAGES = 8
_MAX_COLLECTIONS = 60
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def _seconds_label(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m" if hours and minutes else (f"{hours}h" if hours else f"{minutes}m")


def field_names(collection: Dict[str, Any]) -> Dict[str, str]:
    """``{field id: name}`` from a collection's ``customFields`` (a list of field dicts, or an id->name dict)."""
    fields = collection.get("customFields") or collection.get("custom_fields") or []
    if isinstance(fields, dict):
        return {str(k): str(v.get("name") or k) if isinstance(v, dict) else str(v) for k, v in fields.items()}
    names: Dict[str, str] = {}
    for f in fields:
        if isinstance(f, dict) and f.get("id"):
            names[str(f["id"])] = str(f.get("name") or f["id"])
    return names


def render_value(value: Any) -> str:
    """One field value as text, in full; "" for anything empty (blank string, False, [], {}, None, a dict of empties)."""
    if value is None or value is False:
        return ""
    if value is True:
        return "yes"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ", ".join(p for p in (render_value(v) for v in value) if p)
    if isinstance(value, dict):
        if "value" in value and len(value) <= 2:
            return render_value(value.get("value"))
        return "; ".join(f"{k}: {t}" for k, t in ((k, render_value(v)) for k, v in value.items()) if t)
    return str(value).strip()


def render_entry(entry: Dict[str, Any], names: Dict[str, str], *, edited: bool = False) -> str:
    """One entry as a heading line plus ``- Field: value`` lines, every non-empty field by its name."""
    values = entry.get("fieldValues") or entry.get("field_values") or {}
    created = _as_utc(entry.get("createdAt") or entry.get("created_at"))
    date = str(values.get("entry_date") or "").strip() or (created.strftime("%Y-%m-%d") if created else "undated")
    lines = [f"{date}{' (edited)' if edited else ''}"]
    timer = values.get("habit_timer")
    spent = _seconds_label(timer.get("value") if isinstance(timer, dict) else timer)
    if spent:
        sessions = values.get("sessions")
        count = len(sessions) if isinstance(sessions, list) else 0
        lines.append(f"- Time: {spent}" + (f" over {count} session(s)" if count else ""))
    flat = dict(values)
    extra = flat.pop("extra_fields", None)
    if isinstance(extra, dict):
        flat.update({k: v for k, v in extra.items() if k not in flat})     # fields kept under extra_fields read by name too
    for key, value in flat.items():
        if key in BOOKKEEPING_KEYS:
            continue
        text = render_value(value)
        if not text:
            continue
        label = names.get(str(key)) or ("Completion" if key == "completionType" else str(key))
        if "\n" in text:
            lines.append(f"- {label}:\n" + "\n".join(f"  {ln}" for ln in text.splitlines()))
        else:
            lines.append(f"- {label}: {text}")
    return "\n".join(lines)


def entry_has_category(entry: Dict[str, Any], names: Dict[str, str], wanted: Sequence[str], field: str) -> bool:
    """Whether the entry's categories field holds any of ``wanted`` (case-insensitive; a missing field matches nothing)."""
    want = {str(w).strip().lower() for w in wanted if str(w).strip()}
    if not want:
        return True
    field = (field or "categories").strip().lower()
    values = entry.get("fieldValues") or entry.get("field_values") or {}
    for key, value in values.items():
        if str(key).lower() == field or str(names.get(str(key), "")).lower() == field:
            held = value if isinstance(value, (list, tuple)) else [value]
            if want & {str(h).strip().lower() for h in held}:
                return True
    return False


def new_entries(client: Any, collection_id: str, since: Optional[datetime],
                max_entries: int = 200) -> List[Tuple[Dict[str, Any], bool]]:
    """``[(entry, edited)]`` created or edited after ``since``, newest first, paging only as far as needed."""
    out: List[Tuple[Dict[str, Any], bool]] = []
    for page in range(_MAX_PAGES):
        resp = client.list_collection_entries(collection_id, page=page, limit=_PAGE) or {}
        rows = resp.get("items") if isinstance(resp, dict) else resp
        if not isinstance(rows, list) or not rows:
            break
        any_newer = False
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            created = _as_utc(entry.get("createdAt") or entry.get("created_at"))
            updated = _as_utc(entry.get("updatedAt") or entry.get("updated_at"))
            is_new = since is None or created is None or created > since
            is_edit = bool(since and updated and updated > since and not is_new)
            any_newer = any_newer or is_new
            if is_new or is_edit:
                out.append((entry, is_edit))
        pagination = resp.get("pagination") if isinstance(resp, dict) else None
        has_next = bool(pagination.get("has_next")) if isinstance(pagination, dict) else len(rows) >= _PAGE
        # A page with nothing created after the watermark means everything further down is older too
        # (newest first); this page itself was still searched for edits.
        if not has_next or not any_newer or len(out) >= max_entries:
            break
    return out[:max_entries]


class QuestCollectionsSource(_BaseSource):
    """What changed in the collections that belong to this quest (see the module docstring)."""
    name = "quest_collections"
    describes = ("new or edited entries in the habits, journals and trackers linked to the quest, "
                 "filed under its category, or named by the spec (all_habits, include)")
    judge_relevance = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        if not callable(getattr(client, "list_collections", None)) or not callable(
                getattr(client, "list_collection_entries", None)):
            return []
        all_colls = request.cache.get("collections")
        if all_colls is None:
            all_colls = client.list_collections() or []
            request.cache["collections"] = all_colls
        picked = self.discover(request, [c for c in all_colls if isinstance(c, dict)])
        considered, stale = len(picked), 0
        habit_parts: List[Tuple[Dict[str, Any], str, int, datetime]] = []
        updates: List[ContextUpdate] = []
        for cid, (coll, spec) in list(picked.items())[:_MAX_COLLECTIONS]:
            has_marker = "lastEntryAt" in coll or "last_entry_at" in coll
            last = _as_utc(coll.get("lastEntryAt") or coll.get("last_entry_at"))
            if has_marker and (last is None or (request.since and last <= request.since)):
                stale += 1               # nothing was added since the watermark: never downloaded
                continue
            names = field_names(coll)
            found = new_entries(client, cid, request.since)
            if spec.get("categories"):
                found = [(e, ed) for e, ed in found
                         if entry_has_category(e, names, spec["categories"], spec.get("category_field", ""))]
            if not found:
                continue
            body = "\n\n".join(render_entry(e, names, edited=ed) for e, ed in found)
            newest = max((_as_utc(e.get("updatedAt") or e.get("createdAt")) or _OLDEST) for e, _ in found)
            if str(coll.get("type") or "") == "habit":
                habit_parts.append((coll, body, len(found), newest))
                continue
            label = str(coll.get("name") or cid)
            what = f" (only {', '.join(spec['categories'])})" if spec.get("categories") else ""
            lines = body.splitlines()
            first_line = lines[1].lstrip("- ") if len(lines) > 1 else body
            updates.append(ContextUpdate(
                source=self.name, kind=str(coll.get("type") or "collection"), item_id=cid,
                title=f"{label}{what}: {len(found)} new or edited entr{'y' if len(found) == 1 else 'ies'}",
                body=body, verbatim=True, excerpt=first_line, occurred_at=newest, location=label,
                summary=f"{label}: {len(found)} new", raw={"collection_id": cid, "entries": len(found)}))
        if habit_parts:
            habit_parts.sort(key=lambda p: p[3], reverse=True)
            total = sum(p[2] for p in habit_parts)
            body = "\n\n".join(f"##### {p[0].get('name') or p[0].get('id')}\n{p[1]}" for p in habit_parts)
            updates.append(ContextUpdate(
                source=self.name, kind="habits", item_id="habits",
                title=f"Habits: {total} new or edited entr{'y' if total == 1 else 'ies'} in {len(habit_parts)} habit(s)",
                body=body, excerpt=", ".join(str(p[0].get("name")) for p in habit_parts[:4]),
                occurred_at=habit_parts[0][3], location="habits",
                summary=f"habits: {total} new", raw={"entries": total, "habits": len(habit_parts)}))
        request.account(considered, f"{considered} collection(s) belong to this quest, {stale} unchanged since the last look")
        return updates

    def discover(self, request: CollectRequest, colls: List[Dict[str, Any]]) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        """``{collection id: (collection, include spec)}`` for every collection this quest should look at."""
        by_id = {str(c.get("id")): c for c in colls if c.get("id")}
        by_name = {str(c.get("name") or "").strip().lower(): c for c in colls}
        picked: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        if request.opt("discover", True):
            quest = self._quest(request)
            linked = {str(i) for i in list(quest.get("linked_collection_ids") or []) + list(quest.get("habit_collection_ids") or [])}
            cats = {str(i) for i in list(quest.get("category_ids") or []) + [quest.get("category_id")] if i}
            for c in colls:
                if c.get("enabled") is False:
                    continue
                cid = str(c.get("id") or "")
                in_quest = bool(request.card_id) and request.card_id in (c.get("linkedQuestIds") or [])
                if cid in linked or in_quest or (cats and str(c.get("categoryId") or "") in cats):
                    picked.setdefault(cid, (c, {}))
        if request.opt("all_habits"):
            for c in colls:
                if c.get("enabled") is not False and str(c.get("type") or "") == "habit":
                    picked.setdefault(str(c.get("id")), (c, {}))
        for item in request.opt("include") or []:
            item = {"name": item} if isinstance(item, str) else dict(item or {})
            c = by_id.get(str(item.get("id") or "")) or by_name.get(str(item.get("name") or "").strip().lower())
            if c is None:
                log.warning("context updates: quest_collections include names no collection: %r", item)
                continue
            picked[str(c.get("id"))] = (c, item)     # an explicit include (with its filter) wins over discovery
        return picked

    @staticmethod
    def _quest(request: CollectRequest) -> Dict[str, Any]:
        """The quest record (its category and linked ids); the card passed in often carries only an outcome."""
        card = request.card or {}
        if card.get("category_ids") or card.get("category_id") or card.get("linked_collection_ids"):
            return card
        key = f"quest:{request.card_id}"
        if key not in request.cache:
            getter = getattr(request.client, "get_quest", None)
            try:
                found = getter(request.card_id) if callable(getter) and request.card_id else {}
                if not found and callable(getter):
                    # The client's home team is not always the quest's owner: retry on the owning team.
                    owner = getattr(request.client, "owning_team_for", None)
                    team = owner(request.card_id) if callable(owner) else ""
                    found = getter(request.card_id, team_id=team) if team else {}
                request.cache[key] = found or {}
            except Exception as e:  # noqa: BLE001 -- discovery by category is a refinement
                log.info("context updates: could not read quest %s for discovery (%s)", request.card_id, e)
                request.cache[key] = {}
        found = request.cache[key]
        return found if isinstance(found, dict) else {}
