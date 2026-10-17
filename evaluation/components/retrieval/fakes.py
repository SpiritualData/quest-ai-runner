"""Fakes for the NETWORK EDGE only, so each live adapter's own code runs end to end offline.

* ``network(router)``: stands in for ``urllib.request.urlopen`` while a call runs (Notion, Drive, Chat adapters
  speak stdlib HTTP). ``router(method, url, body) -> (status, payload)``.
* ``mcp_session(...)``: the one seam ``MCPClient`` has to the SDK (``open_mcp_session``), a scripted session.
* ``remote_transport(root)``: the ``RemoteFilesAdapter`` transport, answered by the REAL ``serve-files``
  request dispatch (``files_server.RequestHandler.dispatch`` over ``FileOperations``), minus the socket.
Every record is invented: ids, names and text come from ``corpus.json``."""
import contextlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from types import SimpleNamespace

from retrieval.world import body_lines, markdown


class Response:
    def __init__(self, payload):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def network(router):
    """Replace ``urllib.request.urlopen`` for the duration of the block."""
    def urlopen(req, timeout=None):  # noqa: ARG001
        body = json.loads(req.data.decode("utf-8")) if getattr(req, "data", None) else None
        status, payload = router(req.get_method(), req.full_url, body)
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "fake", {}, io.BytesIO(b"{}"))
        return Response(payload)
    original = urllib.request.urlopen
    urllib.request.urlopen = urlopen
    try:
        yield
    finally:
        urllib.request.urlopen = original


# --- Notion --------------------------------------------------------------------------------------

NOTION_DB = "0123456789abcdef0123456789abcdef"


def dashed(compact):
    return f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-{compact[16:20]}-{compact[20:32]}"


def notion_page_id(index):
    return f"{index:032x}"


def rich(text):
    return [{"type": "text", "plain_text": text, "text": {"content": text}}] if text else []


def notion_pages(docs):
    """One page per document: Name (title), Summary (rich text) and Tags; the sections are page BLOCKS."""
    pages = {}
    for index, doc in enumerate(docs, 1):
        pid = notion_page_id(index)
        pages[pid] = dict(doc=doc, page={
            "object": "page", "id": dashed(pid), "url": f"https://www.notion.so/{doc['id']}",
            "created_time": "2026-01-01T00:00:00.000Z", "last_edited_time": "2026-01-01T00:00:00.000Z",
            "archived": False, "parent": {"type": "database_id", "database_id": dashed(NOTION_DB)},
            "properties": {
                "Name": {"id": "title", "type": "title", "title": rich(doc["title"])},
                "Summary": {"id": "s", "type": "rich_text", "rich_text": rich(doc["summary"])},
                "Doc": {"id": "d", "type": "rich_text", "rich_text": rich(doc["id"])},
            }})
    return pages


def notion_router(docs):
    pages = notion_pages(docs)

    def blocks(doc):
        out = []
        for section in doc["sections"]:
            out.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich(section["heading"])}})
            out.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(section["text"])}})
        return out

    def route(method, url, body):  # noqa: ARG001
        path = url.split("/v1", 1)[1].split("?", 1)[0]
        if method == "POST" and re.match(r"^/databases/[0-9a-f-]{36}/query$", path):
            return 200, {"object": "list", "results": [p["page"] for p in pages.values()],
                         "has_more": False, "next_cursor": None}
        m = re.match(r"^/databases/([0-9a-f-]{36})$", path)
        if m:
            return 200, {"object": "database", "id": m.group(1),
                         "properties": {"Name": {"id": "title", "type": "title"},
                                        "Summary": {"id": "s", "type": "rich_text"},
                                        "Doc": {"id": "d", "type": "rich_text"}}}
        m = re.match(r"^/pages/([0-9a-f-]{36})$", path)
        if m:
            entry = pages.get(m.group(1).replace("-", ""))
            return (200, entry["page"]) if entry else (404, {})
        m = re.match(r"^/blocks/([0-9a-f-]{36})/children$", path)
        if m:
            entry = pages.get(m.group(1).replace("-", ""))
            return (200, {"object": "list", "results": blocks(entry["doc"]), "has_more": False,
                          "next_cursor": None}) if entry else (404, {})
        return 404, {}
    return route, {entry["doc"]["id"]: pid for pid, entry in pages.items()}


# --- Google Drive --------------------------------------------------------------------------------

