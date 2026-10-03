"""A fake ACP agent (Agent Client Protocol v1) for tests, run as a real child process: python fake_acp.py STATE.

Behaviour is chosen by words in the prompt text:
  ASK <command>   a tool call that asks permission, then reports the outcome
  WRITE <f> <t>   writes text t to file f in the session's directory
  SLOW            waits 1.5 s before answering
  SILENT_CRASH    exits as soon as it reads the prompt, before any activity
  NOISE_CRASH     sends updates that are not turn activity, then exits
  REFUSE          answers the prompt with an error, before any activity
  otherwise       one agent message, then end_turn
STATE (JSON) keeps the sessions' histories across restarts, so `session/load` can replay them, and records what the
client sent (capabilities, MCP servers, permission outcomes) for the tests to inspect. `NOLOAD` in the environment
turns the loadSession capability off.
"""
import json
import os
import sys
import time

STATE = sys.argv[1]


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"sessions": {}, "log": [], "counter": 0}


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def update(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def wait_response(rid):
    while True:
        m = read()
        if m.get("id") == rid and "method" not in m:
            return m


def main():
    st = load()
    while True:
        m = read()
        method, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
        st = load()
        st["log"].append({"method": method, "params": p})
        save(st)
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1, "agentInfo": {"name": "fake-acp", "version": "0.1"},
                "agentCapabilities": {"loadSession": not os.environ.get("NOLOAD")}, "authMethods": []}})
        elif method == "session/new":
            st["counter"] += 1
            sid = f"sess_{st['counter']}"
            st["sessions"][sid] = {"cwd": p["cwd"], "history": []}
            save(st)
            update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": []})
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}})
        elif method == "session/load":
            s = st["sessions"].get(p["sessionId"])
            if s is None:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "no such session"}})
                continue
            for role, text in s["history"]:
                kind = "user_message_chunk" if role == "user" else "agent_message_chunk"
                update(p["sessionId"], {"sessionUpdate": kind, "content": {"type": "text", "text": text}})
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/prompt":
            prompt(st, mid, p)
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


def prompt(st, mid, p):
    sid = p["sessionId"]
    text = "".join(b.get("text", "") for b in p["prompt"] if b.get("type") == "text")
    if "SILENT_CRASH" in text:
        sys.exit(3)
    if "NOISE_CRASH" in text:  # updates that can come at any time, then gone: none of them is a turn
        update(sid, {"sessionUpdate": "usage_update", "used": 10, "size": 1000})
        update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": []})
        time.sleep(0.3)
        sys.exit(3)
    if "REFUSE" in text:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "refused"}})
        return
    s = st["sessions"][sid]
    s["history"].append(["user", text])
    save(st)
    if 'WAIT_CANCEL' in text:
        update(sid, {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'working'}})
        while True:
            request = read()
            if request.get('method') == 'session/cancel' and request.get('params', {}).get('sessionId') == sid:
                send({'jsonrpc': '2.0', 'id': mid, 'result': {'stopReason': 'cancelled'}})
                return
    if "SLOW" in text:
        time.sleep(1.5)
    words = text.split()
    if "ASK" in words:
        cmd = words[words.index("ASK") + 1]
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": f"run {cmd}", "kind": "execute",
                     "status": "pending", "rawInput": {"command": cmd}})
        send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission", "params": {
            "sessionId": sid, "toolCall": {"toolCallId": "call_1"},
            "options": [{"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                        {"optionId": "always", "name": "Always", "kind": "allow_always"},
                        {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
        r = wait_response("perm-1")
        st = load()
        st["log"].append({"permission_outcome": r.get("result")})
        save(st)
        ok = (r.get("result") or {}).get("outcome", {}).get("optionId") in ("yes", "always")
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call_1",
                     "status": "completed" if ok else "failed"})
    if "WRITE" in words:
        i = words.index("WRITE")
        with open(os.path.join(st["sessions"][sid]["cwd"], words[i + 1]), "w", encoding="utf-8") as f:
            f.write(words[i + 2].replace("\\n", "\n"))
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
    st = load()
    st["sessions"][sid]["history"].append(["agent", "done"])
    save(st)
    send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})


if __name__ == "__main__":
    main()
