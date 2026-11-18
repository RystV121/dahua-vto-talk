"""Real-browser test of the ringtones on the device that rings.

Chrome opens the card, the bell chip is tapped (opts this device in and
unlocks sound, as on the wall tablet), then for every ringtone: pick it in
Home Assistant (select entity), ring, and analyse what the ringer plays
(energy and pitch). Answering must stop the ringing. A second browser that
never tapped the chip must show no popup.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from browser_e2e import PORT, Cdp  # noqa: E402
from preview_audio_e2e import HA, login  # noqa: E402

EXPECT = {  # first-note pitches (Hz) the analyser may see
    "dingdong": [659, 523, 1318],
    "bells": [1760, 1318, 1568],
    "marimba": [523, 659, 784, 1046],
    "drop": list(range(500, 1300, 10)),
    "harp": [392, 494, 587, 784, 988],
}

LISTEN = """(async () => {
  const R = window.__dahuaVtoRinger;
  for (let i = 0; i < 40 && !(R.el && !R.el.paused && R.el.currentTime > 0.05); i++) await new Promise(r => setTimeout(r, 50));
  const el = R.el;
  if (!el || el.paused) return {playing: false, blocked: !!R.blocked};
  const ctx = new AudioContext(); await ctx.resume();
  const an = ctx.createAnalyser(); an.fftSize = 8192;
  ctx.createMediaStreamSource(el.captureStream()).connect(an);
  await new Promise(r => setTimeout(r, 300));
  const f = new Float32Array(an.frequencyBinCount); an.getFloatFrequencyData(f);
  let best = 0; for (let i = 1; i < f.length; i++) if (f[i] > f[best]) best = i;
  ctx.close();
  return {playing: true, loop: el.loop, ringing: R.ringing, peakHz: Math.round(best * ctx.sampleRate / an.fftSize), peakDb: Math.round(f[best])};
})()"""


async def page(http, port, profile):
    chrome = subprocess.Popen(
        ["google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--window-size=1100,800", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            async with http.get(f"http://127.0.0.1:{port}/json") as r:
                pages = [p for p in await r.json() if p["type"] == "page"]
                if pages:
                    break
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.2)
    ws = await http.ws_connect(pages[0]["webSocketDebuggerUrl"], max_msg_size=0)
    cdp = Cdp(ws)
    await cdp.call("Runtime.enable")
    await cdp.call("Page.enable")
    return chrome, ws, cdp


async def main() -> int:
    results = {}
    async with aiohttp.ClientSession() as http:
        tokens = await login(http)
        head = {"Authorization": f"Bearer {tokens['access_token']}"}
        async with http.get(f"{HA}/api/states", headers=head) as r:
            states = await r.json()
        select = next(s["entity_id"] for s in states if s["entity_id"].startswith("select.") and "melodia" in s["entity_id"])
        tablet, tws, cdp = await page(http, PORT, "/tmp/chrome-ring-tablet")
        phone, pws, pcdp = await page(http, PORT + 1, "/tmp/chrome-ring-phone")
        try:
            for c in (cdp, pcdp):
                await c.call("Page.navigate", url=f"{HA}/auth/authorize?response_type=code&client_id={HA}/")
            await asyncio.sleep(2)
            for c in (cdp, pcdp):
                await c.js(f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))}); true")
                await c.call("Page.navigate", url=f"{HA}/lovelace/domofon-cam")
            await asyncio.sleep(7)
            # tap the bell chip on the tablet only (real click = user gesture)
            at = await cdp.js("""(() => { const c = document.querySelector('home-assistant').shadowRoot && (function f(r){const h=r.querySelector&&r.querySelector('dahua-vto-talk-card'); if(h) return h; for(const e of r.querySelectorAll?r.querySelectorAll('*'):[]) if(e.shadowRoot){const x=f(e.shadowRoot); if(x) return x;} return null;})(document);
              const b = c.shadowRoot.getElementById('popupchip').getBoundingClientRect(); return {x: b.x + b.width/2, y: b.y + b.height/2}; })()""")
            for kind in ("mousePressed", "mouseReleased"):
                await cdp.call("Input.dispatchMouseEvent", type=kind, x=at["x"], y=at["y"], button="left", clickCount=1)
            await asyncio.sleep(1)
            results["tablet opted in"] = await cdp.js("localStorage.getItem('dahua_vto_talk_ring_here')") == "on"
            for c in (cdp, pcdp):
                await c.call("Page.navigate", url=f"{HA}/lovelace/elsewhere")
            await asyncio.sleep(5)
            # a fresh page nobody touched yet: a browser blocks the sound (the
            # popup still shows); the HA app usually allows it
            async with http.post(f"{HA}/api/services/dahua_vto_talk/ring", headers=head, json={}) as r:
                assert r.status == 200
            await asyncio.sleep(1.5)
            results["untouched page: popup"] = await cdp.js("!!document.querySelector('.dahua-vto-popup')")
            results["untouched page: sound blocked by browser"] = await cdp.js("!!window.__dahuaVtoRinger.blocked")
            async with http.post(f"{HA}/api/services/dahua_vto_talk/dismiss", headers=head, json={}) as r:
                pass
            await asyncio.sleep(1)
            # someone touches the tablet once
            for kind in ("mousePressed", "mouseReleased"):
                await cdp.call("Input.dispatchMouseEvent", type=kind, x=900, y=700, button="left", clickCount=1)
            await asyncio.sleep(1)
            for tone, pitches in EXPECT.items():
                async with http.post(f"{HA}/api/services/select/select_option", headers=head,
                                     json={"entity_id": select, "option": tone}) as r:
                    assert r.status == 200
                await asyncio.sleep(1.5)
                async with http.post(f"{HA}/api/services/dahua_vto_talk/ring", headers=head, json={}) as r:
                    assert r.status == 200
                heard = await cdp.js(LISTEN)
                popup_phone = await pcdp.js("!!document.querySelector('.dahua-vto-popup')")
                ok = heard.get("playing") and (heard.get("peakDb") or -200) > -80 and any(
                    abs(heard["peakHz"] - p) <= max(12, p * 0.03) for p in pitches)
                results[f"ringtone {tone}"] = {"ok": bool(ok), "heard": heard, "phone popup": popup_phone}
                async with http.post(f"{HA}/api/services/dahua_vto_talk/dismiss", headers=head, json={}) as r:
                    pass
                await asyncio.sleep(1.5)
                results[f"ringtone {tone}"]["stopped"] = not await cdp.js("window.__dahuaVtoRinger.ringing")
            cdp.reader.cancel()
            pcdp.reader.cancel()
        finally:
            for ch, w in ((tablet, tws), (phone, pws)):
                await w.close()
                ch.terminate()
                ch.wait()
    print(json.dumps(results, indent=1))
    ok = results["tablet opted in"] and results["untouched page: popup"] and all(
        v["ok"] and v["stopped"] and not v["phone popup"] for k, v in results.items() if k.startswith("ringtone"))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
