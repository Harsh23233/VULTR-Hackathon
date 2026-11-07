#!/usr/bin/env python3
"""Glovebox spike: check the containment claims with Microsandbox microVMs before kickoff.

Every check boots its own fresh microVM, tries something an attacker or a buggy agent would
try, and records what actually happened. Nothing here touches Vultr; it runs on an Apple
Silicon Mac (or Linux with KVM). The Python SDK ships with its own runtime.

  python3 -m venv .venv && .venv/bin/pip install "microsandbox==0.7.11"
  .venv/bin/python prep/spikes/microsandbox_spike.py                 # 10 checks, a few minutes
  .venv/bin/python prep/spikes/microsandbox_spike.py --only secrets  # one or more checks
  .venv/bin/python prep/spikes/microsandbox_spike.py --dry-run       # build configs, boot nothing
  .venv/bin/pip install "playwright==1.64.0"                         # only for --browser
  .venv/bin/python prep/spikes/microsandbox_spike.py --browser       # + Chromium inside a microVM

Results: prep/spikes/out/spike_results.json (and browser_example.png with --browser).
Set MSB_HOME to keep images and sandbox state somewhere other than ~/.microsandbox.
"""
import argparse
import asyncio
import importlib.metadata
import json
import platform
import secrets as pysecrets
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import microsandbox as msb
from microsandbox import (Action, Destination, DestGroup, ExecTimeoutError, MicrosandboxError, Network,
                          NetworkPolicy, Protocol, Rlimit, Rule, Sandbox, Secret, SecretSubstitution,
                          ViolationAction)

IMAGE = "python:3.12-slim"
BROWSER_IMAGE = "mcr.microsoft.com/playwright/python:v1.64.0-noble"
OUT = Path(__file__).resolve().parent / "out"
RUN = pysecrets.token_hex(3)

# Runs inside the guest: one HTTP request, one JSON line out. Optional second argument names
# an environment variable whose value is sent as a bearer token.
PROBE = r"""
import json, os, sys, urllib.error, urllib.request
url = sys.argv[1]
env = sys.argv[2] if len(sys.argv) > 2 else ""
headers = {"Authorization": "Bearer " + os.environ.get(env, "")} if env else {}
out = {"url": url}
try:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=6) as r:
        out.update(status=r.status, body=r.read(300).decode("utf-8", "replace"))
except urllib.error.HTTPError as e:
    out.update(status=e.code, body=e.read(300).decode("utf-8", "replace"))
except Exception as e:
    out.update(status=None, error=type(e).__name__ + ": " + str(e)[:200])
print(json.dumps(out))
"""


def deny_all():
    return Network.none()


def egress(*rules, strict=True, ingress=Action.DENY):
    """Deny-by-default egress with DNS to the gateway plus the given allow rules."""
    return Network(policy=NetworkPolicy(default_egress=Action.DENY, default_ingress=ingress,
                                        rules=(*Rule.allow_dns(), *rules)), strict=strict)


def blocked(probe):
    body = (probe.get("body") or "")
    if probe.get("status") == 403 and ("network_policy_denied" in body or "secret_policy_denied" in body):
        return "denied by policy (HTTP 403 " + ("secret_policy_denied" if "secret" in body else "network_policy_denied") + ")"
    if probe.get("status") is None:
        return "connection refused or unresolved (" + (probe.get("error") or "")[:80] + ")"
    return None


async def probe(sb, url, env=""):
    args = ["-c", PROBE, url] + ([env] if env else [])
    out = await sb.exec("python", args, timeout=25)
    try:
        return json.loads(out.stdout_text.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"url": url, "status": None, "error": "no output; exit " + str(out.exit_code) + " " + out.stderr_text[-200:]}


def vm_name(check):
    return f"gbx-spike-{check}-{RUN}"


async def boot(name, **cfg):
    started = time.perf_counter()
    sb = await Sandbox.create(name, image=cfg.pop("image", IMAGE), replace=True, ephemeral=True,
                              max_duration=cfg.pop("max_duration", 300), **cfg)
    return sb, round((time.perf_counter() - started) * 1000)


