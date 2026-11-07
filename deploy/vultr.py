#!/usr/bin/env python3
"""Deploy Glovebox to Vultr: a control plane and a VX1 sandbox host on a private VPC.

  python3 deploy/vultr.py plan                 # what gets created and what it costs (no key needed)
  export VULTR_API_KEY=...                     # Vultr console > Account > API; allow your IP under access control
  python3 deploy/vultr.py up                   # SSH key, VPC, firewall groups, two instances
  python3 deploy/vultr.py push                 # copy the code, set up both hosts, start services, print the URL
  python3 deploy/vultr.py status               # public URL health, services, runner
  python3 deploy/vultr.py ssh control          # print an ssh command for a host
  python3 deploy/vultr.py down --yes           # delete everything this tool created

Resource ids and IPs are kept in deploy/state.json and production tokens in deploy/secrets.env;
both are gitignored. The inference key and model roster come from .env. Standard library only;
needs ssh and rsync on this machine.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "deploy" / "state.json"
SECRETS = ROOT / "deploy" / "secrets.env"
KNOWN_HOSTS = Path.home() / ".ssh" / "glovebox_known_hosts"  # ssh splits this option on spaces, so not in the project folder
API = "https://api.vultr.com/v2"
APP_DIR = "/opt/glovebox/app"
RUNNER_PORT = 8081
ERP_PORT = 8090
DEFAULTS = {"region": "sea", "sandbox_plan": "vx1-g-2c-8g-120s", "control_plan": "vc2-1c-2gb", "os_id": 2284,
            "vpc_subnet": "10.73.10.0", "vpc_mask": 24}
ROLES = ("sandbox", "control")
USER_DATA = """#cloud-config
ssh_pwauth: false
package_update: true
packages: [python3-venv, python3-pip, rsync, ufw, curl]
"""
RSYNC_EXCLUDES = [".git", ".venv", ".msb", ".pydeps", "data", ".env", ".claude", "__pycache__", ".DS_Store",
                  "deploy/state.json", "deploy/secrets.env", "prep/spikes/out"]
MODEL_VARS = ["MODEL_PLANNER", "MODEL_CODER", "MODEL_ACTOR", "MODEL_VERIFIER", "MODEL_READER", "MODEL_EXPLAINER",
              "MODEL_SAFETY", "MODEL_DECISION"]


# ------------------------------------------------------------------ small helpers

def die(msg: str):
    raise SystemExit(f"error: {msg}")


def read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.split(" #")[0].strip()
    return out


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state: dict):
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def api(method: str, path: str, body: dict | None = None, *, key: str | None = None, attempts: int = 6):
    headers = {"Content-Type": "application/json", "User-Agent": "glovebox-deploy/0.1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(attempts):
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                time.sleep(2 ** attempt)
                continue
            if e.code == 401:
                die(f"Vultr API refused the key (401). Check VULTR_API_KEY and allow this machine's IP under API access control. {detail}")
            die(f"{method} {path} -> {e.code}: {detail}")
        except urllib.error.URLError as e:
            if attempt == attempts - 1:
                die(f"cannot reach the Vultr API: {e}")
            time.sleep(2 ** attempt)


def account_key() -> str:
    key = os.environ.get("VULTR_API_KEY") or read_env(ROOT / ".env").get("VULTR_API_KEY")
    if not key:
        die("export VULTR_API_KEY first (Vultr console > Account > API).")
    return key


def ssh_opts(identity: str | None = None) -> list[str]:
    opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20"]
    return opts + (["-i", identity] if identity else [])


def ssh(state: dict, role: str, command: str, *, stdin: str | None = None, capture: bool = False, check: bool = True):
    host = state[role]["main_ip"]
    return subprocess.run(["ssh", *ssh_opts(state.get("identity")), f"root@{host}", command], input=stdin, text=True,
                          capture_output=capture, check=check)


def wait_port(host: str, port: int, timeout_s: int = 600):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            socket.create_connection((host, port), timeout=5).close()
            return
        except OSError:
            time.sleep(5)
    die(f"{host}:{port} did not open within {timeout_s} s")


# ------------------------------------------------------------------ plan

def check_catalog(args) -> dict:
    regions = {r["id"]: r for r in api("GET", "/regions?per_page=500")["regions"]}
    plans = {p["id"]: p for p in api("GET", "/plans?per_page=500&type=all")["plans"]}
    os_ids = {o["id"]: o["name"] for o in api("GET", "/os?per_page=500")["os"]}
    if args.region not in regions:
        die(f"unknown region {args.region}; US options: {', '.join(k for k, v in regions.items() if v.get('country') == 'US')}")
    chosen = {}
    for role, plan_id in (("sandbox", args.sandbox_plan), ("control", args.control_plan)):
        plan = plans.get(plan_id) or die(f"unknown plan {plan_id}")
        if args.region not in plan.get("locations", []):
            die(f"plan {plan_id} is not offered in {args.region}; it is in {', '.join(plan.get('locations', []))}")
        chosen[role] = plan
    if not chosen["sandbox"]["id"].startswith("vx1"):
        print("warning: the sandbox host needs nested virtualization (/dev/kvm); Vultr documents this for VX1 plans.")
    if args.os_id not in os_ids:
        die(f"unknown os_id {args.os_id}")
    return {"region": regions[args.region], "plans": chosen, "os": os_ids[args.os_id]}


def cmd_plan(args):
    cat = check_catalog(args)
    hourly = sum(p["hourly_cost"] for p in cat["plans"].values())
    print(f"Region   {args.region} ({cat['region']['city']}); most Vultr inference models are served from Seattle")
    print(f"OS       {cat['os']} (os_id {args.os_id})")
    for role, p in cat["plans"].items():
        print(f"{role:8} {p['id']:20} {p['vcpu_count']} vCPU, {p['ram'] // 1024} GB RAM, {p['disk']} GB disk, "
              f"${p['hourly_cost']}/hour (${p['monthly_cost']}/month)")
    print(f"VPC      {args.vpc_subnet}/{args.vpc_mask}, private link between the two hosts")
    print("Firewall control: 80 and 443 from anywhere, 22 from " + args.ssh_from + "; sandbox: 22 from " + args.ssh_from)
    print(f"Cost     ${hourly:.3f}/hour, about ${hourly * 24:.2f}/day or ${hourly * 24 * 30:.0f} for 30 days")


# ------------------------------------------------------------------ up

def cmd_up(args):
    key = account_key()
    state = load_state()
    if state.get("sandbox", {}).get("id") or state.get("control", {}).get("id"):
        die("deploy/state.json already lists instances. Run `push`, or `down --yes` first.")
    cat = check_catalog(args)
    pub = Path(os.path.expanduser(args.ssh_key))
    if not pub.exists():
        die(f"no public key at {pub}. Create one with: ssh-keygen -t ed25519")
    state.update({"region": args.region, "vpc_subnet": f"{args.vpc_subnet}/{args.vpc_mask}", "created": {},
                  "identity": str(pub.with_suffix("")) if pub.suffix == ".pub" else None})
    save_state(state)

    print("creating SSH key")
    ssh_key = api("POST", "/ssh-keys", {"name": f"glovebox-{int(time.time())}", "ssh_key": pub.read_text().strip()}, key=key)["ssh_key"]
    state["created"]["ssh_key"] = ssh_key["id"]
    save_state(state)

    print("creating VPC")
    vpc = api("POST", "/vpcs", {"region": args.region, "description": "glovebox",
                                "v4_subnet": args.vpc_subnet, "v4_subnet_mask": args.vpc_mask}, key=key)["vpc"]
    state["created"]["vpc"] = vpc["id"]
    save_state(state)

    ssh_net = ipaddress.ip_network(args.ssh_from, strict=False)
    rules = {
        "control": [("80", "0.0.0.0/0", "http (certificate issuance, redirect)"), ("443", "0.0.0.0/0", "https console"),
                    ("22", str(ssh_net), "ssh")],
        "sandbox": [("22", str(ssh_net), "ssh")],
    }
    for role in ROLES:
        print(f"creating firewall group for {role}")
        group = api("POST", "/firewalls", {"description": f"glovebox-{role}"}, key=key)["firewall_group"]
        state["created"][f"firewall_{role}"] = group["id"]
        save_state(state)
        for port, cidr, note in rules[role]:
            net = ipaddress.ip_network(cidr)
            api("POST", f"/firewalls/{group['id']}/rules", {"ip_type": "v4", "protocol": "tcp", "subnet": str(net.network_address),
                                                            "subnet_size": net.prefixlen, "port": port, "notes": note}, key=key)

    for role in ROLES:
        plan = cat["plans"][role]["id"]
        print(f"creating {role} instance ({plan})")
        inst = api("POST", "/instances", {
            "region": args.region, "plan": plan, "os_id": args.os_id, "label": f"glovebox-{role}",
            "hostname": f"glovebox-{role}", "tags": ["glovebox"], "firewall_group_id": state["created"][f"firewall_{role}"],
            "attach_vpc": [vpc["id"]], "sshkey_id": [ssh_key["id"]], "backups": "disabled", "activation_email": False,
            "user_data": base64.b64encode(USER_DATA.encode()).decode()}, key=key)["instance"]
        state[role] = {"id": inst["id"], "plan": plan}
        save_state(state)

    for role in ROLES:
        print(f"waiting for {role} to boot", end="", flush=True)
        for _ in range(120):
            inst = api("GET", f"/instances/{state[role]['id']}", key=key)["instance"]
            if inst["status"] == "active" and inst["power_status"] == "running" and inst["main_ip"] not in ("", "0.0.0.0"):
                break
            print(".", end="", flush=True)
            time.sleep(10)
        else:
            die(f"{role} did not become active in 20 minutes")
        state[role].update(main_ip=inst["main_ip"], internal_ip=inst.get("internal_ip") or "")
        save_state(state)
        print(f" {inst['main_ip']}")
        wait_port(inst["main_ip"], 22)

    if not SECRETS.exists():
        SECRETS.write_text(f"RUNNER_TOKEN={secrets.token_urlsafe(32)}\n"
                           f"RECEIPT_SIGNING_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}\n")
        SECRETS.chmod(0o600)
    print("\nInstances are up. Next: python3 deploy/vultr.py push")


# ------------------------------------------------------------------ push

def vpc_ip(state: dict, role: str) -> str:
    net = ipaddress.ip_network(state["vpc_subnet"])
    out = ssh(state, role, "ip -4 -o addr show", capture=True).stdout
    for line in out.splitlines():
        addr = line.split()[3].split("/")[0]
        if ipaddress.ip_address(addr) in net:
            return addr
    die(f"{role} has no address in the VPC {net}. See deploy/README.md, 'VPC address missing'.")


def env_text(values: dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in values.items())


def cmd_push(args):
    state = load_state()
    if not state.get("control", {}).get("main_ip"):
        die("no instances yet. Run `up` first (or fill deploy/state.json by hand; see deploy/README.md).")
    local = read_env(ROOT / ".env")
    prod = read_env(SECRETS)
    if not prod.get("RUNNER_TOKEN"):
        die("deploy/secrets.env is missing; `up` creates it.")
    inference = "scripted" if args.scripted else "vultr"
    if inference == "vultr" and not local.get("VULTR_INFERENCE_API_KEY"):
        die("put VULTR_INFERENCE_API_KEY in .env (or pass --scripted for an infrastructure-only test deploy).")
    for role in ROLES:
        wait_port(state[role]["main_ip"], 22, 120)
        state[role]["vpc_ip"] = vpc_ip(state, role)
    host = args.domain or state.get("domain") or f"glovebox.{state['control']['main_ip'].replace('.', '-')}.sslip.io"
    state["domain"] = host
    save_state(state)
    subnet = state["vpc_subnet"]
    envs = {
        "sandbox": {"RUNNER_TOKEN": prod["RUNNER_TOKEN"], "RUNNER_BIND": state["sandbox"]["vpc_ip"],
                    "RUNNER_PORT": str(RUNNER_PORT), "MAX_SANDBOXES": local.get("MAX_SANDBOXES", "4"),
                    "MSB_HOME": "/var/lib/glovebox/msb", "HOME": "/var/lib/glovebox", "VPC_SUBNET": subnet},
        "control": {"VULTR_INFERENCE_API_KEY": local.get("VULTR_INFERENCE_API_KEY", ""),
                    "VULTR_INFERENCE_BASE_URL": local.get("VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1"),
                    "GLOVEBOX_INFERENCE": inference,
                    **{k: local[k] for k in MODEL_VARS if local.get(k)},
                    "RUNNER_URL": f"http://{state['sandbox']['vpc_ip']}:{RUNNER_PORT}", "RUNNER_TOKEN": prod["RUNNER_TOKEN"],
                    "ERP_BIND": f"{state['control']['vpc_ip']}:{ERP_PORT}", "ERP_GUEST_HOST": state["control"]["vpc_ip"],
                    "ERP_GUEST_PORT": str(ERP_PORT), "ERP_SCHEME": "http",
                    "VENDOR_BIND": state["control"]["vpc_ip"], "VENDOR_GUEST_HOST": state["control"]["vpc_ip"],
                    "RECEIPT_SIGNING_KEY": prod["RECEIPT_SIGNING_KEY"], "PUBLIC_BASE_URL": f"https://{host}",
                    "GLOVEBOX_HOST": host, "MAX_CONCURRENT_MISSIONS": local.get("MAX_CONCURRENT_MISSIONS", "2"),
                    "DATA_DIR": "/var/lib/glovebox/data", "HOME": "/var/lib/glovebox", "VPC_SUBNET": subnet},
    }
    env_files = {"sandbox": "/etc/glovebox/runner.env", "control": "/etc/glovebox/orchestrator.env"}
    rsh = shlex.join(["ssh", *ssh_opts(state.get("identity"))])
    for role in ROLES:  # sandbox first, so the runner is up before the control plane checks it
        ip = state[role]["main_ip"]
        print(f"\n### {role} ({ip}): copying code")
        ssh(state, role, f"mkdir -p {APP_DIR} /etc/glovebox")
        subprocess.run(["rsync", "-az", "--delete", *[f"--exclude={x}" for x in RSYNC_EXCLUDES], "-e", rsh,
                        f"{ROOT}/", f"root@{ip}:{APP_DIR}/"], check=True)
        ssh(state, role, f"umask 077 && cat > {env_files[role]}", stdin=env_text(envs[role]))
        print(f"### {role}: setting up (first run takes a few minutes)")
        ssh(state, role, f"bash {APP_DIR}/deploy/remote/setup.sh {role}")
    print(f"\nwaiting for https://{host} (certificate issuance can take a minute)")
    for _ in range(36):
        status = fetch_status(host)
        if status:
            print_status(host, status)
            return
        time.sleep(5)
    die(f"https://{host}/api/status did not answer. Check: python3 deploy/vultr.py ssh control, then journalctl -u caddy -u glovebox-orchestrator")


# ------------------------------------------------------------------ status, ssh, down

def fetch_status(host: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"https://{host}/api/status", timeout=10) as r:
            return json.load(r)
    except Exception:
        return None


def print_status(host: str, status: dict):
    roster = status.get("roster", [])
    runner = status.get("runner", {})
    print(f"Console    https://{host}")
    print(f"Inference  {status.get('inference')} · {sum(r.get('ok', False) for r in roster)}/{len(roster)} roles ready")
    print(f"Runner     {'ok' if runner.get('ok') else 'DOWN: ' + str(runner.get('error'))} · guest {runner.get('guest_arch')} · "
          f"{runner.get('running')} running · bytecode {runner.get('bytecode')}")
    print(f"Receipts   Ed25519 key {status.get('signing_key', {}).get('key_id')}")


def cmd_status(_args):
    state = load_state()
    if not state.get("domain"):
        die("nothing pushed yet")
    status = fetch_status(state["domain"])
    if status:
        print_status(state["domain"], status)
    else:
        print(f"https://{state['domain']}/api/status is not answering")
    for role, unit in (("sandbox", "glovebox-runner"), ("control", "glovebox-orchestrator caddy")):
        out = ssh(state, role, f"systemctl is-active {unit}", capture=True, check=False).stdout.split()
        print(f"{role:8} services: {' '.join(f'{u}={s}' for u, s in zip(unit.split(), out))}")


def cmd_ssh(args):
    state = load_state()
    host = state.get(args.role, {}).get("main_ip") or die(f"no {args.role} host in deploy/state.json")
    print(shlex.join(["ssh", *ssh_opts(state.get("identity")), f"root@{host}"]))


def cmd_down(args):
    if not args.yes:
        die("this deletes both instances, the firewall groups, the VPC and the SSH key. Re-run with --yes.")
    key = account_key()
    state = load_state()
    for role in ROLES:
        if state.get(role, {}).get("id"):
            print(f"deleting {role} instance")
            api("DELETE", f"/instances/{state[role]['id']}", key=key)
    created = state.get("created", {})
    time.sleep(20)  # firewall groups and the VPC can't be deleted while instances still hold them
    for name in ("firewall_control", "firewall_sandbox"):
        if created.get(name):
            print(f"deleting {name}")
            api("DELETE", f"/firewalls/{created[name]}", key=key)
    if created.get("vpc"):
        print("deleting VPC")
        for _ in range(12):
            try:
                api("DELETE", f"/vpcs/{created['vpc']}", key=key, attempts=1)
                break
            except SystemExit:
                time.sleep(10)
    if created.get("ssh_key"):
        api("DELETE", f"/ssh-keys/{created['ssh_key']}", key=key)
    STATE.unlink(missing_ok=True)
    KNOWN_HOSTS.unlink(missing_ok=True)
    print("done. deploy/secrets.env was kept; delete it if you want fresh tokens next time.")


def main():
    ap = argparse.ArgumentParser(description="Deploy Glovebox to Vultr")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "up"):
        p = sub.add_parser(name)
        p.add_argument("--region", default=DEFAULTS["region"])
        p.add_argument("--sandbox-plan", default=DEFAULTS["sandbox_plan"])
        p.add_argument("--control-plan", default=DEFAULTS["control_plan"])
        p.add_argument("--os-id", type=int, default=DEFAULTS["os_id"])
        p.add_argument("--vpc-subnet", default=DEFAULTS["vpc_subnet"])
        p.add_argument("--vpc-mask", type=int, default=DEFAULTS["vpc_mask"])
        p.add_argument("--ssh-from", default="0.0.0.0/0", help="CIDR allowed to SSH, e.g. 203.0.113.7/32")
        p.add_argument("--ssh-key", default="~/.ssh/id_ed25519.pub")
    push = sub.add_parser("push")
    push.add_argument("--domain", help="your own hostname pointing at the control plane's IP (default: sslip.io)")
    push.add_argument("--scripted", action="store_true", help="test deploy without an inference key (TEST MODE banner)")
    sub.add_parser("status")
    s = sub.add_parser("ssh")
    s.add_argument("role", choices=ROLES)
    d = sub.add_parser("down")
    d.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    {"plan": cmd_plan, "up": cmd_up, "push": cmd_push, "status": cmd_status, "ssh": cmd_ssh, "down": cmd_down}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: command failed ({e.returncode}): {shlex.join(map(str, e.cmd))[:300]}")
