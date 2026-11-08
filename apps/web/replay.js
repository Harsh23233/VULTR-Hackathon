"use strict";
// Static replay of recorded missions (docs/replay, built by scripts/export_replay.py). Loaded before app.js;
// app.js hands its API calls and event stream to these functions when window.GLOVEBOX_REPLAY is set.
// Receipts are checked here in the browser: the same hash chain and Ed25519 signature as receipts.py.

window.GLOVEBOX_REPLAY = true;
const REPLAY_SPEED = 1.5;
const GENESIS = "0".repeat(64);

async function readReplay(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Recording could not be loaded (${response.status}).`);
  return response.json();
}

async function replayGet(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  if (url === "/api/status") return readReplay("data/status.json");
  if (url === "/api/missions" && method === "GET") return readReplay("data/missions.json");
  if (url === "/api/missions" && method === "POST") {
    const want = JSON.parse(opts.body || "{}").template || "analyst";
    const list = await readReplay("data/missions.json");
    const found = list.find((m) => m.template === want);
    if (!found) throw new Error("This replay has no recorded run of that mission.");
    return { id: found.id };
  }
  if (url === "/api/verify") {
    const archive = await readReplay("data/status.json");
    return verifyReceipt(JSON.parse(opts.body), archive.replay_signing_keys || [archive.signing_key?.public_key]);
  }
  return readReplay(url);
}

// Stands in for EventSource: plays the recorded events back with their original spacing, sped up and capped.
function replaySource(id, play) {
  const src = { onmessage: null, onerror: null, closed: false, close() { this.closed = true; }, finish() { play = false; } };
  readReplay(`data/${encodeURIComponent(id)}/events.json`).then(async (list) => {
    let prev = null;
    for (const e of list) {
      if (src.closed) return;
      const gap = play && prev ? Math.min(Math.max(Date.parse(e.ts) - Date.parse(prev.ts), 0), 700) / REPLAY_SPEED : 0;
      prev = e;
      if (gap) await new Promise((done) => setTimeout(done, gap));
      if (!src.closed && src.onmessage) src.onmessage({ data: JSON.stringify(e) });
    }
  }).catch(() => { if (!src.closed) src.onerror?.(); });
  return src;
}

function replayChips(status) {
  return [`<span class="chip"><span class="dot ok"></span>Replay <b>${status.inference === "vultr" ? "recorded on Vultr" : "recorded test runs"}</b></span>`,
    `<span class="chip">Receipts <b>verified in this browser</b></span>`].join("");
}

function replayBanner(status) {
  const banner = $("#testmode");
  banner.innerHTML = `<b>Read-only replay.</b> ${status.inference === "vultr" ? "The latest recordings ran on Vultr. Earlier test runs are labeled Scripted."
    : "These test runs used real microVMs with scripted model responses."}
    <a href="https://glovebox.149-28-8-192.sslip.io/">Run a new mission on Vultr ↗</a>`;
  banner.hidden = false;
  $("#goal").readOnly = true;
  for (const input of document.querySelectorAll(".toggle")) input.hidden = true;
  $("#launcher-label").textContent = "Play a recording";
  $("#redteam").textContent = "Play Red Team";
  $("#history-label").textContent = "Recorded missions";
  $("#project-link").href = "../";
  $("#project-link").textContent = "Project home";
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

async function verifyReceipt(receipt, trustedKeys) {
  try {
    if (!trustedKeys.includes(receipt.signature?.public_key)) return { valid: false, reason: "the signer is outside this archive" };
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
