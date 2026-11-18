/* Dahua VTO Talk card
 *
 * Shows the door camera (your existing camera entity) and a talk button.
 * The microphone is captured in the browser, resampled to the rate the
 * door station asked for (8 kHz for G.711A), turned into 16-bit PCM and sent
 * to Home Assistant over the websocket's binary channel (the same one Assist
 * uses). Home Assistant encodes it and plays it on the VTO speaker through
 * the ONVIF RTSP backchannel.
 *
 * type: custom:dahua-vto-talk-card
 * camera_entity: camera.domofon_main      # optional
 * open_door_entity: button.domofon_open_door   # optional
 * mode: toggle | hold                     # default toggle
 * mute_while_talking: false               # mute door audio while you talk
 * entry_id: ...                           # only with several door stations
 */

const CARD_VERSION = "0.1.1";

const WORKLET_SOURCE = `
class VtoPcmSender extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = options.processorOptions || {};
    this.target = o.targetRate || 8000;
    this.ratio = sampleRate / this.target;
    this.frame = Math.max(80, Math.round(this.target / 50)); // 20 ms
    this.out = new Int16Array(this.frame);
    this.n = 0;
    this.t = 0;
    this.prev = 0;
    this.peak = 0;
    this.frames = 0;
    // 6th order Butterworth low-pass (3 RBJ biquads) at ~0.41 x target
    // rate, so 4-8 kHz content from a 48 kHz mic does not alias into the
    // telephone band the door speaker plays
    const fc = Math.min(0.4125 * this.target, 0.45 * sampleRate);
    const w0 = 2 * Math.PI * fc / sampleRate;
    const cos = Math.cos(w0);
    this.stages = [0.5176, 0.7071, 1.9319].map((q) => {
      const alpha = Math.sin(w0) / (2 * q);
      const a0 = 1 + alpha;
      return {
        b0: ((1 - cos) / 2) / a0, b1: (1 - cos) / a0, b2: ((1 - cos) / 2) / a0,
        a1: (-2 * cos) / a0, a2: (1 - alpha) / a0, x1: 0, x2: 0, y1: 0, y2: 0,
      };
    });
    this.filter = this.ratio > 1.01;
  }
  push(sample) {
    let v = sample;
    if (v > 1) v = 1; else if (v < -1) v = -1;
    const s = v < 0 ? v * 0x8000 : v * 0x7fff;
    this.out[this.n++] = s;
    const a = Math.abs(s);
    if (a > this.peak) this.peak = a;
    if (this.n === this.frame) {
      const buf = this.out.buffer.slice(0);
      this.port.postMessage({ pcm: buf, peak: this.peak / 32768 }, [buf]);
      this.n = 0;
      this.peak = 0;
    }
  }
  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch = input[0];
    const L = ch.length;
    // filter in place into a copy
    const x = new Float32Array(L + 1);
    x[0] = this.prev;
    for (let i = 0; i < L; i++) {
      let v = ch[i];
      if (this.filter) {
        for (const f of this.stages) {
          const y = f.b0 * v + f.b1 * f.x1 + f.b2 * f.x2 - f.a1 * f.y1 - f.a2 * f.y2;
          f.x2 = f.x1; f.x1 = v; f.y2 = f.y1; f.y1 = y; v = y;
        }
      }
      x[i + 1] = v;
    }
    let t = this.t;
    while (t < L) {
      const i = Math.floor(t);
      const f = t - i;
      this.push(x[i] + (x[i + 1] - x[i]) * f);
      t += this.ratio;
    }
    this.t = t - L;
    this.prev = x[L];
    return true;
  }
}
registerProcessor("vto-pcm-sender", VtoPcmSender);
`;

