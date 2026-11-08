"use strict";
// Static replay of recorded missions (docs/replay, built by scripts/export_replay.py). Loaded before app.js;
// app.js hands its API calls and event stream to these functions when window.GLOVEBOX_REPLAY is set.
// Receipts are checked here in the browser: the same hash chain and Ed25519 signature as receipts.py.

window.GLOVEBOX_REPLAY = true;
const REPLAY_SPEED = 1.5;
const GENESIS = "0".repeat(64);

async function replayGet(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  if (url === "/api/status") return (await fetch("data/status.json")).json();
  if (url === "/api/missions" && method === "GET") return (await fetch("data/missions.json")).json();
  if (url === "/api/missions" && method === "POST") {
    const want = JSON.parse(opts.body || "{}").template || "analyst";
    const list = await (await fetch("data/missions.json")).json();
    const found = list.find((m) => m.template === want);
    if (!found) throw new Error("This replay has no recorded run of that mission.");
    return { id: found.id };
  }
  if (url === "/api/verify") return verifyReceipt(JSON.parse(opts.body));
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

// Stands in for EventSource: plays the recorded events back with their original spacing, sped up and capped.
function replaySource(id) {
  const src = { onmessage: null, onerror: null, closed: false, close() { this.closed = true; } };
  fetch(`data/${encodeURIComponent(id)}/events.json`).then((r) => r.json()).then(async (list) => {
    let prev = null;
    for (const e of list) {
      if (src.closed) return;
      const gap = prev ? Math.min(Math.max(Date.parse(e.ts) - Date.parse(prev.ts), 0), 700) / REPLAY_SPEED : 0;
      prev = e;
      if (gap) await new Promise((done) => setTimeout(done, gap));
      if (!src.closed && src.onmessage) src.onmessage({ data: JSON.stringify(e) });
    }
  }).catch(() => src.onerror && src.onerror());
  return src;
}

function replayChips(status) {
  return [`<span class="chip"><span class="dot"></span>Replay <b>recorded runs</b></span>`,
    `<span class="chip"><span class="dot ok"></span>Real microVMs <b>${esc(status.runner?.guest_arch || "")}</b></span>`,
    `<span class="chip">Receipts <b>Ed25519 · ${esc(status.signing_key?.key_id)}</b> · checked in your browser</span>`].join("");
}

function replayBanner() {
  const banner = $("#testmode");
  banner.innerHTML = `<b>Replay.</b> These missions were recorded on real Microsandbox microVMs, with scripted AI answers:
    our Vultr credits ran out before we could deploy. Every receipt is checked in your browser.
    Code: <a href="https://github.com/Harsh23233/VULTR-Hackathon">github.com/Harsh23233/VULTR-Hackathon</a>`;
  banner.hidden = false;
  $("#run").textContent = "Play the recorded run";
  $("#redteam").textContent = "Replay the Red Team";
}

// ---------------------------------------------------------------- receipts, as receipts.py checks them

function pyNumber(x) {  // Python's float repr, for the values a receipt can hold
  if (Number.isInteger(x)) return String(x);
  const a = Math.abs(x);
  if (a < 1e-4 || a >= 1e16) {
    const [mantissa, exp] = x.toExponential().split("e");
    return `${mantissa}e${exp[0] === "-" ? "-" : "+"}${exp.replace(/^[+-]/, "").padStart(2, "0")}`;
  }
  return String(x);
}

function canonical(v) {  // json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
  if (v === null || typeof v !== "object") return typeof v === "number" ? pyNumber(v) : JSON.stringify(v);
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
}

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return new Uint8Array(digest);
}

const toHex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
const fromB64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

async function verifyReceipt(receipt) {
  try {
    let prev = GENESIS;
    for (const record of receipt.records) {
      const { hash, ...body } = record;
      if (record.prev_hash !== prev) return { valid: false, reason: `record ${record.seq} does not link to the record before it` };
      if (toHex(await sha256(canonical(body))) !== hash) return { valid: false, reason: `record ${record.seq} was changed after it was written` };
      prev = hash;
    }
    if (receipt.chain_head !== prev) return { valid: false, reason: "chain head does not match the last record" };
    const { signature: sig, ...body } = receipt;
    const key = await crypto.subtle.importKey("raw", fromB64(sig.public_key), { name: "Ed25519" }, false, ["verify"]);
    const ok = await crypto.subtle.verify({ name: "Ed25519" }, key, fromB64(sig.value), await sha256(canonical(body)));
    if (!ok) return { valid: false, reason: "signature does not match the receipt's contents" };
    return { valid: true, reason: `${receipt.records.length} records intact; signature valid (key ${sig.key_id}), checked in your browser` };
  } catch (e) {
    return { valid: false, reason: `could not check it in this browser (${e.message || e})` };
  }
}
