#!/usr/bin/env python3
"""Download the Python packages sandboxes use, built for the guest's CPU, into .pydeps/.

The runner mounts these folders read-only at /opt/pydeps, so sandboxes stay offline and no custom
image is needed. Guests run on the host's architecture: aarch64 on an Apple Silicon Mac, x86_64 on
a Vultr VX1 instance.

  code     .pydeps/<arch>          data packages for python:3.12-slim code VMs
  browser  .pydeps/<arch>-browser  Playwright for the browser VMs; the image already has the browsers,
                                   and the package version must match them

  python3 scripts/fetch_pydeps.py            # both sets, architecture of this machine
  python3 scripts/fetch_pydeps.py --set browser --arch x86_64
"""
import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETS = {
    "code": ("", ["pandas==2.3.3", "numpy==2.3.4", "matplotlib==3.10.7", "openpyxl==3.1.5"]),
    "browser": ("-browser", ["playwright==1.64.0"]),  # mcr.microsoft.com/playwright/python:v1.64.0-noble
}


def guest_arch():
    m = platform.machine().lower()
    return "aarch64" if m in ("arm64", "aarch64") else "x86_64"


def fetch(arch: str, suffix: str, packages: list[str], if_missing: bool):
    target = ROOT / ".pydeps" / f"{arch}{suffix}"
    marker = target / ".glovebox-packages"
    if if_missing and marker.exists() and marker.read_text().split() == packages:
        print(f"{target} already has {', '.join(packages)}")
        return
    target.mkdir(parents=True, exist_ok=True)
    platforms = [f"manylinux1_{arch}", f"manylinux2014_{arch}", f"manylinux_2_17_{arch}", f"manylinux_2_28_{arch}"]
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "--target", str(target),
           "--python-version", "3.12", "--implementation", "cp", "--only-binary=:all:"]
    for p in platforms:
        cmd += ["--platform", p]
    subprocess.run(cmd + packages, check=True)
    marker.write_text("\n".join(packages) + "\n")
    print(f"Installed {', '.join(packages)} for {arch} into {target}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arch", choices=["aarch64", "x86_64"], default=guest_arch())
    ap.add_argument("--set", choices=[*SETS, "all"], default="all")
    ap.add_argument("--if-missing", action="store_true", help="skip when these exact versions are already installed")
    args = ap.parse_args()
    for name, (suffix, packages) in SETS.items():
        if args.set in (name, "all"):
            fetch(args.arch, suffix, packages, args.if_missing)


if __name__ == "__main__":
    main()
