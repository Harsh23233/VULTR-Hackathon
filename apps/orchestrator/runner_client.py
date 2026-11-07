"""HTTP client for the runner on the sandbox host (over the Vultr VPC in production)."""
from __future__ import annotations

import base64

import httpx


class RunnerError(RuntimeError):
    pass


class RunnerClient:
    def __init__(self, url: str, token: str):
        if not token:
            raise SystemExit("Set RUNNER_TOKEN to the same value the runner uses.")
        self.http = httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {token}"},
                                      timeout=httpx.Timeout(660, connect=10))

    async def _call(self, method: str, path: str, **kw):
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RunnerError(f"runner unreachable: {e}") from e
        if r.status_code >= 400:
            raise RunnerError(f"runner {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    async def health(self) -> dict:
        return (await self._call("GET", "/v1/health")).json()

    async def create(self, *, mission_id: str, step_id: str, files: dict[str, bytes], profile: str = "code",
                     network: dict | None = None, secrets: list[dict] | None = None, env: dict | None = None) -> dict:
        body = {"profile": profile, "mission_id": mission_id, "step_id": step_id, "network": network or {"mode": "none"},
                "secrets": secrets or [], "env": env or {},
                "files": {name: base64.b64encode(data).decode() for name, data in files.items()}}
        return (await self._call("POST", "/v1/sandboxes", json=body)).json()

    async def exec(self, sid: str, argv: list[str], timeout_s: int | None = None, user: str | None = None) -> dict:
        body = {"argv": argv, "timeout_s": timeout_s, "user": user}
        return (await self._call("POST", f"/v1/sandboxes/{sid}/exec", json=body)).json()

    async def browse(self, sid: str, action: dict) -> dict:
        """One browser action; the result carries the page and base64 screenshots."""
        return (await self._call("POST", f"/v1/sandboxes/{sid}/browser", json=action)).json()

    async def outputs(self, sid: str) -> list[dict]:
        return (await self._call("GET", f"/v1/sandboxes/{sid}/outputs")).json()

    async def output(self, sid: str, name: str) -> bytes:
        return (await self._call("GET", f"/v1/sandboxes/{sid}/outputs/{name}")).content

    async def metrics(self, sid: str) -> dict:
        return (await self._call("GET", f"/v1/sandboxes/{sid}/metrics")).json()

    async def destroy(self, sid: str) -> None:
        await self._call("DELETE", f"/v1/sandboxes/{sid}")

    async def close(self):
        await self.http.aclose()
