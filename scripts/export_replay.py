#!/usr/bin/env python3
"""Export recorded missions as a static replay of the console, for GitHub Pages (docs/replay/).

  python3 scripts/export_replay.py                          # missions in data/archive/replay-source
  python3 scripts/export_replay.py data/missions/msn_abc    # or name the mission folders
  python3 scripts/export_replay.py --assets-only           # update the UI, keep the archive

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

def patched(text: str, patches) -> str:
    for old, new in patches:
        if text.count(old) != 1:
            raise SystemExit(f"index.html changed; this patch no longer applies: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def export_missions(folders, status_url, append):
    data = OUT / "data"
    with urllib.request.urlopen(status_url, timeout=10) as response:
        status = json.load(response)
    status["runner"] = {"ok": True, "guest_arch": status.get("runner", {}).get("guest_arch", "")}
    saved_runs = [(folder, json.loads((folder / "mission.json").read_text())) for folder in folders]
    summaries = {}
    keys = set()
    if append and (data / "missions.json").exists():
        previous = json.loads((data / "status.json").read_text())
        keys.update(previous.get("replay_signing_keys") or [previous["signing_key"]["public_key"]])
        summaries = {s["id"]: {"inference": previous["inference"], **s}
                     for s in json.loads((data / "missions.json").read_text())}
    elif data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True, exist_ok=True)
    for folder, saved in saved_runs:
        mid = saved["summary"]["id"]
        events = json.dumps(saved["events"]).replace(f"/api/missions/{mid}/artifacts/", f"data/{mid}/") \
                                            .replace(f"/api/missions/{mid}/receipt", f"data/{mid}/receipt.json")
        target = data / mid
        target.mkdir(exist_ok=True)
        (target / "events.json").write_text(events)
        (target / "receipt.json").write_text(json.dumps(saved["receipt"]))
        for png in folder.glob("*.png"):
            shutil.copy2(png, target / png.name)
        inference = next(e["inference"] for e in saved["events"] if e["type"] == "mission.started")
        summaries[mid] = {**saved["summary"], "inference": inference}
        keys.add(saved["receipt"]["signature"]["public_key"])
        print(f"{mid}  {saved['summary']['template']:8}  {len(saved['events'])} events  {len(list(folder.glob('*.png')))} images")
    latest = sorted(summaries.values(), key=lambda s: s["created_at"], reverse=True)
    (data / "missions.json").write_text(json.dumps(latest))
    (data / "status.json").write_text(json.dumps({**status, "replay_signing_keys": sorted(keys)}))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("missions", nargs="*", type=Path)
    ap.add_argument("--status-url", default="http://127.0.0.1:8000/api/status")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--assets-only", action="store_true", help="refresh the UI without modifying recorded missions")
    mode.add_argument("--append", action="store_true", help="keep existing recordings and add the supplied missions")
    args = ap.parse_args()
    if not args.assets_only:
        folders = args.missions or sorted(DEFAULT_SOURCE.glob("msn_*"))
        if not folders:
            raise SystemExit(f"No missions to export (looked in {DEFAULT_SOURCE}).")
        export_missions(folders, args.status_url, args.append)
    elif not (OUT / "data" / "missions.json").is_file():
        raise SystemExit("There is no replay archive yet. Export missions first.")

    html = patched((WEB / "index.html").read_text(), [
        ('href="/static/style.css?v=5"', 'href="style.css?v=5"'),
        ('<script src="/static/app.js?v=4"></script>', '<script src="replay.js?v=4"></script>\n<script src="app.js?v=4"></script>'),
        ('class="brand" href="/"', 'class="brand" href="../"'),
        ("<title>Glovebox</title>", "<title>Glovebox replay</title>"),
    ])
    (OUT / "index.html").write_text(html)
    for name in ("app.js", "replay.js", "style.css"):
        shutil.copy2(WEB / name, OUT / name)
    (ROOT / "docs" / ".nojekyll").write_text("")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"Wrote {OUT.relative_to(ROOT)} ({size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
