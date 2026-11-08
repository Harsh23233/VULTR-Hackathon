#!/usr/bin/env python3
"""End-to-end check of a full Analyst mission against running services (runner + orchestrator).

  .venv/bin/python tests/live_mission_check.py [--no-attack] [--base http://127.0.0.1:8000]

Asserts every claim the demo makes: quarantine, plan, gate, self-heal, real chart, blocked egress,
placeholder-only credential, verification, answer content, and a receipt that verifies and fails
when tampered with.
"""
import argparse
import copy
import json
import sys
import time

import httpx

MUST_FLAG = ["PMT-26Q3-0131", "PMT-26Q3-0179", "PMT-26Q3-0333"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--no-attack", action="store_true")
    args = ap.parse_args()
    client = httpx.Client(base_url=args.base, timeout=60)
    failures = []

    def expect(ok, what):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    status = client.get("/api/status").json()
    scripted = status["inference"] == "scripted"
    print("inference:", status["inference"], "| runner:", {k: status["runner"].get(k) for k in ("ok", "guest_arch", "running")})
    started = time.time()
    created = client.post("/api/missions", json={"simulate_compromise": not args.no_attack}).json()
    mid = created["id"]
    print("mission", mid)
    events = []
    with httpx.stream("GET", f"{args.base}/api/missions/{mid}/events", timeout=600) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                e = json.loads(line[6:])
                events.append(e)
                if e["type"] not in ("metrics.sample", "model.call"):
                    print(f"  {e['seq']:>3} {e['type']:<18} " + json.dumps({k: v for k, v in e.items()
                          if k not in ("mission", "seq", "ts", "type", "code", "stdout", "plan", "sandbox", "answers")})[:150])
    secs = time.time() - started
    by = lambda t: [e for e in events if e["type"] == t]  # noqa: E731
    contain = by("containment")
    expect(by("mission.finished"), f"mission finished in {secs:.1f} s")
    expect(any(c["layer"] == "quarantine" and "PMT-26Q3-0215" in c["detail"] for c in contain),
           "injected memo PMT-26Q3-0215 quarantined before any model saw it")
    expect(by("plan.created") and by("gate.scored") and by("gate.scored")[0]["decision"] == "auto", "plan made; gate allowed the code step")
    gens = by("code.generated")
    steps = [e for e in by("exec.finished") if e["purpose"] == "step"]
    if scripted:
        expect(len(gens) >= 2 and by("selfheal"), f"self-heal: {len(gens)} code attempts")
        expect(steps and steps[0]["exit_code"] != 0 and "doesn't match format" in steps[0]["stderr"],
               "first attempt failed on the date format")
    expect(steps and steps[-1]["exit_code"] == 0, "final attempt succeeded")
    expect(by("artifact.saved"), "chart saved")
    png = client.get(by("artifact.saved")[0]["url"]).content if by("artifact.saved") else b""
    expect(png[:8] == b"\x89PNG\r\n\x1a\n", f"chart downloads ({len(png)} bytes)")
    expect(by("secret.used"), "ERP API received the real token while the VM held a placeholder")
    if not args.no_attack:
        attack = [e for e in by("exec.finished") if e["purpose"] == "attack"]
        expect(attack and "$MSB_ERP_TOKEN" in attack[0]["stdout"], "simulated attacker only saw the placeholder")
        hosts = {c.get("host") for c in contain if c["layer"] == "network"}
        expect({"169.254.169.254", "exfil.attacker.example"} <= hosts, f"metadata and exfiltration blocked {sorted(h for h in hosts if h)}")
    expect(by("verify.result") and by("verify.result")[0]["passed"], "verifier passed the result")
    answer = by("mission.answer")[0]["text"] if by("mission.answer") else ""
    expect(all(p in answer for p in MUST_FLAG), f"answer names {MUST_FLAG}")
    expect(any(f"[{e['step']}]" in answer for e in steps) and "Contained:" in answer,
           "answer cites an executed step and includes the containment summary")
    blast = by("mission.finished")[0]["blast_radius"] if by("mission.finished") else {}
    expect(blast.get("secrets_exposed") == 0 and blast.get("hosts_reached_outside_allowlist") == 0
           and blast.get("unapproved_irreversible_actions") == 0, f"blast radius zero: {blast}")
    expect(sum(1 for e in events if e["type"] == "metrics.sample") > 0, "live sandbox metrics streamed")

    receipt = client.get(f"/api/missions/{mid}/receipt").json()
    good = client.post("/api/verify", json=receipt).json()
    expect(good["valid"], f"receipt verifies: {good['reason']}")
    tampered = copy.deepcopy(receipt)
    rec = next((r for r in tampered["records"] if r["kind"] == "exec"), tampered["records"][0])
    rec["kind"] += "_edited"
    bad = client.post("/api/verify", json=tampered).json()
    expect(not bad["valid"], f"tampered receipt rejected: {bad['reason']}")
    forged = copy.deepcopy(receipt)
    forged["blast_radius"]["secrets_exposed"] = 0
    forged["cost_usd"] = 0.0 if receipt["cost_usd"] else 1.0
    bad2 = client.post("/api/verify", json=forged).json()
    expect(not bad2["valid"], f"edited summary rejected: {bad2['reason']}")
    sandboxes = [e for e in by("sandbox.created")]
    expect(len(sandboxes) == len(by("sandbox.destroyed")), f"every sandbox destroyed ({len(sandboxes)} created)")
    print(f"\nmodel calls: {len(by('model.call'))}, cost ${receipt['cost_usd']}, records {len(receipt['records'])}")
    print(f"{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
