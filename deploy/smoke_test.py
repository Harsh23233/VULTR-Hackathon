#!/usr/bin/env python3
"""Smoke-test deploy/remote/setup.sh in a fresh Ubuntu 24.04 microVM, before touching Vultr.

  MSB_HOME=$PWD/.msb .venv/bin/python deploy/smoke_test.py

Runs the setup for both roles with GLOVEBOX_SETUP_DRY=1 (no systemd, ufw or /dev/kvm in a VM),
then starts the control plane in scripted mode inside the VM and checks its API and the Caddyfile.
Needs internet access for apt and pip. Takes a few minutes.
"""
import asyncio
import sys
import time
from pathlib import Path

from microsandbox import Sandbox, Volume

ROOT = Path(__file__).resolve().parents[1]
EXCLUDES = ["./.venv", "./.msb", "./.pydeps", "./data", "./.env", "./.git", "./deploy/state.json",
            "./deploy/secrets.env", "./deploy/.known_hosts"]

# Throwaway values for this smoke test only; a real deployment generates its own (deploy/vultr.py).
RUNNER_ENV = """RUNNER_TOKEN=smoke-test-token-0123456789
RUNNER_BIND=127.0.0.1
RUNNER_PORT=8081
MAX_SANDBOXES=2
MSB_HOME=/var/lib/glovebox/msb
HOME=/var/lib/glovebox
VPC_SUBNET=10.73.10.0/24
"""

CONTROL_ENV = """GLOVEBOX_INFERENCE=scripted
RUNNER_URL=http://127.0.0.1:8081
RUNNER_TOKEN=smoke-test-token-0123456789
ERP_BIND=127.0.0.1:8090
ERP_GUEST_HOST=127.0.0.1
ERP_GUEST_PORT=8090
ERP_SCHEME=http
VENDOR_BIND=127.0.0.1
VENDOR_GUEST_HOST=127.0.0.1
RECEIPT_SIGNING_KEY=AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=
PUBLIC_BASE_URL=https://glovebox.203-0-113-10.sslip.io
GLOVEBOX_HOST=glovebox.203-0-113-10.sslip.io
DATA_DIR=/var/lib/glovebox/data
HOME=/var/lib/glovebox
VPC_SUBNET=10.73.10.0/24
"""


async def run(sb, label, script, timeout=900):
    started = time.perf_counter()
    out = await sb.exec("bash", ["-c", script], timeout=timeout)
    secs = time.perf_counter() - started
    text = (out.stdout_bytes + out.stderr_bytes).decode("utf-8", "replace")
    ok = out.exit_code == 0
    print(f"{'PASS' if ok else 'FAIL'} {label} ({secs:.0f} s, exit {out.exit_code})")
    tail = [line for line in text.strip().splitlines() if line.strip()][-12:]
    print("    " + "\n    ".join(tail))
    return ok, text


async def main():
    print("booting ubuntu:24.04 (2 vCPU, 2 GiB, public network)")
    excludes = " ".join(f"--exclude={e}" for e in EXCLUDES)
    async with await Sandbox.create("gbx-deploy-smoke", image="ubuntu:24.04", cpus=2, memory=2048, replace=True,
                                    max_duration=1800, volumes={"/src": Volume.bind(str(ROOT), readonly=True)}) as sb:
        results = []
        ok, _ = await run(sb, "copy code like rsync would", f"mkdir -p /opt/glovebox/app /etc/glovebox && "
                          f"tar -C /src {excludes} -cf - . | tar -C /opt/glovebox/app -xf - && ls /opt/glovebox/app")
        results.append(ok)
        await sb.fs.write("/etc/glovebox/runner.env", RUNNER_ENV.encode())
        await sb.fs.write("/etc/glovebox/orchestrator.env", CONTROL_ENV.encode())
        ok, _ = await run(sb, "setup.sh sandbox", "GLOVEBOX_SETUP_DRY=1 bash /opt/glovebox/app/deploy/remote/setup.sh sandbox")
        results.append(ok)
        ok, _ = await run(sb, "setup.sh control", "GLOVEBOX_SETUP_DRY=1 bash /opt/glovebox/app/deploy/remote/setup.sh control")
        results.append(ok)
        ok, text = await run(sb, "setup.sh sandbox again, as on a second push (idempotent, packages kept)",
                             "GLOVEBOX_SETUP_DRY=1 bash /opt/glovebox/app/deploy/remote/setup.sh sandbox")
        results.append(ok and "already has" in text)
        ok, _ = await run(sb, "service user still owns the in-VM packages",
                          "test \"$(stat -c %U /opt/glovebox/app/.pydeps/*/pandas)\" = glovebox && stat -c '%U %n' /opt/glovebox/app/.pydeps/*/pandas")
        results.append(ok)
        ok, _ = await run(sb, "Caddyfile is valid", "caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1 | tail -3")
        results.append(ok)
        ok, _ = await run(sb, "control plane starts as the service user and answers", r"""
            set -a; . /etc/glovebox/orchestrator.env; set +a
            cd /opt/glovebox/app
            runuser -u glovebox -- env $(grep -v '^#' /etc/glovebox/orchestrator.env | xargs) \
              /opt/glovebox/venv/bin/python -m uvicorn apps.orchestrator.main:app --host 127.0.0.1 --port 8000 > /tmp/orch.log 2>&1 &
            for i in $(seq 1 40); do curl -fsS http://127.0.0.1:8000/api/status > /tmp/status.json 2>/dev/null && break; sleep 0.5; done
            python3 -c "import json; s = json.load(open('/tmp/status.json')); print('inference', s['inference'], '| runner', s['runner'].get('ok'), s['runner'].get('error', '')[:60])"
            curl -fsS http://127.0.0.1:8000/ | grep -o '<title>[^<]*</title>'
            curl -s -o /dev/null -w 'erp without token -> %{http_code}\n' http://127.0.0.1:8090/vendors
            curl -fsS http://127.0.0.1:8103/ | grep -o '<title>[^<]*</title>'
            """, timeout=120)
        results.append(ok)
        ok, _ = await run(sb, "guest packages fetched for this CPU", "ls /opt/glovebox/app/.pydeps/ && "
                          "ls /opt/glovebox/app/.pydeps/*/ | grep -E '^(pandas|numpy|matplotlib|openpyxl|playwright)$'")
        results.append(ok)
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
