"""Builder adapter for the Agent Client Protocol (ACP, agentclientprotocol.com, protocol version 1).

Imperium launches the agent as its own child process (`opencode acp`, Gemini CLI, Claude Code or Codex through
their ACP adapters, ...) and speaks JSON-RPC 2.0 over its stdin/stdout, one message per line. The adapter offers
the engine the same two faces as the OpenCode one, so delivery, approvals and rounds are one code path:

- a reader: `poll(checkpoint)` yields (observations, checkpoint) with the same observation types and checkpoint
  fields (status, open, permissions, questions, busy_children, attached, phase, version);
- a client: `prompt_async`, `message` (the delivery lookup), `reply_permission`.

What counts as proof (SPEC §3):
- **delivered and running**: the agent's first turn activity after the prompt (a message, thought, plan or tool
  call), or its answer to the prompt. Writing to the pipe proves nothing; updates that can arrive at any time
  (commands, modes, usage, session info) are not counted.
- **refused**: an error answer before any activity: the message did not run (REJECTED).
- after a restart, an agent that can load sessions replays its history; Imperium's own header in a replayed user
  message is proof of delivery, an agent reply after it proof that it ran. Without that, a prompt in flight when
  the agent died is UNCERTAIN, as with OpenCode.

Permission requests (`session/request_permission`) become approvals; the reply selects the agent's own option of
the matching kind. Imperium offers no file system or terminal to the agent (it is not an editor), so the agent
uses its own tools. The builder's MCP tool (report, escalate) is passed in `session/new`.

The agent is a child of the daemon and runs as the daemon's account unless its command switches account (see
docs/ISOLATION.md).
"""
import collections
import json
import logging
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time

from . import opencode, untrusted
from .reader import TOKEN

log = logging.getLogger("imperiumd.acp")
PROTOCOL_VERSION = 1
TURN_UPDATES = {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update",
                "plan"}
PERMISSION_NAMES = {"execute": "bash", "edit": "edit", "delete": "edit", "move": "edit", "fetch": "webfetch"}
REPLY_KINDS = {"once": ("allow_once",), "always": ("allow_always", "allow_once"), "reject": ("reject_once",)}
STARTUP_TIMEOUT = 60.0
LOAD_TIMEOUT = 180.0


class AcpError(RuntimeError):
    pass


def parse_command(value):
    """A command as a JSON list, or a string split like a shell would (no shell is ever used)."""
    if isinstance(value, list):
        argv = value
    else:
        s = str(value).strip()
        if s.startswith("["):
            argv = json.loads(s)
        else:
            import shlex
            argv = shlex.split(s, posix=os.name != "nt")
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise AcpError("the ACP command must be a non-empty list of strings")
    return argv


def endpoint_for(argv):
    return "acp:" + json.dumps(argv, separators=(",", ":"))


def argv_of(endpoint):
    return json.loads(endpoint[len("acp:"):])


def _patterns(tool):
    """What an approval rule matches: the paths a tool touches, else its command, else its title."""
    paths = [loc.get("path") for loc in tool.get("locations") or [] if isinstance(loc, dict) and loc.get("path")]
    if paths:
        return [str(p) for p in paths]
    raw = tool.get("rawInput")
    if isinstance(raw, dict):
        cmd = raw.get("command") or raw.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        if cmd:
            return [str(cmd)]
        for k in ("path", "file_path", "filePath", "url"):
            if raw.get(k):
                return [str(raw[k])]
    return [str(tool.get("title") or tool.get("name") or "")]


