"""Vultr Serverless Inference: chat for the model roles, decisions for the gate, live catalog for prices.

Every product LLM call goes through this module, to https://api.vultrinference.com/v1.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import asdict, dataclass

import httpx

from .config import Settings

log = logging.getLogger("glovebox.inference")

# role: (needs image input, needs tool calling, needs structured output, output type)
REQUIREMENTS = {
    "planner": (False, False, True, "text"),
    "coder": (False, False, False, "text"),
    "browser_actor": (True, True, False, "text"),
    "verifier": (True, False, True, "text"),
    "quarantine_reader": (False, False, True, "text"),
    "explainer": (False, False, False, "text"),
    "safety": (False, False, False, "text"),
    "decision": (True, False, False, "decisions"),
}


class InferenceError(RuntimeError):
    pass


@dataclass
class ModelCall:
    role: str
    requested: str
    served: str
    ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0

    def event(self) -> dict:
        return asdict(self)


@dataclass
class ChatResult:
    text: str | None
    tool_call: dict | None
    call: ModelCall


@dataclass
class Decision:
    probs: dict[str, float]
    answers: dict
    call: ModelCall


def unit_prices(entry: dict) -> tuple[float, float]:
    p_in = p_out = 0.0
    for m in entry.get("input_modalities", []):
        for p in m.get("pricing") or []:
            if p.get("unit") == "token":
                p_in = float(p["cost_usd"])
    for m in entry.get("output_modalities", []):
        for p in m.get("pricing") or []:
            if p.get("unit") == "token":
                p_out = float(p["cost_usd"])
    return p_in, p_out


def roster_report(catalog: dict, models: dict[str, str]) -> list[dict]:
    rows = []
    for role, (image, tools, structured, out_type) in REQUIREMENTS.items():
        mid = models[role]
        m = catalog.get(mid)
        if not m:
            rows.append({"role": role, "model": mid, "ok": False, "notes": "not in the live catalog"})
            continue
        ins = {i["type"] for i in m.get("input_modalities", [])}
        out = (m.get("output_modalities") or [{}])[0]
        params = set((out.get("supported_parameters") or {}).keys())
        notes = []
        if out.get("type") != out_type:
            notes.append(f"outputs {out.get('type')}")
        if image and "image" not in ins:
            notes.append("no image input")
        if tools and "tools" not in params:
            notes.append("no tool calling")
        if structured and not ({"structured_outputs", "response_format"} & params):
            notes.append("no structured output")
        p_in, p_out = unit_prices(m)
        rows.append({"role": role, "model": mid, "ok": not notes, "notes": "; ".join(notes),
                     "usd_per_m_in": round(p_in * 1e6, 3), "usd_per_m_out": round(p_out * 1e6, 3),
                     "region": ",".join(d.get("region", "") for d in m.get("datacenters", []))})
    return rows


class VultrInference:
    mode = "vultr"

    def __init__(self, settings: Settings):
        self.models = settings.models
        self.http = httpx.AsyncClient(base_url=settings.vultr_base, timeout=httpx.Timeout(600, connect=20),
                                      headers={"Authorization": f"Bearer {settings.vultr_key}", "User-Agent": "glovebox/0.1"})
        self.catalog: dict[str, dict] = {}
        self.roster: list[dict] = []

    async def start(self):
        r = await self.http.get("/models")
        r.raise_for_status()
        self.catalog = {m["id"]: m for m in r.json()["data"]}
        self.roster = roster_report(self.catalog, self.models)
        bad = [r for r in self.roster if not r["ok"]]
        if bad:
            log.warning("model roster problems: %s", bad)

    async def close(self):
        await self.http.aclose()

    async def _post(self, path: str, body: dict, headers: dict | None = None, attempts: int = 5) -> dict:
        for attempt in range(attempts):
            r = await self.http.post(path, json=body, headers=headers)
            if r.status_code < 400:
                return r.json()
            if r.status_code not in (429, 502, 503, 504, 529) or attempt == attempts - 1:
                raise InferenceError(f"{path} {r.status_code}: {r.text[:500]}")
            await asyncio.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
        raise InferenceError(f"{path}: retries exhausted")

    def _cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        p_in, p_out = unit_prices(self.catalog.get(model, {}))
        return prompt_tokens * p_in + completion_tokens * p_out

    async def chat(self, role: str, system: str, user: str, *, schema: tuple[str, dict] | None = None,
                   tools: list | None = None, image_png: bytes | None = None, effort: str = "low",
                   max_tokens: int = 8192) -> ChatResult:
        model = self.models[role]
        content = user
        if image_png:
            content = [{"type": "text", "text": user},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_png).decode()}}]
        body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
                "max_completion_tokens": max_tokens, "temperature": 0.2, "reasoning": {"effort": effort}}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": schema[0], "schema": schema[1], "strict": True}}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "required"
        started = time.perf_counter()
        resp = await self._post("/chat/completions", body)
        served = resp.get("model") or model
        if served != model:
            log.warning("asked for %s, %s answered (unknown ids fall back silently)", model, served)
        usage = resp.get("usage") or {}
        call = ModelCall(role, model, served, round((time.perf_counter() - started) * 1000),
                         usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                         self._cost(served, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))
        msg = resp["choices"][0]["message"]
        tool_call = None
        if msg.get("tool_calls"):
            fn = msg["tool_calls"][0]["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            tool_call = {"name": fn["name"], "arguments": args}
        return ChatResult(msg.get("content"), tool_call, call)

    async def decide(self, questions: dict, state, image_png: bytes | None = None) -> Decision:
        model = self.models["decision"]
        body = {"model": model, "state": state, "questions": questions}
        headers = None
        if image_png:  # viewport screenshots only: each side 280 to 1,568 px
            body["state"] = {"context": state, "screenshot": "screenshot-1"}
            body["assets"] = {"screenshot-1": {"type": "image", "format": "png", "data_base64": base64.b64encode(image_png).decode()}}
            headers = {"TypeSafe-Danger-Zone-Images": "true"}
        started = time.perf_counter()
        resp = await self._post("/systemone", body, headers)
        served = resp.get("model") or model
        tokens = (resp.get("usage") or {}).get("input_tokens", 0)
        p_in, p_out = unit_prices(self.catalog.get(served, {}))
        call = ModelCall("decision", model, served, round((time.perf_counter() - started) * 1000), tokens, 0, tokens * (p_in or p_out))
        answers = resp.get("answers", {})
        probs = {q: float(a["noul"]) for q, a in answers.items() if a.get("type") == "noul" and "noul" in a}
        return Decision(probs, answers, call)
