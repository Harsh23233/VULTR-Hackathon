"""Missions, their live event logs, and approvals."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventLog:
    """Ordered events for one mission; any number of SSE clients can follow it."""

    TERMINAL = ("mission.finished", "mission.failed")

    def __init__(self, mission_id: str):
        self.mission_id = mission_id
        self.items: list[dict] = []
        self.closed = False
        self._cond = asyncio.Condition()

    async def emit(self, type_: str, **data) -> dict:
        async with self._cond:
            event = {"mission": self.mission_id, "seq": len(self.items) + 1, "ts": now_iso(), "type": type_, **data}
            self.items.append(event)
            self.closed = self.closed or type_ in self.TERMINAL
            self._cond.notify_all()
        return event

    async def follow(self, start: int = 0, keepalive_s: float = 15.0):
        """Yield events from index `start`, then new ones as they arrive; None means 'send a keepalive'."""
        idx = start
        while True:
            async with self._cond:
                try:
                    await asyncio.wait_for(self._cond.wait_for(lambda: len(self.items) > idx or self.closed), keepalive_s)
                except TimeoutError:
                    batch = None
                else:
                    batch = self.items[idx:]
            if batch is None:
                yield None
                continue
            for event in batch:
                yield event
            idx += len(batch)
            if self.closed and idx >= len(self.items):
                return


@dataclass
class Mission:
    id: str
    template: str
    goal: str
    options: dict
    created_at: str = field(default_factory=now_iso)
    status: str = "queued"
    answer: str | None = None
    receipt: dict | None = None
    cost_usd: float = 0.0
    blast_radius: dict = field(default_factory=dict)
    artifacts: dict[str, bytes] = field(default_factory=dict)
    approvals: dict[str, asyncio.Future] = field(default_factory=dict)
    events: EventLog = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.events is None:
            self.events = EventLog(self.id)

    def summary(self) -> dict:
        return {"id": self.id, "template": self.template, "goal": self.goal, "status": self.status,
                "created_at": self.created_at, "cost_usd": round(self.cost_usd, 6), "blast_radius": self.blast_radius,
                "artifacts": sorted(self.artifacts), "options": self.options, "events": len(self.events.items)}

    async def request_approval(self, timeout_s: float = 900, **info) -> bool:
        approval_id = "apr_" + secrets.token_hex(4)
        future = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = future
        await self.events.emit("approval.requested", approval=approval_id, **info)
        try:
            approved = await asyncio.wait_for(future, timeout_s)
        except TimeoutError:
            approved = False
        self.approvals.pop(approval_id, None)
        await self.events.emit("approval.resolved", approval=approval_id, approved=approved)
        return approved


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "missions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.missions: dict[str, Mission] = {}

    def create(self, template: str, goal: str, options: dict) -> Mission:
        mission = Mission("msn_" + secrets.token_hex(6), template, goal, options)
        self.missions[mission.id] = mission
        return mission

    def get(self, mission_id: str) -> Mission | None:
        return self.missions.get(mission_id)

    def recent(self, limit: int = 20) -> list[dict]:
        live = [m.summary() for m in self.missions.values()]
        known = {m["id"] for m in live}
        saved = []
        for path in sorted(self.dir.glob("msn_*/mission.json"), reverse=True):
            if path.parent.name not in known:
                try:
                    saved.append(json.loads(path.read_text())["summary"])
                except (ValueError, KeyError):
                    continue
        return sorted(live + saved, key=lambda m: m["created_at"], reverse=True)[:limit]

    def persist(self, mission: Mission) -> None:
        folder = self.dir / mission.id
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in mission.artifacts.items():
            (folder / name).write_bytes(data)
        (folder / "mission.json").write_text(json.dumps({
            "summary": mission.summary(), "answer": mission.answer, "receipt": mission.receipt,
            "events": mission.events.items}, indent=1), encoding="utf-8")

    def load_saved(self, mission_id: str) -> dict | None:
        path = self.dir / mission_id / "mission.json"
        return json.loads(path.read_text()) if path.exists() else None

    def saved_artifact(self, mission_id: str, name: str) -> bytes | None:
        path = self.dir / mission_id / name
        return path.read_bytes() if path.is_file() and path.parent == self.dir / mission_id else None
