"use strict";
// Glovebox console. The whole view is derived from the mission's event stream (SSE), so a live
// mission and a replayed one render the same way. Every string from a model, a file or a sandbox
// goes through esc() before it touches the DOM.

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const usd = (n) => (n >= 0.01 ? `$${n.toFixed(3)}` : `$${(n || 0).toFixed(5)}`);
const dur = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);
const ROLE = { planner: "Planner", coder: "Coder", verifier: "Verifier", explainer: "Explainer", decision: "Decision gate",
  quarantine_reader: "Quarantine reader", browser_actor: "Browser actor", safety: "Safety screen" };
const QUESTION = { irreversible: "Irreversible", credential_use: "Uses a credential", off_task: "Off task", injection: "Injection" };
const SHORT_Q = { irreversible: "irreversible", credential_use: "credential", off_task: "off task", injection: "injection" };
const DECISION = { auto: ["ok", "auto"], require_approval: ["warn", "needs a human"], block_and_replan: ["bad", "blocked"] };
const ATTACK_LABEL = { analyst: "Simulate an attempt to steal the ERP token",
  operator: "Simulate the hidden note's browser detour" };
const TERMINAL = new Set(["mission.finished", "mission.failed"]);
const MISSION_NAME = { analyst: "Analyst", operator: "Operator", redteam: "Red Team" };
const REPLAY = Boolean(window.GLOVEBOX_REPLAY);

let status = null;
let events = [];
let seen = new Set();
let metrics = [];
let es = null;
let current = null;
let receiptChecks = [];
let template = "analyst";
let autoScroll = false;
let playback = false;
let streamIssue = "";
const open = new Set();

async function getJSON(url, opts) {
  if (REPLAY) return replayGet(url, opts);
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function thresholds() {
  const out = {};
  for (const g of status?.policy?.gates || []) for (const c of g.any) out[c.q] = c.gte;
  return out;
}

// ------------------------------------------------------------------ boot and streaming

async function boot() {
  try {
    status = await getJSON("/api/status");
  } catch (e) {
    $("#chips").innerHTML = `<span class="chip"><span class="dot bad"></span>Control plane unreachable</span>`;
    $("#timeline").innerHTML = `<div class="empty"><h1>Console unavailable</h1><p>Reload this page to reconnect.</p></div>`;
    return;
  }
  renderChips();
  const vendors = (status.templates || []).find((t) => t.id === "operator")?.vendors || [];
  $("#vendor-list").textContent = vendors.map((x) => x.vendor).join(" · ") + " · untrusted websites";
  for (const b of document.querySelectorAll(".seg button")) b.addEventListener("click", () => pickTemplate(b.dataset.template));
  $(".seg").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const next = e.key === "Home" ? "analyst" : e.key === "End" ? "operator" : template === "analyst" ? "operator" : "analyst";
    pickTemplate(next);
    $(`.seg button[data-template="${next}"]`).focus();
  });
  pickTemplate("analyst");
  $("#testmode").hidden = status.inference !== "scripted";
  if (REPLAY) replayBanner(status);
  if (matchMedia("(max-width: 740px)").matches) $(".history").open = false;
  setLaunchDisabled(false);
  $("#run").addEventListener("click", () => start(template));
  $("#redteam").addEventListener("click", () => start("redteam"));
  $("#approve").addEventListener("click", () => answerApproval(true));
  $("#reject").addEventListener("click", () => answerApproval(false));
  $("#skip-replay").addEventListener("click", () => es?.finish?.());
  for (const button of document.querySelectorAll("[data-jump]")) {
    button.addEventListener("click", () => {
      autoScroll = false;
      document.getElementById(button.dataset.jump)?.focus({ preventScroll: true });
      document.getElementById(button.dataset.jump)?.scrollIntoView({ block: "start" });
    });
  }
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (d.tagName === "DETAILS" && d.dataset.key) d.open ? open.add(d.dataset.key) : open.delete(d.dataset.key);
  }, true);
  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (id !== current && (!id || id.startsWith("msn_"))) {
      autoScroll = false;
      follow(id || null);
      if (id) $("#mission-toolbar").scrollIntoView({ block: "start" });
    }
  });
  await loadRecent();
  const id = location.hash.slice(1);
  id.startsWith("msn_") ? follow(id) : render();
  if (!REPLAY) setInterval(refreshChips, 15000);
}

function renderChips() {
  if (REPLAY) { $("#chips").innerHTML = replayChips(status); return; }
  const roster = status.roster || [];
  const ok = roster.filter((r) => r.ok).length;
  const runner = status.runner || {};
  const live = status.inference === "vultr";
  $("#chips").innerHTML = [
    `<span class="chip"><span class="dot ${live ? (ok === roster.length ? "ok" : "bad") : ""}"></span>${live
      ? `Vultr Serverless Inference <b>${ok}/${roster.length} roles ready</b>` : `Scripted models <b>test mode</b>`}</span>`,
    `<span class="chip"><span class="dot ${runner.ok ? "ok" : "bad"}"></span>Sandbox host <b>${runner.ok
      ? `microVMs · ${esc(runner.guest_arch)}` : "unreachable"}</b>${runner.ok ? ` · ${runner.running} running` : ""}</span>`,
  ].join("");
}

async function refreshChips() {
  try {
    status = { ...status, ...(await getJSON("/api/status")) };
    renderChips();
  } catch { /* keep the last known state */ }
}

async function loadRecent() {
  let list = [];
  try { list = await getJSON("/api/missions"); } catch { /* empty */ }
  $("#recent").innerHTML = list.length
    ? list.map((m) => `<li><a href="#${esc(m.id)}" ${m.id === current ? 'aria-current="true"' : ""}>
        <span class="recent-label"><b>${esc(MISSION_NAME[m.template] || "Mission")}</b><small>${REPLAY ? (m.inference === "vultr" ? "Vultr" : "Scripted") : esc(m.status)}</small></span>
        <span class="recent-goal">${esc(m.goal)}</span></a></li>`).join("")
    : `<li class="muted">No missions yet.</li>`;
}

function pickTemplate(id) {
  template = id;
  for (const b of document.querySelectorAll(".seg button")) {
    b.setAttribute("aria-checked", String(b.dataset.template === id));
    b.tabIndex = b.dataset.template === id ? 0 : -1;
  }
  for (const el of document.querySelectorAll("[data-for]")) el.hidden = el.dataset.for !== id;
  $("#goal").value = (status.templates || []).find((t) => t.id === id)?.default_goal || status.default_goal;
  $("#attack-label").textContent = ATTACK_LABEL[id];
  $("#run").textContent = `${REPLAY ? "Play" : "Run"} ${MISSION_NAME[id]}`;
}

