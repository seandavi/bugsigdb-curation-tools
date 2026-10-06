# Project ledger — BugSigDB automated curation

**Purpose.** An append-only record of what we built, tested, decided, and measured, at
enough granularity to reconstruct the story for a paper's Methods section. This is a lab
notebook, not a plan: plans live in [`docs/plans/`](plans/); this file records what
actually happened, in order, with the evidence.

**Conventions.**
- **Append-only.** Never edit or delete a past entry. Corrections, reversals, and updates
  are *new* entries that reference the earlier one (e.g. "supersedes L011"). Git history is
  the tamper-evident backstop.
- Each entry is anchored to the **commit hash(es)** that realized it, so any claim here can
  be checked out and re-run.
- Entry ids are monotonic (`L001`, `L002`, …). Dates are calendar dates; where many entries
  share a date, the id order is the true sequence (git preserves it).
- Metrics quoted here are the numbers as measured at that point in time; if a later run
  changes them, that is a new entry, not an edit.

---

## Provenance & environment (standing reference — update by appending, not editing)

- **Source data.** BugSigDB curation export from `waldronlab/bugsigdbexports` (git ref
  `devel`), fetched via the `bugsigdb export` CLI (L002). The canonical flat file is
  `full_dump.csv` (one row per curated Signature, parent Study/Experiment columns repeated).
  The live curation UI is the Semantic MediaWiki at https://bugsigdb.org.
- **Corpus size (as split this session, L004).** 2,068 studies · 8,942 experiments ·
  14,670 signatures · 110,187 signature–taxon links · 9,274 distinct taxa. All generated
  artifacts live under `data/` (gitignored); regenerate with `export`→`split`→`pmc-map`.
- **Software stack.** Python ≥3.11, `uv` (env + runner), src layout. Core deps: `linkml`
  (schema + validation), `httpx` (async I/O), `typer`+`rich` (CLI), `pyyaml`. No
  numpy/scipy (numeric routines are pure-Python by design). Tests: `pytest`; network tests
  gated behind `@pytest.mark.network` and deselected by default (`-m 'not network'`).
- **Held-out gold (data firewall, L013).** The curated relational CSVs and the PMID→PMCID
  map are **evaluation-only ground truth**; the de-novo curator never sees them. See §6e of
  the workflow plan and memory `curation-data-firewall`.
- **Models.** The figure-extraction pilot (L010) used Claude sonnet vision subagents,
  invoked ad-hoc (not a pinned, scripted runner — that is deferred to the model sweep).
  The end-to-end model sweep (planned) will pin exact model ids via a LiteLLM interface
  across Claude {haiku-4-5, sonnet-5, opus-4-8} and Google Gemini tiers.
- **Retrieval channels that work in this environment (verified, L010).** EuropePMC REST
  `fullTextXML` (legends + figure filenames); PMC article HTML → `cdn.ncbi.nlm.nih.gov/pmc/blobs/…`
  image URLs → plain GET (browser User-Agent). NCBI idconv for PMID↔PMCID. **Egress-blocked
  here:** NCBI OA-package FTP, the europepmc.org figure renderer. bugsigdb.org sits behind
  Cloudflare (direct curl 403; use the browser + same-origin `/w/api.php`).

---

## L001 — Schema: BugSigDB curation model → LinkML — 2026-07-13
**Commit:** `6b77a30` (initial). **Artifacts:** `schema/bugsigdb.yaml`, `sources/`.
Derived a LinkML schema (6 classes, 63 slots, 12 enums) from a faithful local scrape of the
SMW schema pages (forms, templates, properties, controlled-value lists, help) in `sources/`.
Ontology-bound fields (condition→EFO/MONDO, body_site→UBERON, taxa/host→NCBITaxon) are
string-ranged with the binding documented in comments. Comments are dual-audience
(`CURATOR:` / `AGENT:`). This schema is the **output contract** every downstream component
targets.

## L002 — `bugsigdb export` CLI — 2026-07-13
**Commits:** `d2fb7ac`, review fixes `3cf35cc`, merge `852814e`.
Async downloader for the export files (`full_dump.csv`, `file_size.csv`, GMT sets) from
`waldronlab/bugsigdbexports` at a chosen git ref. Establishes the reproducible data-ingest
step.

## L003 — `bugsigdb validate` CLI — 2026-07-13
**Commits:** `82fc032`, review fixes `88eb8dc`, merge `f3406ca`. **Module:** `validate.py`.
LinkML/JSON-Schema structural validator over `schema/bugsigdb.yaml` (enum membership,
required fields, types, patterns). The schema is force-included into the wheel so the
validator is self-contained. This is the **hard structural gate (stage S9)** for any emitted
record. Exposes a programmatic API (`validate_instance`, `validate_file`, `InstanceResult`).

