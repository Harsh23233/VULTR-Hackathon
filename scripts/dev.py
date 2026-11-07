#!/usr/bin/env python3
"""Run Glovebox locally: python3 scripts/dev.py runner | orchestrator

Loads .env (creating it on first run), keeps Microsandbox state in ./.msb, and starts uvicorn
from the project venv.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = {"runner": ("apps.runner.main:app", "RUNNER_PORT", "8081"),
        "orchestrator": ("apps.orchestrator.main:app", "PORT", "8000")}


def load_env():
    env_file = ROOT / ".env"
    if not env_file.exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "init_env.py")], check=True)
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.split(" #")[0].strip())


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in APPS:
        raise SystemExit("usage: python3 scripts/dev.py runner|orchestrator")
    load_env()
    os.environ.setdefault("MSB_HOME", str(ROOT / ".msb"))  # short path: socket length limit
    os.chdir(ROOT)
    app, port_var, default_port = APPS[sys.argv[1]]
    python = str(ROOT / ".venv" / "bin" / "python")
    os.execv(python, [python, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", os.environ.get(port_var, default_port)])


if __name__ == "__main__":
    main()
