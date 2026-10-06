/* BugSigDB review packet: reviewer state, verdict export, and DOM wiring.
 *
 * Everything outside `init` is a pure function of its arguments (no DOM, no storage, no clock), so
 * the same file runs under Node for tests. The packet must never touch the network: results leave
 * the page only as a file the reviewer saves.
 *
 * State is a flat map of dotted keys -> strings (the form-control values), e.g.
 *   study.verdict, study.note, reviewer.name, minutes_spent,
 *   exp.0.verdict, exp.0.note, exp.0.missing_note,
 *   exp.0.sig.1.direction, exp.0.sig.1.taxon.2.verdict, exp.0.sig.1.taxon.2.note,
 *   missing_experiments_note, overall.time_saved_rating, overall.would_publish_after_edits, overall.comment.
 * Every `[data-key]` element in the page edits exactly one of these.
 */
(function (root) {
  "use strict";

  var SCHEMA_VERSION = 1;
  var ITEM_VERDICTS = ["ok", "needs_edit", "wrong", "unsure"];
  var DIRECTION_VERDICTS = ["ok", "flipped", "unsure"];
  var TAXON_VERDICTS = ["correct", "wrong_taxon", "not_in_source", "unsure"];
  var RATINGS = ["1", "2", "3", "4", "5"];
  var PUBLISH_ANSWERS = ["yes", "no", "unsure"];
  var STATE_VERSION = 1;

  function experimentsOf(record) {
    return record.experiments || [];
  }
  function signaturesOf(experiment) {
    return experiment.signatures || [];
  }
  function taxaOf(signature) {
    return signature.taxa || [];
  }

  function sigKey(e, s) {
    return "exp." + e + ".sig." + s;
  }
  function taxonKey(e, s, k) {
    return sigKey(e, s) + ".taxon." + k;
  }

  /** Keys of every item a reviewer is expected to judge (what the progress counter counts). */
  function reviewKeys(record) {
    var keys = ["study.verdict"];
    experimentsOf(record).forEach(function (experiment, e) {
      keys.push("exp." + e + ".verdict");
      signaturesOf(experiment).forEach(function (signature, s) {
        keys.push(sigKey(e, s) + ".direction");
        taxaOf(signature).forEach(function (_taxon, k) {
          keys.push(taxonKey(e, s, k) + ".verdict");
        });
      });
    });
    return keys;
  }

  /** Every key the page may hold: the review items plus all free-text and reviewer fields. */
  function allKeys(record) {
    var keys = reviewKeys(record).concat([
      "study.note",
      "missing_experiments_note",
      "overall.time_saved_rating",
      "overall.would_publish_after_edits",
      "overall.comment",
      "reviewer.name",
      "reviewer.email",
      "reviewer.role",
      "minutes_spent",
    ]);
    experimentsOf(record).forEach(function (experiment, e) {
      keys.push("exp." + e + ".note", "exp." + e + ".missing_note");
      signaturesOf(experiment).forEach(function (signature, s) {
        taxaOf(signature).forEach(function (_taxon, k) {
          keys.push(taxonKey(e, s, k) + ".note");
        });
      });
    });
    return keys;
  }

  /** The permitted values for a select-type key, or null for a free-text key. */
  function allowedValues(key) {
    if (/\.direction$/.test(key)) return DIRECTION_VERDICTS;
    if (/\.taxon\.\d+\.verdict$/.test(key)) return TAXON_VERDICTS;
    if (/\.verdict$/.test(key)) return ITEM_VERDICTS;
    if (key === "overall.time_saved_rating") return RATINGS;
    if (key === "overall.would_publish_after_edits") return PUBLISH_ANSWERS;
    return null;
  }

  function newState(nowIso) {
    return { version: STATE_VERSION, started_at: nowIso, values: {} };
  }

  function getValue(state, key) {
    var v = state.values[key];
    return typeof v === "string" ? v : "";
  }

  function setValue(state, key, value) {
    state.values[key] = String(value);
    return state;
  }

  /** Rebuild a state from whatever was saved: unknown keys and out-of-range select values are dropped. */
  function restoreState(raw, record, nowIso) {
    var state = newState(nowIso);
    if (!raw || typeof raw !== "object" || !raw.values || typeof raw.values !== "object") return state;
    if (typeof raw.started_at === "string") state.started_at = raw.started_at;
    allKeys(record).forEach(function (key) {
      var v = raw.values[key];
      if (typeof v !== "string") return;
      var allowed = allowedValues(key);
      if (allowed && v !== "" && allowed.indexOf(v) === -1) return;
      state.values[key] = v;
    });
    return state;
  }

  function markTaxaCorrect(state, record, e, s) {
    var signature = signaturesOf(experimentsOf(record)[e] || {})[s];
    taxaOf(signature || {}).forEach(function (_taxon, k) {
      setValue(state, taxonKey(e, s, k) + ".verdict", "correct");
    });
    return state;
  }

  function progress(state, record) {
    var keys = reviewKeys(record);
    var done = keys.filter(function (key) {
      return getValue(state, key) !== "";
    }).length;
    return { done: done, total: keys.length };
  }

  function choice(state, key) {
    var v = getValue(state, key);
    return v === "" ? null : v;
  }

  /** The verdict JSON (schema_version 1; see schema/review_verdict.schema.json). */
  function buildVerdicts(record, meta, state, nowIso) {
    var minutes = getValue(state, "minutes_spent").trim();
    var minutesNumber = minutes === "" || isNaN(Number(minutes)) || Number(minutes) < 0 ? null : Number(minutes);
    var rating = choice(state, "overall.time_saved_rating");
    return {
      schema_version: SCHEMA_VERSION,
      packet_id: meta.packet_id,
      pmid: meta.pmid,
      draft_sha256: meta.draft_sha256,
      reviewer: {
        name: getValue(state, "reviewer.name").trim(),
        email: getValue(state, "reviewer.email").trim(),
        role: getValue(state, "reviewer.role").trim(),
      },
      started_at: state.started_at,
      exported_at: nowIso,
      minutes_spent: minutesNumber,
      study: { verdict: choice(state, "study.verdict"), note: getValue(state, "study.note") },
      experiments: experimentsOf(record).map(function (experiment, e) {
        return {
          index: e,
          verdict: choice(state, "exp." + e + ".verdict"),
          note: getValue(state, "exp." + e + ".note"),
          missing_note: getValue(state, "exp." + e + ".missing_note"),
          signatures: signaturesOf(experiment).map(function (signature, s) {
            return {
              index: s,
              direction_verdict: choice(state, sigKey(e, s) + ".direction"),
              taxa: taxaOf(signature).map(function (taxon, k) {
                return {
                  name: taxon.taxon_name,
                  verdict: choice(state, taxonKey(e, s, k) + ".verdict"),
                  note: getValue(state, taxonKey(e, s, k) + ".note"),
                };
              }),
            };
          }),
        };
      }),
      missing_experiments_note: getValue(state, "missing_experiments_note"),
      overall: {
        time_saved_rating: rating === null ? null : Number(rating),
        would_publish_after_edits: choice(state, "overall.would_publish_after_edits"),
        comment: getValue(state, "overall.comment"),
      },
    };
  }

  function csvCell(value) {
    var text = value === null || value === undefined ? "" : String(value);
    return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  /** One row per judged item (and per free-text note): level, experiment, signature, taxon, verdict, note. */
  function toCsv(verdicts) {
    var header = ["pmid", "packet_id", "reviewer", "level", "experiment", "signature", "taxon", "verdict", "note"];
    var rows = [header];
    var who = verdicts.reviewer.name;
    function add(level, e, s, taxon, verdict, note) {
      rows.push([verdicts.pmid, verdicts.packet_id, who, level, e, s, taxon, verdict, note]);
    }
    add("study", "", "", "", verdicts.study.verdict, verdicts.study.note);
    verdicts.experiments.forEach(function (experiment) {
      add("experiment", experiment.index, "", "", experiment.verdict, experiment.note);
      if (experiment.missing_note) add("experiment_missing", experiment.index, "", "", "", experiment.missing_note);
      experiment.signatures.forEach(function (signature) {
        add("signature_direction", experiment.index, signature.index, "", signature.direction_verdict, "");
        signature.taxa.forEach(function (taxon) {
          add("taxon", experiment.index, signature.index, taxon.name, taxon.verdict, taxon.note);
        });
      });
    });
    if (verdicts.missing_experiments_note) add("missing_experiments", "", "", "", "", verdicts.missing_experiments_note);
    var overall = verdicts.overall;
    add("overall_time_saved_rating", "", "", "", overall.time_saved_rating, "");
    add("overall_would_publish_after_edits", "", "", "", overall.would_publish_after_edits, "");
    if (overall.comment) add("overall_comment", "", "", "", "", overall.comment);
    return (
      rows
        .map(function (row) {
          return row.map(csvCell).join(",");
        })
        .join("\r\n") + "\r\n"
    );
  }

  function exportFileName(verdicts, extension) {
    var slug =
      verdicts.reviewer.name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "") || "anonymous";
    return "verdicts_" + verdicts.pmid + "_" + slug + "." + extension;
  }

  // ---------------------------------------------------------------------------------------------
  // DOM wiring (only runs from init)
  // ---------------------------------------------------------------------------------------------

  function toArray(list) {
    return Array.prototype.slice.call(list);
  }

  /**
   * Wire a packet page. `env` supplies document, storage (localStorage or null), Blob, URL, confirm, now.
   * Returns the live state, for tests.
   */
  function init(env) {
    var doc = env.document;
    var data = JSON.parse(doc.getElementById("packet-data").textContent);
    var images = JSON.parse(doc.getElementById("packet-images").textContent);
    var record = data.record;
    var meta = data.meta;
    var storageKey = "bugsigdb-review:" + meta.packet_id;
    var reviewerKey = "bugsigdb-review:reviewer";
    var storageOk = !!env.storage;

    function readStored(key) {
      try {
        var text = env.storage ? env.storage.getItem(key) : null;
        return text ? JSON.parse(text) : null;
      } catch (_err) {
        return null;
      }
    }

    var state = restoreState(readStored(storageKey), record, env.now());
    if (!getValue(state, "reviewer.name")) {
      var remembered = readStored(reviewerKey) || {};
      ["name", "email", "role"].forEach(function (field) {
        if (typeof remembered[field] === "string") setValue(state, "reviewer." + field, remembered[field]);
      });
    }

    var controls = toArray(doc.querySelectorAll("[data-key]"));
    var progressText = doc.getElementById("progress-text");
    var progressBar = doc.getElementById("progress-bar");
    var saveStatus = doc.getElementById("save-status");
    var exportError = doc.getElementById("export-error");

    function syncControls() {
      controls.forEach(function (el) {
        var key = el.getAttribute("data-key");
        el.value = getValue(state, key);
        if (allowedValues(key)) el.setAttribute("data-v", el.value);
      });
    }

    function refresh() {
      var p = progress(state, record);
      progressText.textContent = p.done + " of " + p.total + " items reviewed";
      progressBar.max = p.total;
      progressBar.value = p.done;
    }

    function persist() {
      if (!storageOk) {
        saveStatus.textContent = "Not auto-saved (browser storage unavailable): export before closing this page.";
        return;
      }
      try {
        env.storage.setItem(storageKey, JSON.stringify(state));
        var name = getValue(state, "reviewer.name");
        env.storage.setItem(
          reviewerKey,
          JSON.stringify({
            name: name,
            email: getValue(state, "reviewer.email"),
            role: getValue(state, "reviewer.role"),
          }),
        );
        saveStatus.textContent = "Progress saved in this browser.";
      } catch (_err) {
        storageOk = false;
        saveStatus.textContent = "Not auto-saved (browser storage unavailable): export before closing this page.";
      }
    }

    controls.forEach(function (el) {
      var key = el.getAttribute("data-key");
      function onEdit() {
        setValue(state, key, el.value);
        if (allowedValues(key)) el.setAttribute("data-v", el.value);
        persist();
        refresh();
      }
      el.addEventListener("input", onEdit);
      el.addEventListener("change", onEdit);
    });

    toArray(doc.querySelectorAll("img[data-image-ref]")).forEach(function (img) {
      var image = images[img.getAttribute("data-image-ref")];
      if (!image) return;
      img.src = "data:" + image.type + ";base64," + image.data;
      img.addEventListener("click", function () {
        img.classList.toggle("zoomed");
      });
    });

    function download(filename, mime, text) {
      var blob = new env.Blob([text], { type: mime });
      var url = env.URL.createObjectURL(blob);
      var a = doc.createElement("a");
      a.href = url;
      a.download = filename;
      doc.body.appendChild(a);
      a.click();
      doc.body.removeChild(a);
      env.setTimeout(function () {
        env.URL.revokeObjectURL(url);
      }, 1000);
    }

    function exportAs(kind) {
      exportError.textContent = "";
      if (!getValue(state, "reviewer.name").trim()) {
        exportError.textContent = "Please enter your name (Overall section) before exporting.";
        var nameInput = controls.filter(function (el) {
          return el.getAttribute("data-key") === "reviewer.name";
        })[0];
        if (nameInput && nameInput.focus) nameInput.focus();
        return null;
      }
      var p = progress(state, record);
      if (p.done < p.total && !env.confirm((p.total - p.done) + " item(s) have no verdict yet. Export anyway?")) {
        return null;
      }
      var verdicts = buildVerdicts(record, meta, state, env.now());
      if (kind === "csv") {
        download(exportFileName(verdicts, "csv"), "text/csv;charset=utf-8", toCsv(verdicts));
      } else {
        download(
          exportFileName(verdicts, "json"),
          "application/json",
          JSON.stringify(verdicts, null, 2) + "\n",
        );
      }
      return verdicts;
    }

    toArray(doc.querySelectorAll("[data-action]")).forEach(function (button) {
      var action = button.getAttribute("data-action");
      button.addEventListener("click", function () {
        if (action === "export-json") {
          exportAs("json");
        } else if (action === "export-csv") {
          exportAs("csv");
        } else if (action === "mark-taxa-correct") {
          markTaxaCorrect(
            state,
            record,
            Number(button.getAttribute("data-exp")),
            Number(button.getAttribute("data-sig")),
          );
          syncControls();
          persist();
          refresh();
        } else if (action === "reset") {
          if (env.confirm("Discard all of your verdicts for this packet and start over?")) {
            state = newState(env.now());
            if (env.storage) {
              try {
                env.storage.removeItem(storageKey);
              } catch (_err) {
                /* nothing saved to remove */
              }
            }
            syncControls();
            refresh();
            saveStatus.textContent = "Reset.";
          }
        }
      });
    });

    syncControls();
    refresh();
    if (!storageOk) persist();
    return {
      getState: function () {
        return state;
      },
    };
  }

  var api = {
    SCHEMA_VERSION: SCHEMA_VERSION,
    reviewKeys: reviewKeys,
    allKeys: allKeys,
    allowedValues: allowedValues,
    newState: newState,
    getValue: getValue,
    setValue: setValue,
    restoreState: restoreState,
    markTaxaCorrect: markTaxaCorrect,
    progress: progress,
    buildVerdicts: buildVerdicts,
    toCsv: toCsv,
    exportFileName: exportFileName,
    init: init,
  };
  root.ReviewPacket = api;

  if (typeof document !== "undefined" && typeof window !== "undefined") {
    var storage = null;
    try {
      storage = window.localStorage || null;
    } catch (_err) {
      storage = null;
    }
    init({
      document: document,
      storage: storage,
      Blob: Blob,
      URL: URL,
      confirm: function (message) {
        return window.confirm(message);
      },
      now: function () {
        return new Date().toISOString();
      },
      setTimeout: function (fn, ms) {
        return window.setTimeout(fn, ms);
      },
    });
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