## L004 — `bugsigdb split` + `bugsigdb load` — 2026-07-13
**Commits:** `3e228b5` (split), `2264dcd` (load), review fixes `7d4044d`, merge `2b9fb90`.
**Modules:** `split.py`, `loader.py`.
`split` partitions the flat `full_dump.csv` into normalized relational CSVs
(`studies`, `experiments`, `signatures`, `signatures_taxa`, `taxa`) — the **gold tables**.
`load` folds the flat dump into nested `Study → experiments[] → signatures[] → taxa[]`
dicts whose keys are schema slot names — this **nested-dict shape is the emission/prediction
contract** reused everywhere downstream. Delimiter/coercion conventions were derived by
inspecting the real dump (documented inline), because the wiki docs and the dump occasionally
disagree.

## L005 — Study identity: `uid` (page name), not PMID — 2026-07-13
**Commits:** `cdd8ad3`, doc fix `245b54e`, merge `362ee56`.
Decision: a Study is identified by its stable wiki page name (`uid`), because ~16 of ~2,068
studies have no PMID. PMID is optional. (Note the downstream nuance recorded in L008: the
*relational* CSVs from `split` key on `study_id`, which equals the PMID for 2,052/2,068
studies and the `"Study N"` page name for the 12 PMID-less ones.)

## L006 — Ontology integration plan — 2026-07-13
**Commit:** `e8ed408`. **Artifact:** `docs/plans/ontology-integration-plan.md`.
Design for CURIE mapping/validation: body_site→UBERON, condition→EFO/MONDO set,
host_species→NCBITaxon, taxa→NCBI integer; a V1–V4 validation posture (well-formed →
right-ontology → resolves/not-obsolete → label-consistent); offline OAK + pinned subsets
preferred over online OLS4. Open decision: CURIE vs UMLS CUI (leaning CURIE).

## L007 — `bugsigdb pmc-map`: PMID→PMCID gold/availability set — 2026-07-13
**Commits:** `545b18c`, review fixes `5c5131d`, merge `4d1195f`. **Module:** `pmc_map.py`.
**Output:** `data/eval/pmid_pmcid_map.csv` (`study_id,pmid,pmcid,doi,has_pmc`).
Queried NCBI idconv (now at `pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/`).
**Measured full-text availability: 1,732 of 2,052 distinct study PMIDs have a PMCID (84%).**
(This measured figure supersedes the ~73% back-of-envelope estimate in the research brief's
constraint list.) This file is both the S0-resolve cache and the eval-corpus availability
stratifier.

## L008 — Research brief + multi-agent workflow plan — 2026-07-13
**Commit:** `d64f664`. **Artifacts:** `de-novo-curation-research-brief.md`,
`de-novo-curation-workflow-plan.md`.
Feasibility brief (access %, where signatures live, difficulty gradient, hallucination
concentration at the taxa/ID layer, imperfect gold) and the architecture/eval plan: a
stage DAG **S0–S10**, three candidate topologies (A linear / B hierarchical / C adversarial-
verifier), and the eval-harness design (§4) — gold join, alignment-based metrics, source-type
ablation, fair-comparison rules.

## L009 — Figure-extraction benchmark: curated eval set — 2026-07-13
**Commit:** `6c645fe`. **Artifacts:** `benchmarks/figure-extraction/{manifest.json,README.md}`.
Built a 15-study set whose BugSigDB signatures are **figure-sourced**, spanning 6 figure
types (stacked-bar composition, LEfSe LDA bar, heatmap, box/violin+stats, cladogram, STAMP).
Gold = the taxa (NCBI IDs) + directions BugSigDB curators extracted from those figures.

## L010 — Figure-extraction benchmark: blind run + scorer + results — 2026-07-13
**Commits:** `dd18f46`, merge `6edb169`. **Artifacts:** `retrieve.py`, `score.py`,
`predictions/`, `results.json`, `RESULTS.md`.
**Protocol.** Retrieval recipe (see Provenance) turns `(pmcid, "Figure N")` → legend +
downloaded image, all scriptable, no browser/FTP/login. Extraction was **blind**: five
independent Claude sonnet vision agents saw only `blind_inputs.json` (image path + legend +
group names), never the gold. Condition tested = **figure image + legend** (the realistic
pipeline input). Scored by `score.py`: taxa-set precision/recall/F1 two ways — **strict**
(exact normalized name) and **genus-lenient** (collapse to genus token) — plus **direction
accuracy** over strictly-matched taxa. Direction is relative to Group 1 ("increased" = higher
in Group 1).
**Results (by figure type; strict F1 / genus F1 / direction acc):** stacked_bar 0.91 / 0.91 /
100%; LEfSe_bar 0.75 / 0.80 / 55%*; heatmap 0.68 / 0.67 / 88%; box/violin 0.62 / 0.71 / 100%;
cladogram 0.59 / 0.57 / 94%; STAMP(other) 0.43 / 0.71 / 100%. (*LEfSe direction is dragged
down entirely by one figure, PMC7342497, which flipped **every** direction.)
**Findings.** (1) Retrieval is fully solved/scriptable. (2) **Orientation is a distinct
failure mode** needing its own verifier — one LEfSe figure inverted all directions while its
taxa recall stayed high. (3) Error is mostly **recall** (missed small labels), not
fabrication. (4) Next accuracy ceiling is **NCBI nomenclature drift** (needs synonym
resolution). **Caveat:** predictions were produced ad-hoc by subagents, not a pinned scripted
runner (that is the deferred model sweep). This benchmark is what justified including the
figure channel in the first end-to-end config (L012).

