#!/usr/bin/env python3
"""Run the Red Team against running services and require every attack to be contained.

  .venv/bin/python tests/live_redteam_check.py [--base http://127.0.0.1:8000]
"""
import argparse
import json
import sys
import time

import httpx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    client = httpx.Client(base_url=args.base, timeout=60)
    started = time.time()
    mid = client.post("/api/missions", json={"template": "redteam"}).json()["id"]
    print("red team", mid)
    events = []
    with httpx.stream("GET", f"{args.base}/api/missions/{mid}/events", timeout=900) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                e = json.loads(line[6:])
                events.append(e)
                if e["type"] == "attack.finished":
                    print(f"  {e['outcome'].upper():9} {e['attack']:<10} {e['duration_ms']:>6} ms  " + " | ".join(e["evidence"])[:150])
    failures = []

    def expect(ok, what):
        print(("PASS " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    by = lambda t: [e for e in events if e["type"] == t]  # noqa: E731
    done = by("redteam.finished")
    expect(done and done[0]["contained"] == done[0]["total"] == 12,
           f"{done[0]['contained'] if done else '?'}/12 contained in {time.time() - started:.1f} s")
    expect(len(by("attack.finished")) == 12, "every attack reported")
    expect(by("mission.finished"), "run finished and sealed")
    created = {e["sandbox"]["id"] for e in by("attack.sandbox")}
    expect(len(created) >= 11, f"{len(created)} microVMs used")
    expect(client.get("/api/status").json()["runner"]["running"] == 0, "no sandboxes left running")
    receipt = client.get(f"/api/missions/{mid}/receipt").json()
    verdict = client.post("/api/verify", json=receipt).json()
    expect(verdict["valid"], f"receipt verifies: {verdict['reason']}")
    expect(sum(1 for r in receipt["records"] if r["kind"] == "attack") == 12, "receipt records all 12 attacks")
    if done:
        print(f"host load {done[0]['host_load_start']} → peak {done[0]['host_load_peak']} on {done[0]['cpus']} CPUs")
    print(f"{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
