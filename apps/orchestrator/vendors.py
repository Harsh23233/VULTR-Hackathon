"""Mock vendor websites for Operator missions: three shops, one port each, served by the control plane.

They stand in for real supplier sites; Northgate carries a hidden prompt injection. Browser microVMs
reach them by name (northgate.vendors.glovebox.test): Chromium inside the VM maps each name to where
the site is served, and the VM's network policy allows only these ports on this host.
"""
from __future__ import annotations

from pathlib import Path

from starlette.staticfiles import StaticFiles

VENDORS = [
    {"slug": "ridgeline", "name": "Ridgeline Office Supply", "port": 8101},
    {"slug": "seatwise", "name": "Seatwise", "port": 8102},
    {"slug": "northgate", "name": "Northgate Furnishings", "port": 8103},
]


def sites(settings) -> list[dict]:
    """The vendors as a browser sandbox sees them: the name it uses and where that name is served."""
    return [{"vendor": v["name"], "name": f"{v['slug']}.{settings.vendor_domain}", "host": settings.vendor_guest_host,
             "port": v["port"], "url": f"http://{v['slug']}.{settings.vendor_domain}/"} for v in VENDORS]


def site_app(folder: Path):
    files = StaticFiles(directory=folder, html=True)

    async def app(scope, receive, send):
        async def no_store(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"cache-control", b"no-store")]
            await send(message)

        await files(scope, receive, no_store)

    return app


def apps(fixtures_dir: Path) -> list[tuple[int, object]]:
    root = fixtures_dir / "vendors" / "sites"
    if not (root / "northgate" / "index.html").exists():
        raise SystemExit("Vendor sites not built. Run: python3 prep/fixtures/vendors/build_sites.py")
    return [(v["port"], site_app(root / v["slug"])) for v in VENDORS]
