"""Glovebox runner: the only process that touches sandboxes. It runs on the sandbox host.

  .venv/bin/uvicorn apps.runner.main:app --host 127.0.0.1 --port 8081

Every call needs `Authorization: Bearer $RUNNER_TOKEN`. The runner holds no platform keys:
task-scoped credentials arrive with a create request and live only in Microsandbox's host-side
proxy while that sandbox exists. In production it listens on the Vultr VPC address only.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import logging
import os
import platform
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .sandboxes import (ROOT, NetworkSpec, SandboxPool, SecretSpec, cleanup_stale, ensure_browser_image, ensure_bytecode,
                        guest_arch)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("glovebox.runner")

TOKEN = os.environ.get("RUNNER_TOKEN", "")
PYDEPS = Path(os.environ.get("PYDEPS_DIR", ROOT / ".pydeps" / guest_arch()))
BROWSER_DEPS = Path(f"{PYDEPS}-browser")
pool = SandboxPool(PYDEPS, max_boxes=int(os.environ.get("MAX_SANDBOXES", "4")))
state = {"bytecode": "pending", "browser": "pending", "stale_removed": 0}
HOSTNAME = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"


@asynccontextmanager
async def lifespan(_app):
    if not TOKEN:
        raise SystemExit("Set RUNNER_TOKEN (any long random string shared with the orchestrator).")
    if not (PYDEPS / "pandas").exists():
        raise SystemExit(f"Missing {PYDEPS}. Run: python3 scripts/fetch_pydeps.py")
    state["stale_removed"] = await cleanup_stale()

    async def warm():
        try:
            state["bytecode"] = await ensure_bytecode(PYDEPS)
        except Exception as e:
            state["bytecode"] = f"skipped: {e}"
        log.info("guest bytecode: %s", state["bytecode"])
        if not (BROWSER_DEPS / "playwright").exists():
            state["browser"] = f"unavailable: missing {BROWSER_DEPS.name} (python3 scripts/fetch_pydeps.py)"
            return
        try:
            state["browser"] = await ensure_browser_image()
        except Exception as e:
            state["browser"] = f"unavailable: {e}"
        log.info("browser image: %s", state["browser"])

    tasks = [asyncio.create_task(warm()), asyncio.create_task(pool.reap())]
    yield
    for t in tasks:
        t.cancel()
    for sid in list(pool.boxes):
        await pool.destroy(sid)


app = FastAPI(title="Glovebox runner", lifespan=lifespan)


def authorized(authorization: str = Header(default="")):
    if not secrets.compare_digest(authorization, f"Bearer {TOKEN}"):
        raise HTTPException(401, "bad runner token")


class SiteIn(BaseModel):
    name: str = Field(max_length=253, pattern=HOSTNAME)  # the name the browser uses
    host: str = Field(min_length=1, max_length=253)  # where it is served: host.microsandbox.internal, an IP or a domain
    port: int = Field(ge=1, le=65535)


class NetworkIn(BaseModel):
    mode: Literal["none", "api", "browse"] = "none"
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    scheme: Literal["http", "https"] = "http"
    decoy_host: str | None = None
    decoy_port: int | None = Field(default=None, ge=1, le=65535)
    sites: list[SiteIn] = Field(default=[], max_length=8)


class SecretIn(BaseModel):
    env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,40}$")
    value: str = Field(min_length=8, max_length=512)
    allow_host: str


class CreateIn(BaseModel):
    profile: Literal["code", "redteam", "browser"] = "code"
    mission_id: str = Field(pattern=r"^[a-z0-9_-]{4,64}$")
    step_id: str = Field(default="s1", pattern=r"^[a-z0-9_-]{1,16}$")
    network: NetworkIn = NetworkIn()
    secrets: list[SecretIn] = []
    env: dict[str, str] = {}
    files: dict[str, str] = {}  # file name -> base64


class ExecIn(BaseModel):
    argv: list[str] = Field(min_length=1)
    timeout_s: int | None = Field(default=None, ge=1, le=600)
    user: str | None = None


class FilesIn(BaseModel):
    files: dict[str, str]


class BrowserIn(BaseModel):
    action: Literal["observe", "goto", "click", "type_text", "select_option", "scroll"]
    url: str | None = Field(default=None, max_length=2000, pattern=r"^https?://")
    element_id: int | None = Field(default=None, ge=1, le=10000)
    text: str | None = Field(default=None, max_length=2000)
    label: str | None = Field(default=None, max_length=200)
    direction: Literal["up", "down"] = "down"


def _decode(files: dict[str, str]) -> dict[str, bytes]:
    out = {name: base64.b64decode(data) for name, data in files.items()}
    if sum(map(len, out.values())) > 20 * 1024 * 1024:
        raise HTTPException(413, "files over 20 MiB")
    return out


def _known(sid: str):
    if sid not in pool.boxes:
        raise HTTPException(404, f"unknown sandbox {sid}")


@app.get("/v1/health")
async def health():
    return {"ok": True, "driver": "microsandbox", "sdk": importlib.metadata.version("microsandbox"),
            "guest_arch": guest_arch(), "running": len(pool.boxes), "max_sandboxes": pool.max_boxes,
            "created_total": pool.created_total, "bytecode": state["bytecode"], "browser": state["browser"],
            "stale_removed": state["stale_removed"],
            "host": {"system": platform.system(), "release": platform.release(), "cpus": os.cpu_count(),
                     "load1": round(os.getloadavg()[0], 2)}}


@app.post("/v1/sandboxes", dependencies=[Depends(authorized)])
async def create(body: CreateIn):
    for s in body.secrets:
        if body.network.mode != "api" or s.allow_host != body.network.host:
            raise HTTPException(400, "a secret may only be bound to the sandbox's one allowed API host")
    if body.network.decoy_host and body.profile != "redteam":
        raise HTTPException(400, "decoy hosts are only for red-team sandboxes")
    if (body.network.mode == "browse") != (body.profile == "browser") or (body.profile == "browser" and not body.network.sites):
        raise HTTPException(400, "browser sandboxes need a browse network with at least one site, and only they get one")
    if body.profile == "browser" and (body.secrets or body.files):
        raise HTTPException(400, "browser sandboxes hold no secrets and no files")
    if body.profile == "browser" and not state["browser"].startswith("ready"):
        raise HTTPException(503, f"browser sandboxes are not available yet: {state['browser']}")
    try:
        return await pool.create(body.profile, body.mission_id, body.step_id,
                                 NetworkSpec(**body.network.model_dump()),
                                 [SecretSpec(**s.model_dump()) for s in body.secrets], body.env, _decode(body.files))
    except RuntimeError as e:
        raise HTTPException(429, str(e))


@app.post("/v1/sandboxes/{sid}/files", dependencies=[Depends(authorized)])
async def put_files(sid: str, body: FilesIn):
    _known(sid)
    return await pool.put_files(sid, _decode(body.files))


@app.post("/v1/sandboxes/{sid}/exec", dependencies=[Depends(authorized)])
async def run(sid: str, body: ExecIn):
    _known(sid)
    return await pool.exec(sid, body.argv, body.timeout_s, body.user)


@app.post("/v1/sandboxes/{sid}/browser", dependencies=[Depends(authorized)])
async def browse(sid: str, body: BrowserIn):
    _known(sid)
    if body.action == "goto" and not body.url:
        raise HTTPException(400, "goto needs a url")
    try:
        return await pool.browse(sid, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/v1/sandboxes/{sid}/outputs", dependencies=[Depends(authorized)])
async def outputs(sid: str):
    _known(sid)
    return await pool.outputs(sid)


@app.get("/v1/sandboxes/{sid}/outputs/{name}", dependencies=[Depends(authorized)])
async def output(sid: str, name: str):
    _known(sid)
    try:
        data = pool.output(sid, name)
    except KeyError:
        raise HTTPException(404, "list outputs first")
    media = "image/png" if name.endswith(".png") else "application/octet-stream"
    return Response(data, media_type=media)


@app.get("/v1/sandboxes/{sid}/metrics", dependencies=[Depends(authorized)])
async def metrics(sid: str):
    _known(sid)
    return await pool.metrics(sid)


@app.delete("/v1/sandboxes/{sid}", dependencies=[Depends(authorized)])
async def destroy(sid: str):
    await pool.destroy(sid)
    return {"destroyed": sid}
