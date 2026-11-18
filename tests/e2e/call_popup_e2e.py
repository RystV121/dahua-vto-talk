"""Real-browser test of the call popup.

Chrome (fake mic: 600 Hz) shows a dashboard WITHOUT the card; the doorbell
"rings" (dahua_vto_talk.ring); the popup must appear over that page; Answer
opens mic + door sound, Mute sends silence, Hang up closes everything. The
simulated VTO must have heard the tone and be released; the call sensor must
go ringing -> answered -> idle.

Needs: HA on 127.0.0.1:18123 with the preview e2e config (views "domofon-cam"
(its card has call_popup: true, so this browser opts in)
and "elsewhere"), the fake VTO (tests/e2e/run_fake_vto.py), go2rtc for the
preview (optional) and google-chrome.

    python tests/e2e/call_popup_e2e.py [screenshot.png]
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from browser_e2e import FAKE, PORT, Cdp, make_mic_wav  # noqa: E402
from preview_audio_e2e import HA, login  # noqa: E402

SHOT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/call_popup.png"

POPUP = """(() => {
  const pop = document.querySelector('.dahua-vto-popup');
  if (!pop) return null;
  const card = pop.querySelector('dahua-vto-talk-card');
  if (!card || !card.shadowRoot) return {popup: true};
  const r = card.shadowRoot;
  const vis = (id) => { const el = r.getElementById(id); return !!el && !el.hidden; };
  const center = (id) => { const el = r.getElementById(id); if (!el) return null;
    const b = el.getBoundingClientRect(); return {x: b.x + b.width / 2, y: b.y + b.height / 2}; };
  return {popup: true, answer: vis('answer'), hangup: vis('hangup'), mute: vis('mute'),
          status: r.getElementById('stxt').textContent, ring: r.getElementById('ringtxt').textContent,
          micMuted: !!card._micMuted, inCall: !!card._inCall,
          at: {answer: center('answer'), mute: center('mute'), hangup: center('hangup')}};
})()"""


async def state(http: aiohttp.ClientSession, token: str, contains: str) -> str:
    async with http.get(f"{HA}/api/states", headers={"Authorization": f"Bearer {token}"}) as r:
        for s in await r.json():
            if s["entity_id"].startswith("sensor.") and contains in s["entity_id"]:
                return s["state"]
    return "?"


async def main() -> int:
    wav = "/tmp/mic600.wav"
    make_mic_wav(wav)
    chrome = subprocess.Popen(
        [
            "google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--remote-debugging-port={PORT}", "--user-data-dir=/tmp/chrome-call",
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={wav}", "--allow-loopback-in-peer-connection",
            "--disable-features=WebRtcHideLocalIpsWithMdns", "--window-size=1180,820", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    ok = False
    try:
        async with aiohttp.ClientSession() as http:
            tokens = await login(http)
            tok = tokens["access_token"]
            head = {"Authorization": f"Bearer {tok}"}
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
                # 1) the card is seen once on this device (remembers its config for the popup)
                await cdp.call("Page.navigate", url=f"{HA}/lovelace/domofon-cam")
                await asyncio.sleep(6)
                cfg = await cdp.js("localStorage.getItem('dahua_vto_talk_popup_config')")
                print("popup config remembered:", cfg)
                # 2) somewhere else, no card on screen
                await cdp.call("Page.navigate", url=f"{HA}/lovelace/elsewhere")
                await asyncio.sleep(5)
                print("card on screen:", await cdp.js("!!document.querySelector('home-assistant')") )
                # 3) the doorbell rings
                async with http.post(f"{HA}/api/services/dahua_vto_talk/ring", headers=head, json={}) as r:
                    assert r.status == 200, await r.text()
                info = None
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    info = await cdp.js(POPUP)
                    if info and info.get("answer"):
                        break
                print("popup:", {k: v for k, v in (info or {}).items() if k != "at"})
                print("call sensor:", await state(http, tok, "polaczenie"))
                assert info and info.get("answer"), "popup did not appear"
                await asyncio.sleep(4)  # let the preview start for the screenshot
                shot = await cdp.call("Page.captureScreenshot", format="png")
                Path(SHOT.replace(".png", "_ringing.png")).write_bytes(base64.b64decode(shot["data"]))

                async def click(name: str) -> None:
                    at = (await cdp.js(POPUP))["at"][name]
                    for kind in ("mousePressed", "mouseReleased"):
                        await cdp.call("Input.dispatchMouseEvent", type=kind, x=at["x"], y=at["y"], button="left", clickCount=1)

                # 4) answer: mic + door sound
                await click("answer")
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    info = await cdp.js(POPUP)
                    if info and ("Mówisz" in info["status"] or "Talking" in info["status"]):
                        break
                print("after answer:", {k: v for k, v in info.items() if k != "at"})
                print("call sensor:", await state(http, tok, "polaczenie"))
                await asyncio.sleep(3)
                shot = await cdp.call("Page.captureScreenshot", format="png")
                Path(SHOT).write_bytes(base64.b64decode(shot["data"]))
                # 5) mute for 2 s, unmute
                await click("mute")
                await asyncio.sleep(0.3)
                print("muted:", (await cdp.js(POPUP))["micMuted"])
                await asyncio.sleep(2)
                await click("mute")
                await asyncio.sleep(2)
                # 6) hang up
                await click("hangup")
                await asyncio.sleep(2.5)
                gone = await cdp.js(POPUP)
                print("popup after hang up:", gone)
                print("call sensor:", await state(http, tok, "polaczenie"))
                async with http.get(FAKE) as r:
                    heard = await r.json()
                talk = heard["talks"][-1] if heard["talks"] else None
                print("door speaker:", json.dumps(talk)[:400] if talk else None, "held:", heard["talk_holder"])
                if cdp.console:
                    print("browser console:", *cdp.console[-8:], sep="\n  ")
                ok = (
                    gone is None
                    and talk is not None
                    and abs(talk["dominant_hz"] - 600) <= 10
                    and talk["seconds"] >= 4
                    and not heard["talk_holder"]
                    and await state(http, tok, "polaczenie") in ("idle", "Brak")
                )
                print("RESULT:", "PASS" if ok else "FAIL")
                cdp.reader.cancel()
    finally:
        chrome.terminate()
        chrome.wait()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