async def destroy(sb):
    try:
        await sb.destroy(force=True, timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------- checks

async def check_boot(ctx):
    """A task gets its own kernel, not the host's."""
    sb, cold_ms = await boot(vm_name("boot-cold"), cpus=1, memory=512, network=deny_all())
    try:
        out = await sb.exec("python", ["-c", "import platform, socket; print(platform.system(), platform.release(), socket.gethostname())"])
        guest = out.stdout_text.strip()
    finally:
        await destroy(sb)
    sb, warm_ms = await boot(vm_name("boot-warm"), cpus=1, memory=512, network=deny_all())
    await destroy(sb)
    host = f"{platform.system()} {platform.release()}"
    ok = guest.startswith("Linux") and platform.release() not in guest
    ctx["timings"].update(cold_boot_ms=cold_ms, warm_boot_ms=warm_ms)
    return ok, f"guest '{guest}' vs host '{host}'; cold boot {cold_ms} ms (includes image pull), warm boot {warm_ms} ms"


async def check_offline(ctx):
    """A code VM with no network cannot reach anything."""
    sb, _ = await boot(vm_name("offline"), network=deny_all())
    try:
        p = await probe(sb, "https://example.com/")
    finally:
        await destroy(sb)
    why = blocked(p)
    return why is not None, f"https://example.com from an offline VM: {why or p}"


async def check_metadata(ctx):
    """Public egress allowed, yet cloud metadata and private ranges stay unreachable."""
    net = egress(Rule.allow(protocol=Protocol.TCP, destination=Destination.group(DestGroup.PUBLIC)))
    sb, _ = await boot(vm_name("metadata"), network=net)
    try:
        public = await probe(sb, "http://example.com/")
        metadata = await probe(sb, "http://169.254.169.254/v1.json")
        private = await probe(sb, "http://10.0.0.1/")
    finally:
        await destroy(sb)
    ok = public.get("status") == 200 and blocked(metadata) and blocked(private)
    return bool(ok), (f"example.com -> {public.get('status')}; 169.254.169.254 -> {blocked(metadata) or metadata}; "
                      f"10.0.0.1 -> {blocked(private) or private}")


async def check_allowlist(ctx):
    """Only the mission's domains resolve and connect (browser-style: SNI/DNS rules, strict off)."""
    net = egress(Rule.allow(protocol=Protocol.TCP, port=443, destination=Destination.domain("example.com")), strict=False)
    sb, _ = await boot(vm_name("allowlist"), network=net)
    try:
        allowed = await probe(sb, "https://example.com/")
        other = await probe(sb, "https://example.org/")
    finally:
        await destroy(sb)
    ok = allowed.get("status") == 200 and blocked(other)
    return bool(ok), f"example.com -> {allowed.get('status') or allowed.get('error')}; example.org -> {blocked(other) or other}"


async def check_memory(ctx):
    """Asking for 2 GiB inside a 512 MiB VM fails inside the VM."""
    sb, _ = await boot(vm_name("memory"), memory=512, network=deny_all())
    try:
        try:
            out = await sb.exec("python", ["-c", "x = b'\\x01' * (2 * 1024**3); print(len(x))"], timeout=60)
            result = f"exit {out.exit_code}; {out.stderr_text.strip().splitlines()[-1] if out.stderr_text.strip() else 'no stderr'}"
            failed = out.exit_code != 0
        except ExecTimeoutError:
            result, failed = "timed out while thrashing (contained, but slow)", True
        alive = (await sb.exec("echo", ["alive"], timeout=20)).stdout_text.strip() == "alive"
    finally:
        await destroy(sb)
    return failed, f"{result}; VM still answering afterwards: {alive}"


async def check_timeout(ctx):
    """A runaway loop is killed at the exec timeout."""
    sb, _ = await boot(vm_name("timeout"), network=deny_all())
    started = time.perf_counter()
    try:
        try:
            await sb.exec("python", ["-c", "while True: pass"], timeout=5)
            killed = False
        except ExecTimeoutError:
            killed = True
    finally:
        await destroy(sb)
    secs = time.perf_counter() - started
    return killed and secs < 15, f"infinite loop stopped by the 5 s timeout after {secs:.1f} s"


async def check_forkbomb(ctx):
    """A fork bomb run as an unprivileged user hits the process limit; the VM keeps answering."""
    sb, _ = await boot(vm_name("forkbomb"), cpus=1, network=deny_all())
    try:
        try:
            await sb.exec("bash", ["-c", ":(){ :|:& };:"], user="nobody", rlimits=[Rlimit.nproc(64)], timeout=10)
            note = "bomb returned"
        except ExecTimeoutError:
            note = "bomb reaped at the 10 s timeout"
        started = time.perf_counter()
        alive = (await sb.exec("echo", ["alive"], timeout=30)).stdout_text.strip() == "alive"
        reply_ms = round((time.perf_counter() - started) * 1000)
    finally:
        await destroy(sb)
    return alive, f"{note}; VM answered in {reply_ms} ms. Linux skips NPROC for root, so untrusted code must run as a non-root user"


async def check_diskfill(ctx):
    """A runaway write is capped by the file-size limit; the host disk barely moves."""
    home = Path.home()
    before = shutil.disk_usage(home).free
    sb, _ = await boot(vm_name("diskfill"), network=deny_all())
    try:
        out = await sb.exec("dd", ["if=/dev/zero", "of=/tmp/fill", "bs=1M", "count=1024"], user="nobody",
                            rlimits=[Rlimit.fsize(64 * 1024 * 1024)], timeout=60)
        size = (await sb.exec("stat", ["-c", "%s", "/tmp/fill"])).stdout_text.strip()
    finally:
        await destroy(sb)
    after = shutil.disk_usage(home).free
    size_mib = int(size) / 1048576 if size.isdigit() else -1
    ok = out.exit_code != 0 and 0 <= size_mib <= 64
    return ok, f"dd exit {out.exit_code}; file stopped at {size_mib:.0f} MiB; host free space change {(before - after) / 1048576:+.0f} MiB"


async def check_secrets(ctx):
    """The VM only sees a placeholder. The real value is swapped in outside the VM for the one
    allowed host; sending the placeholder anywhere else is blocked and the VM is terminated."""
    real = "gbx-demo-" + pysecrets.token_hex(12)

    class Verify(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("Authorization") == f"Bearer {real}"
            self.send_response(200 if ok else 401)
            self.end_headers()
            self.wfile.write(b'{"accepted": true}' if ok else b'{"accepted": false}')  # never echoes the token

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Verify)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    net = egress(Rule.allow(protocol=Protocol.TCP, port=port, destination=Destination.group(DestGroup.HOST)),
                 Rule.allow(protocol=Protocol.TCP, port=80, destination=Destination.domain("example.com")))
    # Python SDK 0.7.11 rejects a per-secret violation_action (serialised as a str the native
    # layer refuses), so terminate-on-violation is set sandbox-wide instead.
    token = Secret.env("DEMO_TOKEN", value=real, allow=["host.microsandbox.internal"], require_tls_identity=False,
                       substitution=SecretSubstitution(header_fields=("Authorization",)))
    name = vm_name("secrets")
    sb, _ = await boot(name, network=net, secrets=[token],
                       secret_violation_action=ViolationAction.BLOCK_AND_TERMINATE)
    try:
        seen = (await sb.exec("printenv", ["DEMO_TOKEN"])).stdout_text.strip()
        use = await probe(sb, f"http://host.microsandbox.internal:{port}/check", "DEMO_TOKEN")
        started = time.perf_counter()
        try:
            leak = blocked(await probe(sb, "http://example.com/?collect=1", "DEMO_TOKEN")) or "NOT blocked"
        except MicrosandboxError as e:  # terminate-on-violation kills the VM while the request is in flight
            leak = f"VM killed mid-request ({type(e).__name__})"
        leak_ms = round((time.perf_counter() - started) * 1000)
        await asyncio.sleep(1)
        try:
            after = await sb.exec("echo", ["still running"], timeout=10)
            terminated = after.stdout_text.strip() != "still running"
        except Exception as e:
            terminated = True
            ctx["notes"].append(f"secrets: exec after the violation raised {type(e).__name__}. An ephemeral VM is "
                                "removed as soon as it is terminated, so stream SYSTEM logs while it runs to keep evidence.")
    finally:
        await destroy(sb)
        server.shutdown()
    placeholder_only = bool(seen) and real not in seen
    ok = placeholder_only and use.get("status") == 200 and leak != "NOT blocked" and terminated
    return ok, (f"guest sees '{seen}'; allowed host received the real value: {use.get('status') == 200}; "
                f"leak to example.com -> {leak} after {leak_ms} ms; VM terminated: {terminated}")


async def check_lifecycle(ctx):
    """Nothing written in one task survives into the next."""
    sb, _ = await boot(vm_name("life-a"), network=deny_all())
    try:
        await sb.exec("sh", ["-c", "echo implant > /tmp/implant && ls -l /tmp/implant"])
    finally:
        await destroy(sb)
    sb, _ = await boot(vm_name("life-b"), network=deny_all())
    try:
        out = await sb.exec("sh", ["-c", "test -e /tmp/implant && echo present || echo absent"])
    finally:
        await destroy(sb)
    state = out.stdout_text.strip()
    return state == "absent", f"/tmp/implant in the next task's VM: {state}"


async def check_browser(ctx):
    """Chromium runs inside the microVM; the host only drives it. Off-list domains fail."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "skipped: pip install playwright==1.64.0 (no local browsers needed)"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        host_port = s.getsockname()[1]
    net = egress(Rule.allow(protocol=Protocol.TCP, port=443, destination=Destination.domain("example.com")),
                 strict=False, ingress=Action.ALLOW)
    sb, boot_ms = await boot(vm_name("browser"), image=BROWSER_IMAGE, cpus=2, memory=2048, network=net,
                             ports={host_port: 3000}, max_duration=600)
    try:
        await sb.exec_detached("playwright", ["run-server", "--port", "3000", "--host", "0.0.0.0"])
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", host_port), timeout=1).close()
                break
            except OSError:
                await asyncio.sleep(1)
        launch = json.dumps({"args": ["--disable-dev-shm-usage"]})
        async with async_playwright() as p:
            browser = await p.chromium.connect(f"ws://127.0.0.1:{host_port}/", timeout=30000,
                                               headers={"x-playwright-launch-options": launch})
            page = await browser.new_page()
            await page.goto("https://example.com/", timeout=30000)
            title = await page.title()
            OUT.mkdir(exist_ok=True)
            await page.screenshot(path=str(OUT / "browser_example.png"))
            try:
                await page.goto("https://example.org/", timeout=15000)
                other = "loaded (NOT blocked)"
            except Exception as e:
                other = "blocked: " + str(e).splitlines()[0][:90]
            await browser.close()
    finally:
        await destroy(sb)
    ok = title == "Example Domain" and other.startswith("blocked")
    return ok, f"boot {boot_ms} ms; example.com title '{title}' (screenshot saved); example.org {other}"


CHECKS = {
    "boot": check_boot, "offline": check_offline, "metadata": check_metadata, "allowlist": check_allowlist,
    "memory": check_memory, "timeout": check_timeout, "forkbomb": check_forkbomb, "diskfill": check_diskfill,
    "secrets": check_secrets, "lifecycle": check_lifecycle, "browser": check_browser,
}


def dry_run():
    """Build every network policy and secret the checks use, without booting anything."""
    egress(Rule.allow(protocol=Protocol.TCP, destination=Destination.group(DestGroup.PUBLIC)))
    egress(Rule.allow(protocol=Protocol.TCP, port=443, destination=Destination.domain("example.com")), strict=False)
    egress(Rule.allow(protocol=Protocol.TCP, port=8080, destination=Destination.group(DestGroup.HOST)))
    Secret.env("DEMO_TOKEN", value="x", allow=["host.microsandbox.internal"], require_tls_identity=False,
               substitution=SecretSubstitution(header_fields=("Authorization",)))
    Rlimit.nproc(64), Rlimit.fsize(64 * 1024 * 1024)
    print(f"dry run OK: microsandbox {importlib.metadata.version('microsandbox')}, "
          f"runtime installed: {msb.is_runtime_installed()}, {len(CHECKS)} checks defined")


async def main():
    ap = argparse.ArgumentParser(description="Glovebox Microsandbox spike")
    ap.add_argument("--only", help="comma-separated checks: " + ",".join(CHECKS))
    ap.add_argument("--browser", action="store_true", help="also run the Chromium-in-microVM check")
    ap.add_argument("--dry-run", action="store_true", help="build configs only; boot nothing")
    ap.add_argument("--install-runtime", action="store_true", help="download msb + libkrunfw if missing")
    args = ap.parse_args()

    if args.dry_run:
        dry_run()
        return 0
    if not msb.is_runtime_installed():
        if not args.install_runtime:
            raise SystemExit("Microsandbox runtime not found. Re-run with --install-runtime, or install from https://docs.microsandbox.dev")
        await msb.ensure_runtime()

    names = args.only.split(",") if args.only else [c for c in CHECKS if c != "browser" or args.browser]
    ctx = {"timings": {}, "notes": []}
    results = []
    for name in names:
        print(f"-> {name}: {CHECKS[name].__doc__.splitlines()[0]}", flush=True)
        started = time.perf_counter()
        try:
            ok, evidence = await CHECKS[name](ctx)
            result = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        except Exception as e:  # report and keep going; a spike is for finding out
            result, evidence = "ERROR", f"{type(e).__name__}: {str(e)[:300]}"
        ms = round((time.perf_counter() - started) * 1000)
        results.append({"check": name, "result": result, "evidence": evidence, "ms": ms})
        print(f"   {result} ({ms} ms) {evidence}", flush=True)

    OUT.mkdir(exist_ok=True)
    report = {
        "run": RUN,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "microsandbox": importlib.metadata.version("microsandbox"),
        "image": IMAGE,
        "results": results,
        "timings": ctx["timings"],
        "notes": ctx["notes"],
    }
    (OUT / "spike_results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    passed = sum(r["result"] == "PASS" for r in results)
    print(f"\n{passed}/{len(results)} passed. Report: {OUT / 'spike_results.json'}")
    return 0 if all(r["result"] in ("PASS", "SKIP") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
