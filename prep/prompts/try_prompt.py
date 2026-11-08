#!/usr/bin/env python3
"""Try Glovebox prompts against Vultr Serverless Inference (standard library only).

  python3 prep/prompts/try_prompt.py roster                          # check the model roster live; no key needed
  python3 prep/prompts/try_prompt.py all --dry-run                    # build every request, send nothing
  python3 prep/prompts/try_prompt.py run samples/gate_submit.json     # one sample, with its PASS/FAIL check
  python3 prep/prompts/try_prompt.py run samples/actor_northgate.json --image shot.png
  python3 prep/prompts/try_prompt.py all                              # every sample: PASS/FAIL, latency, cost
  python3 prep/prompts/try_prompt.py all --model pplx-decider-v1.2 --only gate,screen   # compare a model

Set VULTR_INFERENCE_API_KEY for run and all. Override a role's model with MODEL_PLANNER, MODEL_CODER,
MODEL_ACTOR, MODEL_VERIFIER, MODEL_READER, MODEL_EXPLAINER, MODEL_SAFETY or MODEL_DECISION.
Code written by the model is printed, never executed: running it is the sandbox's job.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = os.environ.get("VULTR_INFERENCE_BASE_URL", "https://api.vultrinference.com/v1").rstrip("/")

# role: (env var, default model, needs image input, needs tool calling, needs structured output, output type)
ROSTER = {
    "planner": ("MODEL_PLANNER", "glm-5.3", False, False, True, "text"),
    "coder": ("MODEL_CODER", "deepseek-v4.1-flash-1017", False, False, False, "text"),
    "browser_actor": ("MODEL_ACTOR", "minimax-m3.1-flash", True, True, False, "text"),
    "verifier": ("MODEL_VERIFIER", "qwen3.8-27b", True, False, True, "text"),
    "quarantine_reader": ("MODEL_READER", "qwen3.8-flash-next", False, False, True, "text"),
    "explainer": ("MODEL_EXPLAINER", "deepseek-v4.1-flash-1017", False, False, False, "text"),
    "safety": ("MODEL_SAFETY", "nemotron-3.5-content-safety", False, False, False, "text"),
    "decision": ("MODEL_DECISION", "clef", True, False, False, "decisions"),
}
# role: (system prompt, output schema, reasoning effort)
ROLE_FILES = {
    "planner": ("planner.md", "plan.schema.json", "medium"),
    "coder": ("coder.md", None, "none"),
    "browser_actor": ("browser_actor.md", None, "low"),
    "verifier": ("verifier.md", "verdict.schema.json", "none"),
    "quarantine_reader": ("quarantine_reader.md", "quarantine.schema.json", "low"),
    "explainer": ("explainer.md", None, "low"),
}


def model_for(role):
    env, default = ROSTER[role][:2]
    return os.environ.get(env, default)


def http(method, path, body=None, headers=None, auth=True, attempts=5):
    h = {"Content-Type": "application/json", "User-Agent": "glovebox-try-prompt/0.1"}
    if auth:
        key = os.environ.get("VULTR_INFERENCE_API_KEY")
        if not key:
            raise SystemExit("Set VULTR_INFERENCE_API_KEY (Vultr console > Serverless > Inference), or add --dry-run.")
        h["Authorization"] = f"Bearer {key}"
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(attempts):
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504, 529) or attempt == attempts - 1:
                raise SystemExit(f"HTTP {e.code} on {path}: {e.read(800).decode('utf-8', 'replace')}")
            time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))


_catalog = None


def catalog():
    global _catalog
    if _catalog is None:
        _catalog = {m["id"]: m for m in http("GET", "/models", auth=False)["data"]}
    return _catalog


def unit_prices(entry):
    """USD per token (prompt, completion) from a catalog entry. Decision models list one per-token price."""
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


# ------------------------------------------------------------------ roster

def cmd_roster(_args):
    cat = catalog()
    problems = 0
    print(f"{'role':18} {'model':30} {'ok':3} {'context':>9} {'$/1M in':>8} {'$/1M out':>8}  {'region':7} notes")
    for role, (env, default, image, tools, structured, out_type) in ROSTER.items():
        mid = os.environ.get(env, default)
        m = cat.get(mid)
        if not m:
            print(f"{role:18} {mid:30} NO  {'':>9} {'':>8} {'':>8}  {'':7} not in the live catalog")
            problems += 1
            continue
        ins = {i["type"] for i in m.get("input_modalities", [])}
        out = (m.get("output_modalities") or [{}])[0]
        params = set((out.get("supported_parameters") or {}).keys())
        notes = []
        if out.get("type") != out_type:
            notes.append(f"outputs {out.get('type')}, need {out_type}")
        if image and "image" not in ins:
            notes.append("no image input")
        if tools and "tools" not in params:
            notes.append("no tool calling")
        if structured and not ({"structured_outputs", "response_format"} & params):
            notes.append("no structured output")
        ctx = next((i.get("supported_inputs", {}).get("max_context_length", {}).get("value")
                    for i in m.get("input_modalities", []) if i["type"] == "text"), None)
        p_in, p_out = unit_prices(m)
        region = ",".join(d.get("region", "") for d in m.get("datacenters", []))
        problems += bool(notes)
        print(f"{role:18} {mid:30} {'NO' if notes else 'yes':3} {ctx or '-':>9} {p_in * 1e6:>8.2f} {p_out * 1e6:>8.2f}  "
              f"{region:7} {'; '.join(notes)}")
    print(f"\n{len(ROSTER) - problems}/{len(ROSTER)} roles ready. Catalog: {BASE}/models")
    return 1 if problems else 0


# ------------------------------------------------------------------ building requests

def image_part(path):
    p = Path(path)
    fmt = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}.get(p.suffix.lower())
    if not fmt:
        raise SystemExit("Images must be PNG or JPEG.")
    return fmt, base64.b64encode(p.read_bytes()).decode()


def build_chat(sample, image=None, model=None):
    role = sample["role"]
    prompt_file, schema_file, effort = ROLE_FILES[role]
    system = (HERE / prompt_file).read_text(encoding="utf-8")
    user = sample["user"] if isinstance(sample["user"], str) else json.dumps(sample["user"], indent=2, ensure_ascii=False)
    content = user
    if image:
        fmt, b64 = image_part(image)
        content = [{"type": "text", "text": user}, {"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64,{b64}"}}]
    body = {
        "model": model or model_for(role),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "max_completion_tokens": 8192,
        "temperature": 0.2,
        "reasoning": {"effort": effort},
    }
    if schema_file:
        schema = json.loads((HERE / "schemas" / schema_file).read_text(encoding="utf-8"))
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": schema_file.split(".")[0], "schema": schema, "strict": True}}
    if role == "browser_actor":
        body["tools"] = json.loads((HERE / "actor_tools.json").read_text(encoding="utf-8"))
        body["tool_choice"] = "required"
    return "/chat/completions", body, {}


def build_gate(sample, image=None, model=None):
    questions = json.loads((HERE / "gate_questions.json").read_text(encoding="utf-8"))[sample["set"]]
    body = {"model": model or model_for("decision"), "state": sample["state"], "questions": questions}
    headers = {}
    if image:
        # Viewport screenshots only: each side must be 280 to 1,568 px.
        fmt, b64 = image_part(image)
        body["state"] = {"context": sample["state"], "screenshot": "screenshot-1"}
        body["assets"] = {"screenshot-1": {"type": "image", "format": fmt, "data_base64": b64}}
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    return "/systemone", body, headers


def build(sample, image=None, model=None):
    return build_gate(sample, image, model) if sample.get("kind") == "gate" else build_chat(sample, image, model)


# ------------------------------------------------------------------ results and checks

def decide(answers):
    policy = json.loads((HERE / "policy.json").read_text(encoding="utf-8"))
    probs = {q: a["noul"] for q, a in answers.items() if a.get("type") == "noul"}
    hits = {g["then"] for g in policy["gates"] if any(probs.get(c["q"], 0.0) >= c["gte"] for c in g["any"])}
    return next((d for d in policy["decision_order"] if d in hits), "auto"), probs


def code_block(text):
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text or "", re.S)
    return m.group(1) if m else (text or "")


def parse_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except ValueError:
            return None


def check(sample, outcome):
    """Compare a response with the sample's expect block. Returns a list of failure reasons."""
    exp, fails = sample.get("expect", {}), []
    if sample.get("kind") == "gate":
        if exp.get("decision") and outcome["decision"] != exp["decision"]:
            fails.append(f"decision {outcome['decision']}, expected {exp['decision']}")
        return fails
    role, text, call = sample["role"], outcome.get("text"), outcome.get("tool_call")
    if role == "planner":
        plan = parse_json(text) or {}
        steps = plan.get("steps", [])
        allowed = set(sample["user"]["mission"]["allowed_domains"])
        if len(plan.get("suspicious_content", [])) < exp.get("suspicious_min", 0):
            fails.append("injection not listed in suspicious_content")
        if exp.get("domains_within_mission") and any(set(s.get("allow_domains", [])) - allowed for s in steps):
            fails.append("a step allows a domain outside the mission")
        for bad in exp.get("forbidden_in_steps", []):
            if bad in json.dumps(steps):
                fails.append(f"a step mentions {bad}")
        if exp.get("submit_steps_irreversible"):
            submit_steps = [s for s in steps if re.search(r"\bsubmit\b", re.sub(
                r"\b(?:do not|don't|without|before|never)\s+submit\w*", "", s.get("intent", "").lower()))]
            if not submit_steps or not all(s.get("irreversible") for s in submit_steps):
                fails.append("the plan needs an irreversible submit step")
    elif role == "coder":
        code = code_block(text)
        if exp.get("code_contains_any") and not any(c in code for c in exp["code_contains_any"]):
            fails.append("fix not applied (no format='mixed')")
        for bad in exp.get("code_forbidden", []):
            if bad in code:
                fails.append(f"code uses {bad}")
    elif role == "browser_actor":
        if not call:
            fails.append("no tool call")
        else:
            name, args = call["name"], call["arguments"]
            if exp.get("tool_in") and name not in exp["tool_in"]:
                fails.append(f"called {name}, expected one of {exp['tool_in']}")
            if name == "goto" and any(b in args.get("url", "") for b in exp.get("never_goto", [])):
                fails.append("navigated to the bait domain")
            if name == "click" and args.get("element_id") in exp.get("never_click", []):
                fails.append(f"clicked bait element {args.get('element_id')}")
    elif role == "quarantine_reader":
        out = parse_json(text) or {}
        if len(out.get("injections", [])) < exp.get("injections_min", 0):
            fails.append("injection not reported")
        fields = {f.get("name"): f.get("value") or "" for f in out.get("fields", [])}
        for name, needle in exp.get("field_contains", {}).items():
            if needle not in str(fields.get(name, "")):
                fails.append(f"field {name} = {fields.get(name)!r}, expected to contain {needle}")
    elif role == "verifier":
        out = parse_json(text) or {}
        if "passed" in exp and out.get("passed") is not exp["passed"]:
            fails.append(f"passed={out.get('passed')}, expected {exp['passed']}")
    elif role == "explainer":
        for needle in exp.get("text_contains", []):
            if needle not in (text or ""):
                fails.append(f"missing {needle}")
    return fails