function setLaunchDisabled(disabled) {
  $("#run").disabled = disabled;
  $("#redteam").disabled = disabled;
}

async function start(kind) {
  $("#runhint").textContent = "";
  setLaunchDisabled(true);
  try {
    const options = kind === "redteam" ? {} : { goal: $("#goal").value, erp_api: $("#erp").checked,
      simulate_compromise: $("#attack").checked };
    const { id } = await getJSON("/api/missions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template: kind, ...options }),
    });
    location.hash = id;
    autoScroll = true;
    follow(id, REPLAY);
    $("#mission-toolbar").scrollIntoView({ block: "start" });
  } catch (e) {
    $("#runhint").textContent = e.message;
    setLaunchDisabled(false);
  }
}

function follow(id, play = false) {
  if (es) es.close();
  es = null;
  if ($("#approval").open) $("#approval").close();
  pendingApproval = null;
  current = id;
  streamIssue = "";
  playback = play;
  events = [];
  seen = new Set();
  metrics = [];
  receiptChecks = [];
  open.clear();
  render();
  loadRecent();
  setLaunchDisabled(Boolean(id));
  if (!id) return;
  const source = REPLAY ? replaySource(id, play) : new EventSource(`/api/missions/${encodeURIComponent(id)}/events`);
  es = source;
  let frame = null;
  source.onmessage = (msg) => {
    if (source !== es) return;
    const e = JSON.parse(msg.data);
    if (seen.has(e.seq)) return;
    seen.add(e.seq);
    if (e.type === "metrics.sample") {
      metrics.push(e);
      drawSpark();
      return;
    }
    // Follow a mission started here while its newest card is on screen; the side columns can be taller.
    const end = () => $("#timeline").getBoundingClientRect().bottom - window.innerHeight;
    const stick = autoScroll && end() <= 400;
    events.push(e);
    streamIssue = "";
    if (REPLAY && e.type === "mission.started" && e.template in ATTACK_LABEL) pickTemplate(e.template);
    if (frame === null) frame = requestAnimationFrame(() => {
      frame = null;
      if (source !== es) return;
      render();
      if (stick && autoScroll && end() > 0) window.scrollBy(0, end() + 24);
    });
    if (e.type === "approval.requested" && !REPLAY) showApproval(e);
    if (e.type === "approval.resolved" && e.approval === pendingApproval) {
      pendingApproval = null;
      if ($("#approval").open) $("#approval").close();
    }
    if (TERMINAL.has(e.type)) {
      source.close();
      playback = false;
      setLaunchDisabled(false);
      loadRecent();
      if (!REPLAY) refreshChips();
    }
  };
  source.onerror = () => {
    if (source !== es) return;
    if (REPLAY || source.readyState === EventSource.CLOSED) {
      streamIssue = REPLAY ? "Recording unavailable" : "Mission unavailable";
      playback = false;
      source.close();
      setLaunchDisabled(false);
    } else {
      streamIssue = "Reconnecting…";
    }
    render();
  };
}

// ------------------------------------------------------------------ view model

function attempt(v, step, n) {
  const key = `${step}#${n}`;
  return (v.attempts[key] ||= { step, n });
}

function op(v, step) {
  return (v.ops[step] ||= { step, actions: [], quarantine: [] });
}

function derive(list) {
  const v = { attempts: {}, containment: [], calls: [], artifacts: [], sandboxes: {}, gates: [], skipped: [], host: [],
    ops: {}, opOrder: [], shots: [] };
  for (const e of list) {
    switch (e.type) {
      case "mission.queued": v.queued = e; break;
      case "mission.started":
        v.started = e;
        if (e.attacks) v.attacks = Object.fromEntries(e.attacks.map((a) => [a.id, { ...a, state: "wait" }]));
        break;
      case "attack.started": if (v.attacks?.[e.attack]) v.attacks[e.attack].state = "run"; break;
      case "attack.sandbox":
        v.sandboxes[e.sandbox.id] = { ...e.sandbox, purpose: e.attack, state: "running" };
        v.lastSandbox = e.sandbox.id;
        break;
      case "attack.finished": {
        const a = v.attacks?.[e.attack];
        if (a) Object.assign(a, { state: e.outcome, evidence: e.evidence, ms: e.duration_ms });
        if (e.outcome === "contained") v.containment.push({ layer: a?.layer || "contained", detail: `${a?.name || e.attack}: ${e.evidence[0] || ""}` });
        if (e.outcome === "escaped") v.containment.push({ layer: "alarm", detail: `${a?.name || e.attack} got through: ${e.evidence.join("; ")}` });
        break;
      }
      case "host.sample": v.host.push(e); break;
      case "redteam.finished": v.rt = e; break;
      case "quarantine.scan": v.scan = e; break;
      case "containment":
        v.containment.push(e);
        if (e.layer === "quarantine" && v.curStep) op(v, v.curStep).quarantine.push(e);
        break;
      case "secret.used": v.containment.push({ ...e, layer: "secret used", good: true }); v.secretUsed = e; break;
      case "model.call": v.calls.push(e); break;
      case "plan.created": v.plan = e.plan; break;
      case "gate.scored":
        v.gates.push(e);
        if (e.turn !== undefined && e.simulated) { if (v.detour) v.detour.gate = e; }
        else if (e.turn !== undefined) op(v, e.step).actions.push({ gate: e });
        break;
      case "approval.requested": {
        v.approval = e;
        const rows = v.ops[e.step]?.actions || [];
        if (rows.length) rows[rows.length - 1].approval = e;
        break;
      }
      case "approval.resolved":
        v.approvalResolved = e;
        for (const s of Object.values(v.ops)) for (const r of s.actions) if (r.approval?.approval === e.approval) r.approved = e.approved;
        break;
      case "attack.simulated": if (e.action) v.detour = { sim: e, gate: null, actions: [], result: null }; else v.attack = e; break;
      case "attack.result": if (v.detour) v.detour.result = e; break;
      case "step.started":
        Object.assign(op(v, e.step), { intent: e.intent, allow: e.allow_domains, irreversible: e.irreversible, state: "running" });
        v.opOrder.push(e.step);
        v.curStep = e.step;
        break;
      case "browser.action": {
        if (e.screenshot) { v.shot = e; v.shots.push(e); }
        if (e.purpose === "attack") { v.detour?.actions.push(e); break; }
        const rows = op(v, e.step).actions;
        const last = rows[rows.length - 1];
        if (last && last.gate && !last.act && last.gate.decision !== "block_and_replan") last.act = e; else rows.push({ act: e });
        break;
      }
      case "actor.reported": op(v, e.step).actions.push({ report: e }); break;
      case "step.finished":
        Object.assign(op(v, e.step), { result: e, state: e.status });
        if (!v.opOrder.includes(e.step)) v.opOrder.push(e.step);
        break;
      case "code.generated": attempt(v, e.step, e.attempt).code = e; break;
      case "sandbox.created":
        if (e.purpose === "attack") v.attackSandbox = e;
        else if (e.purpose === "browser") v.browserBox = e.sandbox;
        else attempt(v, e.step, e.attempt).sandbox = e;
        v.sandboxes[e.sandbox.id] = { ...e.sandbox, purpose: e.purpose, state: "running" };
        v.lastSandbox = e.sandbox.id;
        break;
      case "sandbox.destroyed": if (v.sandboxes[e.sandbox]) v.sandboxes[e.sandbox].state = "destroyed"; break;
      case "exec.finished": if (e.purpose === "attack") v.attackRun = e; else attempt(v, e.step, e.attempt).run = e; break;
      case "selfheal": attempt(v, e.step, e.attempt).heal = e; break;
      case "artifact.saved": v.artifacts.push(e); break;
      case "verify.result": v.verify = e; if (v.ops[e.step]) v.ops[e.step].verify = e; break;
      case "step.skipped": v.skipped.push(e); break;
      case "mission.answer": v.answer = e.text; break;
      case "receipt.sealed": v.receipt = e; break;
      case "mission.finished": v.finished = e; break;
      case "mission.failed": v.failed = e; break;
      default: break;
    }
  }
  return v;
}

