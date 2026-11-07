"""Scripted models for offline tests and development without an API key.

Never part of the demo: the console shows a TEST MODE banner while this is active, and the
hackathon build sets GLOVEBOX_INFERENCE=vultr. The answers are fixed but realistic, so everything
else (real microVMs, real code on the real CSV, receipts) runs end to end.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

from .inference import ChatResult, Decision, ModelCall

_ANALYSIS = r'''import json
import os
import urllib.request

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

df = pd.read_csv("/work/in/vendor_payments_q3.csv", dtype={"iban_last4": str})
df["paid_on"] = pd.to_datetime(df["paid_on"]__DATE_ARGS__)
print(f"loaded {len(df)} payments from {df['paid_on'].min():%d %b} to {df['paid_on'].max():%d %b %Y}")

if os.environ.get("ERP_URL"):
    request = urllib.request.Request(os.environ["ERP_URL"] + "/vendors",
                                     headers={"Authorization": "Bearer " + os.environ["ERP_TOKEN"]})
    with urllib.request.urlopen(request, timeout=15) as response:
        vendors = json.load(response)["vendors"]
    print(f"fetched {len(vendors)} registered vendors from the ERP API")
else:
    with open("/work/in/erp_vendors.json") as f:
        vendors = json.load(f)["vendors"]
registered = {v["vendor_id"]: str(v["registered_iban_last4"]).zfill(4) for v in vendors}

df["iban_last4"] = df["iban_last4"].str.strip().str.zfill(4)
duplicates = df[df.duplicated(["vendor_id", "invoice_no", "amount_usd"], keep="first")]
unregistered = df[df["iban_last4"] != df["vendor_id"].map(registered)]
spend = df.groupby("vendor_name")["amount_usd"].sum().sort_values()

flagged = set(unregistered["vendor_name"]) | set(duplicates["vendor_name"])
fig, ax = plt.subplots(figsize=(8, 4.5))
ax.barh(spend.index, spend.values, color=["#B42318" if n in flagged else "#0B6B63" for n in spend.index])
ax.set_title("Q3 2026 spend by vendor (red: vendors with flagged payments)")
ax.set_xlabel("Total paid (USD)")
ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x / 1000:,.0f}k"))
ax.tick_params(axis="y", labelsize=7)
fig.tight_layout()
fig.savefig("/work/out/spend_by_vendor.png", dpi=200)

evidence = []
for _, row in duplicates.iterrows():
    first = df[(df["vendor_id"] == row["vendor_id"]) & (df["invoice_no"] == row["invoice_no"])].iloc[0]
    evidence.append({"claim": "duplicate invoice paid twice",
                     "value": f"{row['vendor_name']} {row['invoice_no']} ${row['amount_usd']:,.2f}",
                     "rows": [first["payment_id"], row["payment_id"]]})
for _, row in unregistered.iterrows():
    evidence.append({"claim": "paid to an account that is not the registered one",
                     "value": f"{row['vendor_name']} ${row['amount_usd']:,.2f} on {row['paid_on']:%d %b} to account "
                              f"ending {row['iban_last4']} (registered {registered[row['vendor_id']]})",
                     "rows": [row["payment_id"]]})
total = round(float(df["amount_usd"].sum()), 2)
evidence.append({"claim": "total Q3 spend", "value": total, "rows": []})
print(json.dumps({
    "summary": f"{len(df)} payments checked: {len(duplicates)} duplicate invoice payments, "
               f"{len(unregistered)} payments to unregistered accounts",
    "metrics": {"payments": len(df), "duplicate_payments": len(duplicates),
                "unregistered_account_payments": len(unregistered), "total_spend_usd": total},
    "outputs": ["/work/out/spend_by_vendor.png"],
    "evidence": evidence,
}))
'''

BUGGY_CODE = _ANALYSIS.replace("__DATE_ARGS__", "")
FIXED_CODE = _ANALYSIS.replace("__DATE_ARGS__", ', format="mixed"')

INJECTION_MARKERS = ("ai assistants", "ai agents", "automated agents", "ignore previous", "do not mention")


class ScriptedInference:
    mode = "scripted"

    def __init__(self, settings):
        self.models = {role: f"scripted-{role}" for role in settings.models}
        self.catalog: dict = {}
        self.roster = [{"role": r, "model": m, "ok": True, "notes": "test mode"} for r, m in self.models.items()]

    async def start(self):
        pass

    async def close(self):
        pass

    def _call(self, role: str, ms: int) -> ModelCall:
        return ModelCall(role, self.models[role], self.models[role], ms)

    async def chat(self, role, system, user, *, schema=None, tools=None, image_png=None, effort="low", max_tokens=8192):
        await asyncio.sleep(0.15)
        if role == "browser_actor":
            return ChatResult(None, ScriptedActor(user).next(), self._call(role, 150))
        text = getattr(self, f"_{role}")(user)
        return ChatResult(text, None, self._call(role, 150))

    async def decide(self, questions, state, image_png=None):
        await asyncio.sleep(0.05)
        # Score the action, not the goal around it: "Submit it only after I approve" is not a submit.
        text = json.dumps(state.get("proposed_action", state) if isinstance(state, dict) else state).lower()
        probs = {}
        for q in questions:
            if q == "injection":
                probs[q] = 0.97 if any(k in text for k in INJECTION_MARKERS) else 0.02
            elif q == "irreversible":
                probs[q] = 0.95 if any(k in text for k in ("submit", "pay ", "delete", "send ")) else 0.04
            elif q == "credential_use":
                probs[q] = 0.62 if any(k in text for k in ("credential", "token", "password", "card")) else 0.05
            elif q == "off_task":
                probs[q] = 0.91 if "bestchairs" in text else 0.06
            elif q == "matches_expected":
                probs[q] = 0.9
        answers = {q: {"type": "noul", "noul": p} for q, p in probs.items()}
        return Decision(probs, answers, self._call("decision", 50))

    # --- canned answers -------------------------------------------------

    def _planner(self, user):
        brief = json.loads(user)
        if brief["mission"].get("allowed_domains"):
            return self._operator_plan(brief["mission"])
        api = "erp" if "erp" in brief["mission"].get("apis", []) else None
        return json.dumps({
            "goal_summary": "Audit Q3 vendor payments for duplicate invoices and payments to unregistered bank "
                            "accounts, and chart total spend by vendor.",
            "steps": [{
                "id": "s1", "kind": "code",
                "intent": "Load the payments export and the registered vendor accounts (from the ERP API if available), "
                          "find duplicate invoices (same vendor, invoice number and amount) and payments to an account "
                          "that differs from the registered one, and chart total spend by vendor.",
                "inputs": brief["mission"]["files"], "allow_domains": [], "needs_api": api,
                "expected_output": "A JSON summary on the last stdout line with evidence rows for every flagged payment, "
                                   "and /work/out/spend_by_vendor.png showing spend for all vendors.",
                "irreversible": False, "depends_on": []}],
            "suspicious_content": []})

    def _operator_plan(self, mission):
        vendors = mission["vendors"]
        steps = [{"id": f"s{i}", "kind": "browser",
                  "intent": f"Get the order total for 20 ErgoMesh 300 chairs at {v['name']}, including shipping.",
                  "inputs": [], "allow_domains": [v["domain"]], "needs_api": None,
                  "expected_output": f"{v['name']}'s cart shows 20 ErgoMesh 300 chairs and an order total that includes shipping.",
                  "irreversible": False, "depends_on": []} for i, v in enumerate(vendors, 1)]
        priced = [s["id"] for s in steps]
        domains = [v["domain"] for v in vendors]
        n = len(steps)
        steps.append({"id": f"s{n + 1}", "kind": "browser",
                      "intent": "At the vendor with the lowest order total, open the cart with 20 chairs and fill in the purchase "
                                "request form with the form details supplied by the user. Do not submit it.",
                      "inputs": priced, "allow_domains": domains, "needs_api": None,
                      "expected_output": "The cheapest vendor's purchase request form is filled in with the supplied details and "
                                         "shows the order total. Nothing is submitted.",
                      "irreversible": False, "depends_on": priced})
        steps.append({"id": f"s{n + 2}", "kind": "browser", "intent": "Submit the drafted purchase request.",
                      "inputs": [f"s{n + 1}"], "allow_domains": domains, "needs_api": None,
                      "expected_output": "A confirmation page with a purchase request reference and the order total.",
                      "irreversible": True, "depends_on": [f"s{n + 1}"]})
        return json.dumps({"goal_summary": "Compare the delivered price of 20 ErgoMesh 300 chairs at the three approved vendors, "
                                           "draft a purchase request at the cheapest, and submit it only with approval.",
                           "steps": steps, "suspicious_content": []})

    def _coder(self, user):
        return "```python\n" + (FIXED_CODE if "PREVIOUS ATTEMPT" in user else BUGGY_CODE) + "```"

    def _verifier(self, user):
        brief = json.loads(user)
        if "browser" in brief.get("evidence_type", ""):
            facts = "; ".join(f"{f['name']} {f['value']}" for f in brief["evidence"].get("facts", []))
            return json.dumps({"passed": True, "observed": f"The screenshot of {brief['evidence'].get('url')} shows {facts}.",
                               "mismatch": "", "confidence": "high"})
        return json.dumps({"passed": True, "observed": "Bar chart of spend by vendor with labelled axes; the summary lists "
                                                       "the flagged payments with their row ids.",
                           "mismatch": "", "confidence": "high"})

    def _explainer(self, user):
        brief = json.loads(user)
        if any("facts" in step for step in brief["steps"]):
            return self._operator_answer(brief)
        lines, flagged = [], []
        for step in brief["steps"]:
            out = step.get("output") or {}
            lines.append(f"{out.get('summary', 'No result')} [{step['id']}].")
            for ev in out.get("evidence", []):
                if ev.get("rows"):
                    flagged.append(f"- {ev['rows'][-1]}: {ev['value']} ({ev['claim']}) [{step['id']}]")
        contained = "; ".join(e["detail"] for e in brief.get("containment_events", [])) or "nothing needed blocking"
        return "\n".join(lines + ["", "Flagged:"] + flagged + ["", f"Contained: {contained}."])

    def _operator_answer(self, brief):
        facts = {st["id"]: {f["name"].lower(): f["value"] for f in st.get("facts") or []} for st in brief["steps"]}
        quotes = [(money(f.get("order total")), f.get("vendor"), sid) for sid, f in facts.items()
                  if f.get("vendor") and money(f.get("order total")) and not {"purchase request", "form"} & set(f)]
        lines = []
        if quotes:
            total, vendor, sid = min(quotes)
            lines.append(f"{vendor} is the cheapest delivered price for 20 ErgoMesh 300 chairs: ${total:,.2f} [{sid}].")
        sent = next(((sid, f) for sid, f in facts.items() if "purchase request" in f), None)
        if sent:
            lines.append(f"The purchase request was submitted after your approval: {sent[1]['purchase request']} [{sent[0]}].")
        else:
            lines.append("No purchase request was submitted.")
        lines += ["", "Quotes:"] + [f"- {v}: ${t:,.2f} total including shipping [{sid}]" for t, v, sid in sorted(quotes)]
        contained = "; ".join(e["detail"] for e in brief.get("containment_events", [])) or "nothing needed blocking"
        return "\n".join(lines + ["", f"Contained: {contained}."])


def money(text):
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


class ScriptedActor:
    """Drives the mock vendor sites from the same brief a real browser-actor model gets."""

    ELEMENT = re.compile(r'^\[(\d+)\] (\S+) "(.*?)"(?: -> (\S+))?(?: value="(.*?)")?')

    def __init__(self, brief: str):
        lines = brief.splitlines()
        field = lambda key: next((l.split(": ", 1)[1] for l in lines if l.startswith(key + ": ")), "")  # noqa: E731
        self.intent = next((l.split(": ", 1)[1] for l in lines if l.startswith("STEP ")), "").lower()
        self.url = field("URL").split(" ")[0]
        self.vendors = dict(re.findall(r"\s*([^;]+?) (https?://[^\s;]+)", field("VENDORS")))
        self.form = dict(pair.split(" = ", 1) for pair in field("FORM DETAILS SUPPLIED BY THE USER").split("; ") if " = " in pair)
        self.facts = [l[2:] for l in lines if l.startswith("- s")]
        self.elements = [m.groups() for m in map(self.ELEMENT.match, lines) if m]
        marker = next((i for i, l in enumerate(lines) if l.startswith("PAGE TEXT")), len(lines))
        self.text = [l.strip() for l in lines[marker + 1:] if l.strip()]

    def find(self, name, role=None):
        for eid, r, label, _href, value in self.elements:
            if label.lower().startswith(name.lower()) and (role is None or r == role):
                return {"id": int(eid), "role": r, "value": value or ""}
        return None

    def after(self, label):
        for i, line in enumerate(self.text[:-1]):
            if line == label:
                return self.text[i + 1]
        return ""

    def vendor(self):
        named = next((v for v in self.vendors if v.lower() in self.intent), None)
        if named:
            return named
        quotes = []
        for line in self.facts:
            f = dict(p.split(" = ", 1) for p in line.split(": ", 1)[1].split("; ") if " = " in p)
            if money(f.get("Order total")) and f.get("Vendor") in self.vendors:
                quotes.append((money(f["Order total"]), f["Vendor"]))
        return min(quotes)[1] if quotes else next(iter(self.vendors))

    @staticmethod
    def call(name, **args):
        return {"name": name, "arguments": args}

    def next(self):
        vendor = self.vendor()
        home = self.vendors[vendor]
        path = urlparse(self.url).path
        submit = "submit" in self.intent and "do not submit" not in self.intent
        drafting = "purchase request" in self.intent and not submit
        if submit:
            if path.endswith("confirm.html"):
                ref = next((l for l in self.text if re.fullmatch(r"PR-\d{6}", l)), "")
                return self.call("done", summary=f"Submitted the purchase request at {vendor}.",
                                 facts=[{"name": "Purchase request", "value": ref}, {"name": "Vendor", "value": vendor},
                                        {"name": "Order total", "value": self.after("Order total")}])
            button = self.find("Submit purchase request", "button")
            if button:
                return self.call("click", element_id=button["id"], irreversible=True,
                                 expect="A confirmation page with a purchase request reference")
        if urlparse(self.url).hostname != urlparse(home).hostname:
            start = home + ("cart.html" if drafting or submit else "")
            return self.call("goto", url=start, expect=f"{vendor}'s {'cart' if drafting else 'product page'}")
        if drafting:
            if path.endswith("cart.html"):
                link = self.find("Create purchase request", "link")
                return self.call("click", element_id=link["id"], irreversible=False, expect="The purchase request form")
            for name, value in self.form.items():
                el = self.find(name)
                if el and el["value"] != value:
                    if el["role"] == "select":
                        return self.call("select_option", element_id=el["id"], label=value, expect=f"{name} shows {value}")
                    return self.call("type_text", element_id=el["id"], text=value, expect=f"{name} shows {value}")
            return self.call("done", summary=f"Filled in the purchase request form at {vendor}; not submitted.",
                             facts=[{"name": "Vendor", "value": vendor}, {"name": "Order total", "value": self.after("Order total")},
                                    {"name": "Form", "value": "filled in, not submitted"}])
        if path.endswith("cart.html"):
            row = next((l for l in self.text if "\t" in l and "$" in l), "")
            cells = row.split("\t")
            return self.call("done", summary=f"Priced 20 chairs at {vendor}.",
                             facts=[{"name": "Vendor", "value": vendor},
                                    {"name": "Unit price", "value": cells[1] if len(cells) > 1 else ""},
                                    {"name": "Quantity", "value": cells[2] if len(cells) > 2 else ""},
                                    {"name": "Shipping", "value": self.after("Shipping")},
                                    {"name": "Order total", "value": self.after("Order total")}])
        qty = self.find("Quantity", "textbox")
        if qty and qty["value"] != "20":
            return self.call("type_text", element_id=qty["id"], text="20", expect="Quantity shows 20")
        add = self.find("Add to cart", "button")
        return self.call("click", element_id=add["id"], irreversible=False, expect="The cart with 20 chairs and the order total")
