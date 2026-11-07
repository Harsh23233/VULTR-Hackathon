"""Run one browser action inside a browser microVM and report what the page looks like now.

  python3 /opt/glovebox/browser_step.py '{"action": "goto", "url": "http://...", "shot": "s1-01"}'

Actions: observe, goto, click, type_text, select_option, scroll. Prints one JSON line with the URL,
title, visible text, the numbered interactive elements and the requests the page made, and saves a
viewport screenshot to /work/out/<shot>.png. Elements a person could not see (off-screen, hidden,
transparent) are never numbered, so the agent can't be steered into clicking them. If a navigation
fails (say the network refused the site), the error page is screenshotted as evidence and the
browser goes back to where it was, saved as /work/out/<shot>-r.png.
"""
import asyncio
import json
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright

MARK = r"""
() => {
  document.querySelectorAll('[data-gbx]').forEach((e) => e.removeAttribute('data-gbx'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll('a[href], button, input, select, textarea, [role=button], [role=link]')) {
    if (el.type === 'hidden') continue;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    const seen = r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none'
      && Number(st.opacity) > 0.05 && r.right > 0 && r.left < innerWidth && r.bottom > -4000;
    if (!seen) continue;
    n += 1;
    el.setAttribute('data-gbx', String(n));
    const label = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label')
      || el.innerText || el.placeholder || el.name || '';
    const item = { id: n, tag: el.tagName.toLowerCase(), name: label.trim().replace(/\s+/g, ' ').slice(0, 80) };
    if (el.tagName === 'A') item.href = el.getAttribute('href');
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') { item.type = el.type; item.value = el.value; }
    if (el.tagName === 'SELECT') { item.value = el.options[el.selectedIndex]?.text; item.options = [...el.options].map((o) => o.text).slice(0, 12); }
    if (el.tagName === 'BUTTON') item.type = el.type;
    if (r.top > innerHeight || r.bottom < 0) item.below = true;
    out.push(item);
  }
  return out;
}
"""


async def act(page, a):
    kind = a.get("action", "observe")
    target = page.locator(f'[data-gbx="{a.get("element_id")}"]')
    if kind == "goto":
        await page.goto(a["url"], wait_until="domcontentloaded", timeout=15000)
    elif kind == "click":
        await target.click(timeout=5000)
    elif kind == "type_text":
        await target.fill(str(a.get("text", "")), timeout=5000)
    elif kind == "select_option":
        await target.select_option(label=str(a.get("label", "")), timeout=5000)
    elif kind == "scroll":
        await page.mouse.wheel(0, 700 if a.get("direction", "down") == "down" else -700)
    await settle(page)


async def settle(page):
    for _ in range(2):  # a click may start a navigation a moment later
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(0.3)


async def describe(page) -> dict:
    for attempt in range(3):
        try:
            return {"url": page.url, "title": await page.title(), "elements": await page.evaluate(MARK),
                    "text": (await page.evaluate("() => document.body ? document.body.innerText : ''"))[:8000]}
        except Exception:  # the page navigated while we were reading it
            if attempt == 2:
                raise
            await settle(page)


async def main():
    a = json.loads(sys.argv[1])
    shot = a.get("shot", "shot")
    result = {"error": None, "requests": []}
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=15000)
        result["browser"] = browser.version
        context = browser.contexts[0]
        page = context.pages[-1] if context.pages else await context.new_page()
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        log = result["requests"]
        page.on("response", lambda r: len(log) < 40 and log.append(
            {"url": r.url[:200], "host": urlparse(r.url).hostname, "status": r.status}))
        page.on("requestfailed", lambda r: len(log) < 40 and log.append(
            {"url": r.url[:200], "host": urlparse(r.url).hostname, "failure": r.failure}))
        before = page.url
        try:
            await act(page, a)
        except Exception as e:  # e.g. the network policy refused a navigation; still report the page
            result["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        await page.screenshot(path=f"/work/out/{shot}.png", type="png")
        state = await describe(page)
        broken = result["error"] and a.get("action") in ("goto", "click") and (
            state["url"].startswith("chrome-error:") or not state["elements"])
        if broken and before.startswith("http"):
            result["failed_page"] = {"url": state["url"], "title": state["title"], "text": state["text"][:600]}
            await page.goto(before, wait_until="domcontentloaded", timeout=15000)
            await settle(page)
            await page.screenshot(path=f"/work/out/{shot}-r.png", type="png")
            state = await describe(page)
            result["restored_to"] = before
        result.update(state)
    print(json.dumps(result))


asyncio.run(main())