function lastJSON(stdout) {
  for (const line of (stdout || "").trim().split("\n").reverse()) {
    if (line.startsWith("{")) {
      try { return JSON.parse(line); } catch { return null; }
    }
  }
  return null;
}

const callFor = (v, role) => v.calls.filter((c) => c.role === role);
const callMeta = (calls) => calls.length
  ? calls.map((c) => `${esc(c.served)} · ${dur(c.ms)} · ${usd(c.usd)}`).join(" | ") : "";

// ------------------------------------------------------------------ render

function render() {
  const v = derive(events);
  $("#timeline").innerHTML = !current ? emptyState() : streamIssue && !events.length
    ? `<article class="card"><h3>${esc(streamIssue)}</h3><p>Choose a recent mission or start another run.</p></article>` : v.attacks ? redteamView(v)
    : v.started?.template === "operator" ? operatorView(v) : timeline(v);
  renderMissionToolbar(v);
  for (const d of document.querySelectorAll("#timeline details[data-key]")) d.open = open.has(d.dataset.key);
  for (const b of document.querySelectorAll("#timeline [data-approve]")) {
    b.addEventListener("click", () => answerApproval(b.dataset.approve === "1", b.dataset.id));
  }
  bindReceipt(v);
  $("#blast").innerHTML = blast(v);
  $("#sandbox").innerHTML = sandboxPanel(v);
  $("#feed").innerHTML = feed(v);
  $("#cost").innerHTML = costs(v);
  drawSpark();
}

function renderMissionToolbar(v) {
  $("#mission-toolbar").hidden = !current;
  if (!current) return;
  $("#mission-kind").textContent = MISSION_NAME[v.started?.template] || "Mission";
  const pending = v.approval && v.approval.approval !== v.approvalResolved?.approval;
  const [label, state] = streamIssue ? [streamIssue, "warn"] : v.failed ? ["Failed", "bad"] : v.finished ? ["Completed", "ok"]
    : pending ? [REPLAY ? "Recorded approval" : "Awaiting approval", "warn"]
    : v.started ? [playback ? "Playing recording" : "Running", "acc"] : [REPLAY ? "Loading recording" : "Queued", "acc"];
  const indicator = $("#mission-state");
  if (indicator.textContent !== label) indicator.textContent = label;
  indicator.className = `pill ${state}`;
  for (const button of document.querySelectorAll("[data-jump]")) button.hidden = !document.getElementById(button.dataset.jump);
  $("#skip-replay").hidden = !playback;
}

function card(stage, title, body, { cls = "", meta = "", tag = "", id = "" } = {}) {
  return `<article class="card ${cls}"${id ? ` id="${id}" tabindex="-1"` : ""}><div class="card-head"><h3><span class="stage">${esc(stage)}</span>${title}</h3>${tag}</div>
    ${meta ? `<div class="meta">${meta}</div>` : ""}${body}</article>`;
}
const tape = (text) => `<span class="tape"><span>${esc(text)}</span></span>`;

function emptyState() {
  return `<div class="empty workspace-intro">
    <p class="eyebrow">${REPLAY ? "Mission archive" : "Mission control"}</p>
    <h1>${REPLAY ? "Replay a sandbox mission" : "Run an agent in a microVM"}</h1>
    <p>${REPLAY ? "Play a recorded mission or open its finished run from the list. Each receipt can be checked in this browser."
      : "Choose a mission to watch Glovebox execute code or drive Chromium with restricted network access and a record of each action."}</p>
    <dl class="mission-guide">
      <div><dt>01 <b>Analyst</b></dt><dd>Audit 401 vendor payments and inspect the spend chart.</dd></div>
      <div><dt>02 <b>Operator</b></dt><dd>Compare three carts and review the purchase request before submission.</dd></div>
      <div><dt>03 <b>Red Team</b></dt><dd>See which controls contain 12 attacks against the sandbox.</dd></div>
    </dl></div>`;
}

function timeline(v) {
  const cards = [];
  if (!v.started && !v.failed) cards.push(card("Queued", "Waiting for a free slot", `<p class="muted">The mission starts when a sandbox slot is free.</p>`));
  if (v.started) cards.push(missionCard(v));
  if (v.scan) cards.push(quarantineCard(v));
  if (v.plan) cards.push(planCard(v));
  for (const g of v.gates) cards.push(gateCard(v, g));
  if (v.attack) cards.push(attackCard(v));
  for (const a of Object.values(v.attempts).sort((x, y) => x.n - y.n)) cards.push(attemptCard(a));
  for (const s of v.skipped) cards.push(card("Skipped", `Step ${esc(s.step)}`, `<p>${esc(s.reason)}</p>`));
  if (v.artifacts.length || v.verify) cards.push(resultCard(v));
  if (v.answer) cards.push(answerCard(v));
  if (v.failed) cards.push(card("Failed", "Mission stopped", `<pre class="err">${esc(v.failed.error)}</pre>`, { cls: "fail" }));
  if (v.receipt) cards.push(receiptCard(v));
  return cards.join("");
}

