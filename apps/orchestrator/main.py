"""Glovebox control plane: web console, missions API, live event stream, receipts.

  python3 scripts/dev.py orchestrator      # loads .env, serves http://127.0.0.1:8000

Talks to the runner (sandbox host) and to Vultr Serverless Inference, and serves the mock ERP API
on a second port (ERP_BIND) that sandboxes reach with a placeholder credential, and the three mock
vendor sites (VENDOR_BIND, ports 8101-8103) that Operator missions browse.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, vendors
from .analyst import DEFAULT_GOAL, Ctx, run_analyst
from .erp import Erp
from .operator_mission import GOAL as OPERATOR_GOAL
from .operator_mission import REQUESTER, run_operator
from .redteam import GOAL as REDTEAM_GOAL
from .redteam import run_redteam
from .inference import VultrInference
from .missions import Store
from .receipts import Signer, verify
from .runner_client import RunnerClient, RunnerError
from .scripted import ScriptedInference

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("glovebox")

MISSIONS_PER_IP_PER_HOUR = 12


class _SharedProcessServer(uvicorn.Server):
    """The ERP server shares this process; signals belong to the main server."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class State:
    ctx: Ctx
    slots: asyncio.Semaphore
    recent: dict[str, deque] = defaultdict(deque)


state = State()


@asynccontextmanager
async def lifespan(_app):
    s = config.load()
    inference = VultrInference(s) if s.inference_mode == "vultr" else ScriptedInference(s)
    await inference.start()
    runner = RunnerClient(s.runner_url, s.runner_token)
    erp = Erp(s.fixtures_dir)
    store = Store(s.data_dir)
    state.ctx = Ctx(s, config.Prompts(s.prompts_dir), inference, runner, erp, Signer(s.signing_key_b64), store)
    state.slots = asyncio.Semaphore(s.max_concurrent)

    async def on_erp_access(mission_id: str, request: str):
        mission = store.get(mission_id)
        if mission:
            await mission.events.emit("secret.used", api="ERP", request=request,
                                      detail="The ERP API received the real token. The sandbox only ever held "
                                             "$MSB_ERP_TOKEN; Microsandbox swapped it outside the VM.")

    erp.on_access = on_erp_access
    servers = [_SharedProcessServer(uvicorn.Config(erp.app, host=s.erp_bind_host, port=s.erp_bind_port, log_level="warning"))]
    servers += [_SharedProcessServer(uvicorn.Config(app, host=s.vendor_bind_host, port=port, log_level="warning", lifespan="off"))
                for port, app in vendors.apps(s.fixtures_dir)]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    log.info("inference=%s runner=%s erp=%s:%s vendors=%s:8101-8103", s.inference_mode, s.runner_url, s.erp_bind_host,
             s.erp_bind_port, s.vendor_bind_host)
    yield
    for server in servers:
        server.should_exit = True
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    await inference.close()
    await runner.close()


app = FastAPI(title="Glovebox", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=config.ROOT / "apps" / "web"), name="static")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Browsers must check the console's JS and CSS on every load, so a deploy is seen at once."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(config.ROOT / "apps" / "web" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    ctx = state.ctx
    try:
        runner = await ctx.runner.health()
    except RunnerError as e:
        runner = {"ok": False, "error": str(e)}
    return {"inference": ctx.inference.mode, "inference_base": ctx.settings.vultr_base, "roster": ctx.inference.roster,
            "runner": runner, "signing_key": {"key_id": ctx.signer.key_id, "public_key": ctx.signer.public_b64},
            "default_goal": DEFAULT_GOAL, "policy": ctx.prompts.policy,
            "templates": [{"id": "analyst", "name": "Analyst", "pattern": "code", "default_goal": DEFAULT_GOAL},
                          {"id": "operator", "name": "Operator", "pattern": "browser", "default_goal": OPERATOR_GOAL,
                           "vendors": [{k: v[k] for k in ("vendor", "name")} for v in vendors.sites(ctx.settings)],
                           "form_details": REQUESTER},
                          {"id": "redteam", "name": "Red Team", "pattern": "attacks"}]}


