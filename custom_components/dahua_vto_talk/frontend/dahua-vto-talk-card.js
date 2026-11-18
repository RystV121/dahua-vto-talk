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
 * prefer_webrtc: true                     # default; see _preferWebRtc
 * preview_gain_db: 0                      # optional; default: the integration's
 *                                         # "Preview sound boost" slider
 * camera_card:                            # optional, any card instead of the
 *   type: custom:webrtc-camera            # built-in camera view (e.g. a light
 *   url: domofon_sub                      # sub stream via go2rtc / WebRTC)
 * open_door_entity: button.domofon_open_door   # optional
 * mode: toggle | hold                     # default toggle
 * mute_while_talking: false               # mute door audio while you talk
 * entry_id: ...                           # only with several door stations
 */

const CARD_VERSION = "0.1.11";

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
    gaps: "przerwy",
    boost: "wzmocnienie",
    ended: "Połączenie zakończone",
    no_entity: "Nie ma takiej encji kamery",
    sent: "Wysłano",
    loudest: "max głośność",
    silent: "⚠ mikrofon dawał ciszę – sprawdź uprawnienia mikrofonu albo spróbuj w Safari",
    playback: "▶ Odsłuchaj, co poszło do furtki",
    no_recording: "Brak nagrania (zaktualizuj integrację i zrestartuj HA)",
    diag_no_video: "🔊 nie znalazłem odtwarzacza wideo w podglądzie",
    tap_again: "Podgląd przełączony na WebRTC – stuknij 🔊 jeszcze raz, żeby włączyć dźwięk",
    diag_audio: "ścieżka dźwięku",
    diag_muted: "wyciszony",
    diag_volume: "głośność",
    diag_paused: "zatrzymany",
    diag_playing: "gra",
    yes: "tak",
    no: "nie",
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
    gaps: "gaps",
    boost: "boost",
    ended: "Call ended",
    no_entity: "Camera entity not found",
    sent: "Sent",
    loudest: "loudest",
    silent: "⚠ the microphone gave silence – check mic permission or try Safari",
    playback: "▶ Play back what went to the gate",
    no_recording: "No recording (update the integration and restart HA)",
    diag_no_video: "🔊 no video player found in the preview",
    tap_again: "Preview switched to WebRTC – tap 🔊 once more to turn the sound on",
    diag_audio: "audio track",
    diag_muted: "muted",
    diag_volume: "volume",
    diag_paused: "paused",
    diag_playing: "playing",
    yes: "yes",
    no: "no",
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
      if (this._config.camera_entity && !this._config.camera_card && "stateObj" in this._camera) {
        const obj = hass.states[this._config.camera_entity];
        if (obj && this._camera.stateObj !== obj) this._camera.stateObj = obj;
      }
    }
    if (first) this._render();
    if (this._listening) {
      const db = this._previewGainDb();
      if (db !== this._lastBoostDb) {
        this._lastBoostDb = db;
        this._startBoost();
      }
    }
  }

  get _hasCam() {
    return !!(this._config && (this._config.camera_entity || this._config.camera_card));
  }

  getCardSize() {
    return this._hasCam ? 6 : 2;
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
        .cam .msg { color: #fff; padding: 16px; font-size: 14px; }
        .cam ha-card { border-radius: 0; border: 0; box-shadow: none; }
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
        .summary { padding: 0 16px 12px; font-size: 13px; color: var(--secondary-text-color); display:flex; gap:8px; align-items:center; flex-wrap: wrap; }
        .summary[hidden] { display: none; }
        .summary.warn { color: var(--warning-color, #f57c00); }
        .summary button { border: 0; border-radius: 16px; padding: 6px 12px; font-size: 13px; cursor: pointer;
          background: var(--secondary-background-color, #444); color: var(--primary-text-color); }
        .meter > div { height: 100%; width: 0; background: var(--success-color, #2e7d32); transition: width .1s; }
      </style>
      <ha-card ${this._config.title ? `header="${this._config.title}"` : ""}>
        ${this._hasCam ? '<div class="cam" id="cam"></div>' : ""}
        <div class="bar">
          <button class="main" id="talk">🎤 ${this._config.mode === "hold" ? t.hold : t.talk}</button>
          ${this._hasCam ? `<button class="listen" id="listen" title="${t.listen}">🔇</button>` : ""}
          ${this._config.open_door_entity ? `<button class="open" id="open">🔓 ${t.open}</button>` : ""}
        </div>
        <div class="status" id="status"><div class="meter"><div id="lvl"></div></div><span id="stxt">${t.idle}</span></div>
        <div class="summary" id="summary" hidden><span id="sumtxt"></span><button id="playback">${t.playback}</button></div>
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
    if (listen) {
      listen.addEventListener("click", () => {
        this._setListening(!this._listening);
        if (this._listening) {
          this._preferWebRtc("tap").then(async (switched) => {
            if (switched && (await this._afterSwitch())) return;
            setTimeout(() => this._checkPreviewAudio(), 1500);
          });
        }
      });
    }
    this.shadowRoot.getElementById("playback").addEventListener("click", () => this._playBack());
    this._render();
    await this._mountCamera();
  }

  async _mountCamera() {
    const entity = this._config.camera_entity;
    const holder = this.shadowRoot.getElementById("cam");
    if (!holder) return;
    if (this._config.camera_card) {
      // a user-chosen card (e.g. custom:webrtc-camera with a light sub stream)
      if (!holder._llRebuild) {
        // a custom card whose JS is not loaded yet comes back as an error card
        // that fires "ll-rebuild" once the element is defined: mount again then
        holder._llRebuild = true;
        holder.addEventListener("ll-rebuild", (ev) => {
          ev.stopPropagation();
          this._mountCamera();
        });
      }
      try {
        const type = String(this._config.camera_card.type || "");
        if (type.startsWith("custom:")) {
          await Promise.race([
            customElements.whenDefined(type.slice(7)),
            new Promise((r) => setTimeout(r, 5000)),
          ]);
        }
        const helpers = await window.loadCardHelpers();
        const el = await helpers.createCardElement(this._config.camera_card);
        el.hass = this._hass;
        holder.innerHTML = "";
        holder.appendChild(el);
        this._camera = el;
        this._preferWebRtcOnLoad();
      } catch (e) {
        holder.innerHTML = `<div class="msg">camera_card: ${String(e && e.message || e)}</div>`;
      }
      return;
    }
    if (!entity) return;
    if (!this._hass.states[entity]) {
      holder.innerHTML = `<div class="msg">${this._t.no_entity}: ${entity}</div>`;
      return;
    }
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
      if (this._stats) {
        text += ` • ${t.packets}: ${this._stats.packets_sent}`;
        if (this._stats.underruns) text += ` • ${t.gaps}: ${this._stats.underruns}`;
      }
    } else if (this._status) text = this._status;
    stxt.textContent = text;
    status.classList.toggle("err", !!this._error);
    const summary = root.getElementById("summary");
    if (summary) {
      const s = this._summary;
      summary.hidden = !s || this._state !== "idle";
      if (s) {
        const quiet = s.peak < 0.02;
        summary.classList.toggle("warn", quiet);
        root.getElementById("sumtxt").textContent =
          `${t.sent} ${s.seconds.toFixed(1)} s • ${t.loudest} ${Math.round(s.peak * 100)}%` + (quiet ? ` • ${t.silent}` : "");
      }
    }
    // square root so normal speech (peaks around 0.05-0.3) is clearly visible
    if (lvl) lvl.style.width = `${Math.min(100, Math.round(Math.sqrt(this._level) * 130))}%`;
  }

  // Safari (and Chrome) hide the device's local IP from WebRTC until the page
  // has been allowed to use the microphone. Away from that, WebRTC to go2rtc
  // often cannot connect, the WebRTC Camera card falls back to MSE, and on an
  // iPad MSE has no door sound. Asking for the microphone for a moment lifts
  // that, and the player is then reconnected so it can pick WebRTC.
  async _preferWebRtc(why) {
    if (this._config.prefer_webrtc === false || !this._config.camera_card) return false;
    const wc = this.shadowRoot.querySelector("webrtc-camera");
    if (!wc || typeof wc.onconnect !== "function" || typeof wc.ondisconnect !== "function") return false;
    if (wc.pcState === 1 || (wc.video && wc.video.srcObject)) return false; // already WebRTC
    if (typeof wc.mode === "string" && !wc.mode.includes("webrtc")) return false; // WebRTC not allowed
    if (this._rtcUnlock && why !== "tap") return false;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    try {
      const mic = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      mic.getTracks().forEach((tr) => tr.stop());
    } catch (e) {
      this._rtcUnlock = `mic: ${e && e.name}`;
      return false;
    }
    this._rtcUnlock = why;
    wc.ondisconnect();
    // the old websocket's close event must fire first (wsState is CLOSED then,
    // so the player ignores it); reconnecting earlier lets that event drop the
    // new connection
    await new Promise((r) => setTimeout(r, 600));
    if (!wc.ws && !wc.pc && wc.isConnected) wc.onconnect();
    return true;
  }

  // The new WebRTC picture starts after the tap, so browsers start it muted.
  // Unmuting needs another tap: say so instead of leaving silent "🔊".
  async _afterSwitch() {
    const wc = this.shadowRoot.querySelector("webrtc-camera");
    for (let i = 0; i < 25 && wc && !(wc.video && wc.video.srcObject); i++) {
      await new Promise((r) => setTimeout(r, 200));
    }
    await new Promise((r) => setTimeout(r, 300));
    const v = this._findVideos()[0];
    if (v && v.muted && this._listening) {
      this._listening = false;
      this._status = this._t.tap_again;
      this._render();
      return true;
    }
    return false;
  }

  async _preferWebRtcOnLoad() {
    // only when the microphone is already allowed: then no prompt appears
    if (this._config.prefer_webrtc === false || !this._config.camera_card) return;
    try {
      if (!navigator.permissions || !navigator.permissions.query) return;
      const st = await navigator.permissions.query({ name: "microphone" });
      if (st.state !== "granted") return;
    } catch (e) {
      return;
    }
    // give the player a moment: if WebRTC connects on its own, nothing to do
    await new Promise((r) => setTimeout(r, 2500));
    await this._preferWebRtc("load");
  }

  _findVideos() {
    const found = [];
    const walk = (node) => {
      if (!node) return;
      if (node.tagName === "VIDEO") found.push(node);
      const kids = node.shadowRoot ? [...node.shadowRoot.children, ...node.children] : node.children;
      for (const k of kids) walk(k);
    };
    walk(this.shadowRoot.getElementById("cam"));
    return found;
  }

  // One line telling whether the preview really has sound and is unmuted,
  // so "I hear nothing" can be narrowed down from a screenshot.
  _checkPreviewAudio() {
    const t = this._t;
    if (this._listening) this._startBoost();
    const v = this._findVideos()[0];
    if (!v) { this._status = t.diag_no_video; this._render(); return; }
    let track = null;
    if (v.srcObject && v.srcObject.getAudioTracks) {
      const a = v.srcObject.getAudioTracks();
      track = a.length > 0 && a.some((x) => x.readyState === "live");
    } else if (typeof v.webkitAudioDecodedByteCount === "number") {
      track = v.webkitAudioDecodedByteCount > 0;
    } else if (v.audioTracks) {
      track = v.audioTracks.length > 0;
    }
    const mode = v.srcObject ? "WebRTC" : v.src ? "MSE/HLS" : "?";
    const parts = [
      `${t.diag_audio}: ${track === null ? "?" : track ? t.yes : t.no}`,
      `${t.diag_muted}: ${v.muted ? t.yes : t.no}`,
      `${t.diag_volume}: ${Math.round((v.volume || 0) * 100)}%`,
      mode,
      v.paused ? t.diag_paused : t.diag_playing,
    ];
    if (this._boost) parts.push(`${t.boost} +${this._boost.db} dB`);
    else if (this._previewGainDb() > 0) parts.push(`${t.boost}: ${this._boostError || "-"}`);
    this._status = `🔊 ${parts.join(" • ")}`;
    const wc = this.shadowRoot.querySelector("webrtc-camera");
    const label = wc && wc.shadowRoot && wc.shadowRoot.querySelector(".mode");
    this._report("preview", {
      card: CARD_VERSION, ua: navigator.userAgent, page: typeof location !== "undefined" ? location.hostname : null,
      audio_track: track, muted: v.muted, volume: v.volume, mode, paused: v.paused,
      player: label ? label.textContent : null, ready_state: v.readyState,
      decoded_audio_bytes: typeof v.webkitAudioDecodedByteCount === "number" ? v.webkitAudioDecodedByteCount : null,
      camera: this._config.camera_card ? "camera_card" : "camera_entity",
      webrtc_unlock: this._rtcUnlock || null,
      boost_db: this._boost ? this._boost.db : 0,
      boost_error: this._boostError || null,
    });
    this._render();
  }

  _setListening(on) {
    this._listening = on;
    this._setVideosMuted(!on);
    if (on) this._startBoost();
    else this._stopBoost();
    this._render();
  }

  // ------------------------------------------------- preview sound boost
  // A <video> cannot play louder than 100 %. With a boost set, the door sound
  // of a WebRTC preview goes through Web Audio (gain + limiter) and the video
  // itself is muted. 0 dB (the default) leaves playback exactly as before; if
  // the browser does not let the Web Audio path run, the plain video stays on.

  _previewGainDb() {
    if (this._config.preview_gain_db != null) return Math.max(0, Number(this._config.preview_gain_db) || 0);
    const h = this._hass;
    if (!h || !h.entities) return 0;
    if (!this._pgEntity) {
      this._pgEntity = Object.keys(h.entities).find((id) => {
        const e = h.entities[id];
        return e && e.platform === "dahua_vto_talk" && e.translation_key === "preview_gain";
      }) || null;
    }
    const st = this._pgEntity && h.states[this._pgEntity];
    const v = st ? parseFloat(st.state) : 0;
    return Number.isFinite(v) && v > 0 ? v : 0;
  }

  _startBoost() {
    const db = this._previewGainDb();
    const v = this._findVideos()[0];
    const tracks = v && v.srcObject && v.srcObject.getAudioTracks ? v.srcObject.getAudioTracks() : [];
    if (!this._listening || db <= 0 || !tracks.length) {
      this._stopBoost();
      return false;
    }
    const level = Math.pow(10, db / 20);
    const b = this._boost;
    if (b && b.video === v && b.track === tracks[0]) {
      b.level = level;
      if (!this._mutedForTalk) b.gain.gain.value = level;
      return true;
    }
    this._stopBoost();
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      ctx.resume().catch(() => {});
      const src = ctx.createMediaStreamSource(new MediaStream([tracks[0]]));
      const gain = ctx.createGain();
      gain.gain.value = this._mutedForTalk ? 0 : level;
      const lim = ctx.createDynamicsCompressor();
      lim.threshold.value = -6;
      lim.knee.value = 6;
      lim.ratio.value = 12;
      lim.attack.value = 0.003;
      lim.release.value = 0.25;
      const an = ctx.createAnalyser();
      an.fftSize = 2048;
      src.connect(gain);
      gain.connect(lim);
      lim.connect(an);
      an.connect(ctx.destination);
      const boost = { ctx, src, gain, an, level, video: v, track: tracks[0], db };
      this._boost = boost;
      const engage = () => {
        if (this._boost !== boost) return;
        if (ctx.state === "running") {
          v.muted = true; // the boosted path plays the sound now
          boost.active = true;
        } else {
          this._boostError = `audio context ${ctx.state}`;
          this._stopBoost();
        }
      };
      if (ctx.state === "running") engage();
      else setTimeout(engage, 700);
      return true;
    } catch (e) {
      this._boostError = String((e && e.message) || e);
      this._stopBoost();
      return false;
    }
  }

  _stopBoost() {
    const b = this._boost;
    if (!b) return;
    this._boost = null;
    try { b.src.disconnect(); } catch (e) { /* ignore */ }
    b.ctx.close().catch(() => {});
    if (this._listening && !this._mutedForTalk && b.video) {
      b.video.muted = false;
      try { Promise.resolve(b.video.play()).catch(() => {}); } catch (e) { /* ignore */ }
    }
  }

  _setVideosMuted(muted) {
    if (this._boost) {
      // boosted: the video stays muted, the gain is the volume knob
      this._mutedForTalk = muted && this._listening;
      this._boost.gain.gain.value = muted ? 0 : this._boost.level;
      if (this._boost.active) return;
    }
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
    this._summary = null;
    this._sentFrames = 0;
    this._sentSamples = 0;
    this._maxPeak = 0;
    this._zeroFrames = 0;
    this._rebuilt = false;
    this._diag = { card: CARD_VERSION, ua: navigator.userAgent, page: typeof location !== "undefined" ? location.hostname : null, started: new Date().toISOString() };
    this._state = "connecting";
    this._render();
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      this._fail(t.no_https);
      return;
    }
    // an AudioContext started inside the tap unlocks audio on iOS
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const early = new Ctx();
    early.resume().catch(() => {});
    this._ctx = early;
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
    const track = stream.getAudioTracks ? stream.getAudioTracks()[0] : null;
    if (track) {
      this._diag.track = {
        label: track.label, muted: track.muted, enabled: track.enabled, ready: track.readyState,
        settings: track.getSettings ? track.getSettings() : null,
      };
    }
    // iOS/WebKit: a context made before the microphone was opened can hand the
    // worklet pure silence. Make a fresh one now (allowed while capturing).
    try {
      const fresh = new Ctx();
      await Promise.race([fresh.resume(), new Promise((r) => setTimeout(r, 800))]);
      if (fresh.state === "running") {
        early.close().catch(() => {});
        this._ctx = fresh;
        this._diag.fresh_context = true;
      } else {
        this._diag.fresh_context = `not running (${fresh.state})`;
        fresh.close().catch(() => {});
      }
    } catch (e) {
      this._diag.fresh_context = `error: ${e && e.message}`;
    }
    if (this._state !== "connecting") return;
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
    if (ctx.state !== "running") {
      await Promise.race([ctx.resume().catch(() => {}), new Promise((r) => setTimeout(r, 800))]);
    }
    Object.assign(this._diag || {}, {
      ctx_rate: ctx.sampleRate, ctx_state: ctx.state, worklet: !!ctx.audioWorklet, target_rate: rate,
    });
    const source = ctx.createMediaStreamSource(this._stream);
    const send = (buf, peak) => {
      this._level = peak;
      const sock = this._hass.connection.socket;
      if (!sock || sock.readyState !== 1 || this._handlerId == null) return;
      const bytes = new Uint8Array(buf.byteLength + 1);
      bytes[0] = this._handlerId;
      bytes.set(new Uint8Array(buf), 1);
      sock.send(bytes);
      this._sentSamples += buf.byteLength / 2;
      this._sentRate = rate;
      this._sentFrames += 1;
      if (peak === 0) this._zeroFrames += 1;
      if (peak > this._maxPeak) this._maxPeak = peak;
      // 2 s of exact digital zeros: the capture chain is broken (a real mic
      // always has some noise). Rebuild it once with a fresh AudioContext.
      if (!this._rebuilt && this._sentFrames === 100 && this._maxPeak === 0) this._rebuildCapture(rate);
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

  async _rebuildCapture(rate) {
    this._rebuilt = true;
    if (this._diag) this._diag.rebuilt_after_silence = true;
    try {
      if (this._node) { this._node.disconnect(); if (this._node.port) this._node.port.onmessage = null; }
      if (this._ctx) this._ctx.close().catch(() => {});
      const Ctx = window.AudioContext || window.webkitAudioContext;
      this._ctx = new Ctx();
      await this._startCapture(rate);
    } catch (e) {
      if (this._diag) this._diag.rebuild_error = String(e && e.message || e);
    }
  }

  _report(kind, data) {
    try {
      const msg = Object.assign({ type: "dahua_vto_talk/client_report", kind, data },
        this._config && this._config.entry_id ? { entry_id: this._config.entry_id } : {});
      Promise.resolve(this._hass.callWS(msg)).catch(() => {});
    } catch (e) { /* reporting must never break the card */ }
  }

  _fail(message) {
    this._error = message;
    this._stopTalk(true);
  }

  _stopTalk(keepMessage) {
    if (this._diag) {
      const tr = this._stream && this._stream.getAudioTracks ? this._stream.getAudioTracks()[0] : null;
      const report = Object.assign({}, this._diag, {
        frames_sent: this._sentFrames || 0,
        zero_frames: this._zeroFrames || 0,
        seconds: Math.round(((this._sentSamples || 0) / (this._sentRate || 8000)) * 10) / 10,
        max_peak: Math.round((this._maxPeak || 0) * 1000) / 1000,
        codec: this._codec || null,
        ctx_state_end: this._ctx ? this._ctx.state : null,
        track_end: tr ? { muted: tr.muted, enabled: tr.enabled, ready: tr.readyState } : null,
        error: this._error || null,
        ended: new Date().toISOString(),
      });
      this._diag = null;
      this._report("talk", report);
    }
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
    if (this._sentSamples > 0) {
      this._summary = { seconds: this._sentSamples / (this._sentRate || 8000), peak: this._maxPeak };
    }
    this._sentSamples = 0;
    this._handlerId = null;
    this._stats = null;
    this._level = 0;
    if (!keepMessage) { this._error = ""; this._status = ""; }
    this._state = "idle";
    this._render();
  }

  async _playBack() {
    const t = this._t;
    const q = this._config.entry_id ? `?entry_id=${encodeURIComponent(this._config.entry_id)}` : "";
    const path = `/api/dahua_vto_talk/last_talk.wav${q}`;
    try {
      const res = this._hass.fetchWithAuth
        ? await this._hass.fetchWithAuth(path)
        : await fetch(path, { headers: { Authorization: `Bearer ${this._hass.auth.data.access_token}` } });
      if (!res.ok) throw new Error(t.no_recording);
      const url = URL.createObjectURL(await res.blob());
      if (this._audio) { try { this._audio.pause(); } catch (e) { /* ignore */ } }
      this._audio = new Audio(url);
      this._audio.onended = () => URL.revokeObjectURL(url);
      await this._audio.play();
    } catch (e) {
      this._error = (e && e.message) || t.no_recording;
      this._render();
    }
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
