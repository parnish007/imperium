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
    def __init__(self, home, as_owner=False, env=None, principal=None):
        """`principal` names a credential explicitly (the builder MCP uses `builder:<name>`); otherwise the
        credential follows tokens.select_token (owner, or this Claude Code session's director token)."""
        self.home = home
        self.as_owner = as_owner
        self.env = os.environ if env is None else env
        self.principal = principal
        self._cred = None

    def credential(self):
        if self._cred is None:
            if self.principal:
                # isolation mode: a builder under its own account gets its token from its own config
                raw = (self.env.get("IMPERIUM_BUILDER_TOKEN") if self.principal.startswith("builder:") else None) \
                    or tokens.read_locator(self.home, self.principal)
                if not raw:
                    raise tokens.NoCredential(f"no token for {self.principal}; the owner runs "
                                              "`imperium builder token <name>`")
                self._cred = (self.principal, raw)
            else:
                self._cred = tokens.select_token(self.home, env=self.env, as_owner=self.as_owner)
        return self._cred

    def info(self):
        if self.principal and self.principal.startswith("builder:") and self.env.get("IMPERIUM_PIPE"):
            return {"port": 0, "pipes": {"builder": self.env["IMPERIUM_PIPE"]}}
        try:
            with open(paths.daemon_json(self.home), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            raise DaemonDown("the daemon is not running (no daemon.json); run `imperium up`") from None

    def call(self, method, path, body=None, auth=True, timeout=10, via_tcp=False, channel=None):
        """One API call. In isolation mode (the daemon lists pipes) it goes over the pipe for this credential's
        channel; `via_tcp` and `channel` exist to test the refusals."""
        info = self.info()
        port = info["port"]
        headers = {"Host": f"127.0.0.1:{port}"}
        if auth:
            headers["Authorization"] = "Bearer " + self.credential()[1]
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        pipes = info.get("pipes") or {}
        if pipes and not via_tcp and auth:
            ch = channel or ("builder" if self.credential()[0].startswith("builder:") else "owner")
            if ch not in pipes:
                raise DaemonDown(f"isolation mode has no {ch} pipe configured")
            status, raw = self._pipe(pipes[ch], method, path, headers, data, timeout)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            try:
                conn.request(method, path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status = resp.status
            except (ConnectionError, OSError) as e:
                raise DaemonDown(f"the daemon is not reachable on port {port} ({e}); run `imperium up`") from None
            finally:
                conn.close()
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            out = {"error": raw[:200].decode("utf-8", "replace")}
        if status != 200:
            raise ApiError(status, out.get("error", f"HTTP {status}"), out)
        return out

    @staticmethod
    def _pipe(name, method, path, headers, data, timeout):
        from . import transport
        lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
        lines += [f"Content-Length: {len(data or b'')}", "Connection: close", "", ""]
        req = "\r\n".join(lines).encode("latin-1") + (data or b"")
        try:
            return transport.pipe_request(name, req, timeout_ms=int(timeout * 1000))
        except (ConnectionError, OSError) as e:
            raise DaemonDown(f"the daemon's pipe is not reachable ({e})") from None

    def health(self):
        try:
            return self.call("GET", "/v1/health", auth=False, timeout=3)
        except (DaemonDown, ApiError):
            return None
