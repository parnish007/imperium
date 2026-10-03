"use strict";
// Read-only Imperium dashboard. The token arrives in the URL fragment (never sent to a server), is removed from
// the address bar at once, and lives only in this page's memory. Every value is written with textContent; agents'
// own words are never requested or shown.
(function () {
  const frag = new URLSearchParams(location.hash.slice(1));
  const token = frag.get("token");
  history.replaceState(null, "", location.pathname);
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 2000;
  let lastOk = 0, timer = null, firstRender = true, showAll = false, lastJournal = [];
  let proven = new Set();  // "round:mark" seals proven at the previous render, to press only new ones

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = String(text);
    return e;
  }
  function add(parent, ...kids) { for (const k of kids) if (k) parent.appendChild(k); return parent; }
  // journal times are ISO text, queue and round times are epoch seconds
  const when = (ts) => new Date(typeof ts === "number" ? ts * 1000 : ts);
  function clock(ts) {
    const d = when(ts);
    if (isNaN(d)) return "";
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString() ? t
      : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + t;
  }
  function ago(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 90) return s + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    return Math.round(s / 3600) + " h";
  }
  const plural = (n, one, many) => n + " " + (n === 1 ? one : many);
  const firstLine = (text) => String(text || "").split("\n")[0].trim();
  const words = (type) => { const s = String(type || "").toLowerCase().replace(/_/g, " "); return s.charAt(0).toUpperCase() + s.slice(1); };
  const who = (principal) => !principal ? "" : principal === "owner" ? "you"
    : principal.startsWith("director") ? "the AI director" : principal;

  class Expired extends Error {}
  async function get(path) {
    const r = await fetch(path, { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
    if (r.status === 401) throw new Expired();
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  // --- header and banners ---------------------------------------------------------------------------------

  function vital(id, text, tone, title) {
    const dd = $(id);
    dd.textContent = text;
    dd.className = tone ? "tone-" + tone : "";
    if (title) dd.title = title; else dd.removeAttribute("title");
  }
  function banner(lines) {
    const b = $("brake");
    b.replaceChildren();
    for (const [title, detail, cmd] of lines) {
      const p = add(el("p"), el("strong", null, title));
      if (detail) p.appendChild(document.createTextNode(" " + detail));
      if (cmd) add(p, document.createTextNode(" "), el("code", null, cmd));
      b.appendChild(p);
    }
    b.hidden = !lines.length;
  }

  // --- needs you ------------------------------------------------------------------------------------------

  function needs(st, rounds, queue, approvals, questions) {
    const out = [];
    for (const a of approvals) {
      const held = a.state === "HELD";
      out.push({ kind: "Permission", tone: "alert", title: `${a.builder} wants to use ${a.permission || "a tool"}`,
        text: held ? "It is on hold while everything is stopped." : "It is waiting for your answer.",
        cmds: [`imperium approve ${a.id}`, `imperium deny ${a.id}`],
        hint: "To see exactly what it asked: imperium approvals" });
    }
    for (const q of questions)
      out.push({ kind: "Question", tone: "alert", title: `${q.builder} asked you a question`,
        text: "It is waiting for your answer.", cmds: ["imperium questions"] });
    for (const m of queue) {
      if (m.state === "UNCERTAIN")
        out.push({ kind: "Instruction", tone: "alert", title: `Did ${m.builder} get your instruction?`,
          text: "Imperium cannot tell whether it arrived. Nothing is sent again without you: wait, cancel, or resend.",
          cmds: [`imperium msg show ${m.id}`, `imperium msg resolve ${m.id} wait`] });
      if (m.state === "STRANDED")
        out.push({ kind: "Instruction", tone: "alert", title: `${m.builder} has not taken in your instruction`,
          text: "It is stuck. Choose: wait, cancel, or resend.",
          cmds: [`imperium msg show ${m.id}`, `imperium msg resolve ${m.id} wait`] });
    }
    for (const r of rounds) {
      const title = firstLine(r.objective) || "Untitled work";
      if (r.escalation === "open")
        out.push({ kind: "Help", tone: "alert", title, text: `${r.builder} asked for help with this work.`,
          cmds: [`imperium round show ${r.id}`] });
      if (r.state === "CLAIMED_READY")
        out.push({ kind: "Check", tone: "mark", title, text: `${r.builder} says this is done. Run the tests to check.`,
          cmds: [`imperium round verify ${r.id}`] });
      if (r.state === "CLAIMED_INCOMPLETE")
        out.push({ kind: "Stuck", tone: "mark", title, text: `${r.builder} says it could not finish this.`,
          cmds: [`imperium round show ${r.id}`] });
      if (r.state === "VERIFIED")
        out.push({ kind: "Decision", tone: "mark", title,
          text: `${r.builder} finished, the tests pass and the goal is met. Accept it, or reject it.`,
          cmds: [`imperium round accept ${r.id}`, `imperium round reject ${r.id}`] });
    }
    if (st.auto_answers_unreported) {
      const n = st.auto_answers_unreported;
      out.push({ kind: "Review", tone: "mark", title: `Your rules answered ${plural(n, "permission request", "permission requests")}`,
        text: "Look over what they allowed.", cmds: ["imperium approvals --auto"] });
    }
    return out;
  }
  function renderNeeds(items) {
    const list = $("needs");
    list.replaceChildren();
    $("needs-h").dataset.count = items.length ? String(items.length) : "";
    if (!items.length) {
      list.appendChild(el("li", "empty", "Nothing needs you right now."));
      return;
    }
    for (const n of items) {
      const li = el("li", "slip tone-" + n.tone);
      const run = el("div", "slip-run");
      run.appendChild(el("span", "slip-run-label", "Run in your terminal"));
      for (const c of n.cmds) run.appendChild(el("code", "slip-cmd", c));
      if (n.hint) run.appendChild(el("span", "slip-hint", n.hint));
      add(li, el("span", "slip-kind", n.kind), el("p", "slip-title", n.title), el("p", "slip-text", n.text), run);
      list.appendChild(li);
    }
  }

  // --- agents ---------------------------------------------------------------------------------------------

  const STATE_WORDS = {
    UNREACHABLE: ["Not answering", "alert"], PAUSED: ["Paused", "quiet"],
    WAITING_APPROVAL: ["Waiting for your permission", "alert"], WAITING_QUESTION: ["Waiting for your answer", "alert"],
    WAITING_PROVIDER: ["Waiting for its AI model", "mark"], WORKING: ["Working", "work"], IDLE: ["Idle", "quiet"],
  };
  const version = (v) => String(v || "").replace(/^acp:/, "").replace("/", " ");
  function renderAgents(st) {
    const list = $("agents");
    list.replaceChildren();
    const bl = st.builders || [];
    if (!bl.length) {
      add(list, add(el("li", "empty"), document.createTextNode("No agents yet. Add one with "),
        el("code", null, "imperium builder add")));
      return;
    }
    for (const b of bl) {
      const [state, tone] = STATE_WORDS[b.operational_state] || ["Starting", "quiet"];
      const li = el("li", "agent tone-" + tone);
      const head = add(el("div", "agent-head"), el("span", "agent-name", b.name),
        el("span", "agent-via", b.adapter === "acp" ? "run by Imperium" : "OpenCode server"));
      const facts = el("dl", "agent-facts");
      const fact = (k, v, title) => {
        const dd = el("dd", null, v);
        if (title) dd.title = title;
        add(facts, add(el("div"), el("dt", null, k), dd));
      };
      fact("Waiting to send", b.queued || 0);
      fact("Sending now", b.in_flight ? "1 instruction" : "nothing");
      // an ACP agent gives no view of its helpers; say so rather than show "none" (empty is not "none busy")
      if (b.adapter === "acp") fact("Helper agents", "not visible", "Agents run this way do not report their sub-agents.");
      else if (!Array.isArray(b.busy_subagents)) fact("Helper agents", "unknown");
      else fact("Helper agents", b.busy_subagents.length ? b.busy_subagents.length + " busy" : "none busy");
      if (b.opencode_version) fact("Version", version(b.opencode_version));
      add(li, head, add(el("p", "agent-state"), el("span", "dot"), el("span", null, state)), facts);
      if (b.dispatch_blocked && b.operational_state !== "WORKING")
        li.appendChild(el("p", "agent-note", "Not sending right now: " + b.dispatch_blocked + "."));
      if (b.paused && !st.stop_all) li.appendChild(el("p", "agent-note", "Paused: only your own instructions go to it."));
      list.appendChild(li);
    }
  }

  // --- work: one docket per round, with its evidence rail ---------------------------------------------------

  // Each mark is "proven", "broken", "running" or "open"; only facts Imperium recorded count, never the agent's say.
  function evidence(r) {
    const g = r.generation;
    const decided = { ACCEPTED: "proven", REJECTED: "broken", ABANDONED: "broken" }[r.state] || "open";
    let claim = "open";
    if (r.claim_generation === g) claim = r.claim_state === "ready" ? "proven" : r.claim_state === "incomplete" ? "broken" : "open";
    let checks = "open";
    if (r.verify_job) checks = "running";
    else if (r.untrusted) checks = "broken";
    else if (r.checks_ok_generation === g) checks = "proven";
    let objective = "open";
    if (r.objective_generation === g) objective = r.objective_met === 1 ? "proven" : r.objective_met === 0 ? "broken" : "open";
    const by = "by " + (who(r.decided_by) || "someone");
    return [
      ["brief", "Sent", r.state === "PENDING" ? "open" : "proven", { proven: "agent has it", open: "not delivered yet" }],
      ["claim", "Agent", claim, { proven: "says done", broken: "says not finished", open: "no report yet" }],
      ["checks", "Tests", checks,
        { proven: "pass", broken: "not trusted: a file they use changed", running: "running now", open: "not run yet" }],
      ["objective", "Goal", objective, { proven: "met", broken: "not met", open: "not judged yet" }],
      ["decision", r.state === "ABANDONED" ? "Abandoned" : r.state === "REJECTED" ? "Rejected" : "Accepted", decided,
        { proven: by, broken: by, open: "your call" }],
    ];
  }
  function rail(r, seen) {
    const ol = el("ol", "rail");
    ol.setAttribute("aria-label", "Progress");
    for (const [key, label, state, says] of evidence(r)) {
      const id = r.id + ":" + key;
      const li = el("li", "mark is-" + state + (key === "decision" ? " is-decision" : ""));
      const seal = el("span", "seal");
      seal.setAttribute("aria-hidden", "true");
      if (state === "proven") {
        seen.add(id);
        if (!firstRender && !proven.has(id)) seal.classList.add("press");
      }
      add(li, seal, el("span", "mark-label", label), el("span", "mark-says", says[state]));
      ol.appendChild(li);
    }
    return ol;
  }
  const CLOSED = ["ACCEPTED", "REJECTED", "ABANDONED"];
  function renderRounds(rounds) {
    const list = $("rounds");
    list.replaceChildren();
    const seen = new Set();
    const live = rounds.filter((r) => !CLOSED.includes(r.state)).sort((a, b) => b.created - a.created);
    const done = rounds.filter((r) => CLOSED.includes(r.state)).sort((a, b) => b.updated - a.updated).slice(0, 8);
    if (!live.length && !done.length) {
      add(list, add(el("li", "empty"), document.createTextNode("No work yet. Give an agent some with "),
        el("code", null, "imperium round open")));
    }
    for (const r of live.concat(done)) {
      const li = el("li", "docket" + (CLOSED.includes(r.state) ? " is-closed" : ""));
      const meta = add(el("p", "docket-meta"), el("span", "docket-agent", r.builder),
        el("span", null, "started " + clock(r.created)));
      if (r.generation > 1) meta.appendChild(el("span", null, "revision " + r.generation));
      meta.appendChild(el("span", "docket-id", r.id));
      const title = el("p", "docket-objective", firstLine(r.objective) || "Untitled work");
      title.title = firstLine(r.objective);
      add(li, add(el("div", "docket-what"), title, meta), rail(r, seen));
      list.appendChild(li);
    }
    proven = seen;
  }

  // --- instructions and activity --------------------------------------------------------------------------

  const STATUS = {
    QUEUED: ["waiting to send", "quiet"], DISPATCHING: ["sending", "work"], POSTED: ["sent, not yet confirmed", "work"],
    UNKNOWN: ["checking", "work"], UNCERTAIN: ["not sure it arrived", "alert"], STRANDED: ["stuck", "alert"],
    DELIVERED: ["delivered", "ok"], ADMITTED: ["taken in", "ok"], REJECTED: ["refused by the agent", "alert"],
    CANCELLED: ["cancelled", "quiet"], SUPERSEDED: ["replaced by a newer one", "quiet"],
  };
  const KIND = { open: "Work brief", repair: "Follow-up", prompt: "Message", compaction_continue: "Resume after compaction" };
  function renderLedger(messages, rounds) {
    const body = $("ledger").querySelector("tbody");
    body.replaceChildren();
    const titles = new Map(rounds.map((r) => [r.id, firstLine(r.objective)]));
    const rows = messages.slice().sort((a, b) => b.created - a.created).slice(0, 40);
    if (!rows.length) {
      const td = el("td", "empty", "No instructions sent yet.");
      td.colSpan = 5;
      body.appendChild(add(el("tr"), td));
      return;
    }
    for (const m of rows) {
      const [word, tone] = STATUS[m.state] || [words(m.state), "quiet"];
      const what = el("td", "what");
      what.appendChild(el("span", "what-kind", KIND[m.kind] || "Message"));
      if (m.round && titles.get(m.round)) what.appendChild(el("span", "what-title", titles.get(m.round)));
      what.title = "Instruction " + m.id;
      body.appendChild(add(el("tr"), what, el("td", null, m.builder), el("td", null, who(m.source) || m.source),
        el("td", "tone-" + tone, word), el("td", "mono", clock(m.created))));
    }
  }

  // Events worth a person's attention, in plain words. Everything else is housekeeping, shown on request; anything
  // marked ACTION or CRITICAL is always shown, even without a sentence here.
  const SAY = {
    ROUND_OPENED: "{b} was given new work", CLAIM_READY: "{b} says the work is done",
    CLAIM_INCOMPLETE: "{b} says it could not finish", CLAIM_REJECTED: "A report from {b} was refused",
    STALE_CLAIM: "{b} reported on an older version of the work", CHECKS_PASSED: "Tests passed on {b}'s work",
    VERIFY_FAILED: "Tests failed on {b}'s work", UNTRUSTED_CHECKS: "Tests not trusted: a file they use changed",
    GATE_NOT_DISCRIMINATING: "A test passes even without the change, so it proves nothing",
    TEST_FILES_CHANGED: "{b} changed test files", OBJECTIVE_MET: "Goal marked as met",
    OBJECTIVE_NOT_MET: "Goal marked as not met", ROUND_VERIFIED: "{b}'s work is verified and waits for a decision",
    ROUND_ACCEPTED: "Work accepted", ROUND_REJECTED: "Work rejected", ROUND_ABANDONED: "Work abandoned",
    ROUND_REOPENED: "Work reopened", VERIFICATION_VOIDED: "Verification cancelled: the work changed",
    VERIFIED_VOIDED: "Verification cancelled: the work changed", ESCALATED: "{b} asked for help",
    PERMISSION_ASKED: "{b} asked for permission", PERMISSION_APPROVED: "{b} was given permission",
    PERMISSION_REJECTED: "{b} was refused permission", PERMISSION_HELD: "{b}'s permission request is on hold",
    PERMISSION_EXPIRED: "A permission request from {b} lapsed", QUESTION_ASKED: "{b} asked a question",
    QUESTION_ANSWERED: "{b}'s question was answered", QUESTION_EXPIRED: "A question from {b} lapsed",
    MSG_ADMITTED: "{b} took in an instruction", MSG_DELIVERED: "An instruction reached {b}",
    MSG_UNCERTAIN: "Not sure {b} got an instruction", MSG_STRANDED: "An instruction to {b} is stuck",
    MSG_REJECTED: "{b} refused an instruction", MSG_CANCELLED: "An instruction to {b} was cancelled",
    MSG_FOUND_LATE: "{b} took in an instruction late", LATE_DELIVERY: "{b} took in an instruction late",
    LATE_ADMISSION: "{b} took in an instruction late", DUPLICATE_RAN: "An instruction ran twice on {b}",
    BUILDER_ADDED: "Agent {b} added", BUILDER_REMOVED: "Agent {b} removed", BUILDER_PAUSED: "{b} paused",
    BUILDER_RESUMED: "{b} resumed", BUILDER_UNREACHABLE: "{b} is not answering",
    BUILDER_REACHABLE: "{b} is answering again", BUILDER_AUTH_FAILED: "{b} refused Imperium's password",
    ACP_SESSION_CREATED: "{b} started", ACP_PROCESS_EXITED: "{b} stopped running", ACP_SESSION_LOST: "{b} lost its session",
    SUSPECTED_STALL: "{b} has gone quiet", HANG_SUSPECTED: "{b} seems stuck", DISPATCH_STALLED: "Sending to {b} is stuck",
    STOP_ALL: "Everything was stopped", RESUME_ALL: "Everything was resumed",
    QUARANTINED: "The record failed its integrity check", INTEGRITY_FAIL: "The record failed its integrity check",
    QUARANTINE_RELEASED: "The damaged record was accepted", DIRECTOR_CLAIMED: "The AI director connected",
    DIRECTOR_RELEASED: "The AI director disconnected", CHECK_DEFINED: "A test was set up",
    CHECK_RETIRED: "A test was retired", APPROVAL_RULE_ADDED: "A permission rule was added",
    APPROVAL_RULE_REMOVED: "A permission rule was removed", RESTORED: "Restored from a backup",
    NOTIFY_FAILED: "Notifications are failing",
  };
  // the sentence as an element; agent names stand out so a name like "agent" still reads as a name
  function sentence(e) {
    const span = el("span", "entry-what");
    const s = SAY[e.type];
    if (!s) {
      span.textContent = words(e.type);
      if (e.builder) add(span, document.createTextNode(" · "), el("b", "name", e.builder));
      return span;
    }
    s.split("{b}").forEach((part, i) => {
      if (i) {
        if (e.builder) span.appendChild(el("b", "name", e.builder));
        else span.appendChild(document.createTextNode(i === 1 && !s.indexOf("{b}") ? "An agent" : "an agent"));
      }
      if (part) span.appendChild(document.createTextNode(part));
    });
    return span;
  }
  const important = (e) => e.type in SAY || e.severity === "ACTION" || e.severity === "CRITICAL";
  function renderJournal(events) {
    lastJournal = events;
    const list = $("journal");
    list.replaceChildren();
    const shown = events.slice().reverse().filter((e) => showAll || important(e));
    const hidden = events.length - events.filter(important).length;
    const btn = $("journal-all");
    btn.textContent = showAll ? "Show less" : hidden ? `Show everything (${hidden} more)` : "Show everything";
    btn.setAttribute("aria-pressed", String(showAll));
    for (const e of shown) {
      const tone = e.severity === "CRITICAL" ? "alert" : e.severity === "ACTION" ? "mark" : important(e) ? "" : "quiet";
      list.appendChild(add(el("li", "entry" + (tone ? " tone-" + tone : "")), el("span", "entry-time", clock(e.ts)),
        sentence(e), el("span", "entry-seq", "#" + e.seq)));
    }
    if (!shown.length) list.appendChild(el("li", "empty", "Nothing has happened yet."));
  }
  $("journal-all").addEventListener("click", () => {
    showAll = !showAll;
    try { localStorage.setItem("imperium.showAll", showAll ? "1" : "0"); } catch (e) { /* storage unavailable */ }
    renderJournal(lastJournal);
  });
  try { showAll = localStorage.getItem("imperium.showAll") === "1"; } catch (e) { /* storage unavailable */ }

  // --- the one-line summary at the top --------------------------------------------------------------------

  function renderSummary(st, items, rounds) {
    const head = $("summary-head"), sub = $("summary-sub");
    const alerts = items.filter((n) => n.tone === "alert").length;
    if (st.quarantine) head.textContent = "Stopped: the record failed its integrity check.";
    else if (items.length) head.textContent = plural(items.length, "thing needs you.", "things need you.");
    else head.textContent = "All quiet. Nothing needs you.";
    head.className = "summary-head" + (st.quarantine || alerts ? " tone-alert" : items.length ? " tone-mark" : "");
    const counts = {};
    for (const b of st.builders || []) {
      const w = (STATE_WORDS[b.operational_state] || ["starting"])[0].toLowerCase();
      counts[w] = (counts[w] || 0) + 1;
    }
    const parts = Object.entries(counts).map(([w, n]) => n + " " + w);
    const open = rounds.filter((r) => !CLOSED.includes(r.state)).length;
    const bl = (st.builders || []).length;
    sub.textContent = (bl ? plural(bl, "agent", "agents") + ": " + parts.join(", ") : "No agents yet")
      + " · " + (open ? plural(open, "piece of work", "pieces of work") + " open" : "no open work");
  }

  // --- the wordmark: the logo's 6-row pixel letters (each pair of rows is one terminal half-block line) -----

  const GLYPHS = {
    I: ["###", ".#.", ".#.", ".#.", ".#.", "###"],
    M: ["#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#"],
    P: ["###.", "#..#", "#..#", "###.", "#...", "#..."],
    E: ["####", "#...", "###.", "#...", "#...", "####"],
    R: ["###..", "#..#.", "#..#.", "###..", "#..#.", "#...#"],
    U: ["#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
  };
  function drawLogo() {
    const rows = ["", "", "", "", "", ""];
    [..."IMPERIUM"].forEach((ch, i) => { for (let r = 0; r < 6; r++) rows[r] += (i ? "." : "") + GLYPHS[ch][r]; });
    for (let r = 0; r < 6; r++) rows[r] += "..%%%";  // the cursor
    const logo = $("logo");
    for (const row of rows) for (const c of row) logo.appendChild(el("i", c === "#" ? "on" : c === "%" ? "cur" : null));
  }

  // --- loop -----------------------------------------------------------------------------------------------

  function standing(st) {
    const lines = [];
    if (st.quarantine)
      lines.push(["The record failed its integrity check.", "Everything that acts is stopped until you look at it.",
        "imperium quarantine release"]);
    if (st.stop_all)
      lines.push(["Everything is stopped: " + st.stop_all + ".",
        "Nothing new is sent" + (/\(by owner\)$/.test(st.stop_all) ? " and permission requests are on hold" : "")
          + ". Work already running inside an agent is not stopped.",
        "imperium resume-all"]);
    if (st.observe_only)
      lines.push(["Watching only, after a restore.", "Nothing is sent until you confirm the restored state.",
        "imperium restore-confirm"]);
    return lines;
  }
  function renderHeader(st) {
    const c = st.chain;
    if (st.quarantine) vital("chain", "damaged", "alert", "Integrity check failed; run imperium quarantine");
    else if (!c) vital("chain", "not checked yet", "quiet");
    else if (!c.ok) vital("chain", "damaged at #" + c.first_bad, "alert");
    else vital("chain", "intact", "ok", `Integrity checked at start-up through event #${c.checked_through_seq}; `
      + `${st.head_seq} events recorded now.`);
    vital("director", st.director_present ? "connected" : "not connected", st.director_present ? "ok" : "quiet",
      "The AI session that plans the work and gives it to the agents.");
  }

  async function refresh() {
    let data;
    try {
      data = await Promise.all([get("/v1/status"), get("/v1/rounds?all=1"), get("/v1/queue?all=1"),
        get("/v1/approvals"), get("/v1/questions"), get("/v1/journal?limit=80")]);
    } catch (err) {
      document.body.classList.add("is-stale");
      if (err instanceof Expired) {
        clearInterval(timer);
        vital("conn", "link expired", "alert");
        if (!lastOk) $("summary-head").textContent = "This link has expired.";
        banner([["This link has expired.", "Run this in your terminal for a new one:", "imperium dashboard"]]);
      } else {
        if (!lastOk) $("summary-head").textContent = "Imperium is not answering.";
        vital("conn", lastOk ? ago(Date.now() / 1000 - lastOk) + " ago" : "no answer", "alert");
        banner([["Imperium is not answering.",
          (lastOk ? "What you see is from " + ago(Date.now() / 1000 - lastOk) + " ago. " : "")
            + "Check that it is running:", "imperium status"]]);
      }
      return;
    }
    const [st, rounds, queue, approvals, questions, journal] = data;
    lastOk = Date.now() / 1000;
    document.body.classList.remove("is-stale", "no-data");
    vital("conn", new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), "ok",
      "Refreshed every 2 seconds");
    renderHeader(st);
    banner(standing(st));
    const items = needs(st, rounds.rounds, queue.messages, approvals.approvals, questions.questions);
    renderSummary(st, items, rounds.rounds);
    renderNeeds(items);
    renderAgents(st);
    renderRounds(rounds.rounds);
    renderLedger(queue.messages, rounds.rounds);
    renderJournal(journal.events);
    firstRender = false;
  }

  drawLogo();
  if (!token) {
    document.body.classList.add("is-stale");
    vital("conn", "no link", "alert");
    $("summary-head").textContent = "This page needs its link.";
    banner([["This page was opened without its link.", "Run this in your terminal to open it properly:",
      "imperium dashboard"]]);
    return;
  }
  refresh();
  timer = setInterval(refresh, POLL_MS);
})();
