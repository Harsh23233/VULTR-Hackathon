# Deploying Glovebox to Vultr

One command creates the infrastructure, one more puts the code on it and prints the public URL.

| Host | Plan (default) | Runs | Public ports |
|---|---|---|---|
| control | `vc2-1c-2gb`, $0.014/h | Caddy (HTTPS) → console and orchestrator on 127.0.0.1:8000; mock ERP API (:8090) and mock vendor sites (:8101-8103) on the VPC address | 80, 443, 22 |
| sandbox | `vx1-g-2c-8g-120s`, $0.076/h | Runner on the VPC address :8081; one Microsandbox microVM per code task, one Chromium microVM per Operator mission | 22 only |

Both hosts sit in Seattle (`sea`), where most Vultr Serverless Inference models run, on a private VPC (`10.73.10.0/24`). Total about **$0.09 per hour** (about $2.16 a day).

## Before you start

1. **Vultr account** with the hackathon credits applied.
2. **Account API key:** Vultr console › Account › API › Enable API. Under *Access Control*, add this machine's public IP.
3. **Inference key** in `.env`: run `python3 scripts/set_key.py VULTR_INFERENCE_API_KEY` and paste the key from Vultr console › Serverless › Inference › your subscription. It also sets `GLOVEBOX_INFERENCE=vultr`.
4. **SSH key** at `~/.ssh/id_ed25519.pub`. If you don't have one: `ssh-keygen -t ed25519`.

The account API key is only read from your shell (or `.env`); it never goes to the servers. The servers get the inference key (control plane only), a runner token and the receipt signing key.

## Deploy

```bash
python3 deploy/vultr.py plan                          # check plans, region and cost; no key needed
python3 scripts/set_key.py VULTR_API_KEY              # account API key, saved in .env (or: export VULTR_API_KEY=...)
python3 deploy/vultr.py up --ssh-from <your-ip>/32    # 3-5 minutes
python3 deploy/vultr.py push                          # first run 5-8 minutes; prints https://glovebox.<ip>.sslip.io
```

`up` creates the SSH key, VPC, two firewall groups and two instances, and records them in `deploy/state.json`. `push` copies the code with rsync, writes `/etc/glovebox/*.env`, runs [remote/setup.sh](remote/setup.sh) on both hosts (sandbox first), and waits for the public URL to answer.

After a code change, run `push` again; it takes about a minute.

## Check it

```bash
python3 deploy/vultr.py status
.venv/bin/python tests/live_mission_check.py --base https://glovebox.<ip>.sslip.io
$(python3 deploy/vultr.py ssh sandbox)    # then: journalctl -u glovebox-runner -f
$(python3 deploy/vultr.py ssh control)    # then: journalctl -u glovebox-orchestrator -u caddy -f
```

## Keep it up through judging

Judging happens after the deadline, and Vultr deployments stop when the event credits end unless a payment method is on file. Add one under Billing. systemd restarts both services if they crash, and Caddy renews the certificate.

## Tear down

```bash
python3 deploy/vultr.py down --yes
```

This deletes the instances, firewall groups, VPC and SSH key that `up` created. `deploy/secrets.env` is kept.

## Manual alternative

If the API route is blocked:
1. In the Vultr console, create a VPC in Seattle (`10.73.10.0/24`).
2. Create two Ubuntu 24.04 instances attached to that VPC, with your SSH key: a VX1 sandbox host and a small control plane.
3. Write `deploy/state.json` by hand:
   ```json
   {"region": "sea", "vpc_subnet": "10.73.10.0/24",
    "sandbox": {"main_ip": "SANDBOX_PUBLIC_IP"}, "control": {"main_ip": "CONTROL_PUBLIC_IP"}}
   ```
4. Write `deploy/secrets.env` with `RUNNER_TOKEN=` and `RECEIPT_SIGNING_KEY=` (`python3 scripts/init_env.py` shows the format).
5. Run `push`.
6. Add firewall groups in the console (control: 80, 443 and 22; sandbox: 22).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No /dev/kvm on this instance` | The sandbox host needs nested virtualization; use a VX1 plan. |
| `has no address in the VPC` | On the host, `ip -4 addr` should show a `10.73.10.x` address. If not, check the VPC is attached (instance › Settings › VPC) and reboot once. |
| HTTPS never answers | `journalctl -u caddy`. Ports 80 and 443 must be open to the internet for the certificate. sslip.io certificates can hit Let's Encrypt limits; use `push --domain your.domain` with an A record pointing at the control plane. |
| `401` from the Vultr API | Add your current IP under the API key's access control. |
| Runner down in `status` | `journalctl -u glovebox-runner`. The first start compiles the in-VM packages and pulls `python:3.12-slim`; give it a minute. |
| Want a test deploy before you have the inference key | `push --scripted` runs scripted models with the TEST MODE banner. Never use that for the demo. |

## Security

| Where | Holds |
|---|---|
| Your machine | Vultr account API key, all `.env` values |
| Control plane | Inference key, receipt signing key, runner token |
| Sandbox host | Runner token only. Each mission's ERP token exists only in Microsandbox's host-side proxy while that mission's VM runs; the VM sees a placeholder. |

The runner listens only on the VPC address. UFW allows port 8081 only from the VPC subnet, and the Vultr firewall group exposes nothing but SSH. On the control plane, the mock ERP API and vendor sites listen on the VPC address only, and UFW admits them only from the VPC subnet; browser microVMs may reach those three vendor ports and nothing else. Password logins are disabled by cloud-init; restrict SSH further with `--ssh-from`.

Code microVMs use `host.microsandbox.internal:8090` for the mock ERP. Microsandbox substitutes the token for that hostname, and a socket bound to loopback on the sandbox host forwards the request to the ERP on the control plane's VPC address.

On its first start the runner pulls the Playwright browser image (about 2 GB) in the background; `/v1/health` reports `browser: ready` when Operator missions can run.

## Smoke test without Vultr

```bash
MSB_HOME=$PWD/.msb .venv/bin/python deploy/smoke_test.py
```

Runs `setup.sh` for both roles in an Ubuntu 24.04 microVM on your machine, then starts the control plane inside it.
