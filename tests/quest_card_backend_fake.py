"""An in-process fake of the Quest backend's card API (stdlib http.server on a thread).

Implements the contract ``QuestApiCardRepository`` speaks: whoami, embedder, stamp, paged list with
``updated_after``, search, PUT/GET/DELETE with ``team_id`` or ``org_id`` scope (an org card is stored under the key ``org:<id>``), team lookup
(``GET /api/teams/{id}`` from ``teams``), and ``POST /bulk``. Offline and
deterministic; ids and urls are obviously fake.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeCardBackend:
    def __init__(self, user_id="user_a", embedder_id="voyage:test-model", dimension=3):
        self.user_id = user_id
        self.embedder_id = embedder_id
        self.dimension = dimension
        self.cards = {}            # (team_id, card_id) -> card (with updated_at float)
        self.clock = 100.0
        self.requests = []         # (method, path, query dict, headers dict, body)
        self.fail_next = []        # list of (status, detail) consumed per request
        self.fail_paths = {}       # path -> (status, detail), applied to every request for it
        self.bulk_batches = []
        self.vectors_seen = {}
        self.whoami_status = 200
        self.teams = {}            # team_id -> team doc (for GET /api/teams/{id})
        self.page_size_cap = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def put(self, card_id, card, team_id="", org_id=""):
        self.clock += 1
        team_id = f"org:{org_id}" if org_id else team_id
        self.cards[(team_id, card_id)] = {**card, "id": card_id, "updated_at_epoch": self.clock}

    def _visible(self, q):
        teams = [t for t in (q.get("team_ids", [""])[0]).split(",") if t]
        out = []
        for (team, cid), card in self.cards.items():
            if teams and team and not team.startswith("org:") and team not in teams:
                continue
            c = {k: v for k, v in card.items() if k != "updated_at_epoch"}
            if team.startswith("org:"):
                c["org_id"] = team[4:]
            elif team:
                c["team_id"] = team
            c["updated_at"] = card["updated_at_epoch"]
            out.append((card["updated_at_epoch"], c))
        return [c for _, c in sorted(out, key=lambda x: x[0])]

    def _handler(self):
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method):
                parsed = urlparse(self.path)
                q = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                path = parsed.path
                backend.requests.append((method, path, {k: v[0] for k, v in q.items()},
                                         dict(self.headers), body))
                if path in backend.fail_paths:
                    status, detail = backend.fail_paths[path]
                    return self._send(status, {"detail": detail})
                if backend.fail_next:
                    status, detail = backend.fail_next.pop(0)
                    return self._send(status, {"detail": detail})
                team = (q.get("team_id") or [""])[0]
                if (q.get("org_id") or [""])[0]:
                    team = "org:" + q["org_id"][0]
                if path.startswith("/api/teams/") and path != "/api/teams/whoami" and method == "GET":
                    doc = backend.teams.get(path.rsplit("/", 1)[-1])
                    return self._send(200, doc) if doc is not None else self._send(404, {"detail": "no team"})
                if path == "/api/teams/whoami":
                    if backend.whoami_status != 200:
                        return self._send(backend.whoami_status, {"detail": "bad key"})
                    return self._send(200, {"user_id": backend.user_id})
                if path == "/api/cards/embedder":
                    return self._send(200, {"embedder_id": backend.embedder_id,
                                            "dimension": backend.dimension})
                if path == "/api/cards/stamp":
                    cards = backend._visible(q)
                    mx = max([c["updated_at"] for c in cards], default=0.0)
                    return self._send(200, {"count": len(cards), "max_updated_at": mx,
                                            "revision": f"{len(cards)}:{mx}"})
                if path == "/api/cards/search":
                    needle = (q.get("q") or [""])[0].lower()
                    hits = [c for c in backend._visible(q) if needle in json.dumps(c).lower()]
                    return self._send(200, {"cards": hits[: int((q.get('max_results') or ['10'])[0])]})
                if path == "/api/cards/bulk" and method == "POST":
                    backend.bulk_batches.append(body)
                    results, counts = [], {"written": 0, "embedded": 0, "vectors_accepted": 0,
                                           "vectors_rejected": 0, "failed": 0}
                    ok_vec = (body.get("embedder_id") == backend.embedder_id
                              and body.get("dimension") == backend.dimension)
                    for item in body["cards"]:
                        vec = item.get("vector")
                        accepted = bool(vec) and ok_vec and len(vec) == backend.dimension
                        if vec:
                            counts["vectors_accepted" if accepted else "vectors_rejected"] += 1
                            if accepted:
                                backend.vectors_seen[item["id"]] = vec
                        if not accepted:
                            counts["embedded"] += 1
                        counts["written"] += 1
                        backend.put(item["id"], item["card"], body.get("team_id") or "", body.get("org_id") or "")
                        results.append({"id": item["id"], "ok": True, "embedded": not accepted,
                                        "vector_accepted": accepted})
                    return self._send(200, {"results": results, "counts": counts,
                                            "embedder_id": backend.embedder_id,
                                            "dimension": backend.dimension})
                if path == "/api/cards" and method == "GET":
                    cards = backend._visible(q)
                    after = float((q.get("updated_after") or ["0"])[0])
                    cards = [c for c in cards if c["updated_at"] > after]
                    limit = int((q.get("limit") or ["0"])[0] or 0)
                    if backend.page_size_cap and (not limit or limit > backend.page_size_cap):
                        limit = backend.page_size_cap
                    start = int((q.get("cursor") or ["0"])[0])
                    nxt = None
                    if limit:
                        page = cards[start:start + limit]
                        if start + limit < len(cards):
                            nxt = str(start + limit)
                        cards = page
                    return self._send(200, {"cards": cards, "next_cursor": nxt, "revision": "r"})
                if path.startswith("/api/cards/"):
                    cid = path[len("/api/cards/"):]
                    if method == "PUT":
                        backend.put(cid, body, team)
                        return self._send(200, {"ok": True, "id": cid, "team_id": team if not team.startswith("org:") else None,
                                                "org_id": team[4:] if team.startswith("org:") else None,
                                                "revision": "r", "updated_at": "t", "embedded": True})
                    if method == "GET":
                        card = backend.cards.get((team, cid))
                        return self._send(200, card) if card else self._send(404, {"detail": "nope"})
                    if method == "DELETE":
                        existed = backend.cards.pop((team, cid), None) is not None
                        return self._send(200, {"ok": True, "deleted": existed})
                return self._send(404, {"detail": "unknown route"})

            def do_GET(self):
                self._handle("GET")

            def do_PUT(self):
                self._handle("PUT")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        return Handler
