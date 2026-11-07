#!/usr/bin/env python3
"""Save a Vultr key in .env without it showing on screen or passing through a chat.

  python3 scripts/set_key.py VULTR_INFERENCE_API_KEY   # Serverless Inference key: the AI models
  python3 scripts/set_key.py VULTR_API_KEY             # account API key: lets deploy/vultr.py create servers

Saving the inference key also sets GLOVEBOX_INFERENCE=vultr, since that is what the key is for.
"""
import getpass
import re
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parents[1] / ".env"
NAMES = {"VULTR_INFERENCE_API_KEY": "Vultr Serverless Inference key", "VULTR_API_KEY": "Vultr account API key"}


def set_var(text: str, name: str, value: str) -> str:
    line = f"{name}={value}"
    pattern = re.compile(rf"^{name}=.*$", re.M)
    return pattern.sub(lambda _m: line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in NAMES:
        raise SystemExit("Usage: python3 scripts/set_key.py " + " | ".join(NAMES))
    name = sys.argv[1]
    if not ENV.exists():
        raise SystemExit("No .env yet. Run: python3 scripts/init_env.py")
    value = getpass.getpass(f"Paste your {NAMES[name]} and press Enter (it stays hidden): ").strip().strip("'\"")
    if not value:
        raise SystemExit("Nothing pasted; .env is unchanged.")
    text = set_var(ENV.read_text(), name, value)
    if name == "VULTR_INFERENCE_API_KEY":
        text = set_var(text, "GLOVEBOX_INFERENCE", "vultr")
    ENV.write_text(text)
    ENV.chmod(0o600)
    print(f"Saved {name} in .env ({len(value)} characters).")
    if name == "VULTR_INFERENCE_API_KEY":
        print("GLOVEBOX_INFERENCE is now vultr: the orchestrator uses the real models after its next restart.")


if __name__ == "__main__":
    main()
