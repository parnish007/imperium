"""A fake OpenCode HTTP server reproducing the 1.18.32 behaviour Imperium depends on, and its quirks.

Shapes follow the OpenCode source (packages/schema/src/v1/*.ts, server/routes/instance/httpapi):
- GET /session/:id/message?limit=&before= returns the newest `limit` messages older than the cursor, in
  chronological order, with `X-Next-Cursor` (base64url JSON {id, time}) when older ones exist;
- GET /session/status lists only sessions that are not idle;
- basic auth with user "opencode" when a password is set; `?directory=` scopes session lookups.
Quirks (switch on with `quirks`): "perm_list_400" (the permission list fails, as in 1.18.32 with a websearch
ask), "rate_limit" (next requests get 429), "drop_prompt" (#46842: a prompt is saved but never run).
"""
import base64
import http.server
import json
import socketserver
import threading
import urllib.parse


def _cursor(msg):
    raw = json.dumps({"id": msg["info"]["id"], "time": msg["info"]["time"]["created"]}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(c):
    pad = "=" * (-len(c) % 4)
    d = json.loads(base64.urlsafe_b64decode(c + pad))
    return d["time"], d["id"]


class FakeOpenCode:
    def __init__(self, password=None, version="1.18.32"):
        self.password = password
        self.version = version
        self.lock = threading.RLock()
        self.sessions = {}
        self.messages = {}
        self.status = {}
        self.permissions = []
        self.questions = []
        self.quirks = set()
        self.rate_limited = 0
        self.clock = 1_700_000_000_000
        self.counter = 0
        self.requests = []
        self.permission_replies = []
        self.question_replies = []
        self.aborts = []
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.fake = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # --- state helpers for tests --------------------------------------------------------------------
    def _id(self, prefix):
        self.counter += 1
        return f"{prefix}_{self.counter:012x}fake"

    def _now(self):
        self.clock += 1000
        return self.clock

    def add_session(self, sid, directory, parent=None):
        with self.lock:
            self.sessions[sid] = {"id": sid, "directory": directory, "parentID": parent, "title": sid,
                                  "version": self.version, "time": {"created": self._now(), "updated": self.clock}}
            self.messages.setdefault(sid, [])
            return self.sessions[sid]

    def _insert(self, sid, msg):
        lst = self.messages[sid]
        lst[:] = [m for m in lst if m["info"]["id"] != msg["info"]["id"]]  # upsert on id
        lst.append(msg)
        lst.sort(key=lambda m: (m["info"]["time"]["created"], m["info"]["id"]))
        return msg

    def add_user(self, sid, text=None, *, id=None, parts=None, synthetic=False, metadata=None, compaction=None,
                 partless=False):
        with self.lock:
            mid = id or self._id("msg")
            info = {"id": mid, "sessionID": sid, "role": "user", "time": {"created": self._now()},
                    "agent": "build", "model": {"providerID": "fake", "modelID": "fake-1"}}
            if parts is None:
                parts = []
                if compaction is not None:
                    parts.append({"id": self._id("prt"), "sessionID": sid, "messageID": mid, "type": "compaction",
                                  "auto": bool(compaction)})
                if text is not None:
                    p = {"id": self._id("prt"), "sessionID": sid, "messageID": mid, "type": "text", "text": text}
                    if synthetic:
                        p["synthetic"] = True
                    if metadata:
                        p["metadata"] = metadata
                    parts.append(p)
            return self._insert(sid, {"info": info, "parts": [] if partless else parts, "_parts": parts})

    def fill_parts(self, sid, mid):
        """Finish writing a user message created with partless=True (metadata is written before parts)."""
        with self.lock:
            m = self.find(sid, mid)
            m["parts"] = m["_parts"]

    def add_assistant(self, sid, parent_id=None, text="ok", *, completed=True, finish="stop", summary=False,
                      error=None, extra_parts=None):
        with self.lock:
            if parent_id is None:
                users = [m for m in self.messages[sid] if m["info"]["role"] == "user"]
                parent_id = users[-1]["info"]["id"]
            mid = self._id("msg")
            created = self._now()
            info = {"id": mid, "sessionID": sid, "role": "assistant", "parentID": parent_id,
                    "time": {"created": created}, "modelID": "fake-1", "providerID": "fake", "mode": "build",
                    "agent": "build", "path": {"cwd": ".", "root": "."}, "cost": 0,
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
            if summary:
                info["summary"] = True
            if completed:
                info["time"]["completed"] = created + 500
                info["finish"] = finish
            if error:
                info["error"] = error
            parts = [{"id": self._id("prt"), "sessionID": sid, "messageID": mid, "type": "text", "text": text}]
            parts.extend(extra_parts or [])
            return self._insert(sid, {"info": info, "parts": parts})

    def complete(self, sid, mid, finish="stop"):
        with self.lock:
            m = self.find(sid, mid)
            m["info"]["time"]["completed"] = self._now()
            m["info"]["finish"] = finish

    def find(self, sid, mid):
        for m in self.messages[sid]:
            if m["info"]["id"] == mid:
                return m
        raise KeyError(mid)

    def delete_message(self, sid, mid):
        with self.lock:
            self.messages[sid] = [m for m in self.messages[sid] if m["info"]["id"] != mid]

    def set_status(self, sid, kind, **extra):
        with self.lock:
            if kind == "idle":
                self.status.pop(sid, None)
            else:
                self.status[sid] = {"type": kind, **extra}

    def add_permission(self, sid, permission="bash", patterns=("ls",)):
        with self.lock:
            p = {"id": self._id("per"), "sessionID": sid, "permission": permission, "patterns": list(patterns),
                 "metadata": {}, "always": list(patterns)}
            self.permissions.append(p)
            return p

    def add_question(self, sid, text="Which one?", options=("A", "B"), multiple=False):
        with self.lock:
            q = {"id": self._id("que"), "sessionID": sid,
                 "questions": [{"question": text, "header": "Pick", "multiple": multiple,
                                "options": [{"label": o, "description": o} for o in options]}]}
            self.questions.append(q)
            return q

    def restart(self, version=None):
        """Simulate a server restart: state persists (it is on disk in OpenCode), status resets."""
        with self.lock:
            if version:
                self.version = version
            self.status.clear()

    # --- request handling ---------------------------------------------------------------------------
    def handle(self, method, path, query, body, headers):
        with self.lock:
            self.requests.append((method, path, query, body))
            if self.password is not None:
                expect = "Basic " + base64.b64encode(f"opencode:{self.password}".encode()).decode()
                if headers.get("Authorization") != expect:
                    return 401, {"error": "unauthorized"}, {}
            if "rate_limit" in self.quirks and self.rate_limited > 0:
                self.rate_limited -= 1
                return 429, {"error": "too many requests"}, {}
            parts = [p for p in path.split("/") if p]
            directory = query.get("directory", [None])[0]
            if parts == ["global", "health"]:
                return 200, {"healthy": True, "version": self.version}, {}
            if parts == ["session", "status"]:
                return 200, dict(self.status), {}
            if parts == ["permission"] and method == "GET":
                if "perm_list_400" in self.quirks:
                    return 400, {"error": "serialisation failure"}, {}
                return 200, list(self.permissions), {}
            if len(parts) == 3 and parts[0] == "permission" and parts[2] == "reply":
                return self._reply(self.permissions, parts[1], body, self.permission_replies)
            if parts == ["question"] and method == "GET":
                return 200, list(self.questions), {}
            if len(parts) == 3 and parts[0] == "question" and parts[2] in ("reply", "reject"):
                return self._reply(self.questions, parts[1], {"kind": parts[2], **(body or {})}, self.question_replies)
            if len(parts) >= 2 and parts[0] == "session":
                sid = parts[1]
                s = self.sessions.get(sid)
                if s is None or (directory is not None and directory != s["directory"]):
                    return 404, {"name": "NotFoundError", "data": {"message": f"Session not found: {sid}"}}, {}
                rest = parts[2:]
                if rest == [] and method == "GET":
                    return 200, s, {}
                if rest == ["children"]:
                    return 200, [x for x in self.sessions.values() if x["parentID"] == sid], {}
                if rest == ["message"] and method == "GET":
                    return self._page(sid, query)
                if len(rest) == 2 and rest[0] == "message" and method == "GET":
                    try:
                        m = self.find(sid, rest[1])
                    except KeyError:
                        return 404, {"name": "NotFoundError"}, {}
                    return 200, {"info": m["info"], "parts": m["parts"]}, {}
                if rest == ["prompt_async"] and method == "POST":
                    return self._prompt(sid, body or {})
                if rest == ["abort"] and method == "POST":
                    self.aborts.append(sid)
                    self.status.pop(sid, None)
                    return 200, True, {}
                if rest == ["summarize"] and method == "POST":
                    self.add_user(sid, compaction=False)
                    return 200, True, {}
            return 404, {"error": f"no route {method} {path}"}, {}

    def _reply(self, items, rid, body, log):
        for i, it in enumerate(items):
            if it["id"] == rid:
                del items[i]
                log.append((rid, body))
                return 200, True, {}
        return 404, {"name": "NotFoundError"}, {}

    def _prompt(self, sid, body):
        mid = body.get("messageID") or self._id("msg")
        parts = []
        for p in body.get("parts") or []:
            q = dict(p)
            q.setdefault("id", self._id("prt"))
            q.update({"sessionID": sid, "messageID": mid})
            parts.append(q)
        info = {"id": mid, "sessionID": sid, "role": "user", "time": {"created": self._now()},
                "agent": body.get("agent", "build"), "model": {"providerID": "fake", "modelID": "fake-1"}}
        self._insert(sid, {"info": info, "parts": parts, "_parts": parts})
        return 204, None, {}

    def _page(self, sid, query):
        msgs = [{"info": m["info"], "parts": m["parts"]} for m in self.messages[sid]]
        limit = query.get("limit", [None])[0]
        before = query.get("before", [None])[0]
        if before is not None and limit is None:
            return 400, {"error": "before needs limit"}, {}
        if limit is None or int(limit) == 0:
            return 200, msgs, {}
        limit = int(limit)
        if before is not None:
            bt, bid = _decode_cursor(before)
            msgs = [m for m in msgs if (m["info"]["time"]["created"], m["info"]["id"]) < (bt, bid)]
        page = msgs[-limit:]
        headers = {}
        if len(msgs) > limit:
            headers["X-Next-Cursor"] = _cursor(page[0])
        return 200, page, headers


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _go(self, method):
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        status, payload, headers = self.server.fake.handle(method, url.path, urllib.parse.parse_qs(url.query),
                                                           body, self.headers)
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")
