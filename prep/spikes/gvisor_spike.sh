#!/usr/bin/env bash
# Fallback spike: Docker + gVisor (runsc) on Ubuntu 24.04. Use only if Microsandbox or nested
# KVM fails on the Vultr VM. Needs sudo. Not run yet; expect to adjust on first use.
set -euo pipefail

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
if ! command -v runsc >/dev/null; then
  curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    | sudo tee /etc/apt/sources.list.d/gvisor.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq runsc
  sudo runsc install && sudo systemctl reload docker
fi

IMAGE=python:3.12-slim
HARDEN=(--rm --runtime=runsc --cpus 1 --memory 512m --pids-limit 128 --read-only
        --tmpfs /tmp:size=64m --cap-drop ALL --security-opt no-new-privileges --user 65534:65534)

check() { printf '%-10s ' "$1"; shift; if "$@" >/tmp/gvisor_check.log 2>&1; then echo PASS; else echo "FAIL ($(tail -1 /tmp/gvisor_check.log))"; fi; }

check kernel   sh -c "docker run ${HARDEN[*]} --network none $IMAGE uname -a | grep -qi gvisor || docker run ${HARDEN[*]} --network none $IMAGE dmesg | grep -qi gvisor"
check offline  sh -c "! docker run ${HARDEN[*]} --network none $IMAGE python -c \"import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)\""
check memory   sh -c "! docker run ${HARDEN[*]} --network none $IMAGE python -c \"x = b'1' * (2 * 1024**3)\""
check pids     sh -c "docker run ${HARDEN[*]} --network none $IMAGE python -c \"
import os, time
n = 0
try:
    while True:
        if os.fork() == 0:
            time.sleep(30)
            os._exit(0)
        n += 1
except OSError:
    print('fork refused after', n)
\""
check tmpfill  sh -c "docker run ${HARDEN[*]} --network none $IMAGE sh -c 'dd if=/dev/zero of=/tmp/x bs=1M count=200; test \$(stat -c %s /tmp/x) -le 67108864'"
check readonly sh -c "! docker run ${HARDEN[*]} --network none $IMAGE sh -c 'echo x > /etc/x'"