@app.get("/api/missions")
async def missions():
    return state.ctx.store.recent()


class MissionIn(BaseModel):
    template: Literal["analyst", "operator", "redteam"] = "analyst"
    goal: str | None = Field(default=None, min_length=10, max_length=600)
    simulate_compromise: bool = False
    erp_api: bool = True


@app.post("/api/missions", status_code=201)
async def create_mission(body: MissionIn, request: Request):
    ip = request.client.host if request.client else "?"
    window = state.recent[ip]
    now = time.time()
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= MISSIONS_PER_IP_PER_HOUR:
        raise HTTPException(429, "Mission limit reached for this address; try again later.")
    window.append(now)
    ctx = state.ctx
    if body.template == "redteam":
        mission = ctx.store.create("redteam", REDTEAM_GOAL, {})
    elif body.template == "operator":
        mission = ctx.store.create("operator", (body.goal or OPERATOR_GOAL).strip(),
                                   {"simulate_compromise": body.simulate_compromise})
    else:
        mission = ctx.store.create("analyst", (body.goal or DEFAULT_GOAL).strip(),
                                   {"simulate_compromise": body.simulate_compromise, "erp_api": body.erp_api})
    runner = {"redteam": run_redteam, "operator": run_operator}.get(body.template, run_analyst)

    async def go():
        await mission.events.emit("mission.queued", position=max(0, ctx.settings.max_concurrent - state.slots._value))
        async with state.slots:
            await runner(ctx, mission)

    asyncio.create_task(go())
    return {"id": mission.id, "events": f"/api/missions/{mission.id}/events"}


def _saved_or_404(mission_id: str) -> dict:
    saved = state.ctx.store.load_saved(mission_id)
    if not saved:
        raise HTTPException(404, "no such mission")
    return saved


@app.get("/api/missions/{mission_id}")
async def mission_detail(mission_id: str):
    m = state.ctx.store.get(mission_id)
    if m:
        return {"summary": m.summary(), "answer": m.answer, "receipt": m.receipt}
    saved = _saved_or_404(mission_id)
    return {"summary": saved["summary"], "answer": saved["answer"], "receipt": saved["receipt"]}


@app.get("/api/missions/{mission_id}/events")
async def mission_events(mission_id: str, request: Request):
    m = state.ctx.store.get(mission_id)
    saved_events = None if m else _saved_or_404(mission_id)["events"]

    async def stream():
        if saved_events is not None:
            for event in saved_events:
                yield f"data: {json.dumps(event)}\n\n"
            return
        async for event in m.events.follow():
            if await request.is_disconnected():
                return
            yield ": keepalive\n\n" if event is None else f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ApprovalIn(BaseModel):
    approve: bool


@app.post("/api/missions/{mission_id}/approvals/{approval_id}")
async def resolve_approval(mission_id: str, approval_id: str, body: ApprovalIn):
    m = state.ctx.store.get(mission_id)
    future = m.approvals.get(approval_id) if m else None
    if not future or future.done():
        raise HTTPException(404, "no pending approval with that id")
    future.set_result(body.approve)
    return {"approval": approval_id, "approved": body.approve}


@app.get("/api/missions/{mission_id}/artifacts/{name}")
async def artifact(mission_id: str, name: str):
    m = state.ctx.store.get(mission_id)
    data = m.artifacts.get(name) if m else state.ctx.store.saved_artifact(mission_id, name)
    if data is None:
        raise HTTPException(404, "no such artifact")
    return Response(data, media_type="image/png" if name.endswith(".png") else "application/octet-stream",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/missions/{mission_id}/receipt")
async def receipt(mission_id: str):
    m = state.ctx.store.get(mission_id)
    data = m.receipt if m else _saved_or_404(mission_id)["receipt"]
    if not data:
        raise HTTPException(409, "the receipt is sealed when the mission ends")
    return JSONResponse(data, headers={"Content-Disposition": f'inline; filename="{mission_id}-receipt.json"'})


@app.post("/api/verify")
async def verify_receipt(receipt: dict):
    ok, reason = verify(receipt, trusted_public_b64=state.ctx.signer.public_b64)
    return {"valid": ok, "reason": reason}
