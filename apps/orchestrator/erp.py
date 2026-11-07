"""Mock ERP API: vendor master data behind a short-lived bearer token minted per mission.

Sandboxes reach it with a placeholder credential that Microsandbox swaps for the real token
outside the VM. When this API sees a valid token, that is proof the swap happened while the
VM itself never held the secret.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Header, HTTPException

TOKEN_TTL_S = 900


class Erp:
    def __init__(self, fixtures_dir: Path):
        data = json.loads((fixtures_dir / "payments" / "erp_vendors.json").read_text(encoding="utf-8"))
        self.vendors = data["vendors"]
        self.as_of = data.get("as_of")
        self.tokens: dict[str, tuple[str, float]] = {}
        self.on_access: Callable[[str, str], Awaitable[None]] | None = None
        self.app = FastAPI(title="Mock ERP", docs_url=None, redoc_url=None)
        self.app.get("/vendors")(self.list_vendors)

    def mint(self, mission_id: str) -> str:
        token = "erp_" + secrets.token_urlsafe(24)
        self.tokens[token] = (mission_id, time.time() + TOKEN_TTL_S)
        return token

    def revoke(self, token: str) -> None:
        self.tokens.pop(token, None)

    async def list_vendors(self, authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ").strip()
        mission_id, expires = self.tokens.get(token, (None, 0.0))
        if not mission_id or expires < time.time():
            raise HTTPException(401, "invalid or expired token")
        if self.on_access:
            await self.on_access(mission_id, "GET /vendors")
        return {"as_of": self.as_of, "count": len(self.vendors), "vendors": self.vendors}
