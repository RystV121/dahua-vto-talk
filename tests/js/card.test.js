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
  const reports = [];
  let contexts = 0;

  class FakeNode {
    constructor(ctx, name, opts) { nodeOpts = opts; this.port = {}; workletNode = this; }
    disconnect() {}
  }
  window.AudioWorkletNode = FakeNode;
  global.AudioWorkletNode = FakeNode;
  window.AudioContext = class {
    constructor() { contexts += 1; this.state = "suspended"; this.sampleRate = 48000; this.audioWorklet = { addModule: async () => {} }; }
    resume() { this.state = "running"; return Promise.resolve(); }
    close() { this.state = "closed"; return Promise.resolve(); }
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
    callWS: async (m) => { reports.push(m); },
  };
  await new Promise((r) => setTimeout(r, 10));
  const root = card.shadowRoot;
  const talk = root.getElementById("talk");
  assert(talk.textContent.includes("Mów"), talk.textContent);
  // nothing said yet: no summary / playback button
  assert.strictEqual(root.getElementById("summary").hidden, true);
  const css = root.querySelector("style").textContent;
  assert(/\.summary\[hidden\]\s*\{\s*display:\s*none/.test(css), "hidden must win over display:flex");

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

  // iOS: a fresh AudioContext is made after the microphone opened
  assert.strictEqual(contexts, 2, "early (unlock) context + fresh context after getUserMedia");

  talk.click(); // stop
  await new Promise((r) => setTimeout(r, 10));
  // the card tells HA what it saw: frames, level, audio context
  const rep = reports.find((r) => r.type === "dahua_vto_talk/client_report" && r.kind === "talk");
  assert(rep, "talk report sent");
  assert.strictEqual(rep.data.frames_sent, 1);
  assert.strictEqual(rep.data.max_peak, 0.03);
  assert.strictEqual(rep.data.ctx_rate, 48000);
  assert.strictEqual(rep.data.fresh_context, true);
  assert.strictEqual(unsubscribed, 1);
  assert.strictEqual(tracksStopped, 1);
  assert(talk.textContent.includes("Mów"));
  // after stopping: how much was sent and how loud, plus the playback button
  const summary = root.getElementById("summary");
  assert.strictEqual(summary.hidden, false);
  assert(root.getElementById("sumtxt").textContent.includes("Wysłano 0.0 s • max głośność 3%"), root.getElementById("sumtxt").textContent);
  assert(!summary.classList.contains("warn"));

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
  assert(!listen.classList.contains("on"), "door sound starts off");
  listen.click();
  assert.strictEqual(stream.muted, false);
  assert.strictEqual(stream.video.muted, false);
  assert(listen.classList.contains("on"));
  listen.click();
  assert.strictEqual(stream.muted, true);
  cam.shadowRoot.getElementById("talk").click();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(stream.muted, false, "talking turns door audio on");
  assert(listen.classList.contains("on"));
  console.log("camera muting: ok");

  // ---- call mode: ringing -> answer -> mute -> hang up
  const callCard = window.document.createElement("dahua-vto-talk-card");
  callCard.setConfig({ open_door_entity: "button.domofon_open_door" });
  window.document.body.appendChild(callCard);
  callCard.hass = card.hass;
  await new Promise((r) => setTimeout(r, 10));
  const cr = callCard.shadowRoot;
  assert.strictEqual(cr.getElementById("answer").hidden, true, "no call: no answer button");
  window.dispatchEvent(new window.CustomEvent("dahua-vto-call", { detail: { entry_id: "e1", state: "ringing", since: new Date().toISOString() } }));
  assert.strictEqual(cr.getElementById("answer").hidden, false);
  assert.strictEqual(cr.getElementById("decline").hidden, false);
  assert.strictEqual(cr.getElementById("talk").hidden, true);
  assert.strictEqual(cr.getElementById("ringbar").hidden, false);
  assert(cr.getElementById("ringtxt").textContent.includes("dzwoni"));
  const subsBefore = subs.length;
  cr.getElementById("answer").click();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(subs.length, subsBefore + 1, "answer opens the talk channel");
  assert(reports.some((m) => m.type === "dahua_vto_talk/call_action" && m.action === "answer"), "server told: answered");
  assert.strictEqual(cr.getElementById("hangup").hidden, false);
  assert.strictEqual(cr.getElementById("mute").hidden, false);
  assert.strictEqual(cr.getElementById("answer").hidden, true);
  await subs[subs.length - 1].cb({ event: "ready", handler_id: 4, sample_rate: 8000, codec: "PCMA/8000", packet_ms: 128 });
  cr.getElementById("mute").click();
  const n0 = sent.length;
  workletNode.port.onmessage({ data: { pcm: new Int16Array(160).fill(5000).buffer, peak: 0.15 } });
  assert.strictEqual(sent.length, n0 + 1, "muted mic still sends frames (no gaps at the door)");
  assert(sent[n0].slice(1).every((b) => b === 0), "...but they are silent");
  cr.getElementById("mute").click(); // unmute
  workletNode.port.onmessage({ data: { pcm: new Int16Array(160).fill(5000).buffer, peak: 0.15 } });
  assert(sent[n0 + 1].slice(1).some((b) => b !== 0), "unmuted: real audio again");
  const unsubBefore = unsubscribed;
  cr.getElementById("hangup").click();
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(unsubscribed, unsubBefore + 1, "hang up closes the talk channel");
  assert(reports.some((m) => m.type === "dahua_vto_talk/call_action" && m.action === "hangup"));
  // the server answers the hang-up with "idle"
  window.dispatchEvent(new window.CustomEvent("dahua-vto-call", { detail: { entry_id: "e1", state: "idle" } }));
  assert.strictEqual(cr.getElementById("talk").hidden, false, "back to the normal buttons");
  console.log("call mode: ok");

  // ---- popup over any page when it rings, closes on decline
  const W = "window.__dahuaVtoCallWatcher";
  const ha = window.document.createElement("home-assistant");
  ha.hass = card.hass;
  window.document.body.appendChild(ha);
  // off by default: a device that never opted in does not ring
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'ringing', since: new Date().toISOString()})`);
  assert(!window.document.querySelector(".dahua-vto-popup"), "not opted in: no popup");
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'idle'})`);
  // the wall panel's card opts this device in
  const wall = window.document.createElement("dahua-vto-talk-card");
  wall.setConfig({ call_popup: true, open_door_entity: "button.domofon_open_door", title: "Panel" });
  assert.strictEqual(window.localStorage.getItem("dahua_vto_talk_popup"), "on");
  // another card shown later does not replace the wall panel's look
  window.document.createElement("dahua-vto-talk-card").setConfig({ title: "Inna" });
  assert(JSON.parse(window.localStorage.getItem("dahua_vto_talk_popup_config")).title === "Panel");
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'ringing', since: new Date().toISOString()})`);
  const pop = window.document.querySelector(".dahua-vto-popup");
  assert(pop, "popup shown");
  const pcard = pop.querySelector("dahua-vto-talk-card");
  await new Promise((r) => setTimeout(r, 10));
  assert.strictEqual(pcard.shadowRoot.getElementById("answer").hidden, false);
  assert(pcard.shadowRoot.getElementById("open"), "popup uses this device's card config (open button)");
  pcard.shadowRoot.getElementById("decline").click();
  await new Promise((r) => setTimeout(r, 10));
  assert(!window.document.querySelector(".dahua-vto-popup"), "declined: popup gone");
  // answered on another screen -> popup closes by itself
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'ringing', since: new Date().toISOString()})`);
  assert(window.document.querySelector(".dahua-vto-popup"));
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'answered', by: 'other-ipad'})`);
  assert(!window.document.querySelector(".dahua-vto-popup"), "answered elsewhere: popup gone");
  // turned off on this device -> no popup
  window.localStorage.setItem("dahua_vto_talk_popup", "off");
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'ringing'})`);
  assert(!window.document.querySelector(".dahua-vto-popup"), "popup off on this device");
  window.localStorage.removeItem("dahua_vto_talk_popup");
  window.eval(`${W}.onInfo({entry_id: 'e1', state: 'idle'})`);
  console.log("call popup: ok");

  // ---- the subscription comes back after a restart / reconnect
  const listeners = {};
  let attempts = 0;
  const live = [];
  const fakeConn = {
    addEventListener: (ev, fn) => { listeners[ev] = fn; },
    subscribeMessage: async (cb, msg, opts) => {
      attempts += 1;
      assert.deepStrictEqual(msg, { type: "dahua_vto_talk/subscribe_call" });
      assert.strictEqual(opts.resubscribe, false);
      if (attempts === 2) throw { code: "unknown_command" }; // integration still starting
      live.push(cb);
      return async () => { live.splice(live.indexOf(cb), 1); };
    },
  };
  const Wt = window.__dahuaVtoCallWatcher;
  window.hassConnection = Promise.resolve({ conn: fakeConn });
  Wt.started = false;
  Wt.start();
  await new Promise((r) => setTimeout(r, 20));
  assert.strictEqual(live.length, 1, "subscribed once");
  listeners.ready(); // websocket came back (HA restarted)
  await new Promise((r) => setTimeout(r, 2100)); // first retry fails...
  await new Promise((r) => setTimeout(r, 2100)); // ...the next one works
  assert.strictEqual(attempts, 3);
  assert.strictEqual(live.length, 1, "exactly one live subscription again");
  assert.strictEqual(Wt.subscribed, true);
  console.log("call popup reconnect: ok");
  setTimeout(() => {
    const busy = [...window.document.querySelectorAll("dahua-vto-talk-card")].filter((c) => c._clock).length;
    console.log("cards with a running clock:", busy);
  }, 1500);
})().catch((e) => { console.error(e); process.exit(1); });
