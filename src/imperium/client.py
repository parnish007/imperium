"""Client for the daemon's local API. Picks its credential by tokens.select_token (no fallback)."""
import http.client
import json
import os

from . import paths, tokens


class DaemonDown(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status, message, body=None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class Client:
    def __init__(self, home, as_owner=False, env=None):
        self.home = home
        self.as_owner = as_owner
        self.env = os.environ if env is None else env
        self._cred = None

    def credential(self):
        if self._cred is None:
            self._cred = tokens.select_token(self.home, env=self.env, as_owner=self.as_owner)
        return self._cred

    def info(self):
        try:
            with open(paths.daemon_json(self.home), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            raise DaemonDown("the daemon is not running (no daemon.json); run `imperium up`") from None

    def call(self, method, path, body=None, auth=True, timeout=10):
        info = self.info()
        port = info["port"]
        headers = {"Host": f"127.0.0.1:{port}"}
        if auth:
            headers["Authorization"] = "Bearer " + self.credential()[1]
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (ConnectionError, OSError) as e:
            raise DaemonDown(f"the daemon is not reachable on port {port} ({e}); run `imperium up`") from None
        finally:
            conn.close()
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            out = {"error": raw[:200].decode("utf-8", "replace")}
        if resp.status != 200:
            raise ApiError(resp.status, out.get("error", f"HTTP {resp.status}"), out)
        return out

    def health(self):
        try:
            return self.call("GET", "/v1/health", auth=False, timeout=3)
        except (DaemonDown, ApiError):
            return None