function missionCard(v) {
  const s = v.started;
  if (s.template === "operator") {
    const opts = [`<span class="pill acc">Chromium in a microVM</span>`,
      s.options?.simulate_compromise ? `<span class="pill bad">red team on</span>` : "",
      `<span class="pill ${s.inference === "vultr" ? "ok" : "warn"}">${s.inference === "vultr" ? "Vultr Serverless Inference" : "scripted models"}</span>`].join(" ");
    const sites = (s.vendors || []).map((x) => `<dt>${esc(x.vendor)}</dt><dd>${esc(x.name)}</dd>`).join("");
    return card("Mission", esc(s.goal), `<div class="sbx">${opts}</div><dl class="kv">${sites}</dl>
      <p class="muted">Form details supplied by the user: ${esc((s.requester || []).join(", "))}.</p>`, { meta: esc(events[0]?.mission || "") });
  }
  const opts = [
    s.options?.erp_api ? `<span class="pill acc">ERP API via placeholder</span>` : `<span class="pill">ERP data as a file</span>`,
    s.options?.simulate_compromise ? `<span class="pill bad">red team on</span>` : "",
    `<span class="pill ${s.inference === "vultr" ? "ok" : "warn"}">${s.inference === "vultr" ? "Vultr Serverless Inference" : "scripted models"}</span>`,
  ].join(" ");
  const files = (s.files || []).map((f) => `<dt>${esc(f.name)}</dt><dd>${(f.bytes / 1024).toFixed(1)} KiB · sha256 ${esc(f.sha256.slice(0, 16))}…</dd>`).join("");
  return card("Mission", esc(v.started.goal), `<div class="sbx">${opts}</div><dl class="kv">${files}</dl>`,
    { meta: esc(events[0]?.mission || "") });
}

function quarantineCard(v) {
  const q = v.containment.filter((c) => c.layer === "quarantine");
  const body = `<p>Screened ${v.scan.distinct_cells} distinct text cells. ${v.scan.candidates} looked like instructions and went to the decision model.</p>`
    + q.map((c) => `<div class="excerpt"><span class="pill warn">p = ${Number(c.probability).toFixed(2)}</span> ${esc((c.rows || []).join(", "))}<br><s>${esc(c.excerpt)}</s></div>`).join("")
    + (q.length ? `<p class="muted">The planner and the coder see "[quarantined by Glovebox: text addressed to AI agents]" instead.</p>` : "");
  return card("Quarantine", q.length ? `${q.length} injected instruction removed` : "No injected instructions found", body,
    { id: v.attack ? "" : "evidence", cls: q.length ? "contain" : "", tag: q.length ? tape("Containment") : "", meta: callMeta(callFor(v, "decision").slice(0, 1)) });
}

function planCard(v) {
  const steps = (v.plan.steps || []).map((s) => `<li><span class="pill">${esc(s.kind)}</span> ${esc(s.intent)}
    ${s.needs_api ? `<span class="pill acc">API: ${esc(s.needs_api)}</span>` : ""} ${s.irreversible ? `<span class="pill warn">irreversible</span>` : ""}</li>`).join("");
  return card("Plan", esc(v.plan.goal_summary || "Plan"), `<ol class="steps">${steps}</ol>`, { meta: callMeta(callFor(v, "planner")) });
}

function bars(probs, exempt = []) {
  const t = thresholds();
  return `<div class="bars">${Object.entries(probs).map(([q, p]) => {
    const skip = exempt.includes(q);
    const hot = !skip && t[q] !== undefined && p >= t[q];
    return `<div class="bar"><span>${esc(QUESTION[q] || q)}${skip ? " <em>(not counted)</em>" : ""}</span><span class="track"><span class="fill ${skip ? "exempt" : hot ? "hot" : ""}" style="width:${(p * 100).toFixed(1)}%"></span>
      ${t[q] !== undefined ? `<span class="mark" style="left:${t[q] * 100}%" title="policy threshold ${t[q]}"></span>` : ""}</span><span class="num">${p.toFixed(2)}</span></div>`;
  }).join("")}</div>`;
}

function gateCard(v, g) {
  const label = { auto: ["ok", "Runs automatically"], require_approval: ["warn", "Needs a human"], block_and_replan: ["bad", "Blocked"] }[g.decision] || ["", g.decision];
  const exempt = (g.exempt || []).length
    ? `<p class="muted">"Uses a credential" is not counted here: ${esc(g.exempt_reason || "the ERP token is held by the platform and the sandbox only gets a placeholder")}.</p>` : "";
  return card("Gate", `Step ${esc(g.step)} risk check`, bars(g.probs, g.exempt || []) + exempt,
    { tag: `<span class="pill ${label[0]}">${esc(label[1])}</span>`, meta: callMeta(callFor(v, "decision").slice(-1)) });
}

function sandboxChips(sbx) {
  if (!sbx) return "";
  const l = sbx.limits || {};
  const net = sbx.network?.egress === "none" ? "network: none" : `network: only ${esc((sbx.network?.allow || []).join(", "))}`;
  const secrets = (sbx.secrets || []).map((s) => `<span class="pill acc">${esc(s.env)} → guest sees ${esc(s.guest_sees)}</span>`).join("");
  return `<div class="sbx"><span class="pill">${esc(sbx.id)}</span><span class="pill">${esc(sbx.isolation)}</span><span class="pill">${esc(sbx.kernel)}</span>
    <span class="pill">${l.cpus} vCPU · ${l.memory_mib} MiB · ${l.exec_timeout_s} s · user ${esc(l.user)}</span><span class="pill">${net}</span>${secrets}
    <span class="pill">boot ${esc(sbx.boot_ms)} ms</span></div>`;
}

function attackCard(v) {
  const run = v.attackRun;
  const blocked = v.containment.filter((c) => c.layer === "network" && v.attackSandbox && c.sandbox === v.attackSandbox.sandbox.id);
  const body = `<p>The red-team switch is on, so Glovebox also runs what a model fooled by the memo would have run, in its own microVM.</p>
    ${sandboxChips(v.attackSandbox?.sandbox)}
    <details data-key="attack-code"><summary>Show the attacker code</summary><pre>${esc(v.attack.code)}</pre></details>
    ${run ? `<pre>${esc(run.stdout)}</pre>` : `<p class="muted">Running…</p>`}
    ${blocked.map((c) => `<div class="excerpt">${esc(c.detail)}</div>`).join("")}`;
  return card("Red team", "Simulated compromised model", body, { id: "evidence", cls: "contain", tag: tape("Containment moment") });
}

