// Node test for the Lovelace card: worklet resampler + talk glue in jsdom.
// Run: node tests/js/card.test.js  (needs `npm i jsdom`)
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { JSDOM } = require("jsdom");

const CARD = path.join(__dirname, "../../custom_components/dahua_vto_talk/frontend/dahua-vto-talk-card.js");
const src = fs.readFileSync(CARD, "utf8");

// ------------------------------------------------------------ worklet DSP
function runWorklet(inputRate, targetRate, freq, seconds) {
  const worklet = src.match(/const WORKLET_SOURCE = `([\s\S]*?)`;/)[1];
  const out = [];
  let Proc;
  const port = { postMessage: (m) => out.push(...new Int16Array(m.pcm)) };
  const fn = new Function(
    "AudioWorkletProcessor", "registerProcessor", "sampleRate",
    worklet + "\nreturn VtoPcmSender;"
  );
  Proc = fn(class { constructor() { this.port = port; } }, () => {}, inputRate);
  const p = new Proc({ processorOptions: { targetRate } });
  const total = Math.round(inputRate * seconds);
  for (let off = 0; off < total; off += 128) {
    const block = new Float32Array(128);
    for (let i = 0; i < 128; i++) block[i] = 0.5 * Math.sin(2 * Math.PI * freq * (off + i) / inputRate);
    p.process([[block]]);
  }
  return out;
}
function dominant(samples, rate) {
  let best = 0, bestP = -1;
  for (let f = 200; f < 3800; f += 10) {
    let re = 0, im = 0;
    for (let i = 0; i < samples.length; i++) {
      re += samples[i] * Math.cos(2 * Math.PI * f * i / rate);
      im += samples[i] * Math.sin(2 * Math.PI * f * i / rate);
    }
    const p = re * re + im * im;
    if (p > bestP) { bestP = p; best = f; }
  }
  return best;
}
for (const inRate of [48000, 44100, 16000, 8000]) {
  const out = runWorklet(inRate, 8000, 700, 1.0);
  assert(Math.abs(out.length - 8000) <= 200, `${inRate}: got ${out.length} samples`);
  const f = dominant(out.slice(400, 4400), 8000);
  assert(Math.abs(f - 700) <= 10, `${inRate}: dominant ${f}`);
  const peak = Math.max(...out.map(Math.abs));
  assert(peak > 14000 && peak < 18000, `${inRate}: amplitude ${peak}`);
}
// an out-of-band tone (5 kHz at 48k input) must be suppressed, not aliased to 3 kHz
{
  const out = runWorklet(48000, 8000, 5000, 1.0);
  const rms = Math.sqrt(out.slice(400).reduce((a, s) => a + s * s, 0) / (out.length - 400));
  assert(rms < 3000, `aliasing too strong: rms ${rms}`);
}
console.log("worklet resampler: ok");

