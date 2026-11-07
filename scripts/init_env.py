#!/usr/bin/env python3
"""Create .env from .env.example with fresh random values for RUNNER_TOKEN and RECEIPT_SIGNING_KEY."""
import base64
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = ROOT / ".env"
    if env.exists():
        print(".env already exists; leaving it alone")
        return
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = text.replace("RUNNER_TOKEN=\n", f"RUNNER_TOKEN={secrets.token_urlsafe(32)}\n")
    text = text.replace("RECEIPT_SIGNING_KEY=\n", f"RECEIPT_SIGNING_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}\n")
    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)
    print("Wrote .env. Add VULTR_INFERENCE_API_KEY, or set GLOVEBOX_INFERENCE=scripted for offline tests.")


if __name__ == "__main__":
    main()
