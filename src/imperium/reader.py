"""The read path: turn what an OpenCode builder does into journal observations (DESIGN §6.7, §9.2).

`Reader.poll(checkpoint)` yields `(observations, checkpoint)` batches. The engine commits each batch's
events and its checkpoint in one transaction, so every observation is journaled exactly once [R-2].
The input checkpoint is never modified; a failed poll leaves the committed state untouched.

History catch-up pages backwards with OpenCode's `before` cursor until it reaches the checkpoint, then
processes oldest first. It is bounded per cycle but never abandons older history: the search position and
the pages still to process are saved in the checkpoint [P2-7]. Messages still being written (an assistant
reply streaming, a user message whose parts are not yet stored) are tracked, not passed [R-1, P2-19].
"""
import copy
import hashlib
import re

from . import opencode, untrusted

TOKEN = re.compile(r"^\[imperium msg=([0-9A-Za-z]+) builder=([a-z0-9][a-z0-9_-]{0,31})((?: [a-z_]+=[^\s\]]+)*)\]")
KNOWN_PARTS = {"text", "subtask", "reasoning", "file", "tool", "step-start", "step-finish", "snapshot", "patch",
               "agent", "retry", "compaction"}
KNOWN_STATUS = {"idle", "busy", "retry"}
RECENT_TEXTS = 50
MAX_DEPTH = 8  # sub-agent nesting followed when deciding whether a busy session belongs to this builder


def _key(info):
    return [info["time"]["created"], info["id"]]


def _norm_dir(d):
    import os
    return os.path.normcase(os.path.normpath(d)) if d else d


