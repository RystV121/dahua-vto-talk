"""Real-browser test: is the door sound audible in the card's WebRTC preview?

Chain under test (all real software, only the door station is simulated):

  simulated VTO sub stream (go2rtc + ffmpeg, H.264 + a 600 Hz tone)
    -> go2rtc (what the WebRTC Camera integration talks to)
    -> Home Assistant + WebRTC Camera integration (AlexxIT, real code)
    -> dahua-vto-talk-card with camera_card: custom:webrtc-camera
    -> Chrome: a real click on 🔊, then the <video>'s audio is analysed.

Two sub stream variants: G.711A 8 kHz (what the integration sets) and the
VTO factory default PCM 16 kHz (L16).

    python tests/e2e/preview_audio_e2e.py setup /tmp/ha-prev /tmp/webrtc-src
    (start HA on that config and both go2rtc instances, see README)
    python tests/e2e/preview_audio_e2e.py run
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent))
from browser_e2e import DEEP, PORT, Cdp  # noqa: E402
import setup_ha  # noqa: E402

HA = "http://127.0.0.1:18123"
PCMA = "rtsp://127.0.0.1:18555/vto_sub_pcma#backchannel=0"
L16 = "rtsp://127.0.0.1:18555/vto_sub_l16#backchannel=0"
# name: (sub stream url, player mode). iPads usually end up on WebRTC.
VARIANTS = {
    "pcma": (PCMA, "webrtc,mse,hls,mjpeg"),
    "l16": (L16, "webrtc,mse,hls,mjpeg"),
    "pcma-webrtc": (PCMA, "webrtc"),
    "l16-webrtc": (L16, "webrtc"),
    # iPad away from home: WebRTC does not connect, the player falls back to
    # MSE, and Safari's MSE cannot play FLAC (go2rtc's wrapper for PCMA)
    "safari-mse": (PCMA, "mse"),
    # fix: a go2rtc stream that also offers AAC (ffmpeg) next to PCMA
    "safari-mse-aac": ("domofon_sub", "mse"),
    # the same fixed stream must still give WebRTC players their PCMA sound
    "fixed-webrtc": ("domofon_sub", "webrtc"),
}

# pretend to be Safari on the "safari-*" views: no FLAC in MediaSource
SAFARI_LIKE = """
if (location.pathname.includes('safari')) {
  for (const MS of [window.MediaSource, window.ManagedMediaSource]) {
    if (!MS) continue;
    const orig = MS.isTypeSupported.bind(MS);
    MS.isTypeSupported = (t) => (/flac/i.test(t) ? false : orig(t));
  }
}
"""


def dashboard() -> str:
    views = []
    for name, (url, mode) in VARIANTS.items():
        views.append(
            f"""  - title: {name}
    path: {name}
    cards:
      - type: custom:dahua-vto-talk-card
        camera_card:
          type: custom:webrtc-camera
          url: "{url}"
          mode: "{mode}"
          muted: true
