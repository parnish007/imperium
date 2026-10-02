"use strict";
// Read-only Imperium dashboard. The token arrives in the URL fragment (never sent to a server), is removed from
// the address bar at once, and lives only in this page's memory. Every value is written with textContent.
(function () {
  const frag = new URLSearchParams(location.hash.slice(1));
  const token = frag.get("token");
  history.replaceState(null, "", location.pathname);
  const $ = (id) => document.getElementById(id);

  function cell(tr, text, cls) {
    const td = document.createElement("td");
    td.textContent = text == null ? "" : String(text);
    if (cls) td.className = cls;
    tr.appendChild(td);
  }
  function rows(tableId, items, fill) {
    const body = $(tableId).querySelector("tbody");
    body.replaceChildren();
    for (const it of items) { const tr = document.createElement("tr"); fill(tr, it); body.appendChild(tr); }
    if (!items.length) { const tr = document.createElement("tr"); cell(tr, "none", "muted"); body.appendChild(tr); }
  }
  function li(list, text, cls) {
    const el = document.createElement("li");
    el.textContent = text;
    if (cls) el.className = cls;
    list.appendChild(el);
  }
  async function get(path) {
    const r = await fetch(path, { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
    if (r.status === 401) throw new Error("token expired: run `imperium dashboard` again");
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  const stateClass = (s) => /UNCERTAIN|STRANDED|REJECTED|UNREACHABLE|HELD|CRITICAL/.test(s) ? "bad"
    : /WAITING|PENDING|QUEUED|CLAIMED|UNKNOWN/.test(s) ? "warn" : /ADMITTED|ACCEPTED|VERIFIED|IDLE/.test(s) ? "ok" : "";

  async function refresh() {
    try {
      const [st, rounds, queue, approvals, questions, journal] = await Promise.all([
        get("/v1/status"), get("/v1/rounds"), get("/v1/queue?all=1"), get("/v1/approvals"), get("/v1/questions"),
        get("/v1/journal?limit=60")]);
      $("conn").textContent = "live"; $("conn").className = "pill ok";
      const chain = st.chain || {};
      $("chain").textContent = st.quarantine ? "QUARANTINED" : (chain.ok ? "chain ok" : "chain BROKEN");
      $("chain").className = "pill " + (st.quarantine || !chain.ok ? "bad" : "ok");
      $("updated").textContent = "updated " + new Date().toLocaleTimeString();

      const needs = $("needs-list"); needs.replaceChildren();
      if (st.stop_all) li(needs, "STOP-ALL is on: " + st.stop_all, "bad");
      if (st.observe_only) li(needs, "Observe-only after a restore: run `imperium restore-confirm` when checked", "warn");
      for (const a of approvals.approvals) li(needs, `Approval ${a.id} (${a.builder}): ${a.permission} — ${a.state}`, stateClass(a.state));
      for (const q of questions.questions) li(needs, `Question ${q.id} (${q.builder}) waits for an answer`, "warn");
      for (const m of queue.messages) if (m.state === "UNCERTAIN" || m.state === "STRANDED")
        li(needs, `Message ${m.id} to ${m.builder} is ${m.state}: decide with imperium msg resolve`, "bad");
      for (const r of rounds.rounds) {
        if (r.state === "CLAIMED_READY") li(needs, `Round ${r.id} (${r.builder}) claims ready: verify it`, "warn");
        if (r.state === "VERIFIED") li(needs, `Round ${r.id} (${r.builder}) is verified: accept or reject`, "ok");
        if (r.escalation === "open") li(needs, `Round ${r.id} (${r.builder}) escalated`, "bad");
      }
      if (st.auto_answers_unreported) li(needs, `${st.auto_answers_unreported} automatic approval answer(s) not yet reviewed: imperium approvals --auto`, "warn");
      if (!needs.children.length) li(needs, "nothing", "muted");

      rows("fleet", st.builders || [], (tr, b) => {
        cell(tr, b.name); cell(tr, b.operational_state || b.status, stateClass(b.operational_state || ""));
        cell(tr, b.queued); cell(tr, b.in_flight || ""); cell(tr, b.dispatch_blocked || "");
        cell(tr, b.opencode_version);
      });
      rows("rounds", rounds.rounds, (tr, r) => {
        cell(tr, r.id, "mono"); cell(tr, r.builder); cell(tr, r.state, stateClass(r.state)); cell(tr, r.generation);
        cell(tr, r.escalation || ""); cell(tr, (r.objective || "").split("\n")[0], "objective");
      });
      rows("ledger", queue.messages.slice(-50).reverse(), (tr, m) => {
        cell(tr, m.id, "mono"); cell(tr, m.builder); cell(tr, m.source); cell(tr, m.kind);
        cell(tr, m.state, stateClass(m.state)); cell(tr, new Date(m.created * 1000).toLocaleString());
        cell(tr, m.round || "", "mono");
      });
      const ev = $("events"); ev.replaceChildren();
      for (const e of journal.events.slice().reverse()) li(ev, e.headline, e.severity === "CRITICAL" ? "bad" : e.severity === "ACTION" ? "warn" : "");
    } catch (err) {
      $("conn").textContent = String(err.message || err); $("conn").className = "pill bad";
    }
  }
  if (!token) { $("conn").textContent = "no token: run `imperium dashboard`"; $("conn").className = "pill bad"; return; }
  refresh();
  setInterval(refresh, 2000);
})();