// ------------------------------------------------------------- card glue
(async () => {
  const dom = new JSDOM("<!doctype html><body></body>", { url: "https://ha.local/", pretendToBeVisual: true });
  const { window } = dom;
  global.window = window;
  for (const k of ["document", "customElements", "HTMLElement", "navigator"]) {
    // Node 21+ has its own read-only navigator; replace it with jsdom's
    Object.defineProperty(global, k, { value: window[k], configurable: true, writable: true });
  }
  Object.defineProperty(window, "isSecureContext", { value: true });

  const sent = [];
  const subs = [];
  let unsubscribed = 0;
  let nodeOpts = null;
  let workletNode = null;
  let tracksStopped = 0;
  const services = [];

  class FakeNode {
    constructor(ctx, name, opts) { nodeOpts = opts; this.port = {}; workletNode = this; }
    disconnect() {}
  }
  window.AudioWorkletNode = FakeNode;
  global.AudioWorkletNode = FakeNode;
  window.AudioContext = class {
    constructor() { this.sampleRate = 48000; this.audioWorklet = { addModule: async () => {} }; }
    resume() { return Promise.resolve(); }
    close() { return Promise.resolve(); }
    createMediaStreamSource() { return { connect: () => {} }; }
  };
  window.URL.createObjectURL = () => "blob:x";
  window.URL.revokeObjectURL = () => {};
  global.URL = window.URL;
  global.Blob = window.Blob;
  Object.defineProperty(window.navigator, "mediaDevices", {
    value: { getUserMedia: async (c) => {
      assert.strictEqual(c.audio.echoCancellation, true);
      return { getTracks: () => [{ stop: () => tracksStopped++ }] };
    } },
  });

  const script = window.document.createElement("script");
  window.eval(src.replace(/if \(typeof module[\s\S]*$/, ""));

  const card = window.document.createElement("dahua-vto-talk-card");
  card.setConfig({ open_door_entity: "button.domofon_open_door" });
  window.document.body.appendChild(card);
  card.hass = {
    language: "pl",
    states: {},
    connection: {
      socket: { readyState: 1, send: (b) => sent.push(b) },
      subscribeMessage: async (cb, msg) => { subs.push({ cb, msg }); return () => { unsubscribed++; }; },
    },
    callService: async (d, s, data) => services.push([d, s, data]),
  };
  await new Promise((r) => setTimeout(r, 10));
  const root = card.shadowRoot;
  const talk = root.getElementById("talk");
  assert(talk.textContent.includes("Mów"), talk.textContent);

  talk.click();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(subs.length, 1);
  assert.deepStrictEqual(subs[0].msg, { type: "dahua_vto_talk/talk" });
  assert(root.getElementById("stxt").textContent.includes("Łączenie"));

  await subs[0].cb({ event: "ready", handler_id: 3, sample_rate: 8000, codec: "PCMA/8000", packet_ms: 128 });
  assert.strictEqual(nodeOpts.processorOptions.targetRate, 8000);
  assert(root.getElementById("stxt").textContent.includes("Mówisz • PCMA/8000"));

  // worklet emits one 20 ms frame
  const frame = new Int16Array(160).fill(1000);
  workletNode.port.onmessage({ data: { pcm: frame.buffer, peak: 0.03 } });
  assert.strictEqual(sent.length, 1);
  assert.strictEqual(sent[0][0], 3, "first byte must be the handler id");
  assert.strictEqual(sent[0].length, 321);
  assert.strictEqual(sent[0][1] | (sent[0][2] << 8), 1000, "16-bit little endian");

  await subs[0].cb({ event: "stats", packets_sent: 7, buffered_ms: 20 });
  assert(root.getElementById("stxt").textContent.includes("pakiety: 7"));

  talk.click(); // stop
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(unsubscribed, 1);
  assert.strictEqual(tracksStopped, 1);
  assert(talk.textContent.includes("Mów"));

  // error path
  talk.click();
  await new Promise((r) => setTimeout(r, 10));
  await subs[1].cb({ event: "error", message: "Kanał mówienia jest zajęty." });
  assert(root.getElementById("stxt").textContent.includes("zajęty"));
  assert.strictEqual(unsubscribed, 2);

  root.getElementById("open").click();
  await new Promise((r) => setTimeout(r, 10));
  assert.deepStrictEqual(services[0], ["button", "press", { entity_id: "button.domofon_open_door" }]);

  console.log("card glue: ok");

  // ---- camera: must start muted (iOS autoplay), 🔊 and "Talk" unmute it
  class FakeStream extends window.HTMLElement {
    constructor() { super(); this.muted = false; }
    connectedCallback() { if (!this.video) { this.video = window.document.createElement("video"); this.appendChild(this.video); } }
  }
  window.customElements.define("ha-camera-stream", FakeStream);
  const cam = window.document.createElement("dahua-vto-talk-card");
  cam.setConfig({ camera_entity: "camera.domofon_main" });
  window.document.body.appendChild(cam);
  cam.hass = Object.assign({}, card.hass, { states: { "camera.domofon_main": { entity_id: "camera.domofon_main", state: "idle" } } });
  await new Promise((r) => setTimeout(r, 30));
  const stream = cam.shadowRoot.querySelector("ha-camera-stream");
  assert(stream, "camera element mounted");
  assert.strictEqual(stream.muted, true, "camera must start muted so iOS autoplays the picture");
  const listen = cam.shadowRoot.getElementById("listen");
  assert.strictEqual(listen.textContent, "🔇");
  listen.click();
  assert.strictEqual(stream.muted, false);
  assert.strictEqual(stream.video.muted, false);
  assert.strictEqual(listen.textContent, "🔊");
  listen.click();
  assert.strictEqual(stream.muted, true);
  cam.shadowRoot.getElementById("talk").click();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(stream.muted, false, "talking turns door audio on");
  assert.strictEqual(listen.textContent, "🔊");
  console.log("camera muting: ok");
})().catch((e) => { console.error(e); process.exit(1); });