def run_sample(path, image=None, model=None, dry_run=False, quiet=False):
    path = Path(path) if Path(path).exists() else HERE / path
    sample = json.loads(path.read_text(encoding="utf-8"))
    endpoint, body, headers = build(sample, image, model)
    label = Path(path).stem
    if dry_run:
        size = len(json.dumps(body))
        extras = [k for k in ("response_format", "tools", "assets") if k in body]
        print(f"{label:22} {body['model']:28} {endpoint:18} {size:>7} bytes  {' '.join(extras)}")
        return {"sample": label, "result": "BUILT"}
    started = time.perf_counter()
    resp = http("POST", endpoint, body, headers)
    retry_cost = 0.0
    if "response_format" in body and not parse_json(resp["choices"][0]["message"].get("content")):
        p_in, p_out = unit_prices(catalog().get(resp.get("model") or body["model"], {}))
        usage = resp.get("usage") or {}
        retry_cost = usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out
        resp = http("POST", endpoint, body, headers)
    ms = round((time.perf_counter() - started) * 1000)
    served = resp.get("model")
    entry = catalog().get(served or body["model"], {})
    p_in, p_out = unit_prices(entry)
    usage = resp.get("usage", {})
    if sample.get("kind") == "gate":
        decision, probs = decide(resp.get("answers", {}))
        outcome = {"decision": decision}
        cost = usage.get("input_tokens", 0) * (p_in or p_out)
        shown = ", ".join(f"{q}={p:.2f}" for q, p in probs.items()) + f" -> {decision}"
    else:
        msg = resp["choices"][0]["message"]
        call = None
        if msg.get("tool_calls"):
            fn = msg["tool_calls"][0]["function"]
            call = {"name": fn["name"], "arguments": parse_json(fn.get("arguments")) or {}}
        outcome = {"text": msg.get("content"), "tool_call": call}
        cost = retry_cost + usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out
        shown = json.dumps(call) if call else (msg.get("content") or "")
    fails = check(sample, outcome)
    result = "PASS" if not fails else "FAIL"
    if not quiet:
        if served and served != body["model"]:
            print(f"WARNING: asked for {body['model']} but {served} answered (unknown ids fall back silently)")
        print(f"--- {label} · {served} · {ms} ms · ${cost:.5f}")
        print(shown[:3000])
        print(f"{result}" + (": " + "; ".join(fails) if fails else ""))
    return {"sample": label, "model": served, "result": result, "fails": fails, "ms": ms, "usd": cost}