"""
        )
    return "title: Domofon\nviews:\n" + "".join(views)


def setup(target: str, webrtc_src: str) -> None:
    setup_ha.main(target)
    cfg = Path(target)
    shutil.copytree(Path(webrtc_src) / "custom_components" / "webrtc", cfg / "custom_components" / "webrtc")
    (cfg / "ui-lovelace.yaml").write_text(dashboard())
    entries_file = cfg / ".storage" / "core.config_entries"
    data = json.loads(entries_file.read_text())
    now = datetime.now(UTC).isoformat()
    data["data"]["entries"].append(
        {
            "created_at": now,
            "data": {},
            "disabled_by": None,
            "discovery_keys": {},
            "domain": "webrtc",
            "entry_id": uuid.uuid4().hex,
            "minor_version": 1,
            "modified_at": now,
            "options": {},
            "pref_disable_new_entities": False,
            "pref_disable_polling": False,
            "source": "user",
            "subentries": [],
            "title": "WebRTC Camera",
            "unique_id": None,
            "version": 1,
        }
    )
    entries_file.write_text(json.dumps(data))
    print("preview config ready in", cfg)


async def login(http: aiohttp.ClientSession) -> dict:
    client_id = f"{HA}/"
    async with http.post(f"{HA}/auth/login_flow", json={"client_id": client_id, "handler": ["homeassistant", None], "redirect_uri": client_id}) as r:
        flow = await r.json()
    async with http.post(
        f"{HA}/auth/login_flow/{flow['flow_id']}",
        json={"client_id": client_id, "username": "tester", "password": "tester1234"},
    ) as r:
        code = (await r.json())["result"]
    async with http.post(f"{HA}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id}) as r:
        tokens = await r.json()
    tokens.update({"hassUrl": HA, "clientId": client_id, "expires": 0})
    return tokens


MEASURE = """
(async (analyse) => {
  const card = window.__vtoCard();
  const find = (root) => {
    if (!root) return null;
    if (root.tagName === 'VIDEO') return root;
    const kids = root.shadowRoot ? [...root.shadowRoot.children, ...root.children] : [...root.children];
    for (const k of kids) { const v = find(k); if (v) return v; }
    return null;
  };
  const video = find(card.shadowRoot.getElementById('cam'));
  if (!video) return {error: 'no video'};
  const out = {muted: video.muted, volume: video.volume, time: video.currentTime,
               srcObject: !!video.srcObject, src: (video.src || '').slice(0, 20),
               rtcAudioTracks: video.srcObject ? video.srcObject.getAudioTracks().length : null};
  const wc = card.shadowRoot.querySelector('webrtc-camera');
  out.mode = wc && wc.shadowRoot ? (wc.shadowRoot.querySelector('.mode') || {}).textContent : null;
  let stream;
  try { stream = video.captureStream(); } catch (e) { out.capture = String(e); return out; }
  out.capturedAudioTracks = stream.getAudioTracks().length;
  if (!out.capturedAudioTracks || !analyse) return out;
  const ctx = new AudioContext();
  await Promise.race([ctx.resume(), new Promise(r => setTimeout(r, 2000))]);
  out.ctxState = ctx.state;
  const an = ctx.createAnalyser();
  an.fftSize = 8192;
  ctx.createMediaStreamSource(new MediaStream(stream.getAudioTracks())).connect(an);
  await new Promise(r => setTimeout(r, 1500));
  const bins = new Float32Array(an.frequencyBinCount);
  an.getFloatFrequencyData(bins);
  const hz = ctx.sampleRate / an.fftSize;
  let best = 0;
  for (let i = 1; i < bins.length; i++) if (bins[i] > bins[best]) best = i;
  const sorted = [...bins].filter(Number.isFinite).sort((a, b) => a - b);
  out.peakHz = Math.round(best * hz);
  out.peakDb = Math.round(bins[best]);
  out.medianDb = Math.round(sorted[Math.floor(sorted.length / 2)]);
  ctx.close();
  return out;
})
"""


async def check(cdp: Cdp, name: str) -> dict:
    await cdp.call("Page.navigate", url=f"{HA}/lovelace/{name}")
    await asyncio.sleep(1)
    await cdp.call("Page.reload")  # a fresh document, so the Safari-like override applies
    for _ in range(60):
        await asyncio.sleep(0.5)
        try:
            await cdp.js(DEEP)
            if await cdp.js("!!(window.__vtoCard() && window.__vtoCard().shadowRoot.getElementById('listen'))"):
                break
        except RuntimeError:
            pass
    # let go2rtc connect and the player settle on WebRTC or MSE
    await asyncio.sleep(8)
    before = await cdp.js(MEASURE + '(false)')
    rect = await cdp.js(
        "(() => { const r = window.__vtoCard().shadowRoot.getElementById('listen').getBoundingClientRect();"
        " return {x: r.x + r.width/2, y: r.y + r.height/2}; })()"
    )
    for kind in ("mousePressed", "mouseReleased"):
        await cdp.call("Input.dispatchMouseEvent", type=kind, x=rect["x"], y=rect["y"], button="left", clickCount=1)
    await asyncio.sleep(2)
    after = await cdp.js(MEASURE + '(true)')
    diag = await cdp.js("window.__vtoCard().shadowRoot.getElementById('stxt').textContent")
    shot = await cdp.call("Page.captureScreenshot", format="png")
    Path(f"/tmp/preview_{name}.png").write_bytes(base64.b64decode(shot["data"]))
    audible = (
        not after.get("muted", True)
        and after.get("volume", 0) > 0
        and abs(after.get("peakHz", 0) - 600) <= 15
        and after.get("peakDb", -200) - after.get("medianDb", 0) > 30
    )
    return {"before_click": before, "after_click": after, "card_diag": diag, "audible": audible}


async def run() -> int:
    chrome = subprocess.Popen(
        [
            "google-chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--remote-debugging-port={PORT}", "--user-data-dir=/tmp/chrome-prev",
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            "--allow-loopback-in-peer-connection", "--disable-features=WebRtcHideLocalIpsWithMdns", "--window-size=900,800", "about:blank",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as http:
            tokens = await login(http)
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
                await cdp.call("Page.addScriptToEvaluateOnNewDocument", source=SAFARI_LIKE)
                await cdp.call("Page.navigate", url=f"{HA}/auth/authorize?response_type=code&client_id={HA}/")
                await asyncio.sleep(2)
                await cdp.js(f"localStorage.setItem('hassTokens', {json.dumps(json.dumps(tokens))}); true")
                results = {name: await check(cdp, name) for name in VARIANTS}
                print(json.dumps(results, indent=1))
                if cdp.console:
                    print("browser console:", *cdp.console[-15:], sep="\n  ")
                cdp.reader.cancel()
                return 0 if results["pcma"]["audible"] else 1
    finally:
        chrome.terminate()
        chrome.wait()


if __name__ == "__main__":
    if sys.argv[1:2] == ["setup"]:
        setup(sys.argv[2], sys.argv[3])
    else:
        sys.exit(asyncio.run(run()))