class Reader:
    def __init__(self, client, builder, secrets=(), page_size=50, max_scan_pages=20, partless_polls=3):
        self.c = client
        self.sid = builder["session_id"]
        self.directory = builder["directory"]
        self.secrets = tuple(secrets)
        self.page_size = page_size
        self.max_scan_pages = max_scan_pages
        self.partless_polls = partless_polls
        self.version = None

    def _obs(self, type_, severity, data=None, text=None, key=None):
        return {"type": type_, "severity": severity, "data": data or {},
                "untrusted": untrusted.clean(text, self.secrets) if text is not None else None,
                "source_key": f"oc:{self.sid}:{key}" if key else None}

    # --- entry --------------------------------------------------------------------------------------
    def poll(self, checkpoint):
        cp = copy.deepcopy(checkpoint) if checkpoint else {}
        out = []
        self.version = (self.c.health() or {}).get("version")
        try:
            sess = self.c.session(self.sid)
        except opencode.OCError as e:
            if e.status != 404:
                raise
            if not cp.get("session_missing"):
                cp["session_missing"] = True
                out.append(self._obs("SESSION_MISSING", "CRITICAL",
                                     {"session_id": self.sid, "directory": self.directory}))
            yield out, cp
            return
        if cp.get("session_missing"):
            cp["session_missing"] = False
            out.append(self._obs("SESSION_FOUND", "INFO", {"session_id": self.sid}))
        if _norm_dir(sess.get("directory")) != _norm_dir(self.directory):
            if not cp.get("session_mismatch"):
                cp["session_mismatch"] = True
                out.append(self._obs("SESSION_MISMATCH", "CRITICAL", {"session_id": self.sid,
                                     "registered": self.directory, "server": sess.get("directory")}))
            yield out, cp
            return
        cp["session_mismatch"] = False
        if not cp.get("attached"):
            yield self._attach(cp, out)
            return
        if self.version != cp.get("version"):
            out.append(self._obs("VERSION_CHANGED", "NOTICE", {"from": cp.get("version"), "to": self.version}))
            cp["version"] = self.version
            self._untested(out)
        self._status(cp, out)
        self._permissions(cp, out)
        self._questions(cp, out)
        self._open(cp, out)
        yield out, cp
        yield from self._new_messages(cp)

    def _untested(self, out):
        if self.version not in opencode.TESTED_VERSIONS:
            out.append(self._obs("VERSION_UNTESTED", "NOTICE",
                                 {"version": self.version, "tested": list(opencode.TESTED_VERSIONS),
                                  "effect": "observe only until the owner allows this version"},
                                 key=f"version:{self.version}:untested"))

    def _attach(self, cp, out):
        items, _ = self.c.messages(self.sid, limit=1)
        last = items[-1] if items else None
        st = self.c.status_map().get(self.sid) or {"type": "idle"}
        cp.update({"attached": True, "version": self.version, "last_key": _key(last["info"]) if last else None,
                   "last_id": last["info"]["id"] if last else None, "phase": None, "scan": [], "open": {},
                   "pending": [], "status": st.get("type"), "retry_attempt": st.get("attempt"), "permissions": [],
                   "perm_last": [], "perm_broken": False, "questions": [], "unknown": [], "texts": [],
                   "busy_children": None})
        if last and last["info"].get("role") == "assistant" and not last["info"].get("time", {}).get("completed"):
            cp["open"][last["info"]["id"]] = {"role": "assistant"}
        out.append(self._obs("BUILDER_ATTACHED", "INFO", {"session_id": self.sid, "directory": self.directory,
                                                          "version": self.version, "status": cp["status"],
                                                          "last_message_id": cp["last_id"]}))
        self._untested(out)
        self._permissions(cp, out)
        self._questions(cp, out)
        return out, cp

    # --- status, permissions, questions --------------------------------------------------------------
    def _status(self, cp, out):
        smap = self.c.status_map()
        self._children(cp, out, smap)
        st = smap.get(self.sid) or {"type": "idle"}
        t = st.get("type")
        if t not in KNOWN_STATUS:
            self._unknown(cp, out, "status type", t)
        if t == cp.get("status") and not (t == "retry" and st.get("attempt") != cp.get("retry_attempt")):
            return
        if t == "busy":
            out.append(self._obs("BUILDER_BUSY", "INFO"))
        elif t == "idle":
            out.append(self._obs("BUILDER_IDLE", "INFO"))
        elif t == "retry":
            out.append(self._obs("BUILDER_RETRY", "NOTICE", {"attempt": st.get("attempt"), "next": st.get("next")},
                                 text={"message": st.get("message")}))
        cp["status"], cp["retry_attempt"] = t, st.get("attempt")

    def _children(self, cp, out, smap):
        """Busy sessions descended from the builder's (sub-agents it started). A builder whose sub-agent is still
        working is not idle, even when its own session says so."""
        busy = []
        for sid, st in smap.items():
            if sid == self.sid or (st or {}).get("type") == "idle":
                continue
            cur, depth = sid, 0
            while cur and depth < MAX_DEPTH:
                try:
                    parent = self.c.session(cur).get("parentID")
                except opencode.OCError as e:
                    if e.status != 404:
                        raise
                    parent = None  # another project's session
                if parent == self.sid:
                    busy.append(sid)
                    break
                cur, depth = parent, depth + 1
        busy.sort()
        before = cp.get("busy_children") or []
        if busy and not before:
            out.append(self._obs("SUBAGENT_BUSY", "INFO", {"sessions": busy}))
        elif before and not busy:
            out.append(self._obs("SUBAGENT_IDLE", "INFO", {"sessions": before}))
        cp["busy_children"] = busy

    def _permissions(self, cp, out):
        try:
            asks = self.c.permissions()
        except opencode.OCError as e:
            if e.status != 400:
                raise
            if not cp["perm_broken"]:
                cp["perm_broken"] = True
                out.append(self._obs("PERMISSION_LIST_BROKEN", "ACTION",
                                     {"status": e.status, "effect": "permission state unknown; nothing is dispatched"}))
            cp["permissions"] = None
            return
        if cp["perm_broken"]:
            cp["perm_broken"] = False
            out.append(self._obs("PERMISSION_LIST_OK", "INFO"))
        mine = [p for p in asks if p.get("sessionID") == self.sid]
        now = [p["id"] for p in mine]
        before = set(cp.get("perm_last") or [])
        for p in mine:
            if p["id"] not in before:
                out.append(self._obs("PERMISSION_ASKED", "ACTION",
                                     {"permission_id": p["id"], "permission": p.get("permission"),
                                      "tool": p.get("tool")},
                                     text={"patterns": p.get("patterns"), "always": p.get("always"),
                                           "metadata": p.get("metadata")},
                                     key=f"perm:{p['id']}:asked"))
        for pid in sorted(before - set(now)):
            out.append(self._obs("PERMISSION_GONE", "INFO", {"permission_id": pid}, key=f"perm:{pid}:gone"))
        cp["permissions"] = now
        cp["perm_last"] = now

    def _questions(self, cp, out):
        mine = [q for q in self.c.questions() if q.get("sessionID") == self.sid]
        now = [q["id"] for q in mine]
        before = set(cp.get("questions") or [])
        for q in mine:
            if q["id"] not in before:
                out.append(self._obs("QUESTION_ASKED", "ACTION",
                                     {"question_id": q["id"], "count": len(q.get("questions") or [])},
                                     text={"questions": q.get("questions")}, key=f"question:{q['id']}:asked"))
        for qid in sorted(before - set(now)):
            out.append(self._obs("QUESTION_GONE", "INFO", {"question_id": qid}, key=f"question:{qid}:gone"))
        cp["questions"] = now

    def _unknown(self, cp, out, what, name):
        tag = f"{what}:{name}"
        if tag not in cp["unknown"]:
            cp["unknown"].append(tag)
            out.append(self._obs("ADAPTER_UNKNOWN", "NOTICE", {"what": what, "name": name},
                                 key=f"unknown:{what}:{name}"))

    # --- messages -----------------------------------------------------------------------------------
    def _open(self, cp, out):
        for mid, st in list(cp["open"].items()):
            try:
                m = self.c.message(self.sid, mid)
            except opencode.OCError as e:
                if e.status != 404:
                    raise
                del cp["open"][mid]
                out.append(self._obs("MESSAGE_VANISHED", "NOTICE", {"message_id": mid}, key=f"{mid}:vanished"))
                continue
            if st["role"] == "assistant":
                if m["info"].get("time", {}).get("completed"):
                    self._end(m, cp, out)
                    del cp["open"][mid]
            elif m.get("parts"):
                self._classify_user(m, cp, out)
                del cp["open"][mid]
            else:
                st["polls"] = st.get("polls", 1) + 1
                if st["polls"] >= self.partless_polls:
                    out.append(self._obs("USER_MESSAGE_EMPTY", "ACTION", {"message_id": mid,
                                         "note": "a user message with no content; Imperium did not send it"},
                                         key=f"{mid}:user"))
                    del cp["open"][mid]

    def _new_messages(self, cp):
        """Search back to the checkpoint (bounded per cycle), then process pages oldest first.

        State in the checkpoint: phase "search" with `scan` (cursors found so far), or phase "process" with
        `pending` (the `before` value of each page still to read, oldest first; None is the newest page).
        """
        if cp.get("phase") != "process":
            scan = cp.get("scan") or []
            before = scan[-1] if scan else None
            pages = 0
            while True:
                items, nxt = self.c.messages(self.sid, limit=self.page_size, before=before)
                pages += 1
                # with no checkpoint (the session was empty at attach) everything is new: search to the start
                reached = cp["last_key"] is not None and (not items or _key(items[0]["info"]) <= cp["last_key"])
                if reached or nxt is None:
                    break
                scan.append(nxt)
                before = nxt
                if pages >= self.max_scan_pages:
                    cp["phase"], cp["scan"] = "search", scan
                    yield [], cp
                    return
            obs = []
            if cp["last_id"] is not None and not any(m["info"]["id"] == cp["last_id"] for m in items):
                obs.append(self._obs("HISTORY_GAP", "ACTION",
                                     {"checkpoint_id": cp["last_id"], "pages_searched": len(scan) + 1,
                                      "note": "the last message seen is gone; later messages are still read"}))
            self._process(items, cp, obs)
            # pages newer than the one just read: before=scan[-2], ..., scan[0], then the newest (None)
            cp["pending"] = list(reversed(scan[:-1])) + [None] if scan else []
            cp["scan"] = []
            cp["phase"] = "process" if cp["pending"] else None
            yield obs, cp
        while cp.get("pending"):
            before = cp["pending"][0]
            items, nxt = self.c.messages(self.sid, limit=self.page_size, before=before)
            if before is None and nxt is not None and items and _key(items[0]["info"]) > cp["last_key"]:
                # Is the newest page contiguous with what was read? Look at the one message just older than it.
                probe, _ = self.c.messages(self.sid, limit=1, before=nxt)
                if probe and _key(probe[-1]["info"]) > cp["last_key"]:
                    # more new messages arrived than one page holds: search again next cycle
                    cp["phase"], cp["pending"] = None, []
                    yield [], cp
                    return
            obs = []
            self._process(items, cp, obs)
            cp["pending"].pop(0)
            if not cp["pending"]:
                cp["phase"] = None
            yield obs, cp

    def _process(self, items, cp, obs):
        for m in items:
            info = m["info"]
            k = _key(info)
            if cp["last_key"] is not None and k <= cp["last_key"]:
                continue
            role = info.get("role")
            if role == "user":
                if m.get("parts"):
                    self._classify_user(m, cp, obs)
                else:
                    cp["open"][info["id"]] = {"role": "user", "polls": 1}
            elif role == "assistant":
                obs.append(self._obs("TURN_STARTED", "INFO", {"message_id": info["id"],
                                     "parent_id": info.get("parentID"), "agent": info.get("agent"),
                                     "model": info.get("modelID")}, key=f"{info['id']}:started"))
                if info.get("time", {}).get("completed"):
                    self._end(m, cp, obs)
                else:
                    cp["open"][info["id"]] = {"role": "assistant"}
            else:
                self._unknown(cp, obs, "message role", role)
            cp["last_key"], cp["last_id"] = k, info["id"]

    def _end(self, m, cp, out):
        info = m["info"]
        for p in m.get("parts") or []:
            t = p.get("type")
            if t not in KNOWN_PARTS:
                self._unknown(cp, out, "part type", t)
            elif t == "tool" and (p.get("state") or {}).get("status") == "error":
                out.append(self._obs("TOOL_ERROR", "NOTICE", {"tool": p.get("tool"), "call_id": p.get("callID"),
                                     "part_id": p.get("id"), "message_id": info["id"]},
                                     text={"error": (p.get("state") or {}).get("error")},
                                     key=f"{p.get('id')}:tool_error"))
        err = info.get("error")
        data = {"message_id": info["id"], "parent_id": info.get("parentID"), "finish": info.get("finish")}
        if err:
            data["error"] = err.get("name") if isinstance(err, dict) else "error"
        out.append(self._obs("TURN_ENDED", "NOTICE" if err else "INFO", data,
                             text={"error": (err.get("data") or {}).get("message")} if isinstance(err, dict) else None,
                             key=f"{info['id']}:ended"))
        if info.get("summary"):
            out.append(self._obs("COMPACTION_DONE", "INFO", {"message_id": info["id"],
                                 "parent_id": info.get("parentID")}, key=f"{info['id']}:compaction_done"))

    def _classify_user(self, m, cp, out):
        info, parts = m["info"], m.get("parts") or []
        mid = info["id"]
        for p in parts:
            if p.get("type") not in KNOWN_PARTS:
                self._unknown(cp, out, "part type", p.get("type"))
        texts = [p for p in parts if p.get("type") == "text"]
        first = texts[0].get("text", "") if texts else ""
        full = "\n".join(p.get("text", "") for p in texts)
        compaction = [p for p in parts if p.get("type") == "compaction"]
        if compaction:
            out.append(self._obs("COMPACTION_SEEN", "INFO", {"message_id": mid, "auto": bool(compaction[0].get("auto"))},
                                 key=f"{mid}:user"))
            return
        if texts and texts[0].get("synthetic") and (texts[0].get("metadata") or {}).get("compaction_continue"):
            pinned = self.version in opencode.TESTED_VERSIONS
            out.append(self._obs("SYSTEM_USER_MESSAGE", "INFO" if pinned else "NOTICE",
                                 {"message_id": mid, "kind": "compaction_continue", "marker_pinned": pinned},
                                 key=f"{mid}:user"))
            return
        tok = TOKEN.match(first)
        digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
        if tok:
            fields = dict(f.split("=", 1) for f in tok.group(3).split())
            out.append(self._obs("USER_MESSAGE_TOKEN", "INFO", {"message_id": mid, "token_msg": tok.group(1),
                                 "token_builder": tok.group(2), **{k: fields[k] for k in ("round", "gen") if k in fields}},
                                 key=f"{mid}:user"))
        elif not full.strip():
            out.append(self._obs("USER_MESSAGE_EMPTY", "ACTION", {"message_id": mid,
                                 "part_types": [p.get("type") for p in parts]}, key=f"{mid}:user"))
        elif digest in cp["texts"]:
            out.append(self._obs("USER_MESSAGE_REPEATED", "ACTION", {"message_id": mid,
                                 "note": "same text as an earlier user message; not assumed to be a replay"},
                                 text={"text": full}, key=f"{mid}:user"))
        else:
            out.append(self._obs("HUMAN_MESSAGE_SEEN", "CRITICAL", {"message_id": mid,
                                 "note": "a user message Imperium did not send"}, text={"text": full},
                                 key=f"{mid}:user"))
        if full.strip():
            cp["texts"] = (cp["texts"] + [digest])[-RECENT_TEXTS:]
