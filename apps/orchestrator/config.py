"""Control-plane settings, read from the environment (scripts/dev.py loads .env)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# role: (env var, default model)
ROLES = {
    "planner": ("MODEL_PLANNER", "glm-5.3"),
    "coder": ("MODEL_CODER", "deepseek-v4.1-flash-1017"),
    "browser_actor": ("MODEL_ACTOR", "minimax-m3.1-flash"),
    "verifier": ("MODEL_VERIFIER", "qwen3.8-27b"),
    "quarantine_reader": ("MODEL_READER", "qwen3.8-flash-next"),
    "explainer": ("MODEL_EXPLAINER", "deepseek-v4.1-flash-1017"),
    "safety": ("MODEL_SAFETY", "nemotron-3.5-content-safety"),
    "decision": ("MODEL_DECISION", "clef"),
}


@dataclass
class Settings:
    inference_mode: str
    vultr_key: str
    vultr_base: str
    models: dict[str, str]
    runner_url: str
    runner_token: str
    erp_bind_host: str
    erp_bind_port: int
    erp_guest_host: str
    erp_guest_port: int
    erp_scheme: str
    vendor_bind_host: str
    vendor_guest_host: str
    vendor_domain: str
    signing_key_b64: str
    public_base_url: str
    max_concurrent: int
    data_dir: Path
    prompts_dir: Path
    fixtures_dir: Path
    web_dir: Path


def load() -> Settings:
    env = os.environ.get
    bind_host, _, bind_port = env("ERP_BIND", "127.0.0.1:8090").rpartition(":")
    key = env("VULTR_INFERENCE_API_KEY", "")
    mode = env("GLOVEBOX_INFERENCE", "vultr")
    if mode not in ("vultr", "scripted"):
        raise SystemExit("GLOVEBOX_INFERENCE must be vultr or scripted")
    if mode == "vultr" and not key:
        raise SystemExit("Set VULTR_INFERENCE_API_KEY in .env, or GLOVEBOX_INFERENCE=scripted for offline tests.")
    return Settings(
        inference_mode=mode,
        vultr_key=key,
        vultr_base=env("VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1").rstrip("/"),
        models={role: env(var, default) for role, (var, default) in ROLES.items()},
        runner_url=env("RUNNER_URL", "http://127.0.0.1:8081"),
        runner_token=env("RUNNER_TOKEN", ""),
        erp_bind_host=bind_host or "127.0.0.1",
        erp_bind_port=int(bind_port or 8090),
        erp_guest_host=env("ERP_GUEST_HOST", "host.microsandbox.internal"),
        erp_guest_port=int(env("ERP_GUEST_PORT", "8090")),
        erp_scheme=env("ERP_SCHEME", "http"),
        vendor_bind_host=env("VENDOR_BIND", "127.0.0.1"),
        vendor_guest_host=env("VENDOR_GUEST_HOST", "host.microsandbox.internal"),
        vendor_domain=env("VENDOR_DOMAIN", "vendors.glovebox.test"),
        signing_key_b64=env("RECEIPT_SIGNING_KEY", ""),
        public_base_url=env("PUBLIC_BASE_URL", "http://127.0.0.1:8000"),
        max_concurrent=int(env("MAX_CONCURRENT_MISSIONS", "2")),
        data_dir=Path(env("DATA_DIR", ROOT / "data")),
        prompts_dir=Path(env("PROMPTS_DIR", ROOT / "prep" / "prompts")),
        fixtures_dir=Path(env("FIXTURES_DIR", ROOT / "prep" / "fixtures")),
        web_dir=ROOT / "apps" / "web",
    )


class Prompts:
    """System prompts, schemas, tools, gate questions and policy, from prep/prompts."""

    def __init__(self, folder: Path):
        read = lambda name: (folder / name).read_text(encoding="utf-8")  # noqa: E731
        self.system = {role: read(f"{role}.md") for role in
                       ("planner", "coder", "browser_actor", "verifier", "quarantine_reader", "explainer")}
        self.schemas = {name: json.loads(read(f"schemas/{name}.schema.json")) for name in ("plan", "verdict", "quarantine")}
        self.actor_tools = json.loads(read("actor_tools.json"))
        self.gate = json.loads(read("gate_questions.json"))
        self.policy = json.loads(read("policy.json"))
