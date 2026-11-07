"""Microsandbox driver: one fresh microVM per task, hardened and offline unless told otherwise."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import platform
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from microsandbox import (Action, Destination, DestGroup, ExecTimeoutError, LogReadSource, MicrosandboxError,
                          Network, NetworkPolicy, Protocol, Rlimit, Rule, Sandbox, Secret, SecretSubstitution,
                          SecurityProfile, ViolationAction, Volume)

log = logging.getLogger("glovebox.runner")

ROOT = Path(__file__).resolve().parents[2]
GUEST_DIR = Path(__file__).resolve().parent / "guest"
NETLOG = "/tmp/.gbx_net.jsonl"
MAX_TEXT = 64_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
INTERESTING = re.compile(r"violation|denied|blocked|terminat|oom|killed", re.I)
BROWSER_IMAGE = "mcr.microsoft.com/playwright/python:v1.64.0-noble"  # Chromium baked in; Playwright comes from .pydeps


@dataclass(frozen=True)
class Profile:
    name: str
    image: str = "python:3.12-slim"
    cpus: int = 1
    memory_mib: int = 512
    exec_timeout_s: int = 60
    max_lifetime_s: int = 600
    user: str = "nobody"
    out_mib: int = 32
    tmp_mib: int = 64
    nproc: int = 64
    fsize_mib: int = 64
    pydeps: str = ""  # suffix of the package folder mounted at /opt/pydeps
    browser: bool = False


PROFILES = {
    "code": Profile("code"),
    "redteam": Profile("redteam", exec_timeout_s=30, max_lifetime_s=120),
    # One headless Chromium per Operator mission. nproc counts threads, and Chromium runs a few hundred.
    "browser": Profile("browser", image=BROWSER_IMAGE, cpus=2, memory_mib=2048, exec_timeout_s=45, max_lifetime_s=1200,
                       user="pwuser", out_mib=64, tmp_mib=512, nproc=1024, fsize_mib=128, pydeps="-browser", browser=True),
}


@dataclass
class NetworkSpec:
    mode: str = "none"  # "none", "api" (one API host) or "browse" (a browser's site allowlist)
    host: str | None = None  # host.microsandbox.internal, an IP, or a domain
    port: int | None = None
    scheme: str = "http"
    decoy_host: str | None = None  # red team only: a reachable host that no secret is bound to
    decoy_port: int | None = None
    sites: list[dict] = field(default_factory=list)  # browse: [{"name", "host", "port"}]

    def describe(self) -> dict:
        if self.mode == "none":
            return {"egress": "none"}
        if self.mode == "browse":
            return {"egress": "allowlist", "allow": [s["name"] for s in self.sites],
                    "served_at": [f"{s['host']}:{s['port']}" for s in self.sites],
                    "always_denied": ["every other site, public ones included", "metadata 169.254.169.254",
                                      "private ranges", "the control plane and runner"]}
        allow = [f"{self.scheme}://{self.host}:{self.port}"]
        if self.decoy_host:
            allow.append(f"decoy {self.decoy_host}:{self.decoy_port} (no secret bound)")
        return {"egress": "allowlist", "allow": allow,
                "always_denied": ["metadata 169.254.169.254", "private ranges", "everything else"]}


@dataclass
class SecretSpec:
    env: str
    value: str
    allow_host: str


@dataclass
class Box:
    sid: str
    name: str
    sandbox: Sandbox
    profile: Profile
    network: NetworkSpec
    secret_values: list[str]
    created: float
    kernel: str = ""
    status: str = "running"
    log_since_ms: float = 0.0
    outputs: dict[str, bytes] = field(default_factory=dict)


def guest_arch() -> str:
    return "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _destination(host: str):
    if host == "host.microsandbox.internal":
        return Destination.group(DestGroup.HOST)
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return Destination.ip(host)
    return Destination.domain(host)


def host_rules(spec: NetworkSpec) -> str:
    """Chromium's name mapping for a browse sandbox: vendor name -> where it is served."""
    return ", ".join(f"MAP {s['name']} {s['host']}:{s['port']}" for s in spec.sites)


def build_network(spec: NetworkSpec) -> Network:
    if spec.mode == "none":
        return Network.none()
    if spec.mode == "browse":
        targets = sorted({(s["host"], s["port"]) for s in spec.sites})
        rules = [*Rule.allow_dns(), *[Rule.allow(protocol=Protocol.TCP, port=port, destination=_destination(host))
                                      for host, port in targets]]
        return Network(policy=NetworkPolicy(default_egress=Action.DENY, default_ingress=Action.DENY, rules=tuple(rules)))
    rules = [*Rule.allow_dns(), Rule.allow(protocol=Protocol.TCP, port=spec.port, destination=_destination(spec.host))]
    if spec.decoy_host:
        rules.append(Rule.allow(protocol=Protocol.TCP, port=spec.decoy_port, destination=_destination(spec.decoy_host)))
    return Network(policy=NetworkPolicy(default_egress=Action.DENY, default_ingress=Action.DENY, rules=tuple(rules)))