const TEXT = {
  pl: {
    talk: "Mów",
    stop: "Zakończ",
    hold: "Przytrzymaj, żeby mówić",
    open: "Otwórz furtkę",
    connecting: "Łączenie z domofonem…",
    listen: "Dźwięk z furtki",
    talking: "Mówisz",
    idle: "Gotowy",
    mic_denied: "Brak dostępu do mikrofonu. Zezwól na mikrofon dla tej strony / aplikacji (wymagane HTTPS).",
    no_https: "Mikrofon działa tylko przez HTTPS (lub localhost).",
    not_setup: "Integracja Dahua VTO Talk nie jest skonfigurowana.",
    packets: "pakiety",
    ended: "Połączenie zakończone",
  },
  en: {
    talk: "Talk",
    stop: "Stop",
    hold: "Hold to talk",
    open: "Open gate",
    connecting: "Connecting to the door…",
    listen: "Door audio",
    talking: "Talking",
    idle: "Ready",
    mic_denied: "Microphone access denied. Allow the microphone for this page / app (HTTPS required).",
    no_https: "The microphone only works over HTTPS (or localhost).",
    not_setup: "Dahua VTO Talk is not set up.",
    packets: "packets",
    ended: "Call ended",
  },
};

class DahuaVtoTalkCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._state = "idle";
    this._status = "";
    this._error = "";
    this._level = 0;
    this._stats = null;
    this._built = false;
  }

  static getStubConfig(hass) {
    const cams = Object.keys(hass.states).filter((e) => e.startsWith("camera."));
    const main = cams.find((e) => /main|domofon|door|vto/i.test(e)) || cams[0];
    const open = Object.keys(hass.states).find((e) => e.startsWith("button.") && /open_door/i.test(e));
    const cfg = {};
    if (main) cfg.camera_entity = main;
    if (open) cfg.open_door_entity = open;
    return cfg;
  }

  setConfig(config) {
    this._config = { mode: "toggle", mute_while_talking: false, ...config };
    this._built = false;
    if (this._hass) this._build();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._built) this._build();
    if (this._camera) {
      this._camera.hass = hass;
      if (this._config.camera_entity && "stateObj" in this._camera) {
        const obj = hass.states[this._config.camera_entity];
        if (obj && this._camera.stateObj !== obj) this._camera.stateObj = obj;
      }
    }
    if (first) this._render();
  }

  getCardSize() {
    return this._config && this._config.camera_entity ? 6 : 2;
  }

  get _t() {
    const lang = (this._hass && this._hass.language) || "en";
    return TEXT[lang.startsWith("pl") ? "pl" : "en"];
  }

  disconnectedCallback() {
    this._stopTalk();
  }

  // ---------------------------------------------------------------- UI

  async _build() {
    if (!this._config || !this._hass) return;
    this._built = true;
    const t = this._t;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .cam { position: relative; background: #000; min-height: 40px; }
        .bar { display: flex; gap: 8px; padding: 12px; align-items: center; flex-wrap: wrap; }
        button.main {
          flex: 1 1 140px; min-height: 56px; border: 0; border-radius: 28px; font-size: 18px; font-weight: 600;
          color: white; background: var(--success-color, #2e7d32); cursor: pointer; user-select: none;
          -webkit-user-select: none; touch-action: none;
        }
        button.main.on { background: var(--error-color, #c62828); }
        button.main.busy { opacity: .7; }
        button.listen {
          flex: 0 0 56px; min-height: 56px; border: 0; border-radius: 28px; font-size: 22px;
          background: var(--secondary-background-color, #444); color: var(--primary-text-color); cursor: pointer;
        }
        button.listen.on { background: var(--info-color, #039be5); color: white; }
        button.open {
          flex: 0 1 160px; min-height: 56px; border: 0; border-radius: 28px; font-size: 16px;
          background: var(--primary-color); color: var(--text-primary-color, white); cursor: pointer;
        }
        .status { padding: 0 16px 12px; font-size: 13px; color: var(--secondary-text-color); display:flex; gap:8px; align-items:center; }
        .status.err { color: var(--error-color, #c62828); }
        .meter { width: 60px; height: 6px; background: var(--divider-color); border-radius: 3px; overflow: hidden; }
        .meter > div { height: 100%; width: 0; background: var(--success-color, #2e7d32); transition: width .1s; }
      </style>
      <ha-card ${this._config.title ? `header="${this._config.title}"` : ""}>
        ${this._config.camera_entity ? '<div class="cam" id="cam"></div>' : ""}
        <div class="bar">
          <button class="main" id="talk">🎤 ${this._config.mode === "hold" ? t.hold : t.talk}</button>
          ${this._config.camera_entity ? `<button class="listen" id="listen" title="${t.listen}">🔇</button>` : ""}
          ${this._config.open_door_entity ? `<button class="open" id="open">🔓 ${t.open}</button>` : ""}
        </div>
        <div class="status" id="status"><div class="meter"><div id="lvl"></div></div><span id="stxt">${t.idle}</span></div>
      </ha-card>`;
    const talk = this.shadowRoot.getElementById("talk");
    if (this._config.mode === "hold") {
      talk.addEventListener("pointerdown", (e) => { e.preventDefault(); this._startTalk(); });
      for (const ev of ["pointerup", "pointercancel", "pointerleave"]) {
        talk.addEventListener(ev, () => this._stopTalk());
      }
      talk.addEventListener("contextmenu", (e) => e.preventDefault());
    } else {
      talk.addEventListener("click", () => (this._state === "idle" ? this._startTalk() : this._stopTalk()));
    }
    const open = this.shadowRoot.getElementById("open");
    if (open) open.addEventListener("click", () => this._openDoor());
    const listen = this.shadowRoot.getElementById("listen");
    if (listen) listen.addEventListener("click", () => this._setListening(!this._listening));
    this._render();
    await this._mountCamera();
  }

  async _mountCamera() {
    const entity = this._config.camera_entity;
    const holder = this.shadowRoot.getElementById("cam");
    if (!entity || !holder) return;
    try {
      // make sure the frontend has loaded its camera components
      if (!customElements.get("ha-camera-stream") && window.loadCardHelpers) {
        const helpers = await window.loadCardHelpers();
        const tmp = await helpers.createCardElement({ type: "picture-entity", entity, camera_view: "live" });
        tmp.hass = this._hass;
      }
      await Promise.race([customElements.whenDefined("ha-camera-stream"), new Promise((r) => setTimeout(r, 4000))]);
    } catch (e) { /* fall through */ }
    let el;
    if (customElements.get("ha-camera-stream")) {
      el = document.createElement("ha-camera-stream");
      el.hass = this._hass;
      el.stateObj = this._hass.states[entity];
      el.controls = true;
      // iOS (and Safari) refuse to autoplay video with sound: the picture
      // stays black. Start muted; the 🔊 button or "Talk" turns sound on.
      el.muted = true;
      el.allowExoPlayer = true;
      el.style.display = "block";
    } else if (window.loadCardHelpers) {
      const helpers = await window.loadCardHelpers();
      el = await helpers.createCardElement({ type: "picture-entity", entity, camera_view: "live", show_name: false, show_state: false });
      el.hass = this._hass;
    }
    if (el) {
      holder.innerHTML = "";
      holder.appendChild(el);
      this._camera = el;
    }
  }

  _render() {
    const root = this.shadowRoot;
    const t = this._t;
    const talk = root.getElementById("talk");
    const stxt = root.getElementById("stxt");
    const status = root.getElementById("status");
    const lvl = root.getElementById("lvl");
    if (!talk || !stxt) return;
    talk.classList.toggle("on", this._state !== "idle");
    const listen = root.getElementById("listen");
    if (listen) {
      listen.textContent = this._listening ? "🔊" : "🔇";
      listen.classList.toggle("on", !!this._listening);
    }
    talk.classList.toggle("busy", this._state === "connecting");
    if (this._config.mode !== "hold") {
      talk.textContent = this._state === "idle" ? `🎤 ${t.talk}` : `⏹ ${t.stop}`;
    }
    let text = t.idle;
    if (this._error) text = this._error;
    else if (this._state === "connecting") text = t.connecting;
    else if (this._state === "talking") {
      text = `${t.talking}${this._codec ? " • " + this._codec : ""}`;
      if (this._stats) text += ` • ${t.packets}: ${this._stats.packets_sent}`;
    } else if (this._status) text = this._status;
    stxt.textContent = text;
    status.classList.toggle("err", !!this._error);
    // square root so normal speech (peaks around 0.05-0.3) is clearly visible
    if (lvl) lvl.style.width = `${Math.min(100, Math.round(Math.sqrt(this._level) * 130))}%`;
  }

  _setListening(on) {
    this._listening = on;
    this._setVideosMuted(!on);
    this._render();
  }

  _setVideosMuted(muted) {
    if (this._camera && "muted" in this._camera) this._camera.muted = muted;
    const walk = (node) => {
      if (!node) return;
      if (node.tagName === "VIDEO") {
        node.muted = muted;
        if (!muted && node.play) {
          try {
            Promise.resolve(node.play()).catch(() => {});
          } catch (e) {
            /* older browsers: play() may throw or return nothing */
          }
        }
      }
      const kids = node.shadowRoot ? [...node.shadowRoot.children, ...node.children] : node.children;
      for (const k of kids) walk(k);
    };
    walk(this.shadowRoot.getElementById("cam"));
  }

  // -------------------------------------------------------------- talk

  async _startTalk() {
    if (this._state !== "idle") return;
    const t = this._t;
    this._error = "";
    this._status = "";
    this._state = "connecting";
    this._render();
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      this._fail(t.no_https);
      return;
    }
    // AudioContext and getUserMedia must start inside the tap (iOS)
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    this._ctx = ctx;
    ctx.resume().catch(() => {});
    // the tap also unlocks the door audio (a user gesture, so iOS allows it)
    this._setListening(true);
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        video: false,
      });
    } catch (e) {
      this._fail(t.mic_denied + (e && e.name ? ` (${e.name})` : ""));
      return;
    }
    if (this._state !== "connecting") { stream.getTracks().forEach((tr) => tr.stop()); return; }
    this._stream = stream;
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        (msg) => this._onEvent(msg),
        Object.assign({ type: "dahua_vto_talk/talk" }, this._config.entry_id ? { entry_id: this._config.entry_id } : {})
      );
    } catch (e) {
      this._fail((e && (e.message || e.code)) || t.not_setup);
    }
  }

  async _onEvent(msg) {
    const t = this._t;
    if (msg.event === "ready") {
      this._handlerId = msg.handler_id;
      this._codec = msg.codec;
      try {
        await this._startCapture(msg.sample_rate);
      } catch (e) {
        this._fail(`Audio: ${e.message || e}`);
        return;
      }
      this._state = "talking";
      if (this._config.mute_while_talking) this._setVideosMuted(true);
    } else if (msg.event === "stats") {
      this._stats = msg;
    } else if (msg.event === "error") {
      this._fail(msg.message);
      return;
    } else if (msg.event === "ended") {
      this._status = `${t.ended}: ${msg.reason}`;
      this._stopTalk(true);
      return;
    }
    this._render();
  }

  async _startCapture(rate) {
    const ctx = this._ctx;
    const source = ctx.createMediaStreamSource(this._stream);
    const send = (buf, peak) => {
      this._level = peak;
      const sock = this._hass.connection.socket;
      if (!sock || sock.readyState !== 1 || this._handlerId == null) return;
      const bytes = new Uint8Array(buf.byteLength + 1);
      bytes[0] = this._handlerId;
      bytes.set(new Uint8Array(buf), 1);
      sock.send(bytes);
      if (!this._meterTick || performance.now() - this._meterTick > 100) {
        this._meterTick = performance.now();
        this._render();
      }
    };
    if (ctx.audioWorklet) {
      const url = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      const node = new AudioWorkletNode(ctx, "vto-pcm-sender", {
        numberOfInputs: 1,
        numberOfOutputs: 0,
        channelCount: 1,
        processorOptions: { targetRate: rate },
      });
      node.port.onmessage = (e) => send(e.data.pcm, e.data.peak);
      source.connect(node);
      this._node = node;
    } else {
      // very old browsers: ScriptProcessor + simple linear resampling
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const ratio = ctx.sampleRate / rate;
      let tpos = 0;
      let prev = 0;
      proc.onaudioprocess = (e) => {
        const ch = e.inputBuffer.getChannelData(0);
        const out = [];
        let peak = 0;
        const x = [prev, ...ch];
        while (tpos < ch.length) {
          const i = Math.floor(tpos);
          const f = tpos - i;
          let v = x[i] + (x[i + 1] - x[i]) * f;
          v = Math.max(-1, Math.min(1, v));
          const s = v < 0 ? v * 0x8000 : v * 0x7fff;
          out.push(s);
          peak = Math.max(peak, Math.abs(s));
          tpos += ratio;
        }
        tpos -= ch.length;
        prev = ch[ch.length - 1];
        const pcm = Int16Array.from(out);
        send(pcm.buffer, peak / 32768);
      };
      source.connect(proc);
      proc.connect(ctx.destination);
      this._node = proc;
    }
  }

  _fail(message) {
    this._error = message;
    this._stopTalk(true);
  }

  _stopTalk(keepMessage) {
    if (this._unsub) {
      const u = this._unsub;
      this._unsub = null;
      Promise.resolve(u()).catch(() => {});
    }
    if (this._node) {
      try { this._node.disconnect(); } catch (e) { /* ignore */ }
      if (this._node.port) this._node.port.onmessage = null;
      this._node = null;
    }
    if (this._stream) {
      this._stream.getTracks().forEach((tr) => tr.stop());
      this._stream = null;
    }
    if (this._ctx) {
      this._ctx.close().catch(() => {});
      this._ctx = null;
    }
    if (this._config && this._config.mute_while_talking) this._setVideosMuted(!this._listening);
    this._handlerId = null;
    this._stats = null;
    this._level = 0;
    if (!keepMessage) { this._error = ""; this._status = ""; }
    this._state = "idle";
    this._render();
  }

  async _openDoor() {
    const entity = this._config.open_door_entity;
    if (!entity) return;
    const domain = entity.split(".")[0];
    const service = { button: "press", lock: "unlock", script: "turn_on", switch: "turn_on" }[domain] || "turn_on";
    try {
      await this._hass.callService(domain, service, { entity_id: entity });
    } catch (e) {
      this._error = `${entity}: ${e.message || e}`;
      this._render();
    }
  }
}

const CARD_TAG = "dahua-vto-talk-card";

// This file is loaded very early (extra_module_url), sometimes before the
// frontend installs its scoped custom element registry polyfill, which then
// replaces window.customElements and forgets what was defined natively.
// Measured on frontend 20260128: the card was defined, yet Lovelace said
// "Custom element doesn't exist". So: define now, and define again (with a
// fresh subclass, a constructor can only be registered once) whenever the
// current registry does not know the tag.
function defineCard() {
  try {
    if (!customElements.get(CARD_TAG)) {
      customElements.define(CARD_TAG, class extends DahuaVtoTalkCard {});
      return true;
    }
  } catch (e) {
    /* already defined in this registry */
  }
  return false;
}

if (typeof customElements !== "undefined") {
  const first = defineCard();
  window.customCards = window.customCards || [];
  if (!window.customCards.some((c) => c.type === CARD_TAG)) {
    window.customCards.push({
      type: CARD_TAG,
      name: "Dahua VTO Talk",
      description: "Door camera with a talk button (two-way audio to a Dahua VTO).",
      preview: false,
    });
  }
  if (first) {
    console.info(`%c DAHUA-VTO-TALK-CARD %c ${CARD_VERSION} `, "background:#2e7d32;color:#fff", "background:#333;color:#fff");
  }
  let checks = 0;
  const recheck = () => {
    defineCard();
    if (++checks < 40) setTimeout(recheck, 250);
  };
  setTimeout(recheck, 0);
  customElements.whenDefined("home-assistant").then(defineCard, () => {});
}

// exported for tests (node)
if (typeof module !== "undefined") {
  module.exports = { WORKLET_SOURCE };
}
