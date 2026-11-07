#!/usr/bin/env python3
"""Serve the three mock vendor sites, one port each (standard library only).

  python3 prep/fixtures/vendors/serve.py              # 127.0.0.1:8101-8103
  python3 prep/fixtures/vendors/serve.py --host 0.0.0.0

Each site gets its own origin, as it will in production (one subdomain per vendor), so
cart state in sessionStorage never leaks between vendors.
"""
import argparse
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITES = Path(__file__).resolve().parent / "sites"
PORTS = {"ridgeline": 8101, "seatwise": 8102, "northgate": 8103}


class Handler(SimpleHTTPRequestHandler):
    site = "?"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        print(f"[{self.site}] {self.address_string()} {fmt % args}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if not (SITES / "northgate" / "index.html").exists():
        raise SystemExit("Sites not built yet. Run: python3 prep/fixtures/vendors/build_sites.py")
    for slug, port in PORTS.items():
        handler = functools.partial(type(slug, (Handler,), {"site": slug}), directory=str(SITES / slug))
        server = ThreadingHTTPServer((args.host, port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"{slug:10} http://{args.host}:{port}/", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