function attemptCard(a) {
  const run = a.run;
  const state = !run ? `<span class="pill acc">running</span>` : run.exit_code === 0 ? `<span class="pill ok">exit 0 · ${dur(run.duration_ms)}</span>`
    : run.timed_out ? `<span class="pill bad">timed out</span>` : `<span class="pill bad">exit ${esc(run.exit_code)} · ${dur(run.duration_ms)}</span>`;
  const err = run && run.exit_code !== 0 ? `<pre class="err">${esc(run.stderr.split("\n").slice(-8).join("\n"))}</pre>` : "";
  const heal = a.heal ? `<p>Self-heal: the error goes back to the coder, and attempt ${a.n + 1} runs in a new microVM.</p>` : "";
  const out = run && run.stdout ? `<details data-key="out-${esc(a.step)}-${a.n}"><summary>Program output</summary><pre>${esc(run.stdout)}</pre></details>` : "";
  return card(`Attempt ${a.n}`, `Step ${esc(a.step)} in a fresh microVM`,
    sandboxChips(a.sandbox?.sandbox)
    + (a.code ? `<details data-key="code-${esc(a.step)}-${a.n}"><summary>Code written by the coder (sha256 ${esc(a.code.sha256.slice(0, 12))}…)</summary><pre>${esc(a.code.code)}</pre></details>` : "")
    + err + heal + out,
    { cls: run ? (run.exit_code === 0 ? "good" : "fail") : "", tag: state });
}

function resultCard(v) {
  const steps = Object.values(v.attempts).filter((a) => a.run && a.run.exit_code === 0);
  const summary = steps.length ? lastJSON(steps[steps.length - 1].run.stdout) : null;
  const metrics = summary?.metrics ? `<div class="metrics">${Object.entries(summary.metrics).map(([k, val]) =>
    `<div class="metric"><b>${typeof val === "number" && k.includes("usd") ? "$" + val.toLocaleString() : esc(val)}</b><span>${esc(k.replaceAll("_", " "))}</span></div>`).join("")}</div>` : "";
  const imgs = v.artifacts.map((a) => `<a href="${esc(a.url)}" target="_blank" rel="noopener"><img class="result-img" src="${esc(a.url)}" alt="${esc(a.name)}"></a>`).join("");
  const ver = v.verify ? `<p><span class="pill ${v.verify.passed ? "ok" : "bad"}">${v.verify.passed ? "verified" : "not verified"}</span>
    <span class="muted">${esc(v.verify.observed)}</span></p>` : `<p class="muted">Verifying…</p>`;
  return card("Result", esc(summary?.summary || "Result"), metrics + imgs + ver, { id: "result-card", meta: callMeta(callFor(v, "verifier")) });
}

const inline = (s) => s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
  .replace(/\[(s\d+)\]/g, '<span class="cite">$1</span>');

