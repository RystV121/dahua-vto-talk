// a custom camera card whose JS arrives late must still show up
const { JSDOM } = require("jsdom");
const fs = require("fs"), path = require("path");
const dom = new JSDOM("<!doctype html><body></body>", { runScripts: "outside-only" });
const w = dom.window;
w.loadCardHelpers = async () => ({ createCardElement: async (c) => {
  const tag = c.type.replace("custom:", "");
  if (!w.customElements.get(tag)) { const e = w.document.createElement("div"); e.className = "err"; return e; }
  return w.document.createElement(tag);
} });
w.eval(fs.readFileSync(path.join(__dirname, "../../custom_components/dahua_vto_talk/frontend/dahua-vto-talk-card.js"), "utf8"));
(async () => {
  const c = w.document.createElement("dahua-vto-talk-card");
  c.setConfig({ camera_card: { type: "custom:late-cam" } });
  w.document.body.appendChild(c);
  c.hass = { language: "pl", states: {}, connection: {} };
  await new Promise((r) => setTimeout(r, 300));
  w.customElements.define("late-cam", class extends w.HTMLElement {});
  await new Promise((r) => setTimeout(r, 50));
  if (!c.shadowRoot.querySelector("late-cam")) throw new Error("late camera card not mounted");
  console.log("late camera card: ok");
})().catch((e) => { console.error(e); process.exit(1); });
