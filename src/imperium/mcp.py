"""A minimal Model Context Protocol server over stdio (JSON-RPC 2.0, one message per line), standard library only.

It implements what a tool server needs: `initialize`, `ping`, `tools/list`, `tools/call`, and ignores
notifications. Tool results are JSON text. A tool that fails returns `isError: true` with the reason, so the
calling agent sees it instead of a protocol error. Both MCP servers (builder and director) use this; each tool is a
thin call to the daemon's local API, so the daemon stays the only writer.
"""
import json
import sys

from . import __version__

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    pass


class Server:
    def __init__(self, name, tools, instructions=None):
        """tools: {name: (description, input_schema, function(arguments) -> JSON-able)}"""
        self.name = name
        self.tools = tools
        self.instructions = instructions

    def handle(self, msg):
        """One incoming message; returns the reply dict, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # a notification (initialized, cancelled, ...): nothing to answer
            return None
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": self.name, "version": __version__}}
                if self.instructions:
                    result["instructions"] = self.instructions
                return _result(mid, result)
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                listed = []
                for n, spec in self.tools.items():
                    t = {"name": n, "description": spec[0], "inputSchema": spec[1]}
                    if len(spec) > 3:
                        t["annotations"] = spec[3]
                    listed.append(t)
                return _result(mid, {"tools": listed})
            if method == "tools/call":
                name = params.get("name")
                if name not in self.tools:
                    return _error(mid, -32602, f"unknown tool {name!r}")
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    return _error(mid, -32602, "arguments must be an object")
                try:
                    out = self.tools[name][2](args)
                    return _result(mid, {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                                         "isError": False})
                except ToolError as e:
                    return _result(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            return _error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # never kill the server over one request
            return _error(mid, -32603, f"internal error: {type(e).__name__}")

    def serve(self, stdin=None, stdout=None):
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply = _error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):  # a batch (older protocol versions)
                    replies = [r for r in (self.handle(m) for m in msg) if r is not None]
                    reply = replies or None
                else:
                    reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STR = {"type": "string"}
STRS = {"type": "array", "items": {"type": "string"}}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def builder_server(client):
    """The builder's tool: report, escalate, and re-read its open rounds. `client` holds the builder token."""
    from .client import ApiError, DaemonDown

    def call(method, path, body=None):
        try:
            return client.call(method, path, body)
        except ApiError as e:
            raise ToolError(f"refused: {e}") from None
        except DaemonDown as e:
            raise ToolError(f"Imperium is not running: {e}") from None

    tools = {
        "imperium_rounds": (
            "Your open Imperium rounds: id, objective, nonce and current generation. Use it after a context "
            "compaction to recover what you are working on.",
            obj({}), lambda a: call("GET", "/v1/builder/rounds")),
        "report_status": (
            "Report the state of an Imperium round: 'ready' when the objective is done and you ran the relevant "
            "tests, 'incomplete' otherwise. Quote the round's nonce and current generation. A report is evidence; "
            "the work is verified and accepted separately.",
            obj({"round": STR, "nonce": STR, "generation": INT, "state": {"type": "string",
                                                                        "enum": ["ready", "incomplete"]},
                 "gates": STRS, "not_done": STRS, "questions": STRS},
                ["round", "nonce", "generation", "state"]),
            lambda a: call("POST", "/v1/builder/report", a)),
        "escalate": (
            "Stop and ask for help on an Imperium round: you are blocked, the task cannot be done as asked, or "
            "finishing it would mean weakening, skipping or editing tests or checks. This is the expected, correct "
            "action in those cases. After escalating, stop and wait.",
            obj({"round": STR, "nonce": STR, "issue_type": STR, "problem_assessment": STR, "approaches_tried": STRS,
                 "recommendation": STR}, ["round", "nonce", "issue_type", "problem_assessment"]),
            lambda a: call("POST", "/v1/builder/escalate", a)),
    }
    return Server("imperium-builder", tools, instructions="Imperium rounds: report with report_status, ask for help "
                                                          "with escalate.")