DRIVE_FOLDER = "folder-handbook"
MIME_DOC = "application/vnd.google-apps.document"
MIME_FOLDER = "application/vnd.google-apps.folder"


def drive_router(docs):
    by_id = {d["id"]: d for d in docs}

    def route(method, url, body):  # noqa: ARG001
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.split("/drive/v3/", 1)[1]
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if path == "files":
            if DRIVE_FOLDER not in params.get("q", ""):
                return 200, {"files": []}
            return 200, {"files": [{"id": d["id"], "name": d["title"], "mimeType": MIME_DOC} for d in docs]}
        m = re.match(r"^files/([^/]+)/export$", path)
        if m and m.group(1) in by_id:
            return 200, markdown(by_id[m.group(1)]).encode("utf-8")
        m = re.match(r"^files/([^/]+)$", path)
        if m:
            fid = m.group(1)
            if fid == DRIVE_FOLDER:
                return 200, {"id": fid, "name": "Handbook", "mimeType": MIME_FOLDER}
            if fid in by_id:
                return 200, {"id": fid, "name": by_id[fid]["title"], "mimeType": MIME_DOC}
        return 404, {}
    return route


# --- Google Chat ---------------------------------------------------------------------------------

CHAT_SPACE = "spaces/HANDBOOK"


def chat_router(docs, now):
    def stamp(seconds_ago):
        import datetime
        return datetime.datetime.fromtimestamp(now - seconds_ago, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    messages = []
    for index, doc in enumerate(docs):
        for line_no, line in enumerate(body_lines(doc)):
            messages.append({"name": f"{CHAT_SPACE}/messages/{doc['id']}.{line_no}", "text": line,
                             "createTime": stamp(3600 + index * 60 - line_no),
                             "thread": {"name": f"{CHAT_SPACE}/threads/{doc['id']}"},
                             "sender": {"name": "users/1", "displayName": "Handbook Bot", "type": "HUMAN"}})

    def route(method, url, body):  # noqa: ARG001
        path = urllib.parse.urlparse(url).path
        if path.endswith(f"{CHAT_SPACE}/messages"):
            return 200, {"messages": messages}
        return 404, {}
    return route


# --- MCP -----------------------------------------------------------------------------------------

def mcp_session(docs):
    """A scripted ``mcp.ClientSession``: resources are the documents, one ``search_docs`` tool does the
    server-side substring search (the server's ranking is the server's, not the adapter's)."""
    by_id = {d["id"]: d for d in docs}
    tool = SimpleNamespace(name="search_docs", description="Search the handbook",
                           input_schema={"type": "object", "properties": {"query": {"type": "string"}}})
    resources = [SimpleNamespace(uri=f"doc://{d['id']}", name=d["title"], description=d["summary"],
                                 mime_type="text/markdown") for d in docs]

    class Session:
        async def list_tools(self, *, params=None):  # noqa: ARG002
            return SimpleNamespace(tools=[tool], next_cursor=None)

        async def list_resources(self, *, params=None):  # noqa: ARG002
            return SimpleNamespace(resources=resources, next_cursor=None)

        async def list_prompts(self):
            return SimpleNamespace(prompts=[])

        async def call_tool(self, name, arguments=None):
            if name != "search_docs":
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="unknown tool")], is_error=True)
            rx = re.compile((arguments or {}).get("query", ""), re.IGNORECASE)
            hits = [f"doc://{d['id']} | {d['title']}" for d in docs if rx.search(markdown(d))]
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="\n".join(hits) or "no results")],
                                   is_error=False)

        async def read_resource(self, uri):
            doc = by_id.get(uri.split("://", 1)[-1])
            if doc is None:
                raise KeyError(uri)
            return SimpleNamespace(contents=[SimpleNamespace(uri=uri, text=markdown(doc), mime_type="text/markdown")])

    session = Session()

    @asynccontextmanager
    async def open_session(spec):  # noqa: ARG001
        yield session, SimpleNamespace(server_info=SimpleNamespace(name="handbook", version="1"), instructions="")
    return open_session


# --- remote file server --------------------------------------------------------------------------

def remote_transport(root):
    from quest_ai_runner.adapters.files_server import FileOperations, RequestHandler
    handler = SimpleNamespace(operations=FileOperations(str(root)))

    def transport(url, headers, body, timeout):  # noqa: ARG001
        op = url.rsplit("/v1/", 1)[1]
        result = RequestHandler.dispatch(handler, op, json.loads(body or b"{}"))
        return 200, json.dumps(result).encode("utf-8")
    return transport
