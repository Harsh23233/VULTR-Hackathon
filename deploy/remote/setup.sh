#!/usr/bin/env bash
# Set up a Glovebox host on Ubuntu 24.04. Idempotent; runs as root after the code is in /opt/glovebox/app
# and the env file is in /etc/glovebox (deploy/vultr.py push does both).
#
#   bash setup.sh sandbox    # runner + Microsandbox; API on the VPC address only
#   bash setup.sh control    # console + orchestrator behind Caddy (HTTPS); mock ERP on the VPC address
#
# GLOVEBOX_SETUP_DRY=1 skips systemd, ufw and the /dev/kvm check, to smoke-test in a container or VM.
set -euo pipefail

ROLE="${1:?usage: setup.sh sandbox|control}"
APP=/opt/glovebox/app
VENV=/opt/glovebox/venv
STATE=/var/lib/glovebox
DRY="${GLOVEBOX_SETUP_DRY:-0}"
case "$ROLE" in
  sandbox) ENV_FILE=/etc/glovebox/runner.env; UNIT=glovebox-runner ;;
  control) ENV_FILE=/etc/glovebox/orchestrator.env; UNIT=glovebox-orchestrator ;;
  *) echo "usage: setup.sh sandbox|control" >&2; exit 2 ;;
esac
log() { printf '\n== %s\n' "$*"; }

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE; deploy/vultr.py push writes it" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
export HOME=/root  # the env file's HOME is for the service user, not this root shell

if [ "$DRY" != 1 ] && command -v cloud-init >/dev/null; then
  log "waiting for cloud-init to finish"
  cloud-init status --wait >/dev/null || true
fi

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip rsync ufw curl ca-certificates gnupg iproute2 >/dev/null

log "service user, folders, permissions"
id glovebox >/dev/null 2>&1 || useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin glovebox
install -d -o glovebox -g glovebox -m 750 "$STATE" "$APP/.pydeps"
chown -R root:root "$APP"
chown -R glovebox:glovebox "$APP/.pydeps"  # the only part of the code tree the service user writes
chown root:glovebox "$ENV_FILE"
chmod 640 "$ENV_FILE"

log "python environment"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"
"$VENV/bin/python" -c "import fastapi, uvicorn, httpx, cryptography, microsandbox; print('python deps ok')"

if [ "$ROLE" = sandbox ]; then
  if [ "$DRY" != 1 ] && [ ! -e /dev/kvm ]; then
    echo "No /dev/kvm on this instance: the sandbox host needs nested virtualization (a Vultr VX1 plan)." >&2
    exit 1
  fi
  getent group kvm >/dev/null && usermod -aG kvm glovebox
  log "packages for $(uname -m) microVMs (mounted read-only: data packages for code, Playwright for the browser)"
  runuser -u glovebox -- env HOME="$STATE" "$VENV/bin/python" "$APP/scripts/fetch_pydeps.py" --if-missing
  log "microsandbox runtime"
  install -d -o glovebox -g glovebox -m 750 "$MSB_HOME"
  # os._exit skips interpreter shutdown, where the SDK's async runtime can abort after the work is done
  runuser -u glovebox -- env HOME="$STATE" MSB_HOME="$MSB_HOME" "$VENV/bin/python" -c \
    "import asyncio, os, microsandbox as m; asyncio.run(m.ensure_runtime()); print('runtime ok, installed:', m.is_runtime_installed(), flush=True); os._exit(0)"
  if [ -z "${RUNNER_BIND:-}" ]; then
    echo "RUNNER_BIND is empty; it must be this host's VPC address" >&2
    exit 1
  fi
  cat > /etc/systemd/system/$UNIT.service <<UNIT
[Unit]
Description=Glovebox runner (sandbox host API, one microVM per task)
After=network-online.target
Wants=network-online.target

[Service]
User=glovebox
Group=glovebox
SupplementaryGroups=kvm
WorkingDirectory=$APP
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/python -m uvicorn apps.runner.main:app --host \${RUNNER_BIND} --port \${RUNNER_PORT}
Restart=always
RestartSec=3
LimitNOFILE=65536
LimitCORE=0
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
  FIREWALL=("allow OpenSSH" "allow from $VPC_SUBNET to any port ${RUNNER_PORT} proto tcp")
  HEALTH="http://${RUNNER_BIND}:${RUNNER_PORT}/v1/health"
else
  if ! command -v caddy >/dev/null; then
    log "caddy (automatic HTTPS)"
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy >/dev/null
  fi
  install -d -m 755 /etc/caddy
  cat > /etc/caddy/Caddyfile <<CADDY
${GLOVEBOX_HOST} {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000 {
		flush_interval -1
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}
}
CADDY
  cat > /etc/systemd/system/$UNIT.service <<UNIT
[Unit]
Description=Glovebox control plane (console, orchestrator, mock ERP API)
After=network-online.target
Wants=network-online.target

[Service]
User=glovebox
Group=glovebox
WorkingDirectory=$APP
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/python -m uvicorn apps.orchestrator.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
  ERP_PORT="${ERP_BIND##*:}"
  FIREWALL=("allow OpenSSH" "allow 80/tcp" "allow 443/tcp" "allow from $VPC_SUBNET to any port ${ERP_PORT} proto tcp"
            "allow from $VPC_SUBNET to any port 8101:8103 proto tcp")  # mock vendor sites, for browser microVMs
  HEALTH="http://127.0.0.1:8000/api/status"
fi

if [ "$DRY" = 1 ]; then
  log "dry run: skipping systemd, ufw and the health check"
  exit 0
fi

log "host firewall (ufw)"
for rule in "${FIREWALL[@]}"; do
  # shellcheck disable=SC2086
  ufw $rule >/dev/null
done
ufw default deny incoming >/dev/null
ufw --force enable >/dev/null
ufw status | sed -n '1,20p'

log "services"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"
if [ "$ROLE" = control ]; then
  systemctl enable caddy >/dev/null 2>&1
  systemctl restart caddy
fi

log "health"
for _ in $(seq 1 60); do
  if curl -fsS "$HEALTH" >/dev/null 2>&1; then
    curl -fsS "$HEALTH"
    echo
    echo "$UNIT is up"
    exit 0
  fi
  sleep 2
done
echo "$UNIT did not answer at $HEALTH; recent logs:" >&2
journalctl -u "$UNIT" -n 60 --no-pager >&2
exit 1
