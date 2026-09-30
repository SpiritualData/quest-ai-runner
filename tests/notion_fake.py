"""A fake Notion HTTP API for offline tests.

It stands in for ``urllib.request.urlopen`` and models the parts of Notion's own contract the
adapter depends on, rather than echoing whatever it is handed (an echo would pass a test the real
API fails):

  * database query is ``POST /v1/databases/{id}/query`` with ``page_size`` (at most 100),
    ``start_cursor``, ``sorts`` and ``filter``; the answer is ``{"object": "list", "results": [...],
    "has_more": bool, "next_cursor": str | None}`` and a cursor is opaque;
  * a FILTER must have Notion's shape: ``{"property": name, <type>: {<operator>: value}}`` where
    ``<type>`` equals the property's real type and ``<operator>`` is valid for that type, or
    ``{"timestamp": "last_edited_time", "last_edited_time": {...}}``, or ``{"and": [...]}`` /
    ``{"or": [...]}``. Anything else is a 400 ``validation_error``, as on the real API;
  * ``last_edited_time`` is reported at MINUTE granularity, like Notion's;
  * block children paginate the same way;
  * every request is recorded, and any request that is not a GET or a database query is recorded
    in ``writes`` and answered with 405, so a test can assert none was ever attempted.
"""
import io
import json
import re
import urllib.error
import urllib.parse
from datetime import datetime, timezone

API = "https://api.notion.com/v1"

OPS = {
    "title": {"equals", "does_not_equal", "contains", "does_not_contain", "starts_with", "ends_with",
              "is_empty", "is_not_empty"},
    "rich_text": {"equals", "does_not_equal", "contains", "does_not_contain", "starts_with",
                  "ends_with", "is_empty", "is_not_empty"},
    "number": {"equals", "does_not_equal", "greater_than", "less_than", "greater_than_or_equal_to",
               "less_than_or_equal_to", "is_empty", "is_not_empty"},
    "checkbox": {"equals", "does_not_equal"},
    "select": {"equals", "does_not_equal", "is_empty", "is_not_empty"},
    "status": {"equals", "does_not_equal", "is_empty", "is_not_empty"},
    "multi_select": {"contains", "does_not_contain", "is_empty", "is_not_empty"},
    "date": {"equals", "before", "after", "on_or_before", "on_or_after", "is_empty", "is_not_empty"},
}

DB_ID = "0123456789abcdef0123456789abcdef"
OTHER_DB_ID = "fedcba9876543210fedcba9876543210"


def dashed(compact):
    return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"


def when(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def minute_rounded(text):
    return when(text).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:00.000Z")


def rich(text):
    return [{"type": "text", "plain_text": text, "text": {"content": text}}] if text else []


def make_page(page_id, title, *, status=None, deadline=None, tags=(), created="2026-01-01T00:00:00Z",
              edited="2026-01-01T00:00:00Z", database=DB_ID, archived=False):
    """A Notion page object in the shape the query and page endpoints return."""
    props = {
        "Name": {"id": "title", "type": "title", "title": rich(title)},
        "Status": {"id": "s", "type": "status", "status": {"name": status} if status else None},
        "Deadline Date": {"id": "d", "type": "date", "date": {"start": deadline} if deadline else None},
        "Tags": {"id": "t", "type": "multi_select", "multi_select": [{"name": t} for t in tags]},
        "Points": {"id": "p", "type": "number", "number": None},
        "Done": {"id": "c", "type": "checkbox", "checkbox": False},
        "Last edited": {"id": "le", "type": "last_edited_time", "last_edited_time": edited},
    }
    return {
        "object": "page", "id": dashed(page_id), "url": f"https://www.notion.so/Row-{page_id}",
        "created_time": minute_rounded(created), "last_edited_time": minute_rounded(edited),
        "archived": archived, "parent": {"type": "database_id", "database_id": dashed(database)},
        "properties": props,
    }


SCHEMA = {
    "Name": "title", "Status": "status", "Deadline Date": "date", "Tags": "multi_select",
    "Points": "number", "Done": "checkbox", "Last edited": "last_edited_time",
}


class Resp:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, message="", error_code="validation_error"):
    body = io.BytesIO(json.dumps({"object": "error", "status": code, "code": error_code,
                                  "message": message}).encode("utf-8"))
    return urllib.error.HTTPError(API, code, message, {}, body)