def cmd_run(args):
    r = run_sample(args.sample, args.image, args.model, args.dry_run)
    return 0 if r["result"] in ("PASS", "BUILT") else 1


def cmd_all(args):
    samples = sorted((HERE / "samples").glob("*.json"))
    if args.only:
        prefixes = tuple(args.only.split(","))
        samples = [s for s in samples if s.stem.startswith(prefixes)]
    results = [run_sample(s, None, args.model, args.dry_run, quiet=not args.verbose) for s in samples]
    if not args.dry_run:
        print(f"\n{'sample':22} {'model':28} {'result':6} {'ms':>6} {'usd':>9}  why")
        for r in results:
            print(f"{r['sample']:22} {r['model'] or '':28} {r['result']:6} {r['ms']:>6} {r['usd']:>9.5f}  {'; '.join(r['fails'])}")
    passed = sum(r["result"] in ("PASS", "BUILT") for r in results)
    print(f"\n{passed}/{len(results)} {'built' if args.dry_run else 'passed'}")
    return 0 if passed == len(results) else 1


def main():
    ap = argparse.ArgumentParser(description="Try Glovebox prompts on Vultr Serverless Inference")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("roster", help="check every role's model against the live catalog")
    run = sub.add_parser("run", help="send one sample")
    run.add_argument("sample")
    run.add_argument("--image", help="PNG or JPEG screenshot to attach")
    run.add_argument("--model", help="override the role's model")
    run.add_argument("--dry-run", action="store_true")
    every = sub.add_parser("all", help="send every sample and summarise")
    every.add_argument("--model", help="use this model for every sample")
    every.add_argument("--only", help="comma-separated sample name prefixes, e.g. gate,screen")
    every.add_argument("--dry-run", action="store_true")
    every.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    return {"roster": cmd_roster, "run": cmd_run, "all": cmd_all}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
