"""Operator mission (Pattern B): a real Chromium in a microVM, driven one action at a time by the
browser-actor model across three vendor sites. Every page is screened for injected instructions
before the actor reads it, every action is risk-scored by the decision model, anything irreversible
waits for a human, and every screenshot is hashed into the signed receipt."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from decimal import Decimal
from urllib.parse import urljoin, urlparse

from . import policy, vendors
from .analyst import INSTRUCTION_HINT, QUARANTINED, AnalystRun, Ctx, parse_json, run_mission
from .missions import Mission
from .receipts import canonical, sha256

GOAL = ("Price 20 ErgoMesh 300 task chairs at our three approved vendors and draft a purchase request at the cheapest. "
        "Submit it only after I approve.")
# Supplied by the user for vendor forms, so typing these values is expected (fictional test data).
REQUESTER = {"Company": "Fernwood Labs", "Requester name": "Jordan Ellis", "Work email": "jordan.ellis@fernwood-labs.test",
             "Cost center": "CC-420 Workplace", "Delivery address": "Fernwood Labs, Receiving dock B, 410 Alder Street, Seattle, WA 98104"}
TURNS_PER_STEP = 12
MAX_TURNS = 50
IRREVERSIBLE_TEXT = re.compile(r"(?i)\b(submit|place order|pay|buy now|confirm order|checkout|delete|send)\b")
CONTROL_SITE = "https://example.com/"  # a real public site: shows refusals come from the VM's policy, not from DNS
URL = re.compile(r"https?://[^\s\"'<>)]+")
ROLE = {"a": "link", "button": "button", "select": "select", "textarea": "textbox", "input": "textbox"}
NET_ERRORS = {"ERR_NAME_NOT_RESOLVED": "the name does not resolve",
              "ERR_CONNECTION_REFUSED": "connection refused by the browser VM's network policy"}


def element_line(e: dict, flagged: set[str]) -> str:
    name = e.get("name", "")
    if name and (name in flagged or (len(name) >= 20 and any(name in f for f in flagged))):
        name = QUARANTINED
    role = ROLE.get(e.get("tag"), e.get("tag"))
    if e.get("tag") == "input" and e.get("type") in ("checkbox", "radio", "submit", "button"):
        role = e["type"]
    line = f'[{e["id"]}] {role} "{name}"'
    if e.get("href"):
        line += f" -> {e['href']}"
    if "value" in e and e.get("tag") != "button":
        line += f' value="{e.get("value") or ""}"'
    if e.get("options"):
        line += " options: " + " | ".join(e["options"])
    if e.get("below"):
        line += " (below the fold)"
    return line


def why_refused(failure: str) -> str:
    code = failure.removeprefix("net::")
    return f"{NET_ERRORS.get(code, 'refused')} ({failure})"


def page_value(page: dict, label: str) -> str:
    lines = [line.strip() for line in (page.get("text") or "").splitlines() if line.strip()]
    return next((value for name, value in zip(lines, lines[1:]) if name == label), "")


class OperatorRun(AnalystRun):
    def __init__(self, ctx: Ctx, mission: Mission):
        super().__init__(ctx, mission)
        self.sites = vendors.sites(self.s)
        self.allowed = {site["name"] for site in self.sites}
        self.box: dict | None = None
        self.page: dict | None = None  # the browser's last report: url, title, elements, text
        self.png: bytes | None = None  # and its screenshot
        self.shot_url: str | None = None
        self.turns = 0
        self.screened: set[str] = set()
        self.flagged: set[str] = set()
        self.detoured = False
        self.results: list[dict] = []
        self.quotes: dict[str, Decimal] = {}
        self.approvals: list[dict] = []

    # ------------------------------------------------------------ flow

    async def run(self):
        m = self.m
        await self.ev.emit("mission.started", goal=m.goal, template="operator", inference=self.ai.mode, options=m.options,
                           models=self.ai.models, files=[], requester=list(REQUESTER),
                           vendors=[{k: s[k] for k in ("vendor", "name", "url")} for s in self.sites])
        self.chain.add("mission", template="operator", goal_sha256=sha256(m.goal), inference=self.ai.mode, models=self.ai.models,
                       options=m.options, allowed_domains=sorted(self.allowed), requester_sha256=sha256(canonical(REQUESTER)))
        plan = await self.plan()
        info = await self.ctx.runner.create(
            profile="browser", mission_id=m.id, step_id="browser", files={},
            network={"mode": "browse", "sites": [{k: s[k] for k in ("name", "host", "port")} for s in self.sites]})
        self.box = info
        await self.ev.emit("sandbox.created", step="browser", attempt=1, purpose="browser", sandbox=info)
        self.chain.add("sandbox", sandbox=info["id"], isolation=info["isolation"], kernel=info["kernel"], image=info["image"],
                       policy_sha256=info["policy_sha256"], allow=info["network"]["allow"])
        stop = asyncio.Event()
        poller = asyncio.create_task(self.poll_metrics(info["id"], stop, every=1.0))
        try:
            for step in plan["steps"]:
                if self.turns >= MAX_TURNS:
                    await self.end_step(step, "skipped: the mission's action budget is spent", 0)
                    continue
                await self.run_step(step)
        finally:
            stop.set()
            await poller
            await self.ctx.runner.destroy(info["id"])
            await self.ev.emit("sandbox.destroyed", step="browser", sandbox=info["id"])
        await self.explain()

    async def plan(self) -> dict:
        brief = {"goal": self.m.goal,
                 "mission": {"allowed_domains": sorted(self.allowed), "apis": [], "files": [],
                             "vendors": [{"name": s["vendor"], "domain": s["name"], "start_url": s["url"]} for s in self.sites],
                             "form_details_supplied_by_user": list(REQUESTER)},
                 "untrusted": {}}
        res = await self.ai.chat("planner", self.p.system["planner"], json.dumps(brief, indent=1),
                                 schema=("plan", self.p.schemas["plan"]), effort="medium")
        await self.model(res.call)
        plan = parse_json(res.text)
        if not plan or not isinstance(plan.get("steps"), list) or not plan["steps"]:
            raise RuntimeError(f"planner returned no usable plan: {(res.text or '')[:300]}")
        kept = []
        for step in plan["steps"][:8]:
            outside = set(step.get("allow_domains") or []) - self.allowed
            if outside:
                await self.contain("policy", f"Step {step.get('id')} asked for {', '.join(sorted(outside))}, outside this "
                                             "mission; step dropped")
                continue
            if step.get("kind") != "browser":
                await self.ev.emit("step.skipped", step=step.get("id"), reason="Operator missions run browser steps only")
                continue
            step["allow_domains"] = sorted(step.get("allow_domains") or self.allowed)
            kept.append(step)
        plan["steps"] = kept
        await self.ev.emit("plan.created", plan=plan)
        for item in plan.get("suspicious_content") or []:
            await self.contain("planner", f"Planner flagged {item.get('source')}: {item.get('why')}", excerpt=str(item.get("excerpt"))[:300])
        self.chain.add("plan", plan_sha256=sha256(canonical(plan)), suspicious=len(plan.get("suspicious_content") or []),
                       steps=[{k: s.get(k) for k in ("id", "kind", "allow_domains", "irreversible")} for s in kept])
        return plan

    async def run_step(self, step):
        sid = step["id"]
        allow = set(step["allow_domains"])
        default_workflow = self.m.goal == GOAL
        quote = default_workflow and len(allow) == 1 and not step.get("irreversible")
        if default_workflow:
            verified = {result["step"] for result in self.results if result["verified"]}
            if set(step.get("depends_on") or []) - verified:
                return await self.end_step(step, "skipped: a prerequisite step was not verified", 0)
            if not quote:
                if self.quotes.keys() != self.allowed:
                    return await self.end_step(step, "skipped: all three verified cart quotes are required", 0)
                cheapest = min(self.quotes, key=lambda host: (self.quotes[host], host))
                allow &= {cheapest}
                if not allow:
                    return await self.end_step(step, "skipped: the lowest-total vendor is outside this step's allowlist", 0)
        await self.ev.emit("step.started", step=sid, intent=step.get("intent", ""), allow_domains=sorted(allow),
                           irreversible=bool(step.get("irreversible")))
        history: list[str] = []
        note = None
        start = next((u for u in step.get("inputs") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))
                      and urlparse(u).hostname in allow), None)
        if quote:
            start = next(s["url"] for s in self.sites if s["name"] in allow)
        elif default_workflow:
            # The draft uses the verified cart. Submit keeps the filled form open.
            start = None
        if start:  # open the step's start page first, gated like any action, so it is screened before the actor reads it
            outcome = await self.act(step, allow, 0, "goto", {"url": start})
            history.append(f"0. {outcome['summary']}")
            note = outcome.get("note")
        for turn in range(1, TURNS_PER_STEP + 1):
            if self.turns >= MAX_TURNS:
                break
            self.turns += 1
            res = await self.ai.chat("browser_actor", self.p.system["browser_actor"], self.brief(step, allow, history, note),
                                     tools=self.p.actor_tools, image_png=self.png)
            await self.model(res.call)
            call, note = res.tool_call, None
            if not call:
                history.append(f"{turn}. no tool call")
                note = "You did not call a tool. Call exactly one tool."
                continue
            name, args = call["name"], call.get("arguments") or {}
            if name == "done":
                if quote:
                    page = self.page or {}
                    location = urlparse(page.get("url", ""))
                    total = page_value(page, "Order total")
                    qty = next((e.get("value") for e in page.get("elements", []) if e.get("name") == "Quantity"), None)
                    facts = {str(f.get("name", "")).lower(): str(f.get("value", "")) for f in args.get("facts", [])
                             if isinstance(f, dict)}
                    vendor = next(s["vendor"] for s in self.sites if s["name"] in allow)
                    if (location.hostname not in allow or location.path != "/cart.html" or qty != "20"
                            or not re.fullmatch(r"\$[\d,]+\.\d{2}", total) or facts.get("order total") != total
                            or facts.get("vendor") != vendor):
                        note = f"Finish only when {vendor}'s cart shows 20 chairs. Report Vendor as {vendor} and copy the Order total exactly."
                        continue
                    if await self.finish_step(step, args, turn):
                        self.quotes[location.hostname] = Decimal(total[1:].replace(",", ""))
                    return
                if default_workflow:
                    note = "Finish the current step on the vendor page before calling done."
                    continue
                return await self.finish_step(step, args, turn)
            if name == "report_injection":
                text = str(args.get("text", ""))[:400]
                await self.ev.emit("actor.reported", step=sid, turn=turn, text=text, location=str(args.get("location", ""))[:200])
                await self.contain("actor", "The browser actor reported text that tries to instruct it", excerpt=text)
                self.chain.add("report", step=sid, turn=turn, text_sha256=sha256(text))
                history.append(f"{turn}. report_injection -> recorded")
                note = "Recorded. Carry on with the step."
                continue
            before = self.page
            outcome = await self.act(step, allow, turn, name, args)
            history.append(f"{turn}. {outcome['summary']}")
            note = outcome.get("note")
            if before and self.page and not note and not outcome.get("stop"):
                if all(before.get(k) == self.page.get(k) for k in ("url", "text", "elements")):
                    note = "That action left the page unchanged. Choose a different action to advance the step."
            if outcome.get("stop"):
                return await self.end_step(step, outcome["stop"], turn)
            if default_workflow and not quote:
                facts = self.purchase_facts(step, allow)
                if facts:
                    return await self.finish_step(step, {"facts": facts}, turn)
        await self.end_step(step, "incomplete: the step ran out of actions", len(history))

    def request_form_ready(self, host: str) -> bool:
        page = self.page or {}
        location = urlparse(page.get("url", ""))
        fields = {e.get("name"): e.get("value") for e in page.get("elements", [])}
        return (host in self.quotes and location.hostname == host and location.path == "/request.html"
                and page_value(page, "Quantity") == "20" and page_value(page, "Order total") == f"${self.quotes[host]:,.2f}"
                and all(fields.get(label) == value for label, value in REQUESTER.items()))

    def purchase_facts(self, step, allow) -> list[dict]:
        page = self.page or {}
        location = urlparse(page.get("url", ""))
        host = location.hostname
        if host not in allow:
            return []
        vendor = next(s["vendor"] for s in self.sites if s["name"] == host)
        facts = {"Vendor": vendor, "Order total": page_value(page, "Order total")}
        if step.get("irreversible"):
            if location.path != "/confirm.html" or not any(a["step"] == step["id"] and a["approved"] for a in self.approvals):
                return []
            ref = next((line for line in (page.get("text") or "").splitlines() if re.fullmatch(r"PR-\d{6}", line)), "")
            if not ref:
                return []
            facts["Purchase request"] = ref
        else:
            if not self.request_form_ready(host):
                return []
            facts["Form"] = "filled in, awaiting submission"
            facts.update(REQUESTER)
        return [{"name": name, "value": value} for name, value in facts.items()]

    async def act(self, step, allow, turn, name, args) -> dict:
        sid = step["id"]
        page = self.page or {"url": "about:blank", "elements": []}
        el = None
        if name in ("click", "type_text", "select_option"):
            el = next((e for e in page.get("elements") or [] if e["id"] == args.get("element_id")), None)
            if not el:
                return {"summary": f"{name} [{args.get('element_id')}] -> no such element",
                        "note": f"There is no element [{args.get('element_id')}] on this page. Use an id from ELEMENTS."}
        if name == "click" and el.get("href"):
            current = urlparse(page.get("url", ""))
            target_url = urlparse(urljoin(current.geturl(), el["href"]))
            if self.m.goal == GOAL and current.hostname in self.allowed and current.path in ("/", "/index.html"):
                if target_url.hostname == current.hostname and target_url.path == "/index.html" and not target_url.query:
                    return {"summary": "click -> this link reloads the product page",
                            "note": "This link reloads the product page. Use Quantity and Add to cart to get the delivered total."}
        if name == "goto":
            target = str(args.get("url", ""))
            if target.startswith("https://") and urlparse(target).hostname in self.allowed:  # the mock sites are plain HTTP
                target = "http://" + target[len("https://"):]
                args = {**args, "url": target}
        elif name == "scroll":
            target = f"page {args.get('direction', 'down')}"
        else:
            target = f'{ROLE.get(el["tag"], el["tag"])} "{el.get("name", "")}"'
        host = urlparse(target).hostname if name == "goto" else None
        link_host = urlparse(urljoin(page.get("url", ""), el["href"])).hostname if name == "click" and el.get("href") else None
        outside = (name == "goto" and host not in allow) or (link_host is not None and link_host not in allow)
        host = host or link_host
        reasons = [f"{host or target} is not on this step's allowlist"] if outside else []
        if args.get("irreversible"):
            reasons.append("the actor marked it irreversible")
        if el and IRREVERSIBLE_TEXT.search(el.get("name", "")):
            reasons.append(f'"{el["name"]}" matches the submit, pay or send rule')
        invalid_request = False
        if self.m.goal == GOAL and name == "click" and el.get("name") == "Submit purchase request":
            invalid_request = not step.get("irreversible") or not self.request_form_ready(next(iter(allow)))
            if invalid_request:
                reasons.append("Submit requires the filled form for the verified lowest-total cart in the submit step")
        proposed = {"type": name, "target": target}
        if name == "type_text":
            proposed["text"] = str(args.get("text", ""))
        if name == "select_option":
            proposed["option"] = str(args.get("label", ""))
        state = {"goal": self.m.goal, "step": step.get("intent", ""), "page": page.get("url"), "proposed_action": proposed}
        decision = await self.ai.decide(self.p.gate["action_gate"], state)
        await self.model(decision.call)
        typed = proposed.get("text", proposed.get("option"))
        exempt = frozenset({"credential_use"}) if typed is not None and typed.strip() in REQUESTER.values() else frozenset()
        verdict = policy.decide(decision.probs, self.p.policy, exempt)
        if outside or invalid_request:
            verdict = "block_and_replan"
        elif reasons and verdict == "auto":
            verdict = "require_approval"
        await self.ev.emit("gate.scored", step=sid, turn=turn, action=proposed, probs=decision.probs, answers=decision.answers,
                           decision=verdict, exempt=sorted(exempt), reasons=reasons,
                           exempt_reason="typing a value the user supplied for this form" if exempt else None)
        self.chain.add("gate", step=sid, turn=turn, tool=name, target_sha256=sha256(target),
                       probs={k: round(v, 4) for k, v in decision.probs.items()}, decision=verdict)
        if verdict == "block_and_replan":
            why = "; ".join(reasons) or f"off task p = {decision.probs.get('off_task', 0):.2f}"
            await self.contain("policy", f"Blocked {name} {target}: {why}")
            return {"summary": f"{name} {target} -> blocked by policy ({why})",
                    "note": f"Blocked by policy: {why}. Stay on the goal and the allowed domains."}
        if verdict == "require_approval":
            approved = await self.m.request_approval(timeout_s=600, step=sid, turn=turn, action={**proposed, "page": page.get("url")},
                                                     probs=decision.probs, reasons=reasons, screenshot=self.shot_url)
            self.approvals.append({"step": sid, "action": proposed, "approved": approved})
            self.chain.add("approval", step=sid, turn=turn, approved=approved)
            if not approved:
                return {"summary": f"{name} {target} -> rejected by a human", "stop": "stopped: a human rejected the action"}
        action = {"action": name, "url": args.get("url"), "element_id": args.get("element_id"), "text": args.get("text"),
                  "label": args.get("label"), "direction": args.get("direction")}
        after = await self.browse(sid, f"{turn:02d}", {k: v for k, v in action.items() if v is not None}, target)
        result = f"now at {after.get('url')}" + (f"; error: {after['error']}" if after.get("error") else "")
        return {"summary": f"{name} {target} -> {result}",
                "note": f"The browser reported: {after['error']}" if after.get("error") else None}

    async def browse(self, sid: str, tag: str, action: dict, target: str, purpose: str = "step") -> dict:
        """Run one action in the browser VM, keep its screenshot(s), audit its requests, screen the new page."""
        r = await self.ctx.runner.browse(self.box["id"], action)
        if r.get("terminated"):
            raise RuntimeError(f"the browser VM was terminated: {r.get('system')}")
        page = r.get("page")
        if not page:
            page = {**(self.page or {}), "error": f"the browser step failed (exit {r.get('exit_code')}): {r.get('stderr', '')[-300:]}"}
            await self.ev.emit("browser.action", step=sid, tag=tag, purpose=purpose, tool=action["action"], target=target,
                               url=page.get("url"), title=page.get("title"), error=page["error"], duration_ms=r.get("duration_ms"))
            return page
        refused = None
        png = base64.b64decode(r["screenshot"]) if r.get("screenshot") else None
        if r.get("screenshot_restored"):  # the first screenshot is the error page; the browser went back after it
            refused = self.save(f"{sid}-{tag}-refused.png", png, sid)
            png = base64.b64decode(r["screenshot_restored"])
        self.page, self.png = page, png
        self.shot_url = self.save(f"{sid}-{tag}.png", png, sid) if png else None
        blocked = await self.audit_requests(page)
        await self.ev.emit("browser.action", step=sid, tag=tag, purpose=purpose, tool=action["action"], target=target,
                           url=page.get("url"), title=page.get("title"), error=page.get("error"), duration_ms=r["duration_ms"],
                           screenshot=self.shot_url, refused_screenshot=refused, failed_page=page.get("failed_page"),
                           elements=len(page.get("elements") or []), blocked=blocked)
        self.chain.add("browser", step=sid, tag=tag, purpose=purpose, tool=action["action"], target_sha256=sha256(target),
                       url=page.get("url"), error=page.get("error"), screenshot_sha256=sha256(png) if png else None,
                       blocked=blocked)
        await self.screen(page)
        if self.m.options.get("simulate_compromise") and not self.detoured and purpose == "step" and self.detour_url():
            await self.detour(sid, self.detour_url())  # the browser ends up back on this page
        return page

    def save(self, name: str, data: bytes, sid: str) -> str:
        self.m.artifacts[name] = data
        return f"/api/missions/{self.m.id}/artifacts/{name}"

    async def audit_requests(self, page: dict) -> list[str]:
        blocked: dict[str, str] = {}
        reached = set()
        for q in page.get("requests") or []:
            host = q.get("host") or ""
            if not host or host in self.allowed:
                continue
            if q.get("failure"):
                blocked[host] = why_refused(q["failure"])
            elif q.get("status") == 403:
                blocked[host] = "HTTP 403 from the sandbox network policy"
            elif isinstance(q.get("status"), int) and q["status"] < 400:
                reached.add(host)
        for host, how in blocked.items():
            self.blast["egress_blocked"] += 1
            await self.contain("network", f"{host}: {how}", sandbox=self.box["id"], host=host)
        for host in reached:
            self.blast["hosts_reached_outside_allowlist"] += 1
            await self.contain("alarm", f"{host} answered the browser VM; it is not on the allowlist", host=host)
        return sorted(blocked)

    async def screen(self, page: dict):
        """Send lines that look like instructions to the decision model; quarantined ones never reach the actor."""
        lines = list(dict.fromkeys(line.strip() for line in (page.get("text") or "").splitlines() if line.strip()))
        new = [line for line in lines if line not in self.screened]
        self.screened.update(new)
        candidates = [line for line in new if len(line) >= 20 and INSTRUCTION_HINT.search(line)][:16]
        if not candidates:
            return
        decisions = await asyncio.gather(*(self.ai.decide(self.p.gate["injection_screen"], c) for c in candidates))
        flagged = []
        for text, decision in zip(candidates, decisions):
            await self.model(decision.call)
            p = decision.probs.get("injection", 0.0)
            if policy.decide({"injection": p}, self.p.policy) == "quarantine_content":
                self.flagged.add(text)
                flagged.append({"text_sha256": sha256(text), "p": round(p, 3)})
                self.blast["quarantined_items"] += 1
                await self.contain("quarantine", f"{urlparse(page.get('url', '')).hostname}: text addressed to AI agents "
                                                 "removed before the actor saw it", excerpt=text[:400], probability=round(p, 3),
                                   url=page.get("url"))
        await self.ev.emit("quarantine.scan", source=page.get("url"), distinct_cells=len(new), candidates=len(candidates),
                           flagged=len(flagged), method="keyword prefilter, then the decision model's injection question")
        self.chain.add("quarantine", source=page.get("url"), screened=len(candidates), quarantined=flagged)

    def clean_text(self, page: dict) -> str:
        out: list[str] = []
        for line in (page.get("text") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            line = QUARANTINED if line in self.flagged else line
            if not (line == QUARANTINED and out and out[-1] == QUARANTINED):
                out.append(line)
        return "\n".join(out)[:6000]

    def brief(self, step, allow, history, note) -> str:
        lines = [f"GOAL: {self.m.goal}",
                 f"STEP {step['id']} (browser): {step.get('intent', '')}",
                 f"EXPECTED OUTPUT: {step.get('expected_output', '')}",
                 f"ALLOWED DOMAINS: {', '.join(sorted(allow))}",
                 "VENDORS: " + "; ".join(f"{s['vendor']} {s['url']}" for s in self.sites),
                 "FORM DETAILS SUPPLIED BY THE USER: " + "; ".join(f"{k} = {v}" for k, v in REQUESTER.items())]
        if self.m.goal == GOAL and len(self.quotes) == len(self.allowed) and len(allow) == 1:
            vendor = next(s for s in self.sites if s["name"] in allow)
            lines.append(f"VERIFIED LOWEST QUOTE: {vendor['vendor']} at ${self.quotes[vendor['name']]:,.2f}. "
                         f"If the draft form is not open yet, open {vendor['url']}cart.html to use the existing cart.")
        earlier = [r for r in self.results if r.get("facts")]
        if earlier:
            lines.append("FACTS FROM EARLIER STEPS:")
            lines += [f"- {r['step']}: " + "; ".join(f"{f['name']} = {f['value']}" for f in r["facts"]) for r in earlier]
        if history:
            lines += ["ACTIONS SO FAR IN THIS STEP:", *history[-8:]]
        if note:
            lines.append(f"LAST RESULT: {note}")
        page = self.page
        if not page:
            return "\n".join(lines + ["URL: about:blank (nothing loaded yet)", "ELEMENTS: none", "PAGE TEXT: none"])
        lines += [f"URL: {page.get('url')}", f"TITLE: {page.get('title')}", "ELEMENTS:"]
        lines += [element_line(e, self.flagged) for e in page.get("elements") or []] or ["none"]
        lines += ["PAGE TEXT (passages addressed to AI agents were replaced by a quarantine marker):", self.clean_text(page)]
        return "\n".join(lines)

    async def finish_step(self, step, args, turn):
        facts = [{"name": str(f.get("name", ""))[:80], "value": str(f.get("value", ""))[:200]}
                 for f in (args.get("facts") or []) if isinstance(f, dict)][:12]
        summary = str(args.get("summary", ""))[:400]
        evidence = {"expected": step.get("expected_output", ""),
                    "evidence_type": "browser screenshot, the page URL and the facts the agent extracted",
                    "evidence": {"url": (self.page or {}).get("url"), "facts": facts, "agent_summary": summary,
                                 "page_text": self.clean_text(self.page or {})[:3000]}}
        if self.m.goal == GOAL and any(f["name"] == "Form" for f in facts):
            evidence["evidence"]["form_values"] = {e["name"]: e["value"] for e in self.page["elements"] if e["name"] in REQUESTER}
        verdict = await self.verify_outcome(evidence, self.png)
        await self.ev.emit("verify.result", step=step["id"], screenshot=self.shot_url, **verdict)
        self.chain.add("verify", step=step["id"], passed=bool(verdict.get("passed")), confidence=verdict.get("confidence"),
                       observed_sha256=sha256(str(verdict.get("observed", ""))))
        result = {"step": step["id"], "intent": step.get("intent", ""), "status": "done", "facts": facts, "summary": summary,
                  "verified": bool(verdict.get("passed")), "turns": turn}
        self.results.append(result)
        await self.ev.emit("step.finished", **result)
        self.chain.add("step", step=step["id"], status="done", turns=turn, facts_sha256=sha256(canonical(facts)),
                       verified=result["verified"])
        return result["verified"]

    async def end_step(self, step, status: str, turns: int):
        result = {"step": step["id"], "intent": step.get("intent", ""), "status": status, "facts": [], "summary": "",
                  "verified": False, "turns": turns}
        self.results.append(result)
        await self.ev.emit("step.finished", **result)
        self.chain.add("step", step=step["id"], status=status, turns=turns)

    # ------------------------------------------------------------ red-team switch

    def detour_url(self) -> str | None:
        for line in sorted(self.flagged):
            for url in URL.findall(line):
                url = url.rstrip(".,;")
                if urlparse(url).hostname not in self.allowed:
                    return url
        return None

    async def detour(self, sid: str, url: str):
        """Do what an actor fooled by the hidden note would do next: score it, then run it anyway."""
        self.detoured = True
        host = urlparse(url).hostname
        await self.ev.emit("attack.simulated", step=sid, action={"type": "goto", "target": url},
                           note="Red-team switch is on: this is what an actor fooled by the hidden note would do next.")
        state = {"goal": self.m.goal, "page": (self.page or {}).get("url"), "proposed_action": {"type": "goto", "target": url}}
        decision = await self.ai.decide(self.p.gate["action_gate"], state)
        await self.model(decision.call)
        verdict = policy.decide(decision.probs, self.p.policy)
        reasons = [f"{host} is not on this mission's allowlist"]
        await self.ev.emit("gate.scored", step=sid, turn=0, simulated=True, action=state["proposed_action"], probs=decision.probs,
                           answers=decision.answers, decision="block_and_replan", exempt=[], reasons=reasons)
        await self.contain("policy", f"Blocked the detour to {host}: off task p = {decision.probs.get('off_task', 0):.2f} "
                                     f"({verdict.replace('_', ' ')}), and not on the allowlist")
        evidence = [f"decision gate: off task p = {decision.probs.get('off_task', 0):.2f} → {verdict.replace('_', ' ')}"]
        shots = []
        for tag, link, label in (("x1", url, f"run anyway in the browser VM, {host}"),
                                 ("x2", CONTROL_SITE, f"control, a real public site, {urlparse(CONTROL_SITE).hostname}")):
            page = await self.browse(sid, tag, {"action": "goto", "url": link}, link, purpose="attack")
            err = page.get("error") or "LOADED: the site answered"
            evidence.append(f"{label}: {err.split(' at ')[0].removeprefix('Error: Page.goto: ')}")
            if f"{sid}-{tag}-refused.png" in self.m.artifacts:
                shots.append(f"/api/missions/{self.m.id}/artifacts/{sid}-{tag}-refused.png")
        await self.ev.emit("attack.result", step=sid, evidence=evidence, screenshots=shots)
        self.chain.add("attack", step=sid, target_sha256=sha256(url), evidence_sha256=sha256("\n".join(evidence)))

    # ------------------------------------------------------------ answer

    async def explain(self):
        brief = {"goal": self.m.goal,
                 "steps": [{"id": r["step"], "intent": r["intent"], "status": r["status"],
                            "verifier": "passed" if r.get("verified") else "not passed", "facts": r["facts"],
                            "agent_summary": r.get("summary")} for r in self.results],
                 "approvals": self.approvals, "containment_events": self.containment}
        res = await self.ai.chat("explainer", self.p.system["explainer"], json.dumps(brief, indent=1)[:60000])
        await self.model(res.call)
        self.m.answer = (res.text or "").strip()
        await self.ev.emit("mission.answer", text=self.m.answer)
        self.chain.add("answer", answer_sha256=sha256(self.m.answer))


async def run_operator(ctx: Ctx, mission: Mission):
    await run_mission(ctx, mission, OperatorRun)