class Connection:
    """One running agent process and the session Imperium drives in it."""

    def __init__(self, builder, home, secrets_=(), env=None):
        self.name = builder["name"]
        self.argv = argv_of(builder["endpoint"])
        self.directory = builder["directory"]
        self.home = home
        self.secrets = list(secrets_)
        self.env = env
        self.session_id = builder["session_id"]
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.proc = None
        self.run = None  # a fresh label per process, so approval ids never repeat across restarts
        self.out = []  # observations collected by the reader thread
        self.responses = {}  # rpc id -> response, for requests the engine waits on
        self.ready = False
        self.info = {}
        self.caps = {}
        self.current = None  # the prompt in flight: {"oc_id", "token", "seen"}
        self.seen = set()  # oc ids proven delivered while this process ran
        self.ran = set()
        self.perms = {}  # approval id -> {"rpc": id, "options": [...]}
        self.tools = {}  # toolCallId -> latest known fields
        self.replaying = False
        self.replay_token = None
        self.stderr = collections.deque(maxlen=50)
        self.next_id = 0
        self.exit_reported = True

    # --- process ------------------------------------------------------------------------------------
    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.proc is not None:
            _close_pipes(self.proc)  # the previous process is gone
        exe = shutil.which(self.argv[0]) or self.argv[0]
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}  # no console window
        try:
            self.proc = subprocess.Popen([exe] + self.argv[1:], cwd=self.directory, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, **kw)
        except OSError as e:
            raise opencode.OCUnreachable(f"cannot start {self.argv[0]}: {e}") from None
        self.run = secrets.token_hex(4)
        with self.lock:
            self.ready, self.current, self.perms, self.tools = False, None, {}, {}
            self.exit_reported = False
        threading.Thread(target=self._read_stdout, args=(self.proc,), daemon=True,
                         name=f"imperium-acp-{self.name}").start()
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
        init = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION,
                                           "clientCapabilities": {"fs": {"readTextFile": False,
                                                                         "writeTextFile": False},
                                                                  "terminal": False},
                                           "clientInfo": {"name": "imperium", "title": "Imperium",
                                                          "version": _version()}}, STARTUP_TIMEOUT)
        if init.get("protocolVersion") != PROTOCOL_VERSION:
            self.close()
            raise opencode.OCError(505, init, "initialize")  # an unsupported protocol version: never sent to
        self.caps = init.get("agentCapabilities") or {}
        self.info = init.get("agentInfo") or {}
        self._open_session()

    def _mcp_servers(self):
        return [{"name": "imperium", "command": sys.executable,
                 "args": ["-m", "imperium", "--home", os.path.abspath(self.home), "builder-mcp", "--builder",
                          self.name], "env": []}]

    def _open_session(self):
        params = {"cwd": os.path.abspath(self.directory), "mcpServers": self._mcp_servers()}
        new = self.session_id.startswith("new:")
        if not new and self.caps.get("loadSession"):
            with self.lock:
                self.replaying, self.replay_token = True, None
            try:
                self.request("session/load", {"sessionId": self.session_id, **params}, LOAD_TIMEOUT)
                self._emit("ACP_SESSION_LOADED", "INFO", {"session_id": self.session_id})
            except opencode.OCError as e:
                self._emit("ACP_SESSION_LOST", "ACTION", {"session_id": self.session_id, "status": e.status,
                                                          "note": "the agent could not load the session; a new one "
                                                                  "is started and earlier context is gone"})
                new = True
            finally:
                with self.lock:
                    self.replaying = False
        elif not new and (self.caps.get("sessionCapabilities") or {}).get("resume") is not None:
            try:
                self.request("session/resume", {"sessionId": self.session_id, **params}, LOAD_TIMEOUT)
                self._emit("ACP_SESSION_RESUMED", "INFO", {"session_id": self.session_id})
            except opencode.OCError as e:
                self._emit("ACP_SESSION_LOST", "ACTION", {"session_id": self.session_id, "status": e.status})
                new = True
        elif not new:
            self._emit("ACP_SESSION_LOST", "ACTION", {"session_id": self.session_id,
                                                      "note": "the agent cannot load or resume sessions; a new one "
                                                              "is started and earlier context is gone"})
            new = True
        if new:
            r = self.request("session/new", params, STARTUP_TIMEOUT)
            sid = r.get("sessionId")
            if not isinstance(sid, str) or not sid:
                raise opencode.OCError(502, r, "session/new")
            self.session_id = sid
            self._emit("ACP_SESSION_CREATED", "NOTICE", {"session_id": sid})
        with self.lock:
            self.ready = True

    def close(self):
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            from .verify import _kill_tree
            _kill_tree(p)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        _close_pipes(p)

    # --- wire -------------------------------------------------------------------------------------------
    def _write(self, msg):
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self.proc.stdin.write(line.encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError, AttributeError) as e:
            raise opencode.OCUnreachable(f"the agent process is gone ({type(e).__name__})") from None

    def request(self, method, params, timeout):
        with self.lock:
            self.next_id += 1
            rid = f"imp-{self.next_id}"
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        with self.cond:
            while rid not in self.responses:
                left = deadline - time.monotonic()
                if left <= 0 or not self.alive:
                    raise opencode.OCUnreachable(f"no answer to {method} ({'timeout' if left <= 0 else 'exited'})")
                self.cond.wait(min(left, 0.5))
            r = self.responses.pop(rid)
        if "error" in r:
            raise opencode.OCError(400, r["error"], method)
        return r.get("result") or {}

    def _read_stderr(self, proc):
        for line in iter(proc.stderr.readline, b""):
            self.stderr.append(line.decode("utf-8", "replace").rstrip()[:300])

    def _read_stdout(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            try:
                msg = json.loads(raw)
            except ValueError:
                self._emit("ADAPTER_UNKNOWN", "NOTICE", {"what": "a line that is not JSON-RPC"})
                continue
            if not isinstance(msg, dict):
                continue
            try:
                self._handle(msg)
            except Exception:  # a malformed message must not stop the reader
                log.exception("acp message handling failed")
        with self.cond:
            self.cond.notify_all()

    def _handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method is None:  # a response
            with self.cond:
                cur = self.current
                if cur is not None and mid == cur["oc_id"]:
                    self._prompt_answered(cur, msg)
                else:
                    self.responses[mid] = msg
                self.cond.notify_all()
            return
        params = msg.get("params") or {}
        if method == "session/update":
            self._update(params)
        elif method == "session/request_permission" and mid is not None:
            self._permission(mid, params)
        elif mid is not None:  # fs/*, terminal/*, elicitation, ...: not offered
            self._write_quiet({"jsonrpc": "2.0", "id": mid,
                               "error": {"code": -32601, "message": f"{method} is not offered by this client"}})

    def _write_quiet(self, msg):
        try:
            self._write(msg)
        except opencode.OCUnreachable:
            pass

    # --- what the agent reports -------------------------------------------------------------------------
    def _emit(self, type_, severity, data, text=None):
        with self.lock:
            self.out.append({"type": type_, "severity": severity, "data": data,
                             "untrusted": untrusted.clean(text, self.secrets) if text is not None else None,
                             "source_key": None})

    def _update(self, params):
        u = params.get("update") or {}
        kind = u.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update") and u.get("toolCallId"):
            t = self.tools.setdefault(u["toolCallId"], {})
            t.update({k: v for k, v in u.items() if v is not None})
        with self.lock:
            replaying, cur = self.replaying, self.current
        if replaying:
            self._replayed(kind, u)
            return
        if kind in TURN_UPDATES and cur is not None and not cur["seen"]:
            self._started(cur)

    def _replayed(self, kind, u):
        """History replayed by session/load: Imperium's own header proves delivery, a reply after it the run."""
        if kind == "user_message_chunk":
            content = u.get("content") or {}
            tok = TOKEN.match(content.get("text") or "") if content.get("type") == "text" else None
            self.replay_token = tok.group(1) if tok and tok.group(2) == self.name else None
            if self.replay_token:
                self._emit("ACP_HISTORY_TOKEN", "INFO", {"token_msg": self.replay_token})
        elif kind in ("agent_message_chunk", "agent_thought_chunk", "tool_call", "plan") and self.replay_token:
            self._emit("ACP_HISTORY_RAN", "INFO", {"token_msg": self.replay_token})
            self.replay_token = None

    def _started(self, cur):
        cur["seen"] = True
        self.seen.add(cur["oc_id"])
        tok = cur["token"]
        data = {"message_id": cur["oc_id"], "token_msg": tok[0], "token_builder": tok[1], **tok[2]}
        self._emit("USER_MESSAGE_TOKEN", "INFO", data)
        self._emit("TURN_STARTED", "INFO", {"message_id": cur["oc_id"] + ":turn", "parent_id": cur["oc_id"],
                                            "agent": self.info.get("name"), "model": None})

    def _prompt_answered(self, cur, msg):
        """Called with the lock held: the agent answered the prompt in flight."""
        self.current = None
        for aid in list(self.perms):  # requests of a finished turn are void
            self.perms.pop(aid)
            self.out.append({"type": "PERMISSION_GONE", "severity": "INFO", "data": {"permission_id": aid},
                             "untrusted": None, "source_key": None})
        if "error" in msg and not cur["seen"]:
            err = msg.get("error") or {}
            self.out.append({"type": "ACP_PROMPT_REFUSED", "severity": "ACTION",
                             "data": {"message_id": cur["oc_id"], "code": err.get("code")},
                             "untrusted": untrusted.clean({"error": err.get("message")}, self.secrets),
                             "source_key": None})
            return
        if not cur["seen"]:
            self.lock.release()
            try:
                self._started(cur)
            finally:
                self.lock.acquire()
        stop = (msg.get("result") or {}).get("stopReason")
        data = {"message_id": cur["oc_id"] + ":turn", "parent_id": cur["oc_id"], "finish": stop}
        if "error" in msg:
            data["error"] = "agent_error"
        self.ran.add(cur["oc_id"])
        self.out.append({"type": "TURN_ENDED", "severity": "NOTICE" if "error" in msg else "INFO", "data": data,
                         "untrusted": None, "source_key": None})

    def _permission(self, rpc_id, params):
        tool = dict(self.tools.get((params.get("toolCall") or {}).get("toolCallId"), {}))
        tool.update({k: v for k, v in (params.get("toolCall") or {}).items() if v is not None})
        aid = f"acp_{self.run}_{rpc_id}"
        kind = tool.get("kind") or "other"
        raw = {"id": aid, "permission": PERMISSION_NAMES.get(kind, kind), "patterns": _patterns(tool),
               "always": []}
        with self.lock:
            self.perms[aid] = {"rpc": rpc_id, "options": params.get("options") or []}
            self.out.append({"type": "PERMISSION_ASKED", "severity": "ACTION",
                             "data": {"permission_id": aid, "permission": raw["permission"],
                                      "patterns": raw["patterns"][:5]},
                             "untrusted": untrusted.clean({"title": tool.get("title")}, self.secrets),
                             "source_key": None, "raw": raw})

    # --- the engine's reader face --------------------------------------------------------------------------
    def poll(self, checkpoint):
        cp = dict(checkpoint or {})
        if not self.alive:
            if not self.exit_reported and self.proc is not None:
                self.exit_reported = True
                with self.lock:
                    gone = list(self.perms)
                    self.perms.clear()
                self._emit("ACP_PROCESS_EXITED", "ACTION", {"exit_code": self.proc.returncode,
                                                            "in_flight": bool(self.current)},
                           text={"stderr": "\n".join(list(self.stderr)[-10:])})
                for aid in gone:
                    self._emit("PERMISSION_GONE", "INFO", {"permission_id": aid})
                obs = self._drain()
                cp.update({"attached": False, "status": "unknown"})
                yield obs, cp
            self.start()  # raises OCUnreachable / OCError; the engine backs off
        with self.lock:
            status = "busy" if self.current else "idle"
            perms = sorted(self.perms)
        cp.update({"attached": self.ready, "phase": None, "protocol": PROTOCOL_VERSION,
                   "version": f"acp:{self.info.get('name', '?')}/{self.info.get('version', '?')}",
                   "acp_session": self.session_id, "permissions": perms, "questions": [], "status": status,
                   "open": {}, "busy_children": []})
        yield self._drain(), cp

    def _drain(self):
        with self.lock:
            out, self.out = self.out, []
        return out

    # --- the engine's client face ---------------------------------------------------------------------------
    def prompt_async(self, session_id, oc_id, parts):
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        tok = TOKEN.match(text)
        if tok is None:
            raise AcpError("an Imperium message without its header")
        fields = dict(f.split("=", 1) for f in tok.group(3).split())
        with self.lock:
            if self.current is not None:
                raise opencode.OCError(409, None, "session/prompt")  # one prompt at a time
            if not self.ready:
                raise opencode.OCUnreachable("the agent is not ready")
            self.current = {"oc_id": oc_id, "seen": False,
                            "token": (tok.group(1), tok.group(2), {k: fields[k] for k in ("round", "gen")
                                                                   if k in fields})}
        try:
            self._write({"jsonrpc": "2.0", "id": oc_id, "method": "session/prompt",
                         "params": {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]}})
        except opencode.OCUnreachable:
            with self.lock:
                self.current = None
            raise

    def message(self, session_id, oc_id):
        """The delivery lookup: found once the agent showed it has the message (here or in a replayed history)."""
        if oc_id in self.seen:
            return {"id": oc_id}
        raise opencode.OCError(404, None, "message")

    def reply_permission(self, aid, reply):
        with self.lock:
            p = self.perms.get(aid)
        if p is None:
            raise opencode.OCError(404, None, "permission")  # gone: nothing left to answer
        option = None
        for kind in REPLY_KINDS[reply]:
            option = next((o for o in p["options"] if o.get("kind") == kind), None)
            if option:
                break
        if option is None and reply == "reject":
            option = next((o for o in p["options"] if o.get("kind") == "reject_always"), None)
        outcome = ({"outcome": "selected", "optionId": option["optionId"]} if option
                   else {"outcome": "cancelled"})  # no option of that kind: never allow by guessing
        self._write({"jsonrpc": "2.0", "id": p["rpc"], "result": {"outcome": outcome}})
        with self.lock:
            self.perms.pop(aid, None)
        self._emit("PERMISSION_GONE", "INFO", {"permission_id": aid})

    def reply_question(self, qid, answers):
        raise opencode.OCError(404, None, "question")

    def reject_question(self, qid):
        raise opencode.OCError(404, None, "question")


def _close_pipes(p):
    for f in (p.stdin, p.stdout, p.stderr):
        try:
            if f is not None:
                f.close()
        except OSError:
            pass


def _version():
    from . import __version__
    return __version__
