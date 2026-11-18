"""The wall-panel case: a page stays open for days, nobody refreshes it.

Opens the card view once (opts the device in), then WITHOUT reloading the
page: reloads the integration (as an options change does) and rings; then
restarts Home Assistant itself and rings again. The popup must appear every
time. Needs the preview e2e HA config (view "domofon-cam" with call_popup:
true) and HA started by this script's caller with RESTART_CMD in the env
(the command that starts HA again after it stopped).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from browser_e2e import PORT, Cdp  # noqa: E402
from preview_audio_e2e import HA, login  # noqa: E402

HAS_POPUP = "!!document.querySelector('.dahua-vto-popup dahua-vto-talk-card')"


async def ring_and_wait(http, cdp, token) -> bool:
    head = {"Authorization": f"Bearer {token}"}
    async with http.post(f"{HA}/api/services/dahua_vto_talk/ring", headers=head, json={}) as r:
        assert r.status == 200, await r.text()
    for _ in range(40):
        await asyncio.sleep(0.25)
        if await cdp.js(HAS_POPUP):
            async with http.post(f"{HA}/api/services/dahua_vto_talk/dismiss", headers=head, json={}) as r:
                pass
            for _ in range(20):
                await asyncio.sleep(0.25)
                if not await cdp.js(HAS_POPUP):
                    break
            return True
    return False


async def wait_ha(http) -> None:
    for _ in range(120):
        try:
            async with http.get(f"{HA}/api/") as r:
                if r.status in (200, 401):
                    return
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(1)


async def main() -> int:
    chrome = subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={PORT}",
         "--user-data-dir=/tmp/chrome-reconnect", "--window-size=1100,800", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    results = {}
    try:
        async with aiohttp.ClientSession() as http:
            tokens = await login(http)
            tok = tokens["access_token"]
            for _ in range(50):
                try:
                    async with http.get(f"http://127.0.0.1:{PORT}/json") as r:
                        pages = [p for p in await r.json() if p["type"] == "page"]
                        if pages:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.2)
            async with http.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                cdp = Cdp(ws)
                await cdp.call("Runtime.enable")
                await cdp.call("Page.enable")
                await cdp.call("Page.navigate", url=f"{HA}/auth/authorize?response_type=code&client_id={HA}/")
                await asyncio.sleep(2)
                await cdp.js(f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))}); true")
                await cdp.call("Page.navigate", url=f"{HA}/lovelace/domofon-cam")
                await asyncio.sleep(6)
                await cdp.call("Page.navigate", url=f"{HA}/lovelace/elsewhere")
                await asyncio.sleep(4)
                results["fresh page"] = await ring_and_wait(http, cdp, tok)

                # options changed -> the integration reloads (page untouched)
                head = {"Authorization": f"Bearer {tok}"}
                async with http.get(f"{HA}/api/config/config_entries/entry", headers=head) as r:
                    entry = next(e for e in await r.json() if e["domain"] == "dahua_vto_talk")
                async with http.post(f"{HA}/api/config/config_entries/entry/{entry['entry_id']}/reload", headers=head) as r:
                    assert r.status == 200, await r.text()
                await asyncio.sleep(3)
                results["after integration reload"] = await ring_and_wait(http, cdp, tok)

                # Home Assistant restarts (page untouched)
                async with http.post(f"{HA}/api/services/homeassistant/stop", headers=head, json={}) as r:
                    pass
                await asyncio.sleep(8)
                subprocess.Popen(os.environ["RESTART_CMD"], shell=True)
                await wait_ha(http)
                tokens2 = await login(http)
                ok = False
                for _ in range(30):  # the page reconnects on its own
                    await asyncio.sleep(2)
                    if await cdp.js("!!(window.__dahuaVtoCallWatcher && window.__dahuaVtoCallWatcher.subscribed)"):
                        ok = True
                        break
                await asyncio.sleep(3)
                results["after HA restart"] = ok and await ring_and_wait(http, cdp, tokens2["access_token"])
                results["page was never reloaded"] = await cdp.js("performance.getEntriesByType('navigation')[0].type") != "reload"
                cdp.reader.cancel()
    finally:
        chrome.terminate()
        chrome.wait()
    print(json.dumps(results, indent=1))
    ok = all(results.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
