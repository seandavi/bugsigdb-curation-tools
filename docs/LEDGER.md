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