class FakeNotion:
    """Notion, in memory. Install with ``monkeypatch.setattr("urllib.request.urlopen", fake)``."""

    def __init__(self, token="test-token"):
        self.token = token
        self.databases = {DB_ID: []}           # id -> [page, ...]
        self.pages = {}                        # compact id -> page
        self.blocks = {}                       # compact page id -> [block, ...]
        self.requests = []                     # (method, path, body)
        self.headers = []
        self.writes = []
        self.fail_with = None                  # an exception (or callable) raised for every request

    # ---- setup -----------------------------------------------------------------------------

    def add(self, page, database=DB_ID):
        self.databases.setdefault(database, []).append(page)
        self.pages[page["id"].replace("-", "")] = page
        return page

    def set_blocks(self, page_id, blocks):
        self.blocks[page_id] = blocks

    @property
    def methods(self):
        return {m for m, _p, _b in self.requests}

    @property
    def queries(self):
        return [(p, b) for m, p, b in self.requests if m == "POST"]

    # ---- the urlopen stand-in --------------------------------------------------------------

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        assert url.startswith(API), f"a request left api.notion.com: {url}"
        path = url[len(API):]
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append((method, path, body))
        self.headers.append({k.lower(): v for k, v in req.header_items()})
        if self.fail_with is not None:
            raise self.fail_with() if callable(self.fail_with) else self.fail_with
        if self.headers[-1].get("authorization") != f"Bearer {self.token}":
            raise http_error(401, "API token is invalid.", "unauthorized")
        bare, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if method == "POST":
            m = re.match(r"^/databases/([0-9a-f-]{36})/query$", bare)
            if not m:
                self.writes.append((method, path))
                raise http_error(405, "method not allowed", "restricted_resource")
            return Resp(self.query(m.group(1).replace("-", ""), body or {}))
        if method != "GET":
            self.writes.append((method, path))
            raise http_error(405, "method not allowed", "restricted_resource")
        m = re.match(r"^/databases/([0-9a-f-]{36})$", bare)
        if m:
            db = m.group(1).replace("-", "")
            if db not in self.databases:
                raise http_error(404, "Could not find database", "object_not_found")
            return Resp({"object": "database", "id": m.group(1),
                         "properties": {n: {"id": n[:2], "type": t} for n, t in SCHEMA.items()}})
        m = re.match(r"^/pages/([0-9a-f-]{36})$", bare)
        if m:
            page = self.pages.get(m.group(1).replace("-", ""))
            if page is None:
                raise http_error(404, "Could not find page", "object_not_found")
            return Resp(page)
        m = re.match(r"^/blocks/([0-9a-f-]{36})/children$", bare)
        if m:
            blocks = self.blocks.get(m.group(1).replace("-", ""), [])
            return Resp(self.paginate(blocks, params.get("page_size"), params.get("start_cursor")))
        raise http_error(404, f"no such route {bare}", "object_not_found")

    # ---- Notion's own behaviour ------------------------------------------------------------

    def paginate(self, items, page_size, cursor):
        size = int(page_size or 100)
        if size > 100 or size < 1:
            raise http_error(400, "page_size must be between 1 and 100")
        start = int(cursor.split(":")[1]) if cursor else 0
        chunk = items[start:start + size]
        more = start + size < len(items)
        return {"object": "list", "results": chunk, "has_more": more,
                "next_cursor": f"cursor:{start + size}" if more else None}

    def query(self, db, body):
        if db not in self.databases:
            raise http_error(404, "Could not find database", "object_not_found")
        rows = list(self.databases[db])
        if "filter" in body:
            self.check_filter(body["filter"])
            rows = [p for p in rows if self.matches(p, body["filter"])]
        for sort in body.get("sorts") or []:
            assert sort == {"timestamp": "last_edited_time", "direction": "descending"}, sort
            rows.sort(key=lambda p: p["last_edited_time"], reverse=True)
        return self.paginate(rows, body.get("page_size"), body.get("start_cursor"))

    def check_filter(self, flt):
        if not isinstance(flt, dict) or not flt:
            raise http_error(400, "filter must be an object")
        if "and" in flt or "or" in flt:
            key = "and" if "and" in flt else "or"
            if set(flt) != {key} or not isinstance(flt[key], list) or not flt[key]:
                raise http_error(400, f"body.filter.{key} must be a non-empty array")
            for sub in flt[key]:
                self.check_filter(sub)
            return
        if "timestamp" in flt:
            ts = flt["timestamp"]
            if ts not in ("created_time", "last_edited_time") or set(flt) != {"timestamp", ts}:
                raise http_error(400, "body.filter.timestamp is malformed")
            ops = flt[ts]
            if not isinstance(ops, dict) or len(ops) != 1 or next(iter(ops)) not in OPS["date"]:
                raise http_error(400, "body.filter timestamp operator is invalid")
            return
        if "property" not in flt:
            raise http_error(400, "body.filter.property should be defined")
        name = flt["property"]
        if name not in SCHEMA:
            raise http_error(400, f"Could not find property with name or id: {name}")
        kind = SCHEMA[name]
        if kind not in OPS or set(flt) != {"property", kind}:
            raise http_error(400, f"body.filter.{kind} should be defined for property {name!r}")
        cond = flt[kind]
        if not isinstance(cond, dict) or len(cond) != 1 or next(iter(cond)) not in OPS[kind]:
            raise http_error(400, f"invalid operator for a {kind} property")

    def matches(self, page, flt):
        if "and" in flt:
            return all(self.matches(page, f) for f in flt["and"])
        if "or" in flt:
            return any(self.matches(page, f) for f in flt["or"])
        if "timestamp" in flt:
            ts = flt["timestamp"]
            (op, val), = flt[ts].items()
            left, right = when(page[ts]), when(val)
            return {"on_or_after": left >= right, "after": left > right, "before": left < right,
                    "on_or_before": left <= right, "equals": left == right}[op]
        name = flt["property"]
        kind = SCHEMA[name]
        (op, val), = flt[kind].items()
        prop = page["properties"][name]
        if kind in ("title", "rich_text"):
            text = "".join(t["plain_text"] for t in prop[kind])
            return {"equals": text == val, "contains": val.lower() in text.lower(),
                    "does_not_equal": text != val, "starts_with": text.startswith(val),
                    "does_not_contain": val.lower() not in text.lower(),
                    "ends_with": text.endswith(val), "is_empty": not text,
                    "is_not_empty": bool(text)}[op]
        if kind in ("select", "status"):
            cur = (prop[kind] or {}).get("name")
            return {"equals": cur == val, "does_not_equal": cur != val,
                    "is_empty": cur is None, "is_not_empty": cur is not None}[op]
        if kind == "multi_select":
            names = [t["name"] for t in prop[kind]]
            return {"contains": val in names, "does_not_contain": val not in names,
                    "is_empty": not names, "is_not_empty": bool(names)}[op]
        if kind == "date":
            cur = (prop[kind] or {}).get("start")
            if op == "is_empty":
                return cur is None
            if op == "is_not_empty":
                return cur is not None
            if cur is None:
                return False
            left, right = when(cur if "T" in cur else cur + "T00:00:00Z"), when(val if "T" in val else val + "T00:00:00Z")
            return {"equals": left == right, "before": left < right, "after": left > right,
                    "on_or_before": left <= right, "on_or_after": left >= right}[op]
        if kind == "number":
            cur = prop[kind]
            if op == "is_empty":
                return cur is None
            if op == "is_not_empty":
                return cur is not None
            return cur is not None and {
                "equals": cur == val, "does_not_equal": cur != val, "greater_than": cur > val,
                "less_than": cur < val, "greater_than_or_equal_to": cur >= val,
                "less_than_or_equal_to": cur <= val}[op]
        if kind == "checkbox":
            return (prop[kind] == val) if op == "equals" else (prop[kind] != val)
        raise AssertionError(kind)


def paragraph(text):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}


def heading(text):
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich(text)}}