class SandboxPool:
    def __init__(self, pydeps_dir: Path, max_boxes: int = 4):
        self.pydeps = pydeps_dir
        self.max_boxes = max_boxes
        self.boxes: dict[str, Box] = {}
        self.created_total = 0

    # ------------------------------------------------------------- lifecycle

    async def create(self, profile_name: str, mission_id: str, step_id: str, network: NetworkSpec,
                     secret_specs: list[SecretSpec], env: dict[str, str], files: dict[str, bytes]) -> dict:
        if len(self.boxes) >= self.max_boxes:
            raise RuntimeError(f"sandbox limit reached ({self.max_boxes} running)")
        profile = PROFILES[profile_name]
        sid = f"sbx-{profile.name}-{secrets.token_hex(4)}"
        name = f"gbx-{mission_id[:12]}-{step_id}-{sid[-8:]}"
        entries = [Secret.env(s.env, value=s.value, allow=[s.allow_host], require_tls_identity=network.scheme == "https",
                              substitution=SecretSubstitution(header_fields=("Authorization",))) for s in secret_specs]
        guest_env = {"PYTHONPATH": "/opt/glovebox:/opt/pydeps", "MPLBACKEND": "Agg", "MPLCONFIGDIR": "/tmp/mpl",
                     "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", **env}
        if profile.browser:  # no audit hook here: Chromium's own requests are reported by browser_step.py
            guest_env.update(PYTHONPATH="/opt/pydeps", PLAYWRIGHT_BROWSERS_PATH="/ms-playwright",
                             GBX_HOST_RULES=host_rules(network))
        started = time.perf_counter()
        sandbox = await Sandbox.create(
            name, image=profile.image, cpus=profile.cpus, memory=profile.memory_mib,
            max_duration=profile.max_lifetime_s, replace=True, ephemeral=False,
            security=SecurityProfile.RESTRICTED, env=guest_env,
            network=build_network(network),
            secrets=entries,
            secret_violation_action=ViolationAction.BLOCK_AND_TERMINATE,  # sandbox-wide; per-secret fails in SDK 0.7.11
            intercept_tls=bool(entries) and network.scheme == "https",
            labels={"glovebox": "1", "mission": mission_id[:60], "profile": profile.name},
            volumes={
                "/opt/pydeps": Volume.bind(f"{self.pydeps}{profile.pydeps}", readonly=True, nosuid=True, nodev=True),
                "/opt/glovebox": Volume.bind(str(GUEST_DIR), readonly=True, nosuid=True, nodev=True),
                "/work/out": Volume.tmpfs(size_mib=profile.out_mib),
                "/tmp": Volume.tmpfs(size_mib=profile.tmp_mib),
            },
        )
        boot_ms = round((time.perf_counter() - started) * 1000)
        box = Box(sid, name, sandbox, profile, network, [s.value for s in secret_specs], time.time())
        self.boxes[sid] = box
        self.created_total += 1
        try:
            setup = await sandbox.exec("sh", ["-c", "mkdir -p /work/in /tmp/mpl && chmod 1777 /work/out /tmp /tmp/mpl "
                                                   "&& chmod 755 /work/in && uname -sr"])
            box.kernel = setup.stdout_bytes.decode("utf-8", "replace").strip()
            await self.put_files(sid, files)
            browser_ms = await self._start_browser(box) if profile.browser else None
        except Exception:
            await self.destroy(sid)
            raise
        info = {
            "id": sid, "name": name, "isolation": "microVM (libkrun, own kernel)", "kernel": box.kernel,
            "image": profile.image, "boot_ms": boot_ms,
            "limits": {"cpus": profile.cpus, "memory_mib": profile.memory_mib, "exec_timeout_s": profile.exec_timeout_s,
                       "max_lifetime_s": profile.max_lifetime_s, "user": profile.user, "nproc": profile.nproc,
                       "file_size_mib": profile.fsize_mib, "out_mib": profile.out_mib, "tmp_mib": profile.tmp_mib},
            "network": network.describe(),
            "secrets": [{"env": s.env, "guest_sees": f"$MSB_{s.env}", "allowed_host": s.allow_host} for s in secret_specs],
            "files": sorted(files),
        }
        if profile.browser:
            info["browser"] = {"engine": "Chromium, headless", "viewport": "1280x800", "ready_ms": browser_ms}
        info["policy_sha256"] = hashlib.sha256(canonical({"limits": info["limits"], "network": info["network"],
                                                          "secrets": info["secrets"]})).hexdigest()
        return info

    async def put_files(self, sid: str, files: dict[str, bytes]) -> list[dict]:
        box = self._box(sid)
        written = []
        for fname, data in files.items():
            if not SAFE_NAME.match(fname):
                raise ValueError(f"bad file name {fname!r}")
            await box.sandbox.fs.write(f"/work/in/{fname}", data)
            written.append({"path": f"/work/in/{fname}", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        if files:
            await box.sandbox.exec("chmod", ["a+r", *[f"/work/in/{f}" for f in files]])
        return written

    async def exec(self, sid: str, argv: list[str], timeout_s: int | None = None, user: str | None = None) -> dict:
        box = self._box(sid)
        p = box.profile
        timeout = min(timeout_s or p.exec_timeout_s, p.exec_timeout_s)
        result = {"exit_code": None, "stdout": "", "stderr": "", "timed_out": False, "terminated": False}
        started = time.perf_counter()
        try:
            out = await box.sandbox.exec(argv[0], argv[1:], user=user or p.user, timeout=timeout,
                                         rlimits=[Rlimit.nproc(p.nproc), Rlimit.fsize(p.fsize_mib * 2**20)])
            result.update(exit_code=out.exit_code, stdout=out.stdout_bytes.decode("utf-8", "replace")[-MAX_TEXT:],
                          stderr=out.stderr_bytes.decode("utf-8", "replace")[-MAX_TEXT:])
        except ExecTimeoutError:
            result["timed_out"] = True
        except MicrosandboxError as e:  # the VM went away mid-command, e.g. terminate-on-violation
            result.update(terminated=True, stderr=f"{type(e).__name__}: {e}")
            box.status = "terminated"
        result["duration_ms"] = round((time.perf_counter() - started) * 1000)
        result["net"] = [] if result["terminated"] or p.browser else await self._read_netlog(box)
        result["system"] = await self._system_events(box)
        if result["terminated"] or any("violation" in e["line"].lower() for e in result["system"]):
            result["terminated"] = True
            box.status = "terminated"
        leaked = [v for v in box.secret_values if v and (v in result["stdout"] or v in result["stderr"])]
        result["secret_exposed"] = bool(leaked)
        return result

    async def browse(self, sid: str, action: dict) -> dict:
        """Run one browser action in a browser sandbox; return the page and its screenshot(s)."""
        box = self._box(sid)
        if not box.profile.browser:
            raise ValueError(f"{sid} is not a browser sandbox")
        shot = f"shot-{secrets.token_hex(4)}"
        r = await self.exec(sid, ["python3", "/opt/glovebox/browser_step.py", json.dumps({**action, "shot": shot})])
        page = None
        for line in reversed(r["stdout"].strip().splitlines()):
            if line.startswith("{"):
                try:
                    page = json.loads(line)
                except ValueError:
                    pass
                break
        out = {"page": page, "exit_code": r["exit_code"], "duration_ms": r["duration_ms"], "timed_out": r["timed_out"],
               "terminated": r["terminated"], "stderr": r["stderr"][-2000:], "system": r["system"]}
        if not r["terminated"]:
            for key, name in (("screenshot", shot), ("screenshot_restored", f"{shot}-r")):
                try:
                    out[key] = base64.b64encode(bytes(await box.sandbox.fs.read(f"/work/out/{name}.png"))).decode()
                except Exception:
                    pass
            await box.sandbox.exec("sh", ["-c", f"rm -f /work/out/{shot}.png /work/out/{shot}-r.png"])
        return out

    async def outputs(self, sid: str) -> list[dict]:
        box = self._box(sid)
        listed = []
        for entry in await box.sandbox.fs.list("/work/out"):
            path = str(entry.path)
            name = path.rsplit("/", 1)[-1]
            if str(entry.kind).lower().endswith("file") and SAFE_NAME.match(name):
                size = entry.size or 0
                if size <= MAX_OUTPUT_BYTES:
                    data = await box.sandbox.fs.read(path)
                    box.outputs[name] = bytes(data)
                    listed.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                else:
                    listed.append({"name": name, "bytes": size, "skipped": "larger than 8 MiB"})
        return listed

    def output(self, sid: str, name: str) -> bytes:
        return self._box(sid).outputs[name]

    async def metrics(self, sid: str) -> dict:
        box = self._box(sid)
        if box.status != "running":
            return {"status": box.status}
        m = await box.sandbox.metrics()
        return {"status": box.status, "cpu_percent": round(m.cpu_percent, 1), "memory_bytes": m.memory_bytes,
                "memory_limit_bytes": m.memory_limit_bytes, "net_tx_bytes": m.net_tx_bytes,
                "net_rx_bytes": m.net_rx_bytes, "uptime_ms": m.uptime_ms}

    async def destroy(self, sid: str) -> None:
        box = self.boxes.pop(sid, None)
        if not box:
            return
        try:
            await box.sandbox.destroy(force=True, timeout=10)
        except Exception as e:  # already gone after terminate-on-violation is fine
            log.info("destroy %s: %s", box.name, e)

    async def reap(self) -> None:
        """Destroy anything that outlived its profile, in case a caller vanished."""
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for sid, box in list(self.boxes.items()):
                if now - box.created > box.profile.max_lifetime_s + 30:
                    log.warning("reaping %s", box.name)
                    await self.destroy(sid)

    # ------------------------------------------------------------- helpers

    def _box(self, sid: str) -> Box:
        try:
            return self.boxes[sid]
        except KeyError:
            raise KeyError(f"unknown sandbox {sid}") from None

    async def _start_browser(self, box: Box) -> int:
        p = box.profile
        started = time.perf_counter()
        await box.sandbox.exec("sh", ["-c", "setsid nohup python3 /opt/glovebox/browser_host.py > /tmp/browser.log 2>&1 "
                                            "< /dev/null &"], user=p.user,
                               rlimits=[Rlimit.nproc(p.nproc), Rlimit.fsize(p.fsize_mib * 2**20)])
        for _ in range(120):
            ready = await box.sandbox.exec("sh", ["-c", "test -f /tmp/browser.ready && echo ready"])
            if ready.stdout_bytes.strip() == b"ready":
                return round((time.perf_counter() - started) * 1000)
            await asyncio.sleep(0.25)
        tail = (await box.sandbox.exec("sh", ["-c", "tail -c 1500 /tmp/browser.log"])).stdout_bytes.decode("utf-8", "replace")
        raise RuntimeError(f"Chromium did not start in 30 s: {tail}")

    async def _read_netlog(self, box: Box) -> list[dict]:
        try:
            raw = bytes(await box.sandbox.fs.read(NETLOG)).decode("utf-8", "replace")
            await box.sandbox.exec("sh", ["-c", f": > {NETLOG}"])
        except Exception:
            return []
        entries = []
        for line in raw.splitlines()[-200:]:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    async def _system_events(self, box: Box) -> list[dict]:
        try:
            entries = await box.sandbox.logs(sources=[LogReadSource.SYSTEM], since_ms=box.log_since_ms or None)
        except Exception:
            return []
        events = []
        for e in entries:
            box.log_since_ms = max(box.log_since_ms, e.timestamp_ms + 1)
            line = e.text().strip()
            if "relay" not in line and INTERESTING.search(line):
                events.append({"ts_ms": e.timestamp_ms, "line": line[:400]})
        return events


async def cleanup_stale() -> int:
    """Remove sandboxes left behind by an earlier runner process."""
    removed = 0
    try:
        page = await Sandbox.list_with(limit=100, labels={"glovebox": "1"})
    except Exception as e:
        log.info("stale sandbox scan skipped: %s", e)
        return 0
    for handle in page.sandboxes:
        try:
            await handle.destroy(force=True, timeout=10)
            removed += 1
        except Exception as e:
            log.info("could not remove %s: %s", handle.name, e)
    return removed


async def ensure_browser_image() -> str:
    """Pull the browser image once at startup, so the first Operator mission doesn't wait for it."""
    started = time.perf_counter()
    async with await Sandbox.create("gbx-browser-pull", image=BROWSER_IMAGE, cpus=1, memory=512, replace=True,
                                    network=Network.none(), max_duration=900) as sb:
        await sb.exec("true", [])
    return f"ready in {time.perf_counter() - started:.0f} s"


async def ensure_bytecode(pydeps: Path) -> str:
    """Compile the read-only package folder once for the guest's Python, so imports don't recompile per run."""
    if any(pydeps.glob("pandas/__pycache__/*.cpython-312.pyc")):
        return "cached"
    started = time.perf_counter()
    async with await Sandbox.create("gbx-pydeps-compile", image=PROFILES["code"].image, cpus=2, memory=1024,
                                    replace=True, network=Network.none(), max_duration=600,
                                    volumes={"/opt/pydeps": Volume.bind(str(pydeps))}) as sb:
        await sb.exec("python", ["-m", "compileall", "-q", "-j", "0", "/opt/pydeps"], timeout=540)
    return f"compiled in {time.perf_counter() - started:.0f} s"
