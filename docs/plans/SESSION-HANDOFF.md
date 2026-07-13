# Session handoff — BugSigDB automated curation

**Purpose:** resume cleanly after a context clear. Read this first, then the plan docs it points to.
**As of:** 2026-07-13. `main` @ `0c39fa2`, working tree clean, no open branches/worktrees.

> **UPDATE 2026-07-13:** the open thread below is **resolved**. Sean answered both questions —
> the **3 designs as-is** (Fused-Lean / Split-Verify / Split-Panel) and **text+tables+figures**
> as the first source config. The experiment-design addendum is written and committed as **§6 of
> `de-novo-curation-workflow-plan.md`** (commit `0c39fa2`). **Next action:** build the eval harness
> + walking skeleton (§6d step 1 = Design 1 over text+tables+figures at a fixed cheap model on the
> ~20-study smoke set) via the **worktree pipeline**. The narrative below is kept for context.

---

## What this project is

Convert the BugSigDB curation model (a Semantic MediaWiki app at https://bugsigdb.org) into a
LinkML schema, then build an **automated de-novo curator**: given a PubMed/PMC ID, extract a
schema-valid `Study → Experiments → Signatures` record. Owner: Sean Davis (a BugSigDB co-author).

## Current state of `main` (all shipped, 198 tests green)

- **`schema/bugsigdb.yaml`** — the LinkML schema (6 classes, 63 slots, 12 enums). Study is
  identified by `uid` (wiki page name), not `pmid` (which is optional). Ontology-bound fields
  (condition→EFO/MONDO, body_site→UBERON, taxa/host→NCBITaxon) are string-ranged with bindings
  documented in comments. Dual-audience comments throughout (`CURATOR:` / `AGENT:`).
- **`sources/`** — faithful local scrape of the wiki schema pages (forms, templates, properties,
  controlled-value lists, help). Derived the schema from these.
- **`bugsigdb` CLI** (`src/bugsigdb_curation/`, uv + src layout) — commands:
  `export` (download exports from waldronlab/bugsigdbexports), `split` (flat dump → relational CSVs),
  `validate` (LinkML validator, schema force-included in wheel), `load` (full_dump.csv → nested
  Study/Experiment/Signature YAML/JSON), `pmc-map` (study PMIDs → PMCIDs via NCBI idconv;
  **1732/2052 = 84% have a PMCID** → `data/eval/pmid_pmcid_map.csv`).
- **`benchmarks/figure-extraction/`** — a 15-study, 6-figure-type benchmark for vision extraction
  of signatures from figures, with a blind run + scorer. See "What we proved" below.
- **`docs/plans/`** — `de-novo-curation-research-brief.md` (feasibility),
  `de-novo-curation-workflow-plan.md` (the main architecture + eval plan), `ontology-integration-plan.md`.
- **`data/`** — gitignored; holds the real export (`full_dump.csv`), split relational CSVs, the
  pmc-map, and figure-benchmark images. Regenerate with the CLI (`export`→`split`→`pmc-map`).

## Working agreement (how Sean wants work done)

- Substantive work → **git worktree + sonnet agent, pipeline: implement → review → fix → merge**,
  merge after one clean round; keep the **main context clean** (orchestrator scopes + merges,
  agents do the heavy lifting). See memory `agent-workflow`.
- Never rebase (merge commits only). Commit trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Planning uses opus/fable planner agents; keep the parameter space bounded (factor, don't cross).

## What we proved (figure extraction — the hardest stage)

- **Retrieval is fully scriptable, no browser/FTP:** EuropePMC REST `fullTextXML` (legends + figure
  filenames) + the PMC article HTML (`cdn.ncbi.nlm.nih.gov/pmc/blobs/…` image URLs) + a plain GET.
  (NCBI OA-package FTP and europepmc.org's renderer are **egress-blocked in this sandbox**; the
  routes that work are baked into `benchmarks/figure-extraction/retrieve.py`.)
- **Blind vision extraction (fig+legend) works**, degrading by figure type: taxa-set F1 ≈ 0.9
  (stacked bar) → ~0.6 (cladogram/heatmap/box). Error is mostly *recall* (missed small labels),
  not fabrication. Direction usually 88–100% BUT one LEfSe figure flipped **all** directions →
  orientation is a distinct failure mode needing its own verifier. Legends reliably locate the DA
  panel. Next accuracy ceiling = NCBI nomenclature drift (needs synonym resolution).
- The pilot predictions were produced ad-hoc by Claude Code sonnet subagents — **not** a scripted,
  model-pinned runner (that's the deferred sweep work).

## THE OPEN THREAD (where we stopped) — end-to-end agentic workflow

Sean redirected from a narrow model sweep to building the **full end-to-end curation workflow**,
evaluated across models AND architectures, without exploding the parameter space.

**My assessment (delivered, awaiting his reply):** the existing workflow plan is strong and does
NOT need a re-plan — it needs a **fine-tune** (re-project the design set onto Sean's axes + fold in
the model dimension). Specifically:

- The plan's A/B/C vary *orchestration topology*; Sean wants to hold topology ~fixed and vary two
  *stage-design* decisions: **reviewer/validator stage** and **NER+reconcile as one agent vs two**.
- Also split S5 into **S5a locate** (cheap, model-insensitive) and **S5b extract** (model-sensitive)
  — our figure benchmark already proved they separate.

**Proposed 3 designs (varying only the two axes over a fixed S0–S4 + S5a + S8 + S9 backbone):**
1. **Fused-Lean** — one agent extracts taxa+direction AND proposes NCBI IDs (tool-verified);
   structural validation only (`validate` + ID/CURIE resolves). Cheapest baseline.
2. **Split-Verify** — separate NER agent → deterministic cached NCBI reconcile (+ disambiguation
   agent on ambiguous hits); + adversarial verifier on the two known failure modes (taxon-in-source,
   direction) with bounded repair.
3. **Split-Panel** — separated NER+reconcile; + independent reviewer panel (fresh-context
   re-derivation from source, arbitration + repair) — and this is where a *stronger/different model*
   reviews a *cheap* extractor (makes the model-agnostic eval end-to-end meaningful).

**Parameter-space discipline (factor, don't cross):** compare the 3 designs at one fixed cheap
worker model → then sweep models only on the winner (+ the mixed-model reviewer in Design 3). Fix
source config to the **proven channels first (text + main tables + figures); defer supplements**
(the one un-built fetcher, plan's #1 open decision). Iterate on a ~20-study smoke set.

**Re-sequenced build:** (1) eval harness + walking skeleton (Design 1, text+tables) — the harness
is what makes everything measurable; (2) add the figure channel (retrieval + extractor + scorer
exist); (3) the 3-design comparison at a fixed model → pick winner; (4) model sweep on the winner
(the deferred sweep folds in here); (5) later: supplements, hardening.

### Two questions I asked Sean and he has NOT yet answered
1. Do the 3 designs capture his intent, or does he want a different cut (e.g. a 4th: fused-extract
   *with* a full panel, to fully de-correlate the two axes)?
2. First end-to-end config — include figures from the start (my lean, we've proven them) or truly
   walking-skeleton on text+tables only and add figures at step 2?

**Recommended next action once he answers:** write a short experiment-design addendum to
`de-novo-curation-workflow-plan.md` capturing the 3 designs + factored eval matrix + re-sequenced
phases, THEN build the eval harness + walking skeleton via the worktree pipeline. Do NOT start
implementing before he confirms the design set (he said "before starting to implement").

## Pinned / deferred (design agreed, not built)

- **Reproducibility packaging (papers-with-code):** in-repo `evaluations/` subproject decoupled from
  the tool's pytest; a **provider-agnostic** runner (Sean wants **model-agnostic — include Google
  Gemini, not just Claude**; use **LiteLLM** for one interface + built-in cost map, keep a thin
  internal Model interface); pinned model ids + versioned prompt file + archived per-run predictions;
  deterministic `score.py`; runs/<date>_<model>/ archival; provenance.json; METHODS.md; .zenodo.json
  for a DOI. Folds into step 4 of the end-to-end plan (model sweep). Reason deferred: Sean may swap
  model(s); scaffolding can be completed later without findings changing.
- **NCBI synonym resolution in the scorer** — score on NCBI **taxid sets** (resolve predicted
  names→taxids via a cached NCBI lookup) so numbers reflect real accuracy, not string luck. Needed
  before "is this model good enough to ship" judgments; add when building the end-to-end scorer.
- **Model-sweep candidates:** Claude {haiku-4-5, sonnet-5, opus-4-8} + Gemini {flash-lite, flash,
  pro}. **Pin exact current Gemini IDs at build time** (training cutoff predates current Gemini;
  confirm against live docs — there may be a Gemini 3.x). Fable 5 only if cheaper tiers all miss.

## Open decisions still on the table (from the plans)
- Supplement/figure **fetching** (the ~21% supplement recall ceiling) — how to fetch OA-package /
  publisher supplements. Deferred for the first end-to-end pass; plan's biggest branch point.
- **CURIE vs UMLS CUI** for ontology ids — Sean leaning CURIE (recommended); unconfirmed.
- Human-in-the-loop (`Incomplete` drafts) vs autonomous `Complete`.
- Cost/latency budget per study (bounds fan-out + how broadly the verifier runs).
- Figure-vision go/no-go at scale (recommended GO — feasibility proven).

## Environment gotchas (save rediscovery)
- bugsigdb.org is behind **Cloudflare** — WebFetch/curl 403. Use the in-app browser, then
  same-origin `fetch('/w/api.php?...')` (API is at **/w/api.php**, not /api.php; action=raw 404s).
- NCBI idconv is now at `pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/` (301 from the old URL;
  `pmid` returns as an int). Figure images: article HTML → cdn.ncbi blob URL → GET (curl works).
- Running the **model sweep** needs `ANTHROPIC_API_KEY` + `GEMINI_API_KEY`/`GOOGLE_API_KEY` — likely
  NOT available in this sandbox, so the runner is built+tested (mocked) here and Sean runs the sweep.
- **obscura headless browser** is available as a fallback figure-retrieval path (would reach the
  egress-blocked europepmc.org renderer / non-OA publisher figures).

## Resume prompt (paste into a fresh session)
> Read `docs/plans/SESSION-HANDOFF.md`, then `de-novo-curation-workflow-plan.md`. We're at "THE OPEN
> THREAD": I need to answer your two questions about the 3 end-to-end designs and the first source
> config, then you write the experiment-design addendum and we build the eval harness + walking
> skeleton via the worktree pipeline.
