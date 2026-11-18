"""Real-browser end-to-end test.

Chrome (fake microphone playing a 600 Hz tone) -> real Home Assistant frontend
-> dahua-vto-talk-card -> AudioWorklet -> websocket binary frames ->
dahua_vto_talk integration -> RTSP backchannel -> simulated VTO speaker.

Needs: HA running on 127.0.0.1:18123 (tests/e2e/setup_ha.py), the fake VTO
(tests/e2e/run_fake_vto.py), /tmp/hass_tokens.json and google-chrome.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import struct
import subprocess
import sys
import wave

import aiohttp

HA = "http://127.0.0.1:18123"
FAKE = "http://127.0.0.1:18081/heard"
PORT = 9333
SHOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e_card.png"
FREQ = 600

DEEP = """
(() => {
  const find = (root, sel) => {
    const hit = root.querySelector && root.querySelector(sel);
    if (hit) return hit;
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) {
      if (el.shadowRoot) { const r = find(el.shadowRoot, sel); if (r) return r; }
    }
    return null;
  };
  window.__vtoCard = () => find(document, 'dahua-vto-talk-card');
  return true;
})()
"""


def make_mic_wav(path: str) -> None:
    rate = 48000
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * FREQ * i / rate))) for i in range(rate * 10)
        )
        w.writeframes(frames)


class Cdp:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.n = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.console: list[str] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for msg in self.ws:
            data = json.loads(msg.data)
            if "id" in data and data["id"] in self.pending:
                self.pending.pop(data["id"]).set_result(data)
            elif data.get("method") == "Runtime.consoleAPICalled":
                args = data["params"]["args"]
                self.console.append(" ".join(str(a.get("value", a.get("description", ""))) for a in args))
            elif data.get("method") == "Runtime.exceptionThrown":
                det = data["params"]["exceptionDetails"]
                self.console.append("EXCEPTION " + str(det.get("exception", {}).get("description") or det.get("exception", {}).get("value") or det.get("text"))[:300])

    async def call(self, method: str, **params):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send_json({"id": self.n, "method": method, "params": params})
        res = await asyncio.wait_for(fut, 30)
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error']}")
        return res["result"]

    async def js(self, expr: str):
        res = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in res:
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:500])
        return res["result"].get("value")


async def main() -> int:
    wav = "/tmp/mic600.wav"
    make_mic_wav(wav)
    tokens = json.load(open("/tmp/hass_tokens.json"))
    chrome = subprocess.Popen(
        [
            "google-chrome",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={PORT}",
            "--user-data-dir=/tmp/chrome-e2e",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=900,700",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as http:
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
                await cdp.call(
                    "Page.addScriptToEvaluateOnNewDocument",
                    source="window.addEventListener('unhandledrejection', e => console.log('UNHANDLED', JSON.stringify(e.reason)));",
                )
                # put the login tokens where the HA frontend looks for them
                await cdp.call("Page.navigate", url=f"{HA}/auth/authorize?response_type=code&client_id={HA}/")
                await asyncio.sleep(2)
                await cdp.js(f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))}); true")
                await cdp.call("Page.navigate", url=f"{HA}/lovelace/domofon")

                card_ok = False
                for _ in range(60):
                    await asyncio.sleep(0.5)
                    try:
                        await cdp.js(DEEP)
                        if await cdp.js("!!(window.__vtoCard() && window.__vtoCard().shadowRoot.getElementById('talk'))"):
                            card_ok = True
                            break
                    except RuntimeError:
                        pass
                if not card_ok:
                    shot = await cdp.call("Page.captureScreenshot", format="png")
                    open("/tmp/e2e_fail.png", "wb").write(base64.b64decode(shot["data"]))
                    print("URL:", await cdp.js("location.href"))
                    print("BODY:", (await cdp.js("document.body.innerHTML"))[:400])
                    print("CARD NOT FOUND; console:", *cdp.console[-20:], sep="\n  ")
                    return 1
                print("card found:", await cdp.js("window.__vtoCard().shadowRoot.getElementById('talk').textContent"))

                rect = await cdp.js(
                    "(() => { const r = window.__vtoCard().shadowRoot.getElementById('talk').getBoundingClientRect();"
                    " return {x: r.x + r.width/2, y: r.y + r.height/2}; })()"
                )

                async def mouse_click() -> None:
                    for kind in ("mousePressed", "mouseReleased"):
                        await cdp.call(
                            "Input.dispatchMouseEvent", type=kind, x=rect["x"], y=rect["y"], button="left", clickCount=1
                        )

                await mouse_click()  # a real click: user activation, like a finger on the iPad
                status = ""
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    status = await cdp.js("window.__vtoCard().shadowRoot.getElementById('stxt').textContent")
                    if "Mówisz" in status or "Błąd" in status or "Brak" in status:
                        break
                print("status after click:", status)
                await asyncio.sleep(4)
                status = await cdp.js("window.__vtoCard().shadowRoot.getElementById('stxt').textContent")
                print("status while talking:", status)
                shot = await cdp.call("Page.captureScreenshot", format="png")
                with open(SHOT, "wb") as f:
                    f.write(base64.b64decode(shot["data"]))
                await mouse_click()  # stop
                await asyncio.sleep(2)
                print("status after stop:", await cdp.js("window.__vtoCard().shadowRoot.getElementById('stxt').textContent"))
                async with http.get(FAKE) as r:
                    heard = await r.json()
                print("door speaker:", json.dumps(heard, indent=1)[:1500])
                if cdp.console:
                    print("browser console:", *cdp.console[-10:], sep="\n  ")
                talk = heard["talks"][-1] if heard["talks"] else None
                ok = (
                    talk is not None
                    and talk["payload_type"] == 8
                    and abs(talk["dominant_hz"] - FREQ) <= 10
                    and talk["seconds"] >= 3
                    and not heard["talk_holder"]
                )
                print("RESULT:", "PASS" if ok else "FAIL")
                cdp.reader.cancel()
                return 0 if ok else 1
    finally:
        chrome.terminate()
        chrome.wait()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
