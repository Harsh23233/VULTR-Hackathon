"""Keep one headless Chromium running inside a browser microVM.

Started detached by the runner. The DevTools endpoint listens on the VM's own loopback only;
browser_step.py connects to it for each action, so the browser keeps its state (cart, form)
between steps while the host never talks to it directly.

Full Chromium in new headless mode, not the headless shell, so a refused site shows Chrome's own
error page in the screenshot. GBX_HOST_RULES maps the vendor names the agent sees
(northgate.vendors.glovebox.test) to where the sites are served. It is a convenience, not a
boundary: the VM's network policy decides what is reachable, and refuses everything else.
"""
import asyncio
import os

from playwright.async_api import async_playwright

ARGS = ["--remote-debugging-port=9222", "--remote-debugging-address=127.0.0.1", "--disable-dev-shm-usage",
        "--disable-gpu", "--no-first-run", "--disable-background-networking", "--disk-cache-size=1048576",
        "--disable-quic"]


async def main():
    args = list(ARGS)
    if os.environ.get("GBX_HOST_RULES"):
        args.append(f"--host-resolver-rules={os.environ['GBX_HOST_RULES']}")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            "/tmp/chrome-profile", channel="chromium", headless=True, args=args, viewport={"width": 1280, "height": 800},
            device_scale_factor=1, locale="en-US", accept_downloads=False)
        if not context.pages:
            await context.new_page()
        with open("/tmp/browser.ready", "w") as f:
            f.write("ready\n")
        await asyncio.Event().wait()


asyncio.run(main())
