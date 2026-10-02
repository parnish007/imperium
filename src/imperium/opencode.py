"""HTTP client for an OpenCode server (DESIGN §9.2). Shapes follow OpenCode 1.18.32's source.

The API is private and changes between versions, so the adapter declares the versions its fixtures cover;
outside them Imperium observes only [M-5].
"""
import base64
import http.client
import json
import urllib.parse

TESTED_VERSIONS = ("1.18.32",)
LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class OCError(RuntimeError):
    def __init__(self, status, body, path=""):
        super().__init__(f"OpenCode answered HTTP {status} for {path}")
        self.status = status
        self.body = body


class OCUnreachable(RuntimeError):
    pass


def canonical_endpoint(url):
    """Normalise a server URL so one server cannot be registered twice under two spellings [P2-8]."""
    u = urllib.parse.urlsplit(url.strip())
    scheme = u.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"endpoint must be http:// or https://, not {url!r}")
    host = (u.hostname or "").lower()
    if not host:
        raise ValueError(f"endpoint has no host: {url!r}")
    if host in LOOPBACK:
        host = "127.0.0.1"
    port = u.port or (443 if scheme == "https" else 80)
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{port}"


class OpenCodeClient:
    def __init__(self, endpoint, directory=None, password=None, username="opencode", timeout=10):
        self.endpoint = canonical_endpoint(endpoint)
        u = urllib.parse.urlsplit(self.endpoint)
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.directory = directory
        self.timeout = timeout
        self.auth = None
        if password:
            self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()

    def _req(self, method, path, query=None, body=None):
        q = dict(query or {})
        if self.directory is not None:
            q["directory"] = self.directory
        full = path + ("?" + urllib.parse.urlencode(q) if q else "")
        headers = {"Accept": "application/json"}
        if self.auth:
            headers["Authorization"] = self.auth
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, full, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        except (ConnectionError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise OCUnreachable(f"{self.endpoint} unreachable: {e}") from None
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = {"raw": raw[:200].decode("utf-8", "replace")}
        if resp.status >= 400:
            raise OCError(resp.status, payload, path)
        return payload, resp_headers

    def get(self, path, query=None):
        return self._req("GET", path, query)[0]

    def post(self, path, body=None):
        return self._req("POST", path, body=body if body is not None else {})[0]

    # --- read ---------------------------------------------------------------------------------------
    def health(self):
        return self.get("/global/health")

    def session(self, sid):
        return self.get(f"/session/{_q(sid)}")

    def children(self, sid):
        return self.get(f"/session/{_q(sid)}/children") or []

    def status_map(self):
        """Statuses of sessions that are not idle (an absent session is idle)."""
        return self.get("/session/status") or {}

    def messages(self, sid, limit=None, before=None):
        """(messages in chronological order, cursor for older ones or None)."""
        q = {}
        if limit is not None:
            q["limit"] = str(int(limit))
        if before is not None:
            q["before"] = before
        payload, headers = self._req("GET", f"/session/{_q(sid)}/message", q)
        return payload or [], headers.get("x-next-cursor")

    def message(self, sid, mid):
        return self.get(f"/session/{_q(sid)}/message/{_q(mid)}")

    def permissions(self):
        return self.get("/permission") or []

    def questions(self):
        return self.get("/question") or []

    # --- write (used from stage 3 on) ---------------------------------------------------------------
    def prompt_async(self, sid, message_id, parts, agent=None):
        body = {"messageID": message_id, "parts": parts}
        if agent:
            body["agent"] = agent
        return self._req("POST", f"/session/{_q(sid)}/prompt_async", body=body)[0]

    def abort(self, sid):
        return self.post(f"/session/{_q(sid)}/abort")

    def summarize(self, sid, provider_id, model_id):
        return self.post(f"/session/{_q(sid)}/summarize", {"providerID": provider_id, "modelID": model_id})

    def reply_permission(self, rid, reply, message=None):
        body = {"reply": reply}
        if message:
            body["message"] = message
        return self.post(f"/permission/{_q(rid)}/reply", body)

    def reply_question(self, rid, answers):
        return self.post(f"/question/{_q(rid)}/reply", {"answers": answers})

    def reject_question(self, rid):
        return self.post(f"/question/{_q(rid)}/reject")


def _q(s):
    return urllib.parse.quote(str(s), safe="")