function formatAnswer(text) {
  let html = "";
  let inList = false;
  for (const line of esc(text).split("\n")) {
    if (/^\s*-\s+/.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(line.replace(/^\s*-\s+/, ""))}</li>`;
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    html += /^Contained:/.test(line.trim()) ? `<span class="contained-line">${inline(line)}</span>` : `${inline(line)}\n`;
  }
  return html + (inList ? "</ul>" : "");
}

function answerCard(v) {
  const id = v.started?.template === "operator" || !(v.artifacts.length || v.verify) ? "result-card" : "";
  return card("Answer", "What Glovebox found", `<div class="answer">${formatAnswer(v.answer)}</div>`, { id, cls: "good", meta: callMeta(callFor(v, "explainer")) });
}

function receiptCard(v) {
  const r = v.receipt;
  const checks = receiptChecks.map((c) => `<div><span class="pill ${c.valid ? "ok" : "bad"}">${c.valid ? "valid" : "rejected"}</span> ${esc(c.label)}: ${esc(c.reason)}</div>`).join("");
  return card("Receipt", "Signed record of everything that ran",
    `<dl class="kv"><dt>Records</dt><dd>${r.records}</dd><dt>Chain head</dt><dd>${esc(r.chain_head)}</dd><dt>Signing key</dt><dd>Ed25519 · ${esc(r.key_id)}</dd></dl>
     <div class="verify-row"><button type="button" id="verify-btn">Verify receipt</button><button type="button" id="tamper-btn">Tamper with one record</button>
     <a class="ghost" href="${esc(r.url)}" target="_blank" rel="noopener">Open JSON</a></div><div class="verify-out" role="status">${checks}</div>`, { id: "receipt-card" });
}

function bindReceipt(v) {
  if (!v.receipt) return;
  const check = async (button, mutate) => {
    const mission = current;
    button.disabled = true;
    try {
      const receipt = await getJSON(v.receipt.url);
      const label = mutate ? mutate(receipt) : "Original receipt";
      const res = await getJSON("/api/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(receipt) });
      if (mission !== current) return;
      receiptChecks.push({ label, ...res });
      render();
      document.getElementById(button.id)?.focus({ preventScroll: true });
    } catch (error) {
      if (mission === current) $(".verify-out").textContent = `Could not verify the receipt. ${error.message}`;
    } finally {
      button.disabled = false;
    }
  };
  $("#verify-btn")?.addEventListener("click", (e) => check(e.currentTarget));
  $("#tamper-btn")?.addEventListener("click", (e) => check(e.currentTarget, (rec) => {
    const failed = rec.records.find((x) => x.kind === "exec" && x.exit_code !== 0);
    const approval = rec.records.find((x) => x.kind === "approval");
    if (failed) { failed.exit_code = 0; return "After marking the failed run as passed"; }
    if (approval) { approval.approved = !approval.approved; return "After flipping the human's approval"; }
    rec.records[0].goal_sha256 = "0".repeat(64);
    return "After changing one record";
  }));
}

const STATE = { wait: ["wait", "queued"], run: ["run", "running"], contained: ["ok", "contained"], escaped: ["bad", "escaped"], error: ["err", "test error"] };

function attackTile(a) {
  const [cls, label] = STATE[a.state] || STATE.wait;
  return `<div class="atk ${cls}"><div class="atk-head"><h4>${esc(a.name)}</h4><span class="verdict ${cls}">${label}</span></div>
    <code>${esc(a.technique)}</code>
    ${a.evidence ? `<ul>${a.evidence.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    <div class="foot"><span>${esc(a.layer)}</span><span>${a.ms !== undefined ? dur(a.ms) : ""}</span></div></div>`;
}

function redteamView(v) {
  const list = Object.values(v.attacks);
  const count = (s) => list.filter((a) => a.state === s).length;
  const contained = count("contained");
  const escaped = count("escaped");
  const errors = count("error");
  const finished = contained + escaped + errors;
  const headline = !v.rt ? `${finished} of ${list.length} attacks finished`
    : escaped ? `${escaped} attack${escaped > 1 ? "s" : ""} got through`
    : errors ? `${contained} contained, ${errors} could not run` : "Every attack contained";
  const loads = v.host.map((h) => h.load1);
  const host = loads.length ? `sandbox host load ${loads[0].toFixed(2)} → peak ${Math.max(...loads).toFixed(2)} on ${v.host[v.host.length - 1].cpus} CPUs` : "";
  const secs = v.rt ? ` · ${((Date.parse(v.rt.ts) - Date.parse(v.started.ts)) / 1000).toFixed(1)} s` : "";
  const head = `<article class="card score-card" id="result-card" tabindex="-1"><div class="score ${escaped ? "bad" : ""}">${contained}<small>/${list.length}</small></div>
    <div class="score-meta"><h3>${esc(headline)}</h3>
      <div class="segments">${list.map((a) => `<i class="${(STATE[a.state] || STATE.wait)[0]}" title="${esc(a.name)}"></i>`).join("")}</div>
      <div class="meta">Each attack runs in its own fresh microVM${esc(host ? " · " + host : "")}${secs}</div></div></article>`;
  return head + `<div class="attack-grid" id="evidence" tabindex="-1">${list.map(attackTile).join("")}</div>`
    + (v.failed ? card("Failed", "Run stopped", `<pre class="err">${esc(v.failed.error)}</pre>`, { cls: "fail" }) : "")
    + (v.receipt ? receiptCard(v) : "");
}

// ------------------------------------------------------------------ operator

const hostOf = (url) => { try { return new URL(url).host; } catch { return url || ""; } };
const pathOf = (url) => { try { const u = new URL(url); return u.host.split(".")[0] + u.pathname; } catch { return url || ""; } };
const netError = (err) => /net::[A-Z_]+/.exec(err || "")?.[0] || String(err || "").slice(0, 80);
const money = (s) => { const m = /\$([\d,]+(?:\.\d+)?)/.exec(s || ""); return m ? Number(m[1].replace(/,/g, "")) : null; };

function operatorView(v) {
  const cards = [];
  if (!v.started && !v.failed) cards.push(card("Queued", "Waiting for a free slot", `<p class="muted">The mission starts when a sandbox slot is free.</p>`));
  if (v.started) cards.push(missionCard(v));
  if (v.plan) cards.push(planCard(v));
  for (const id of v.opOrder) {
    const s = v.ops[id];
    cards.push(stepCard(s));
    if (v.detour?.sim.step === id) cards.push(detourCard(v.detour));
    const asked = s.actions.find((r) => r.approval);
    if (asked) cards.push(approvalCard(asked, asked.approval.approval === v.approval?.approval));
  }
  for (const s of v.skipped) cards.push(card("Skipped", `Step ${esc(s.step)}`, `<p>${esc(s.reason)}</p>`));
  if (v.browserBox) cards.push(browserCard(v));
  cards.push(quotesCard(v));
  if (v.answer) cards.push(answerCard(v));
  if (v.failed) cards.push(card("Failed", "Mission stopped", `<pre class="err">${esc(v.failed.error)}</pre>`, { cls: "fail" }));
  if (v.receipt) cards.push(receiptCard(v));
  return cards.join("");
}

function miniProbs(g) {
  const t = thresholds();
  return Object.entries(g.probs).map(([q, p]) => {
    const skip = (g.exempt || []).includes(q);
    const hot = !skip && t[q] !== undefined && p >= t[q];
    const title = `${QUESTION[q] || q}${skip ? ` (not counted: ${g.exempt_reason || "exempt"})` : ""}`;
    return `<span class="mp ${hot ? "hot" : ""} ${skip ? "skip" : ""}" title="${esc(title)}">${esc(SHORT_Q[q] || q)} ${p.toFixed(2)}</span>`;
  }).join("");
}

function actionRow(r) {
  if (r.report) return `<li class="act warn"><span class="thumb none"></span><div><span class="pill warn">reported</span> the actor flagged text that tries to instruct it<div class="excerpt"><s>${esc(r.report.text)}</s></div></div></li>`;
  const g = r.gate;
  const a = r.act;
  const [cls, label] = DECISION[g?.decision] || ["", "ran"];
  const what = g ? g.action : { type: a.tool, target: a.target };
  const typed = what.text !== undefined ? ` <span class="typed">“${esc(what.text)}”</span>` : what.option !== undefined ? ` <span class="typed">${esc(what.option)}</span>` : "";
  const result = a ? (a.error ? `<span class="act-err">${esc(netError(a.error))}</span>` : `<span class="muted">→ ${esc(pathOf(a.url))}</span>`)
    : g?.decision === "block_and_replan" ? "" : `<span class="muted">…</span>`;
  const human = !r.approval ? "" : r.approved === undefined ? `<span class="pill warn">waiting for a human</span>`
    : r.approved ? `<span class="pill ok">approved by a human</span>` : `<span class="pill bad">rejected by a human</span>`;
  const thumb = a?.screenshot ? `<a href="${esc(a.screenshot)}" target="_blank" rel="noopener"><img class="thumb" src="${esc(a.screenshot)}" alt="" loading="lazy"></a>` : `<span class="thumb none"></span>`;
  return `<li class="act ${cls}">${thumb}<div><span class="pill ${cls}">${label}</span> <b>${esc(what.type)}</b> <span class="target">${esc(what.target)}</span>${typed} ${result} ${human}
    ${g ? `<div class="probs">${miniProbs(g)}</div>` : ""}${(g?.reasons || []).length ? `<div class="reasons">${g.reasons.map(esc).join(" · ")}</div>` : ""}</div></li>`;
}

function stepCard(s) {
  const st = s.state || "running";
  const tag = st === "running" ? `<span class="pill acc">running</span>`
    : st === "done" ? `<span class="pill ${s.verify?.passed ? "ok" : "warn"}">${s.verify?.passed ? "done · verified" : "done · not verified"}</span>`
    : `<span class="pill bad">${esc(st)}</span>`;
  const allow = (s.allow || []).map((d) => `<span class="pill">${esc(d)}</span>`).join("");
  const quarantine = s.quarantine.length ? `<div class="quarantine-block">${tape("Quarantine")}${s.quarantine.map((c) =>
    `<div class="excerpt"><span class="pill warn">p = ${Number(c.probability).toFixed(2)}</span> hidden on ${esc(hostOf(c.url))}<br><s>${esc(c.excerpt)}</s></div>`).join("")}
    <p class="muted">Removed before the browser actor read the page; it sees "[quarantined by Glovebox: text addressed to AI agents]" instead.</p></div>` : "";
  const facts = s.result?.facts?.length ? `<dl class="kv facts">${s.result.facts.map((f) => `<dt>${esc(f.name)}</dt><dd>${esc(f.value)}</dd>`).join("")}</dl>` : "";
  const checked = s.verify ? `<p class="muted">Verifier: ${esc(s.verify.observed)}</p>` : "";
  return card(`Step ${s.step}`, esc(s.intent || s.result?.intent || "Browser step"), `<div class="sbx">${allow}${s.irreversible ? `<span class="pill warn">irreversible</span>` : ""}</div>
    <ol class="acts">${s.actions.map(actionRow).join("")}</ol>${quarantine}${facts}${checked}`,
    { cls: st === "done" ? "good" : st === "running" ? "" : "fail", tag, meta: s.result ? `${s.result.turns} actor turns` : "" });
}

function detourCard(d) {
  const shots = (d.result?.screenshots || []).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener"><img class="result-img" src="${esc(u)}" width="1280" height="800" alt="Chromium inside the microVM: this site can't be reached"></a>`).join("");
  return card("Red team", "The detour the hidden note asks for", `<p>The red-team switch is on, so Glovebox also tries what an actor fooled by Northgate's hidden note would do next: <code>goto ${esc(d.sim.action.target)}</code></p>
    ${d.gate ? bars(d.gate.probs, []) + `<p><span class="pill bad">blocked</span> <span class="muted">${(d.gate.reasons || []).map(esc).join(" · ")}</span></p>` : ""}
    ${d.result ? `<p>Then it runs anyway, inside the browser VM, to test the next layer:</p><ul class="evidence">${d.result.evidence.map((x) => `<li>${esc(x)}</li>`).join("")}</ul><div class="shots">${shots}</div>`
    : `<p class="muted">Running it anyway inside the browser VM…</p>`}`, { id: "evidence", cls: "contain", tag: tape("Containment moment") });
}

function approvalCard(r, latest) {
  const e = r.approval;
  const [cls, label] = r.approved === undefined ? ["warn", REPLAY ? "recorded request" : "waiting for you"] : r.approved ? ["ok", "approved"] : ["bad", "rejected"];
  const buttons = !REPLAY && r.approved === undefined ? `<div class="verify-row"><button type="button" data-approve="0" data-id="${esc(e.approval)}">Reject</button>
    <button type="button" class="approve" data-approve="1" data-id="${esc(e.approval)}">Approve</button></div>` : "";
  return card("Approval", "A human decides", `<p>Step ${esc(e.step)} wants to <b>${esc(e.action?.type)} ${esc(e.action?.target)}</b> on ${esc(hostOf(e.action?.page))}. The policy never runs this on its own.</p>
    ${(e.reasons || []).length ? `<ul class="evidence">${e.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}${bars(e.probs || {})}
    ${e.screenshot ? `<img class="result-img" src="${esc(e.screenshot)}" width="1280" height="800" alt="The page this action would submit">` : ""}${buttons}`,
    { id: latest ? "approval-card" : "", cls: r.approved === false ? "fail" : r.approved ? "good" : "contain", tag: `<span class="pill ${cls}">${label}</span>` });
}

function browserCard(v) {
  const box = v.sandboxes[v.browserBox.id] || v.browserBox;
  const live = box.state !== "destroyed";
  const shot = v.shot;
  const l = box.limits || {};
  const allow = (box.network?.allow || []).map((d) => `<span class="pill acc">${esc(d)}</span>`).join("");
  return card("Browser", `Chromium in microVM ${esc(box.id)}`, `<div class="browser"><div class="browser-bar"><span class="dots"><i></i><i></i><i></i></span>
      <span class="url">${esc(shot?.url || "about:blank")}</span><span class="state ${live ? "run" : "gone"}">${live ? "live" : "VM destroyed"}</span></div>
      ${shot ? `<img src="${esc(shot.screenshot)}" width="1280" height="800" alt="${esc(shot.title || "Browser screenshot")}">` : `<div class="blank">Chromium is starting…</div>`}</div>
    <div class="sbx"><span class="pill">${esc(box.kernel)}</span><span class="pill">${l.cpus} vCPU · ${l.memory_mib} MiB · user ${esc(l.user)}</span>
      <span class="pill">no credentials</span><span class="pill">network: only</span>${allow}</div>`,
    { meta: `${v.shots.length} screenshots · each one hashed into the receipt` });
}

function quotesCard(v) {
  const names = Object.fromEntries(((status.templates || []).find((t) => t.id === "operator")?.vendors || []).map((x) => [x.name, x.vendor]));
  const rows = [];
  for (const id of v.opOrder) {
    const s = v.ops[id];
    if (!s.verify?.passed) continue;
    const f = Object.fromEntries((s.result?.facts || []).map((x) => [x.name.toLowerCase().replace(/_/g, " "), x.value]));
    if ("form" in f || "purchase request" in f) continue;
    const totalKey = Object.keys(f).find((k) => k.includes("total") && !k.includes("sub") && !k.includes("line"));
    const vendor = f.vendor || ((s.allow || []).length === 1 ? names[s.allow[0]] : null);
    if (vendor && totalKey && money(f[totalKey]) !== null) {
      rows.push({ id, vendor, unit: f["unit price"] || "", shipping: f.shipping || "", total: f[totalKey], n: money(f[totalKey]) });
    }
  }
  if (!rows.length) return "";
  const complete = Object.values(names).every((name) => rows.some((r) => r.vendor === name));
  const best = complete ? Math.min(...rows.map((r) => r.n)) : null;
  const unitPrices = rows.some((row) => row.unit);
  return card("Quotes", "Verified cart totals", `<div class="table-scroll" tabindex="0" role="region" aria-label="Vendor quotes"><table class="quotes"><thead><tr><th>Vendor</th>${unitPrices ? "<th>Unit price</th>" : ""}<th>Shipping</th><th>Order total</th><th>Step</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="${r.n === best ? "best" : ""}"><td>${esc(r.vendor)}</td>${unitPrices ? `<td>${esc(r.unit) || "—"}</td>` : ""}<td>${esc(r.shipping) || "—"}</td><td><b>${esc(r.total)}</b></td>
      <td><span class="cite">${esc(r.id)}</span>${r.n === best ? ` <span class="pill ok">cheapest</span>` : ""}</td></tr>`).join("")}</tbody></table></div>
    <p class="muted">${complete ? "Order totals include shipping." : "Waiting for a verified quote from every approved vendor before marking the lowest total."}</p>`, { id: "quotes-card" });
}

function blast(v) {
  if (v.attacks) {
    const list = Object.values(v.attacks);
    const n = (s) => list.filter((a) => a.state === s).length;
    const exposed = v.finished?.blast_radius?.secrets_exposed ?? 0;
    return `<div><b>${n("contained")}</b><span>Attacks contained</span></div>`
      + `<div><b class="${n("escaped") ? "bad" : ""}">${n("escaped")}</b><span>Attacks that got through</span></div>`
      + `<div><b class="${exposed ? "bad" : ""}">${exposed}</b><span>Secrets exposed</span></div>`
      + `<div><b class="${n("error") ? "n" : ""}">${n("error")}</b><span>Tests that could not run</span></div>`;
  }
  const live = {
    secrets_exposed: v.containment.filter((c) => c.layer === "alarm" && /secret/.test(c.detail)).length,
    hosts_reached_outside_allowlist: v.containment.filter((c) => c.layer === "alarm" && !/secret/.test(c.detail)).length,
    unapproved_irreversible_actions: 0,
    egress_blocked: v.containment.filter((c) => c.layer === "network").length,
    quarantined_items: v.containment.filter((c) => c.layer === "quarantine").length,
  };
  const b = v.finished?.blast_radius || v.failed?.blast_radius || live;
  const tile = (n, label, zeroIsGood) => `<div><b class="${zeroIsGood ? (n ? "bad" : "") : (n ? "n" : "")}">${n}</b><span>${label}</span></div>`;
  return tile(b.secrets_exposed, "Secrets exposed", true) + tile(b.hosts_reached_outside_allowlist, "Hosts reached outside the allowlist", true)
    + tile(b.unapproved_irreversible_actions, "Irreversible actions without approval", true)
    + tile((b.egress_blocked || 0) + (b.quarantined_items || 0), "Attempts blocked or quarantined", false);
}

function sandboxPanel(v) {
  const all = Object.values(v.sandboxes);
  if (!all.length) return `<p class="muted">No sandbox yet. Each step gets its own microVM, destroyed when the step ends.</p>`;
  const s = v.sandboxes[v.lastSandbox];
  const gone = all.filter((x) => x.state === "destroyed").length;
  return `<div class="sandbox-card"><span class="state ${s.state === "running" ? "run" : "gone"}">${s.state === "running" ? "running" : "destroyed"}</span>
    <div class="id">${esc(s.id)} <span class="muted">· ${esc(s.purpose)}</span></div>
    <div class="muted">${esc(s.kernel)} · ${s.limits.cpus} vCPU · ${s.limits.memory_mib} MiB · ${s.network.egress === "none" ? "no network"
      : s.purpose === "browser" ? `${(s.network.allow || []).length} vendor sites only` : "ERP API only"}</div>
    <div class="muted">${all.length} microVM${all.length > 1 ? "s" : ""} this mission · ${gone} destroyed</div></div>`;
}

function feed(v) {
  if (!v.containment.length) return `<li class="good"><span class="layer">quiet</span>Nothing has needed containing yet.</li>`;
  return v.containment.slice().reverse().map((c) => `<li class="${c.good ? "good" : c.layer === "alarm" ? "alarm" : ""}">
    <span class="layer">${esc(c.layer)}</span>${esc(c.detail)}</li>`).join("");
}

function costs(v) {
  if (!v.calls.length) return `<p class="muted">Model calls and their cost appear here.</p>`;
  const by = {};
  for (const c of v.calls) {
    const r = (by[c.role] ||= { role: c.role, served: c.served, n: 0, usd: 0, ms: 0 });
    r.n += 1; r.usd += c.usd; r.ms += c.ms;
  }
  const total = v.calls.reduce((s, c) => s + c.usd, 0);
  return `<div class="calls">${Object.values(by).map((r) => `<div><span>${esc(ROLE[r.role] || r.role)} ×${r.n}<br><span class="model">${esc(r.served)}</span></span>
    <span class="mono">${usd(r.usd)}</span></div>`).join("")}</div><div class="total"><span>${v.calls.length} calls</span><span>${usd(total)}</span></div>`;
}

function drawSpark() {
  const c = $("#spark");
  if (!c) return;
  const ctx = c.getContext("2d");
  const w = c.width;
  const h = c.height;
  ctx.clearRect(0, 0, w, h);
  const pts = metrics.slice(-80);
  if (!pts.length) return;
  const line = (vals, color) => {
    const xy = (val, i) => [vals.length === 1 ? w / 2 : (i / (vals.length - 1)) * (w - 8) + 4,
      h - 6 - Math.max(0, Math.min(1, val)) * (h - 12)];
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 3;
    ctx.beginPath();
    vals.forEach((val, i) => {
      const [x, y] = xy(val, i);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
    const [x, y] = xy(vals[vals.length - 1], vals.length - 1);
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fill();
  };
  line(pts.map((p) => p.cpu_percent / 100), "#4FC3B3");
  line(pts.map((p) => p.memory_mib / Math.max(1, p.memory_limit_mib)), "#F2C14E");
}

// ------------------------------------------------------------------ approvals

let pendingApproval = null;
let sendingApproval = false;

function showApproval(e) {
  if (events.some((x) => x.type === "approval.resolved" && x.approval === e.approval)) return;
  pendingApproval = e.approval;
  $("#approval-error").hidden = true;
  const a = e.action || {};
  $("#approval-body").innerHTML = (a.target
    ? `<p>Step ${esc(e.step)} wants to <b>${esc(a.type)} ${esc(a.target)}</b>${a.page ? ` on ${esc(hostOf(a.page))}` : ""}. The policy never runs this on its own.</p>`
    : `<p>Step ${esc(e.step)} wants to do something the policy will not run on its own.</p><pre>${esc(JSON.stringify(a, null, 2))}</pre>`)
    + ((e.reasons || []).length ? `<ul class="evidence">${e.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "")
    + (e.screenshot ? `<img class="result-img" src="${esc(e.screenshot)}" width="1280" height="800" alt="The page this action would submit">` : "")
    + bars(e.probs || {});
  if (!$("#approval").open) $("#approval").showModal();
}

async function answerApproval(approve, id = pendingApproval) {
  if (!id || sendingApproval) return;
  const mission = current;
  sendingApproval = true;
  $("#approve").disabled = $("#reject").disabled = true;
  try {
    await getJSON(`/api/missions/${encodeURIComponent(mission)}/approvals/${encodeURIComponent(id)}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approve }) });
    if (mission === current && pendingApproval === id) {
      pendingApproval = null;
      if ($("#approval").open) $("#approval").close();
    }
  } catch (error) {
    if (mission !== current) return;
    const alert = $("#approval-error");
    alert.textContent = `Could not send your decision. ${error.message}`;
    alert.hidden = false;
    if (!$("#approval").open && pendingApproval === id) $("#approval").showModal();
  } finally {
    sendingApproval = false;
    $("#approve").disabled = $("#reject").disabled = false;
  }
}

boot();
