#!/usr/bin/env python3
"""Export recorded missions as a static replay of the console, for GitHub Pages (docs/replay/).

  python3 scripts/export_replay.py                          # missions in data/archive/replay-source
  python3 scripts/export_replay.py data/missions/msn_abc    # or name the mission folders

Reads the console's status from a running orchestrator (scripted mode is fine). The replay plays each
mission's saved events back in the real console code and verifies every receipt in the browser.
"""
import argparse
import json
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
OUT = ROOT / "docs" / "replay"
DEFAULT_SOURCE = ROOT / "data" / "archive" / "replay-source"

# app.js hands its API calls, event stream, approval dialog, chips and banner to replay.js.
PATCHES = [
    ("async function getJSON(url, opts) {\n",
     "async function getJSON(url, opts) {\n  if (window.GLOVEBOX_REPLAY) return replayGet(url, opts);\n"),
    ("es = new EventSource(`/api/missions/${encodeURIComponent(id)}/events`);",
     "es = window.GLOVEBOX_REPLAY ? replaySource(id) : new EventSource(`/api/missions/${encodeURIComponent(id)}/events`);"),
    ('if (e.type === "approval.requested") showApproval(e);',
     'if (e.type === "approval.requested" && !window.GLOVEBOX_REPLAY) showApproval(e);'),
    ("function renderChips() {\n",
     "function renderChips() {\n  if (window.GLOVEBOX_REPLAY) { $(\"#chips\").innerHTML = replayChips(status); return; }\n"),
    ('$("#testmode").hidden = status.inference !== "scripted";',
     '$("#testmode").hidden = status.inference !== "scripted";\n  if (window.GLOVEBOX_REPLAY) replayBanner();'),
]


def patched(text: str, patches) -> str:
    for old, new in patches:
        if text.count(old) != 1:
            raise SystemExit(f"app.js changed; this patch no longer applies: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("missions", nargs="*", type=Path)
    ap.add_argument("--status-url", default="http://127.0.0.1:8000/api/status")
    args = ap.parse_args()
    folders = args.missions or sorted(DEFAULT_SOURCE.glob("msn_*"))
    if not folders:
        raise SystemExit(f"No missions to export (looked in {DEFAULT_SOURCE}).")
    status = json.load(urllib.request.urlopen(args.status_url, timeout=10))
    status["runner"] = {"ok": True, "guest_arch": status.get("runner", {}).get("guest_arch", "")}

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "data").mkdir(parents=True)
    summaries = []
    for folder in folders:
        saved = json.loads((folder / "mission.json").read_text())
        mid = saved["summary"]["id"]
        events = json.dumps(saved["events"]).replace(f"/api/missions/{mid}/artifacts/", f"data/{mid}/") \
                                            .replace(f"/api/missions/{mid}/receipt", f"data/{mid}/receipt.json")
        target = OUT / "data" / mid
        target.mkdir()
        (target / "events.json").write_text(events)
        (target / "receipt.json").write_text(json.dumps(saved["receipt"]))
        for png in folder.glob("*.png"):
            shutil.copy2(png, target / png.name)
        summaries.append(saved["summary"])
        print(f"{mid}  {saved['summary']['template']:8}  {len(saved['events'])} events  {len(list(folder.glob('*.png')))} images")
    summaries.sort(key=lambda s: s["created_at"], reverse=True)
    (OUT / "data" / "missions.json").write_text(json.dumps(summaries))
    (OUT / "data" / "status.json").write_text(json.dumps(status))

    html = patched((WEB / "index.html").read_text(), [
        ('href="/static/style.css?v=3"', 'href="style.css"'),
        ('<script src="/static/app.js?v=3"></script>', '<script src="replay.js"></script>\n<script src="app.js"></script>'),
        ("<title>Glovebox</title>", "<title>Glovebox replay</title>"),
    ])
    (OUT / "index.html").write_text(html)
    (OUT / "app.js").write_text(patched((WEB / "app.js").read_text(), PATCHES))
    shutil.copy2(WEB / "replay.js", OUT / "replay.js")
    shutil.copy2(WEB / "style.css", OUT / "style.css")
    (ROOT / "docs" / ".nojekyll").write_text("")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"Wrote {OUT.relative_to(ROOT)} ({len(summaries)} missions, {size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
