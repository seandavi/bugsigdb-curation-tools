// Drives a built review packet under Node with a minimal fake DOM (no browser).
// usage: node review_packet_driver.js packet.html [scenario] [options-json]
//   -> prints a JSON transcript on stdout. Scenarios are the functions in `scenarios` below.
//
// What this driver does NOT cover (it is a regex-parsed fake DOM, not a browser):
//   * real HTML parsing (attribute quoting, entities, nesting, <noscript> handling);
//   * a real <select>: a browser silently drops an assigned value that is not one of its <option>s,
//     the fake keeps whatever string it is given;
//   * a real Blob download (file name, MIME type and encoding as the browser's save dialog sees them);
//   * localStorage under file:// (per-browser rules about origins and private windows).
// `chromium --headless --screenshot` (see the README) is the check for how the page actually renders.
"use strict";
const fs = require("fs");
const vm = require("vm");

const page = fs.readFileSync(process.argv[2], "utf8");
const formHtml = page.slice(0, page.indexOf('<script type="application/json"'));
const script = page.match(/<script>\n([\s\S]*?)<\/script>/)[1];

function jsonBlock(id) {
  return page.match(new RegExp('<script type="application/json" id="' + id + '">([\\s\\S]*?)</script>'))[1];
}

function parseAttrs(text) {
  const attrs = {};
  text.replace(/([\w-]+)="([^"]*)"/g, (_m, k, v) => (attrs[k] = v));
  return attrs;
}

function fakeElement(tag, attrs) {
  return {
    tag,
    attrs,
    value: "",
    textContent: "",
    max: 0,
    listeners: {},
    classes: new Set(),
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    fire(type) {
      (this.listeners[type] || []).forEach((fn) => fn({ target: this }));
    },
    classList: {
      toggle(name) {
        if (this._owner.classes.has(name)) this._owner.classes.delete(name);
        else this._owner.classes.add(name);
      },
    },
    focus() {
      this.focused = true;
    },
  };
}

function makeWorld(storageData, storageMode) {
  const byKey = {};
  const controls = [];
  const buttons = [];
  const imgs = [];
  for (const m of formHtml.matchAll(/<(select|input|textarea)\b([^>]*)>/g)) {
    const attrs = parseAttrs(m[2]);
    if (!("data-key" in attrs)) continue;
    const el = fakeElement(m[1], attrs);
    el.classList._owner = el;
    controls.push(el);
    byKey[attrs["data-key"]] = el;
  }
  for (const m of formHtml.matchAll(/<button\b([^>]*)>/g)) {
    const attrs = parseAttrs(m[1]);
    if (!("data-action" in attrs)) continue;
    const el = fakeElement("button", attrs);
    el.classList._owner = el;
    buttons.push(el);
  }
  for (const m of formHtml.matchAll(/<img\b([^>]*)>/g)) {
    const el = fakeElement("img", parseAttrs(m[1]));
    el.classList._owner = el;
    imgs.push(el);
  }
  const ids = {};
  for (const m of formHtml.matchAll(/<(\w+)\b[^>]*?\bid="([^"]+)"/g)) {
    ids[m[2]] = fakeElement(m[1], {});
    ids[m[2]].classList._owner = ids[m[2]];
  }
  ids["packet-data"] = { textContent: jsonBlock("packet-data") };
  ids["packet-images"] = { textContent: jsonBlock("packet-images") };

  const downloads = [];
  const blobs = {};
  let blobCount = 0;
  const confirms = [];
  const world = { byKey, controls, buttons, imgs, ids, downloads, confirms, confirmAnswer: true };
  const storage = {
    data: storageData,
    getItem(k) {
      return Object.prototype.hasOwnProperty.call(this.data, k) ? this.data[k] : null;
    },
    setItem(k, v) {
      this.data[k] = String(v);
    },
    removeItem(k) {
      delete this.data[k];
    },
  };
  const document = {
    getElementById: (id) => ids[id] || null,
    querySelectorAll(sel) {
      if (sel === "[data-key]") return controls;
      if (sel === "[data-action]") return buttons;
      if (sel === "img[data-image-ref]") return imgs;
      throw new Error("unexpected selector " + sel);
    },
    createElement: () => ({
      click() {
        downloads.push({ filename: this.download, text: blobs[this.href].parts.join(""), type: blobs[this.href].type });
      },
    }),
    body: { appendChild() {}, removeChild() {} },
  };
  class Blob {
    constructor(parts, opts) {
      this.parts = parts;
      this.type = opts && opts.type;
    }
  }
  const URL = {
    createObjectURL(blob) {
      const url = "blob:fake/" + ++blobCount;
      blobs[url] = blob;
      return url;
    },
    revokeObjectURL() {},
  };
  const window = {
    confirm(message) {
      confirms.push(message);
      return world.confirmAnswer;
    },
    setTimeout() {},
  };
  if (storageMode === "null") {
    window.localStorage = null;
  } else if (storageMode === "denied") {
    Object.defineProperty(window, "localStorage", {
      get() {
        throw new Error("SecurityError");
      },
    });
  } else {
    if (storageMode === "setItem-throws") {
      storage.setItem = () => {
        throw new Error("QuotaExceededError");
      };
    }
    window.localStorage = storage;
  }
  const sandbox = { document, window, Blob, URL, JSON, Date, Object, Array, String, Number, isNaN };
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox); // no `window`-less path: auto-init runs exactly as in a browser
  world.api = sandbox.ReviewPacket;
  world.storage = storage;
  return world;
}