## L011 — Session handoff artifact — 2026-07-13
**Commit:** `de0d0f9`. **Artifact:** `docs/plans/SESSION-HANDOFF.md`.
Durable resume document: repo state, what figure extraction proved, the proposed end-to-end
designs, open decisions, environment gotchas. (Kept current by appending update notes.)

## L012 — Experiment-design addendum §6: the end-to-end evaluation, decided — 2026-07-13
**Commits:** `0c39fa2` (§6), handoff update `62e2265`. **Artifact:** workflow plan §6.
Re-projected the end-to-end curation eval onto **two stage-design axes** — (A1) NER↔reconcile
fused vs split; (A2) reviewer/validator stage: structural-only vs adversarial verifier vs
independent reviewer panel — over a fixed backbone (S0–S4 + **S5a locate** + S8 + S9), after
**splitting S5 into S5a locate (cheap, model-insensitive) and S5b extract (model-sensitive)**.
**Three designs to evaluate** (a diagonal across the axes): (1) **Fused-Lean**, (2)
**Split-Verify** (adversarial verifier on the two known failure modes: taxon-in-source,
direction), (3) **Split-Panel** (independent reviewer panel; the mixed-model cell).
**Two decisions locked with the PI (Sean Davis):** (i) evaluate the **3 designs as-is**;
(ii) **first source config = text + main tables + figures** (figures proven in L010;
supplements deferred). **Factored eval matrix (factor, don't cross):** compare the 3 designs
at one fixed cheap model → sweep models on the **winner only** (+ mixed-model reviewer in
Design 3). Re-sequenced build order: eval-harness + walking skeleton first.

## L013 — Data firewall §6e: curated gold held out — 2026-07-13
**Commit:** `490157a`. **Artifact:** workflow plan §6e; memory `curation-data-firewall`.
PI directive, made non-negotiable: the curated records
(`data/exports/relational/*.csv`, `data/eval/pmid_pmcid_map.csv`) are **evaluation-only**.
The curator receives **only a PMID/PMCID + source artifacts it fetches itself**; no gold
field (study_design, segmentation, body_site/condition, group orientation, taxa sets,
directions, source) is ever visible to extraction. `pmc-map` usable only for S0 resolve.
Curator S6 taxonomy uses a **general NCBI authority, not the gold-derived `taxa.csv`**. Only
the scorer reads gold; enforcement is an import boundary (curator modules never import
`eval.gold`; the pipeline package takes no gold-path argument).

## L014 — Eval-harness core: build, review, fixes — 2026-07-13 — STATUS: under review→fix, NOT yet merged
**Branch:** `feature/eval-harness` (cut from `62e2265`). **Build commits:** `8533676`
(assignment), `2e9c362` (gold join + smoke set), `1e8257d` (taxonomy resolver), `e9d5f8d`
(scorer), `5401095` (reports), `28299b0` (CLI), `128c226` (merge `main` for §6e).
**Process.** Orchestrated via the worktree pipeline (implement → independent review → fix →
merge; sonnet agents; see memory `agent-workflow`).
**What it is (methods-relevant design).** An architecture-agnostic evaluator under
`src/bugsigdb_curation/eval/`:
- **Gold join** (`gold.py`): chains `pmid_pmcid_map` ⋈ `studies` ⋈ `experiments` ⋈
  `signatures` ⋈ `signatures_taxa` ⋈ `taxa` on `study_id` (= PMID for 2,052/2,068; `"Study N"`
  otherwise), then `experiment_id` (`"<study_id>/Experiment N"`), then `signature_id`, into
  nested gold with per-signature **integer NCBI-ID taxa sets**. `source_type` classifier
  (main-table / figure / supplement / other) for stratification.
- **Assignment** (`assignment.py`): pure-Python Kuhn–Munkres (Hungarian) for optimal
  bipartite matching (rectangular matrices), used to align predicted↔gold experiments on a
  field-overlap score.
- **Taxonomy** (`taxonomy.py`): resolves predicted taxon **names → NCBI taxid** so scoring is
  on **taxid sets, not string luck** — cache → seed map (from `taxa.csv`, scorer-side only) →
  optional NCBI E-utilities gap-fill; rank-prefix/case/whitespace normalization; predictions
  already carrying an integer id bypass resolution; unresolved names tracked, never guessed.
- **Scorer** (`score.py`): experiment alignment (with a match-quality threshold; over-/under-
  segmentation reported separately from field accuracy), signature alignment, **taxa-set
  precision/recall/F1 + Jaccard (micro + macro)** on taxid sets, genus-lenient companion,
  name→ID normalization sub-score, direction correctness, known-bad-gold discount.
- **Reports** (`report.py`): per-study JSONL + aggregate markdown + self-contained HTML.
- **CLI**: `bugsigdb eval score` / `bugsigdb eval gold`.
**Contested design call (kept, endorsed on review).** Signatures are aligned by **taxa-set
overlap**, not by the declared `abundance_in_group_1` direction label (the literal §4b
reading). Rationale: a *systematic direction flip* should surface as direction-accuracy = 0
with taxa-recall intact, rather than masquerading as total taxa-recall failure. Valid because
99.8% of experiments have ≤2 signatures. Requires a signature-level match-quality floor
(added in the fix pass).
**Independent review (corpus-verified) found, and the fix pass addresses:**
- *(blocker)* The "known-bad-gold" discount keyed off `curation_state == "Incomplete"`, a
  string that **never occurs** in the real export (incomplete stubs are a *blank* state →
  `None`; 406 such rows, all zero-taxa). The discount was therefore inert, and ~429 blank-
  **direction** gold signatures were silently counted as automatic direction misses,
  deflating the §4d headline. **Fix:** key the discount off blank `curation_state`, and
  exclude blank-direction gold from the direction denominator.
- *(blocker)* `eval score` scored only gold∩predictions, silently dropping studies the
  pipeline skips/fails — inflating every metric and violating the §4d "same corpus, same
  split" rule. **Fix:** score missing predictions as full misses (recall 0) and report them
  as their own bucket.
- Per-study exception isolation (one malformed prediction must not abort a corpus run);
  signature-alignment match-quality floor; exclude both-blank fields from field-accuracy
  denominators; corpus-level segmentation totals.
- **Deferred (tracked follow-ups):** the §4e `bugsigdb validate` structural gate inside
  `eval score` (defer until real pipeline output exists); assorted Low/Nit items.
**Firewall check:** review confirmed no gold committed (only `data/.gitkeep` tracked),
loaders confined to `eval/`. A closing entry will record the merge and the post-fix test
count.

## L015 — Eval-harness core: fixes applied + merged — 2026-07-13 — closes L014
**Fix commits:** `b4d71c0` (known-bad-gold discount → blank `curation_state`; blank-direction
gold excluded from the direction denominator), `3306d3c` (score every gold study, missing
predictions = full miss in their own bucket; per-study error isolation; corpus-level
segmentation totals; `unresolved_taxa.txt` diagnostic), `52c6dd5` (`SIGNATURE_MATCH_THRESHOLD
= 0.1` floor on signature alignment; sub-floor pairs excluded from matching + direction),
`30c8918` (both-blank field pairs excluded from field-accuracy denominators).
**Merge:** `d40c91a` (`git merge --no-ff`); worktree removed, `feature/eval-harness` deleted.
**Verification.** Every fix implemented against the real corpus and confirmed the review's
counts exactly (13,750 `"Complete"` / 406 blank `curation_state`, 0 literal `"Incomplete"`;
all 406 blank-state rows zero-taxa; 429/14,156 blank direction). Each fix commit passes the
full suite in isolation (bisect-safe). **Post-merge on `main`: 291 passed, 6 deselected
(network), 0 failed** (93 tests added over the pre-harness 198). Firewall re-verified at merge
(only `data/.gitkeep` tracked). One fixture (`test_name_to_id_subscore_wrong_mapping_...`) was
adjusted so its single-taxon pair clears the new signature floor while preserving its original
right-name/wrong-ID intent (assertions unchanged) — a fixture bug exposed by the floor, not a
behavior change.
**Net:** the measurable foundation of §6d step 1 is in place — gold join + synonym-resolved
taxid-set scorer + reports + CLI, all offline-verifiable. **Next (worktree 2):** the Design-1
(Fused-Lean) curator pipeline behind a mockable/LiteLLM-ready Model seam, emitting the
nested-dict prediction contract this harness consumes, run over text+tables+figures on the
smoke set — subject to the L013 data firewall (curator takes no gold path; no `eval.gold`
import).

## L016 — Model-access decision: LiteLLM, Google-first — 2026-07-13
**Artifact:** workflow plan §6c (Phase B) updated; memory `model-access-litellm-google`.
PI decision for the curator's real backend and the eventual sweep: **all LLM access is routed
through LiteLLM** (single interface + built-in cost map), and **Google-first** because the PI
has **no Anthropic API key**. Gemini runs via Google AI Studio (LiteLLM `gemini/<id>`); Claude
models, if used, run via **Vertex AI** (`vertex_ai/claude-*`), not the Anthropic API. The
walking skeleton's default real worker = **`gemini/gemini-3.1-flash-lite`** (cheap, stable,
multimodal — so it also serves the S5b figure-vision path); sweep tiers add `gemini-3.5-flash`
and a Pro tier. Current Gemini IDs verified against ai.google.dev/gemini-api/docs/models
(2026-07): stable = `gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-2.5-{pro,flash,flash-lite}`
(2.5 family sunsets 2026-10); `gemini-3.1-pro` is preview. Re-pin at sweep time. This supersedes
the earlier "Claude {haiku/sonnet/opus} + Gemini" framing in the deferred model-sweep notes.