function setControl(world, key, value) {
  const el = world.byKey[key];
  el.value = value;
  el.fire("change");
}
function click(world, action, attrs) {
  const button = world.buttons.find(
    (b) => b.attrs["data-action"] === action && Object.keys(attrs || {}).every((k) => b.attrs[k] === attrs[k]),
  );
  button.fire("click");
}

function lastCsv(world) {
  return world.downloads.filter((d) => d.filename.endsWith(".csv")).pop();
}

function nameYourself(world, name) {
  setControl(world, "reviewer.name", name || "Ada B. Reviewer");
}

const scenarios = {
  /** The full simulated review: export, persistence, reset, restore. */
  review() {
    const transcript = {};
    const storageData = {};
    let w = makeWorld(storageData);
    transcript.initialProgress = w.ids["progress-text"].textContent;
    transcript.imageSrcPrefix = w.imgs.length ? w.imgs[0].src.slice(0, 22) : null;
    transcript.imageCount = w.imgs.length;
    const packetData = JSON.parse(w.ids["packet-data"].textContent);
    transcript.reviewKeys = w.api.reviewKeys(packetData.record);

    // 1. exporting with no reviewer name is refused
    click(w, "export-json");
    transcript.noNameDownloads = w.downloads.length;
    transcript.noNameError = w.ids["export-error"].textContent;

    // 2. simulated review
    setControl(w, "reviewer.name", "Ada B. Reviewer");
    setControl(w, "reviewer.email", "ada@example.org");
    setControl(w, "reviewer.role", "curator");
    click(w, "mark-taxa-correct", { "data-exp": "0", "data-sig": "0" });
    transcript.afterMarkAll = [w.byKey["exp.0.sig.0.taxon.0.verdict"].value, w.byKey["exp.0.sig.0.taxon.1.verdict"].value];
    setControl(w, "exp.0.sig.0.taxon.1.verdict", "wrong_taxon");
    setControl(w, "exp.0.sig.0.taxon.1.note", 'has "quotes", and, commas');
    setControl(w, "exp.0.sig.0.direction", "ok");
    setControl(w, "exp.0.sig.1.direction", "flipped");
    setControl(w, "exp.0.sig.1.taxon.0.verdict", "not_in_source");
    setControl(w, "exp.1.sig.0.taxon.0.verdict", "unsure");
    setControl(w, "exp.0.verdict", "needs_edit");
    setControl(w, "exp.0.note", "check group labels");
    setControl(w, "exp.0.missing_note", "Prevotella copri increased");
    setControl(w, "exp.1.verdict", "ok");
    setControl(w, "study.verdict", "ok");
    setControl(w, "missing_experiments_note", "Experiment on weight loss");
    setControl(w, "overall.time_saved_rating", "4");
    setControl(w, "overall.would_publish_after_edits", "yes");
    setControl(w, "overall.comment", "Nice draft");
    setControl(w, "minutes_spent", "25");
    transcript.progressAfterReview = w.ids["progress-text"].textContent;
    transcript.selectDataV = w.byKey["exp.0.verdict"].attrs["data-v"];

    // 3. export (one item still unreviewed -> confirm is asked)
    click(w, "export-json");
    click(w, "export-csv");
    transcript.confirms = w.confirms.slice();
    transcript.downloads = w.downloads.slice();

    // 4. persistence: a fresh page load with the same storage restores everything
    const savedKeys = Object.keys(storageData);
    const w2 = makeWorld(storageData);
    transcript.storageKeys = savedKeys;
    transcript.restoredProgress = w2.ids["progress-text"].textContent;
    transcript.restored = {
      note: w2.byKey["exp.0.sig.0.taxon.1.note"].value,
      verdict: w2.byKey["exp.0.sig.1.direction"].value,
      name: w2.byKey["reviewer.name"].value,
      rating: w2.byKey["overall.time_saved_rating"].value,
    };

    // 5. zoom toggles on click
    if (w.imgs.length) {
      w.imgs[0].fire("click");
      transcript.zoomed = w.imgs[0].classes.has("zoomed");
    }

    // 6. reset (declined, then accepted)
    w2.confirmAnswer = false;
    click(w2, "reset");
    transcript.progressAfterDeclinedReset = w2.ids["progress-text"].textContent;
    w2.confirmAnswer = true;
    click(w2, "reset");
    transcript.progressAfterReset = w2.ids["progress-text"].textContent;
    transcript.valueAfterReset = w2.byKey["exp.0.verdict"].value;
    transcript.storageAfterReset = Object.keys(storageData).filter((k) => k.indexOf("reviewer") === -1);

    // 7. restoreState tolerates garbage and drops out-of-range values
    const api = w.api;
    const bad = api.restoreState(
      { values: { "exp.0.verdict": "great", "exp.0.note": "kept", "bogus.key": "x", "study.verdict": 5 } },
      packetData.record,
      "2026-01-01T00:00:00.000Z",
    );
    transcript.restoreGarbage = bad.values;
    transcript.restoreNull = api.restoreState(null, packetData.record, "2026-01-01T00:00:00.000Z").values;

    return transcript;
  },
};

const options = process.argv[4] ? JSON.parse(process.argv[4]) : {};
process.stdout.write(JSON.stringify(scenarios[process.argv[3] || "review"](options)));