## L017 — CI + formal PR process established — 2026-07-13
**Commits:** `e2bc11e` (CI workflow), `7f45084` (test fix), merge `03a3756` (**PR #1**).
GitHub Actions CI (`.github/workflows/ci.yml`): `uv sync` + `uv run pytest` on every push to
`main` and every PR, matrix over Python **3.11 & 3.12**, concurrency-cancel on superseded runs.
Network-marked tests are deselected by the pyproject `-m 'not network'` default, so **CI needs no
secrets** and runs fully offline. Local `main` (24 commits) was pushed to `origin` to establish the
trunk baseline; CI was landed as **the first formal PR** (PR #1). CI immediately earned its keep —
it surfaced a latent, path-length-dependent test-brittleness bug (rich word-wrapped a long CI
tmp-path so `"does not exist"` straddled a newline, failing three eval-CLI substring assertions
that pass locally); fixed by whitespace-normalizing the assertions (verified under `COLUMNS=40`).
**Standing workflow set by the PI (memory `agent-workflow`):** work is done in a worktree
(implement → review → fix), then **commit → PR → merge on green** (`gh pr merge --merge
--delete-branch`; merge commit, never rebase/squash); optional GitHub Copilot review for
particularly tangled/high-stakes PRs.

## L018 — Design-1 (Fused-Lean) curator walking skeleton — 2026-07-13 — STATUS: PR open, awaiting green+merge
**Branch:** `feature/curator-design1` (base `3c37b2f`; `main` merged in for CI).
**Process.** Worktree pipeline: implement → independent review → fix; then PR + CI + Copilot review
(this PR clears the "second pair of eyes" bar — firewall-critical, ~a dozen new modules).
**What it is.** The §6d walking skeleton — a scripted linear pipeline turning a PMID into a
schema-valid nested prediction the eval-harness scores, under the L013 firewall. Modules under
`src/bugsigdb_curation/curator/`: `resolve` (S0, live idconv), `evidence` (S1, REST text+tables+
figures via the new shared `retrieval.py`), `extract` (S2), `segment` (S3), `experiment` (S4),
`locate` (S5a, shared-artifact heuristic), `signature` (S5b **fused extract+verify**), `taxonomy`
(S6, **live NCBI E-utilities only — never `taxa.csv`**), `assemble` (S8, `closed=True` schema-clean),
`pipeline` (`curate`/`curate_async`). **Model seam** (`model.py`): `Model` ABC → `LiteLLMModel`
(wraps `litellm.completion`, JSON-mode + retry, default `gemini/gemini-3.1-flash-lite`, key resolved
`GOOGLE_API_KEY`→`GEMINI_API_KEY`→`GOOGLE_GENERATIVE_AI_API_KEY` via `.env`+env) and a deterministic
`MockModel` (offline CI path). `retrieval.py` consolidates the figbench parsers (benchmark now a
re-export shim; its 26 tests unchanged). CLI: `bugsigdb curate --pmid/--smoke [--model] [--mock]`.
**Prediction contract** = the loader nested-dict shape exactly (validated `closed=True`); provenance
(`pmcid`/`has_pmc`/`valid`/`problems`) lives on `CurationResult`, outside the record.
**Firewall (verified on review).** No curator module imports `eval`; S0 live idconv (not the cached
map); S6 live NCBI only (no seed/`taxa_csv` param); guard test (`test_curator_firewall.py`, 30 checks)
does AST import + gold-path-literal scans, a resolver/`curate` signature check, and a subprocess
`sys.modules` check — hard to evade.
**Review (independent) → fixes applied:** (must) `study_design: null` crash → `or []` guard; (should,
crux stage) direction membership was case-sensitive so `"Increased"` silently dropped the whole
taxon → normalize direction (real recall loss the moment live Gemini runs, invisible under
`MockModel`); (cheap) within-direction taxon dedup; (nit) rename shared `_extract_leading_number`.
Each with a regression test. Two Architecture-A gaps left as tracked `# NOTE:`s (per-experiment error
isolation; sync model call in async) — deferred to Architecture-B, per plan.
**Live proof.** A real `curate --pmid 30854760` run produced a schema-valid record whose taxa
verified against live NCBI. **Tests: 413 passed, 7 deselected** (+122 over the pre-curator 291).
**Next after merge:** the first real Design-1 numbers — `curate` the smoke set with Gemini →
`bugsigdb eval score` → report (a future ledger entry). A closing note will record the merge commit.

## L019 — Curator skeleton merged + hermetic-`.env` test fix — 2026-07-13 — closes L018
**PRs:** #2 (curator skeleton, merge `5d4072a`), #3 (`5ba07fe` / merge `382cbd9`).
L018's curator merged via **PR #2** (the first CI-gated PR; see L020's process note). Immediately
after, a latent test-isolation bug surfaced: `resolve_google_api_key()` calls `load_dotenv()`, whose
`find_dotenv` walks up from the *module file* (not CWD), so the key-priority test loaded the
developer's real repo-root `.env` and went red **locally** while passing on CI (which has no `.env`).
Fixed by neutralizing `load_dotenv` in the autouse test fixture. Recurring lesson (see also L022):
**CI's clean environment hides env-dependent bugs the maintainer hits locally, and vice-versa.**

## L020 — Process: CI + PR workflow + Copilot; workflow diagram — 2026-07-13
**PRs:** #1 (CI), #8 (`docs/workflow.md`). **Memory:** `agent-workflow` (updated).
GitHub Actions CI (`uv sync` + `uv run pytest`, Python 3.11 & 3.12; network tests deselected, no
secrets). Standing workflow set by the PI: worktree agent pipeline (implement → independent review →
fix) then **commit → PR → merge on green**, autonomous when no concerns; an optional **GitHub Copilot
review** on gnarly/high-stakes PRs (it caught two real recall bugs on the curator, and 4 polish items
on the wiring). Added `docs/workflow.md` — Mermaid diagrams of the CLI/data-flow (with the firewall)
and the S0–S9 stage DAG.

## L021 — Curator NCBI etiquette: throttle, backoff, key, 404-robustness — 2026-07-13
**PR:** #4 (merge `52cfd51`). **Module:** `curator/taxonomy.py`, `curator/evidence.py`.
Added an async rate limiter (~3 req/s, ~10 with `NCBI_API_KEY`), 429/5xx exponential backoff honoring
`Retry-After`, `.env`-resolved `NCBI_API_KEY`, and `tool`/`email` etiquette params to the S6 resolver;
one shared resolver across the `--smoke` batch (so throttle + cache persist across studies). A
EuropePMC `fullTextXML` 404 now degrades to an empty bundle instead of aborting the study.

## L022 — First real Design-1 smoke run (shakedown) — 2026-07-13 — findings, not numbers
**Command:** `bugsigdb curate --smoke --model gemini/gemini-3.1-flash-lite` → `bugsigdb eval score`.
The first end-to-end run against live Gemini + NCBI + EuropePMC. **Not a valid measurement** — it
surfaced two things a first run is meant to catch:
1. **NCBI E-utilities rate-limited us (HTTP 429):** the S6 resolver fired one esearch per taxon name
   unthrottled → **14 of 19 studies errored** mid-resolution. (Fixed structurally in L023 by moving
   resolution to a local DB; L021's throttle is now gap-fill-only insurance.)
2. **The smoke set's gold is supplement-dominated** — by source type **supplement 1056 / figure 260 /
   main-table 51** gold taxa. Design-1 deliberately does not fetch supplements (deferred), so ~77% of
   this set's gold is structurally unreachable; report Design-1 performance off the **main-table +
   figure** source-type rows, not the supplement-capped aggregate.
The pipeline mechanics were sound (schema-valid records produced end-to-end). The harness's
missing-prediction bucket + per-source-type cross-tab (L015 fixes) made the diagnosis clean rather
than a misleading 0.0. This run motivated the local-taxonomy work (L023–L025).

## L023 — Local DuckDB taxonomy backend (build + lookup) — 2026-07-13
**PRs:** #5 (build+resolver+CLI), #6 (simplify to direct `read_csv`, plain-SQL `name_norm`, drop
numpy), #7 (fix real-`nodes.dmp` 26-column parsing). **Package:** `src/bugsigdb_curation/taxonomy/`.
**Decision (vs `taxoniq`):** build a `.duckdb` from a **pinned NCBI taxdump** rather than use taxoniq
— taxoniq keys name lookup on the scientific name (no synonym→taxid), and we need full synonym
coverage (`name_class` incl. `synonym`), a **pinned/citable release** for reproducibility, and SQL
rank/lineage. `TaxonomyDB.resolve` is offline, ms-latency, synonym- and rank-prefix-aware, with a
scientific-name-preferred ambiguity policy exposing homonyms. `name_norm` is one shared SQL expression
used at build AND query time (drift-proof; parity-tested vs the Python normalizer). Cache config:
`--db`/`--out` > `BUGSIGDB_TAXONOMY_DB`/`BUGSIGDB_CACHE_DIR` > `${XDG_CACHE_HOME:-~/.cache}/bugsigdb/`
(machine-global, shared across worktrees). **Real build (release `2026-07-01`):** 4.81M names / 2.85M
nodes in ~9 s → 581 MB. Two bugs caught by *real-data* runs that the synthetic fixture missed (per
L019's lesson): the `read_csv` positional column names zero-pad to 2 digits once a row tab-splits into
≥10 columns (real `nodes.dmp` = 26) — fixed by reading whole lines and `str_split`-ing on `"\t|\t"`.

## L024 — Wire TaxonomyDB into curator S6 + eval scorer — 2026-07-13
**PR:** #9 (merge `8ab0494`). Curator S6 resolution is now **cache → local `TaxonomyDB` → live
E-utilities gap-fill → unresolved** (the 429 wall from L022 removed); the eval scorer resolves
predicted names via `TaxonomyDB` and the gold-derived **`taxa.csv` seed is dropped** (firewall-clean).
DB path via the L023 precedence; no/broken DB → graceful fallback (curator warns + goes live-only;
review-hardened to catch `duckdb.Error`, not just `ValueError`). The report now surfaces
**resolution-coverage counters** so name-based sub-scores can't silently shrink. Normalization
consolidated to one Python source.

## L025 — merged.dmp tax_id canonicalization (metric integrity) — 2026-07-13
**PR:** #10 (merge `70e15ea`). BugSigDB's gold records NCBI tax_ids curated over years; NCBI merges
retired ids into successors (the `2026-07-01` release has **99,687** such mappings). The curator
resolves names → *current* ids, so a retired gold id never matched a current prediction — silently
depressing the headline taxid-set F1 **and** the name/genus sub-scores. Added a `merged(old→new)`
table and `TaxonomyDB.canonical_taxid` (single-hop); the scorer canonicalizes **gold and predicted
ids symmetrically at every comparison/alignment site**. Proof: a retired gold id scores **F1=1.0** vs
a current predicted id with merged data, **F1=0.0** without. Backward-compatible (pre-feature DBs open
via a `_has_merged_table` flag). This is the last correctness gate before trustworthy numbers; the
real DB was rebuilt to populate `merged`. **Next:** structured logging (loguru), then the smoke re-run
for the first genuine Design-1 numbers.

## L026 — Structured logging (loguru) — 2026-07-14
**PR:** #12 (merge `1f65514`). `obs.configure_logging(fmt, level)` — console or JSON-lines
(`serialize=True`), env `BUGSIGDB_LOG_FORMAT`/`BUGSIGDB_LOG_LEVEL`, `--log-format`/`--log-level` on
`curate`/`eval score`. Stdlib intercept routes httpx/litellm through loguru and mutes them to WARNING
— killing the per-call `LiteLLM temperature/... DeprecationWarning` spam that drowned L022's run.
Structured S0–S9 stage events with `logger.contextualize(study_id, pmid, run_id, pmcid)` (e.g.
`event=study_done valid=… n_signatures=… latency_ms=…`). No secrets logged.

## L027 — First genuine Design-1 numbers — 2026-07-14
**Run:** `curate --smoke --model gemini/gemini-3.1-flash-lite --taxonomy-release 2026-07-01`
→ `eval score --smoke` (`main` @ `1f65514`). Predictions/report under `data/runs/design1_smoke_v2/`
(gitignored). **19/19 studies, 0 errors** (vs L022's 14 errors — the 429 wall is gone); 7 valid records.
**Metrics by reachable gold source type** (micro): **figure F1 0.43** (P 0.78 / R 0.30, n=260);
**main-table F1 0.17** (P 0.71 / R 0.10, n=51); supplement ~0.01 (n=1056, structurally unreachable —
Design-1 fetches no supplements). Direction accuracy **80.8%**; name→id accuracy **100%**; **0 gold
taxids unresolved** (local DB + merged-id canonicalisation cover the gold). Recall is the bottleneck:
the ≥21-experiment stratum scores recall ≈0.01 (linear-pipeline under-segmentation; corpus
under-seg total 109). **Interpretation:** at the leanest design × cheapest model × no supplements ×
one run, this is a precision-good / recall-limited **floor**. Levers (by expected impact): supplements
(retrieval), Architecture-B fan-out (large papers), stronger models (sweep), Designs 2/3
(verifier/panel). The taxonomy/scorer/coverage layers that produced these are all trustworthy.

## L028 — Paper started (Quarto WIP) — 2026-07-14
**Artifact:** `paper/bugsigdb-autocuration.qmd` (+ `paper/figures/f1_by_source.png`).
Distilled the ledger into a scientific-paper-structured Quarto document (Background · Materials and
Methods · Results · Conclusions), compiling to HTML/PDF (verified `quarto render`, v1.9.38). Includes
the L028 numbers, the Mermaid stage DAG, and the source-type results figure. Render outputs are
gitignored; the `.qmd` + figure are tracked. **The ledger is the lab notebook; the paper is what we
publish** — kept as a living WIP updated as designs/models are swept.

## L029 — Curator Designs 2 & 3 (split-verify / split-panel) — 2026-07-14
**PR:** #15 (branch `feature/designs-2-3`). Completes the §6b 3-design set behind a `--design`
selector; `fused-lean` (L020) stays the unchanged default. All three share the **fixed backbone**
(S0–S4 + S5a locate + S8 assemble + S9 validate) — only **S5b/S6** (extract/reconcile) and **S10**
(verifier/panel) branch on the design (the two stage-design axes A1/A2 from the plan). New modules:
`ner` (S5b names+direction only, no id proposal), `reconcile` (S6 deterministic `TaxonomyDB`
name→taxid — ids come **only** from the authority, model-gated disambiguation on ambiguous homonyms
using per-experiment source context), `verify` (split-verify A2 adversarial verifier: taxon-in-source
+ direction re-derivation), `panel` (split-panel A2 independent reviewer + arbitration), `repair`
(shared bounded-repair helper, hard ≤2-round cap → flag/drop on exhaustion, never ship a guess),
`design`/`artifact_text`. `CurationResult` gains provenance-only `design`+`flags` (never fed back).
**Firewall (§6e) intact:** no curator module imports `eval` or reads gold; ids only from `TaxonomyDB`.
Built via the worktree pipeline (implement → review → fix): code review found **5 findings, all fixed
in-PR** — (1) context-dependent homonym disambiguation was cached into the shared/persisted resolver
(first experiment's context silently deciding a homonym for the whole run); (2) a taxon in **both**
directions was silently overwritten in `panel` (dict keyed by name only); (3) design dispatch used
`is`-identity on a `str`-subclass enum → crash on a bare-string design; (4) `repair` loop didn't match
its "two consecutive rounds agree" docstring; (5) undecidable homonym polluted the resolver's
no-hit-anywhere set. Plus a test-hygiene fix (a reconcile test was making a live NCBI call → mocked).
**Tests:** 662 passed (+6 regression). **Next:** run the 3-design comparison on the smoke set at fixed
`gemini-3.1-flash-lite` → pick the winner (then Architecture-B fan-out, supplements, model sweep).

## L030 — 3-design comparison + verifier/panel vision fix — 2026-07-14
The 3-design smoke comparison (`gemini-3.1-flash-lite`, 19 studies, text+tables+figures, no
supplements): **fused-lean wins** — micro taxa-set F1 **0.134** (P 0.457 / R 0.078), figure F1
**0.520**, direction 63.6%; **split-panel** F1 0.031 (P 0.148 / R 0.018), figure F1 0.117;
**split-verify** F1 0.018 (P 0.250 / R 0.010), figure F1 0.045, direction 100%. fused-lean is also
the cheapest design. **The finding:** the split A2 stages (verify/panel) were an UNFAIR test on
figures — they grounded vision-extracted (figure) taxa against legend-text-only evidence,
structurally guaranteeing they drop nearly all correct figure taxa (the largest reachable gold
source). A verifier can only prune, never add recall — and recall is the documented bottleneck (L027).
**Decision (Sean, 2026-07-14):** lock fused-lean as the winner; ship this small fix so the merged
split designs are correct-if-unused (`verify`/`panel`/`repair` now receive the figure `image_bytes`,
same multimodal idiom as fused-lean's extractor — `build_signature_messages`/`extract_names`); do NOT
re-run the comparison. **Next lever: Architecture-B fan-out on fused-lean** (attacks the big-paper
recall collapse — under-seg 109, the 21+ experiment bucket at R≈0.01). Run outputs live under
`data/runs/design_compare/` (gitignored).

## L031 — Supplement-retrieval + model-lever demonstration (PMID 34620922) — 2026-07-14
**Motivation.** L027/L030 showed the headline micro-F1 (~0.13, fused-lean/flash-lite) is dominated by
gold the walking skeleton structurally cannot reach: ~77% of smoke-set gold taxa live in **supplements**
(never fetched) and the 21+-experiment papers under-enumerate (R≈0.01). Sean asked the sharp question:
is this approach useful at all? Ran a one-paper **ceiling test** on the hardest, most supplement-dependent
smoke paper — 34620922 (48 gold experiments; main-text pipeline scored it **F1 0.000**).
**Method.** Fed the paper's OWN supplementary PDF (source material, firewall-clean — no gold read to
build the prediction) to a **strong model** (`gemini/gemini-3.1-pro-preview`) as a native PDF document in
one `litellm` call, temperature 0. Prompt: emit every **2-group LEfSe** differential-abundance comparison
as one BugSigDB experiment in the loader contract (experiments→signatures→taxa), genus names only (the
scorer resolves names→taxids), ignoring diversity/PERMANOVA tables. Scored with `bugsigdb eval score`
against the held-out 48-experiment gold. (The supplement is 47 pp: pp1–21 are diversity/ordination stats
— *no* signatures; the DA signatures are LEfSe tables S22–S36.)
**Result.** 30 experiments / 1050 taxa extracted. Vs gold 48: **30/30 predicted experiments matched a
distinct gold experiment, 0 over-segmentation, 0 spurious; 18 gold unmatched (under-seg). Taxa-set micro
F1 = 0.825** (genus 0.826, high precision); **direction accuracy 11.5% (6/52)**. Baseline on this paper
was 0.000 → **0.00 → 0.83**.
**Decomposition.** (1) **Extraction is essentially solved when the evidence is in hand** — zero spurious
experiments, F1 0.83 reading 47 pp of dense multi-column LEfSe tables. (2) **Enumeration partial**: got
the 30 clean *pairwise* (2-group) tables; missed 18 gold = the **multi-group** "all regions"(S24)/"all
species"(S33) LEfSe tables that BugSigDB curates as one-vs-rest experiments — the hard decomposition.
(3) **Direction systematically flipped** (11.5% is *below* a coin flip → a global group_0/group_1
orientation mismatch, fixable by a convention fix or direction-verifier; taxa-set F1 is orientation-robust
so 0.83 stands).
**Interpretation.** The poor headline F1 is overwhelmingly a **retrieval problem, not an intelligence
problem**: when the curator can see the evidence, a strong model curates a 48-experiment paper at F1 0.83.
Validates the two highest-value levers (supplement retrieval + the model lever) and keeps the
curation-assistant thesis alive.
**Caveats.** n=1; the PDF was hand-fed (supplement **fetch + parse** is unbuilt in the pipeline); some
SILVA/ASV genus labels ("Ruminococcaceae UCG_010", "Clostridiales Family XIII AD3011") don't map to NCBI
and drop out of scoring; the multi-group→experiments decomposition is unsolved.
**Artifacts.** `data/runs/supp_demo/` (predictions + report) and the extraction script (scratchpad) —
both gitignored/out-of-repo. **Next:** confirm on a second big paper (37864204) once its supplement is
obtained; then scope supplement retrieval (fetch + PDF/XLSX parse) and a direction-orientation fix.
