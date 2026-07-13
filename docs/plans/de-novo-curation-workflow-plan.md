# De Novo BugSigDB Curation — Multi-Agent Workflow & Evaluation Plan

**Status:** Architecture/planning only. No pipeline code written. Design input for building an
automated de novo curator that turns a PMID/PMCID into a schema-valid Study → Experiments →
Signatures record.
**Author:** systems-architecture pass, 2026-07-13
**Builds on:** `docs/plans/de-novo-curation-research-brief.md` (feasibility + constraints — not
repeated here), `docs/plans/ontology-integration-plan.md` (CURIE mapping/validation),
`schema/bugsigdb.yaml` (output contract).

> Read the research brief first. This plan does not re-argue feasibility; it takes the brief's
> constraints as fixed design inputs and designs the agent teams and the eval harness around them.

---

## 0. Design inputs (the constraints this plan is engineered against)

From the brief, treated as hard constraints:

1. **Full-text access ≈ 73%** of study PMIDs have a PMCID; ~27% are abstract-only through the
   PubMed/PMC tools.
2. **The signature payload lives where the text API can't see it.** Corpus-wide `source` split:
   **~57% figures, ~21% supplements, ~16% main tables, ~3% empty.** `get_full_text_article`
   returns body prose + main tables inline but **no supplements and no figure contents**, and it
   **strips italic genus/species names from prose**. So the fetchable-text channel alone tops out
   at roughly the ~16% main-table (+ some abstract-stated) signatures.
3. **Difficulty gradient by level:** Study ≈ trivial (PubMed gives it) except `study_design`
   inference; Experiment = methods-text extraction + two judgment calls (segmentation, MHT) +
   ontology mapping; Signature = the crux (locate result → orient direction vs Group 0/1 →
   normalize taxa to NCBI IDs → segment many comparisons).
4. **Hallucination is concentrated at the taxa/ID layer.** Every emitted `ncbi_id` and every
   ontology CURIE must be **verified against an authority, never generated free-hand**, and every
   taxon must trace to a cited `source`.
5. **Gold is imperfect** (known "missing NCBI ID" / label-drift states) and **alignment is
   non-trivial** (no shared IDs; segmentation differences must not be scored as field errors).

Repo capabilities to plug into (grounded):

- **`bugsigdb validate`** (`src/bugsigdb_curation/validate.py`) — LinkML/JSON-Schema structural gate
  over `schema/bugsigdb.yaml`: enum membership, required fields, types, patterns. The natural
  hard gate on every emitted record.
- **`bugsigdb load`** — produces the **nested-dict instance shape** (Study → `experiments[]` →
  `signatures[]` → `taxa[]`) that is exactly the emission target; reuse it as the output contract
  and the round-trip target for gold.
- **`bugsigdb pmc-map`** (branch `feature/pmc-map`) — emits `data/eval/pmid_pmcid_map.csv`
  (`study_id,pmid,pmcid,doi,has_pmc`): the PMID→PMCID gold/eval join and the source-availability
  stratifier.
- **Relational gold export** `data/exports/relational/*.csv`:
  `studies.csv` (`study_id,pmid,…,study_design,…`),
  `experiments.csv` (`experiment_id,study_id,…,body_site,uberon_id,condition,efo_id,…,statistical_test,mht_correction,…`),
  `signatures.csv` (`signature_id,experiment_id,source,abundance_in_group_1,…`),
  `signatures_taxa.csv` (`signature_id,ncbi_id`), `taxa.csv`. This is the gold for scoring.
- **MCP tools:** PubMed/PMC (`convert_article_ids`, `get_article_metadata`,
  `get_full_text_article`, `search_articles`; `get_copyright_status` is currently HTTP 500 —
  do not depend on it), Semantic Scholar (`snippet_search`, `paper_details`, `paper_batch_details`
  — secondary abstract source and possible OA-PDF URL route).
- **Orchestration primitives:** the harness's own subagent spawning (Agent tool / `Task`),
  Workflow orchestration, and MCP tool assignment per agent.
- **Ontology mapping** follows `ontology-integration-plan.md`: body_site→UBERON, condition→EFO/MONDO
  (multi-ontology CURIE set), host_species→NCBITaxon, taxa→NCBI integer; V1–V4 validation posture
  (well-formed → right-ontology → resolves/not-obsolete → label-consistent), offline OAK + pinned
  subsets preferred over online OLS4.

---

## 1. Curation task → pipeline stages (architecture-independent)

Every architecture below is a different *orchestration* of the same stage DAG. Defining the stages
once keeps the architectures comparable and lets the eval harness score any of them identically.

| # | Stage | Input → Output | Primary tools | Failure mode it owns |
| --- | --- | --- | --- | --- |
| **S0** | **Resolve & ingest** | PMID → `{pmid, pmcid, doi, has_pmc, license_hint}` | `convert_article_ids`, pmc-map | wrong/no PMCID; non-OA gate |
| **S1** | **Evidence-bundle assembly** | IDs → a normalized *evidence bundle* (abstract+MeSH, section-labeled body text, main tables, **supplementary files**, **figure images**, per-item provenance handles) | `get_article_metadata`, `get_full_text_article`, **PMC OA-package / `oa_file_list` fetch**, **figure image fetch**, Semantic Scholar OA-PDF | **the recall ceiling** — missing supplements/figures caps signature recall (constraint 2) |
| **S2** | **Study-metadata extraction** | bundle → Study fields | metadata + LLM classify | `study_design` misclassification |
| **S3** | **Experiment segmentation** | bundle → list of distinct 2-group comparisons (the experiment scaffold) | LLM over Methods/Results | over/under-segmentation (cascades everywhere) |
| **S4** | **Experiment-metadata extraction** | per experiment → §1b fields | LLM over Methods; enum-constrained decode | group 0/1 orientation; MHT inference; vocab spelling |
| **S5** | **Signature/taxa extraction & direction** | per experiment + source artifact → per-direction taxon-name sets + `source` cite | table parse / **figure vision OCR** / supplement parse | hallucinated taxa; wrong direction; missed source |
| **S6** | **NCBI taxonomy normalization** | taxon-name strings → verified integer `ncbi_id` (or "unresolved") | **NCBI E-utilities / taxonomy resolver** (authority lookup) | fabricated IDs; synonym/greengenes/OTU drift |
| **S7** | **Ontology (CURIE) mapping** | body_site/condition/host labels → UBERON/EFO-set/NCBITaxon CURIEs | OAK + pinned subsets (per ontology plan), OLS4 fallback | wrong-branch or obsolete CURIE |
| **S8** | **Assembly** | all of the above → nested-dict record (`bugsigdb load` shape) | deterministic builder | positional label↔id misalignment (use `split_comma_strict`) |
| **S9** | **Structural validation** | record → pass/fail + errors | **`bugsigdb validate`** (hard gate) | enum/type/required violations |
| **S10** | **Self-critique / repair** | record + validate errors + evidence → corrected record | verifier agent(s) | residual hallucination; unfixable → flag for human |

Design rules that hold across all architectures:

- **S1 is a first-class stage, not a detail.** It has three sub-fetchers — (a) text+main-tables
  (works today), (b) supplement acquisition (XLSX/CSV/DOCX/PDF parse), (c) figure acquisition
  (image → vision OCR). Recall of S5 is bounded by what S1 delivers; the eval's source-type
  ablation (§4) measures exactly this ceiling.
- **S6 and S7 are verification stages, never generation stages.** An LLM proposes a *name/label
  string*; an authority tool returns the *ID/CURIE*. No integer or CURIE is ever accepted from LLM
  free text. Cache resolutions (the corpus reuses ~9,274 taxa and a few hundred ontology terms).
- **S9 is a mechanical gate; S10 is the semantic gate.** `validate` catches structural defects for
  free; it cannot catch a plausible-but-wrong taxon or a flipped direction — that is S10's job.
- **Provenance travels with every value.** Each field carries the evidence handle it came from
  (section id, table cell, figure region, supplement file+sheet+row). This powers S10, the
  `source` field, and post-hoc auditability.

---

## 2. Candidate architectures

Three architectures over the same stage DAG, from simplest to most robust. Each names roles,
I/O, tool assignment, orchestration, and honest trade-offs. A fourth pattern (map-reduce over
comparisons) is described as a composable sub-pattern used inside B and C.

### Architecture A — Linear pipeline of specialists

**One agent per stage, run in sequence; artifacts passed forward as structured JSON.**

```
S0 Resolver → S1 Evidence Assembler → S2 Study Extractor → S3 Segmenter
  → S4 Experiment Extractor → S5 Signature Extractor → S6 Taxo Normalizer
  → S7 Ontology Mapper → S8 Assembler → S9 validate → S10 Critic (single pass) → record
```

- **Roles:** each is a narrow specialist with a fixed schema-fragment output. The Segmenter emits
  a list of comparison stubs; the Experiment/Signature extractors run once per stub in a simple loop.
- **Tools:** Resolver → `convert_article_ids`; Assembler → all S1 fetchers; Study/Experiment/
  Signature extractors → LLM with the relevant evidence slice + enum lists injected; Normalizer →
  NCBI resolver; Mapper → OAK/OLS4; Assembler → deterministic; validate → `bugsigdb validate`.
- **Orchestration:** a straight Workflow chain; each step's typed output is the next step's input.
  Deterministic, cheap, trivially debuggable (inspect the artifact between any two stages).
- **Mitigates:** structural errors (S9 gate), vocab drift (enum-constrained decode), fabricated IDs
  (S6/S7 authority lookups). Clear provenance and cheapest to run over 1,732 studies.
- **Does NOT mitigate:** it is single-pass, so a bad **segmentation** (S3) silently corrupts
  everything downstream with no feedback loop; a single Critic pass is weak against a confident
  wrong direction or a hallucinated taxon that already passed validate. It also does not scale
  gracefully to the 48-experiment papers — the naive loop works but there's no per-experiment
  isolation, so one hard comparison can derail the shared context.
- **Best for:** the walking skeleton and the abstract-only / main-table-sourced happy path.

### Architecture B — Hierarchical orchestrator + specialist subagents (recommended core)

**A Curator-Lead orchestrates; it spawns fresh subagents per experiment and per evidence artifact,
then reduces their outputs.** This maps directly onto the many-comparisons-per-paper reality
(1→48 experiments observed).

```
                    ┌─────────────── Curator-Lead (orchestrator) ───────────────┐
S0 Resolver ─► S1 Evidence Assembler ─► S2 Study Extractor ─► S3 Segmenter
                    │  fan-out: for each comparison stub, spawn an Experiment Worker
                    ▼
      Experiment Worker[i]  (isolated context per comparison)
          ├─ S4 experiment metadata (enum-constrained)
          ├─ fan-out: per source artifact, spawn a Signature Extractor
          │        Signature Extractor  →  S5 taxa-names + direction + source cite
          │        (table-worker | supplement-worker | figure-vision-worker)
          ├─ S6 Taxo Normalizer  (shared cached NCBI resolver service)
          └─ returns a validated Experiment fragment
                    │  reduce: Curator-Lead assembles fragments (S8)
                    ▼
              S7 Ontology Mapper (batched across all experiments) ─► S9 validate ─► S10 Critic ─► record
```

- **Roles:**
  - **Curator-Lead** owns S0–S3 + assembly/reduce (S8) + final validate/critique; holds the
    paper-level context (study design, group orientation conventions) and hands each worker a
    tight, self-contained brief.
  - **Experiment Worker** (one per comparison, isolated context) owns S4 + orchestrates its own
    Signature Extractors + S6. Isolation is the point: a 48-experiment paper becomes 48 small,
    parallelizable, independently-retryable tasks instead of one 48× context.
  - **Signature Extractor** is polymorphic by source type — a **table-worker** (parse inline
    table text), a **supplement-worker** (download + parse XLSX/CSV/DOCX/PDF), a
    **figure-vision-worker** (image → OCR of LEfSe bars / cladograms / heatmaps, decode direction
    from side/color). The Lead dispatches the right one based on the `source` the paper cites.
  - Shared **NCBI resolver** and **Ontology mapper** run as cached services (not per-worker), so
    the 9,274-taxon / few-hundred-term working set is resolved once and reused.
- **Tools:** as A, but S1 sub-fetchers and the vision-worker are first-class; per-worker MCP tool
  scoping keeps each context small.
- **Orchestration:** the harness's subagent spawning + Workflow reduce. Two fan-out/fan-in levels
  (per-experiment, then per-artifact). Parallelism bounded by a cost/latency budget (§5).
- **Mitigates:** the segmentation-scale problem (isolation + parallelism), context bloat on huge
  papers, and per-source-type specialization (the figure/supplement problem gets a dedicated worker
  instead of being an afterthought). Independent retry of a single failed experiment.
- **Does NOT mitigate (on its own):** hallucinated taxa / flipped direction still need the
  adversarial check of C bolted on (B provides the *structure* to place a verifier per worker,
  C provides the *verifier*). Orchestration is more complex and harder to debug than A; cost is
  higher (more agent invocations), though per-worker contexts are smaller.
- **Best for:** the production core once past the skeleton — it is the only one of the three that
  degrades gracefully on the 48-experiment tail.

### Architecture C — Extractor + adversarial verifier/critic loop (robustness layer)

**Every extraction stage is paired with an independent Verifier that only sees the claim + the
evidence and tries to falsify it; disagreements trigger a bounded repair loop.** This is a layer
composed *onto* A or B, targeted at the hallucination-concentrated stages (S5/S6, direction).

```
Extractor (S5/S6) ──claim+provenance──► Verifier (fresh context, evidence-only)
      ▲                                        │
      └────────── repair (≤N rounds) ◄── disagree / unverifiable / not-in-source
                                               │ agree
                                               ▼
                                     accept into record ─► S9 validate ─► final Critic
```

- **Roles:**
  - **Taxa Verifier:** for each proposed taxon, confirm (a) the name actually appears in the cited
    `source` artifact (guards against invented genera — directly counters the brief's "LLM will
    invent plausible names"), and (b) `ncbi_id` is the correct resolution of *that* string via the
    authority tool (guards fabricated IDs). Anything not grounded in the source is dropped, not kept.
  - **Direction Verifier:** independently re-derives `abundance_in_group_1` from the source
    (which side of the LEfSe plot / which column / the sentence) and checks it against Group 0/1
    orientation. A systematic flip surfaces as correlated disagreements.
  - **Segmentation Critic (optional):** checks experiment count against the paper's comparison
    enumeration (body sites × cohorts × timepoints) to catch over/under-segmentation before it
    cascades.
- **Tools:** verifier gets the *same* evidence bundle handle + the authority tools, but a **fresh
  context** and no access to the extractor's reasoning — independence is the whole point.
- **Orchestration:** per-stage propose→verify→repair with a hard round cap; on exhaustion, mark the
  field/taxon **unresolved/flagged** (mirrors the human "missing NCBI ID" escape hatch) rather than
  shipping a guess. Runs inside A (single verifier pass) or per-worker inside B (verifier per
  Experiment Worker — the natural home).
- **Mitigates:** the crux failure modes — hallucinated taxa, fabricated IDs, flipped direction —
  precisely the ones validate cannot catch. Turns "confident and wrong" into "flagged for human."
- **Does NOT mitigate:** recall problems from S1 (if the supplement was never fetched, there is
  nothing to verify — C improves precision, not recall). Roughly doubles LLM cost on the verified
  stages; needs careful round caps to avoid loops.

### Sub-pattern D — Map-reduce over supplementary tables / comparisons

Not a standalone team but the **fan-out primitive** used inside B and reusable in A: given a
multi-sheet supplement or a paper with many comparisons, **map** one extractor per sheet/table/
comparison (parallel, isolated), each emitting per-direction taxa + provenance, then **reduce**
(dedup taxa within a signature, merge sheets that belong to one comparison, split those that don't).
It is the concrete mechanism that makes the 48-experiment / large-supplement case tractable and is
where segmentation reduction logic lives.

---

## 3. Grounding in this stack (implementation primitives)

| Stage | Concrete primitive here |
| --- | --- |
| S0 | `convert_article_ids` MCP + `bugsigdb pmc-map` output as the resolve cache |
| S1a text/tables | `get_article_metadata` + `get_full_text_article` MCP |
| S1b supplements | **build-needed**: PMC OA-package / `oa_file_list` fetch + XLSX/CSV/DOCX/PDF parsers (not covered by any current MCP tool — top open decision, §5) |
| S1c figures | **build-needed**: figure-image fetch + a vision model for LEfSe/cladogram/heatmap OCR |
| S2/S3/S4/S5 | LLM subagents (Agent/Task spawns) with schema fragments + enum permissible-value lists injected into the prompt; **structured output constrained to the schema fragment** so decode can't invent enum values |
| S6 | NCBI E-utilities (esearch/efetch on the taxonomy db) or a local NCBI names dump; a **cached resolver service** shared across workers; returns integer or "unresolved" |
| S7 | OAK + pinned ontology subsets per `ontology-integration-plan.md` (V1–V4), OLS4 `--online` fallback; reuse that plan's prefix map and `split_comma_strict` for label↔id pairing |
| S8 | deterministic Python builder emitting the `bugsigdb load` nested-dict shape |
| S9 | **`bugsigdb validate`** invoked as a subprocess/gate on the emitted JSON/YAML |
| S10 | verifier subagent(s) per Architecture C |
| orchestration | harness subagent spawning for fan-out/fan-in; Workflow tool for the linear/reduce chains; per-agent MCP tool scoping to keep contexts tight |

**Where structured output + validation enter (explicit):**
1. **At decode time** — every extractor is constrained to emit only its schema fragment; enum slots
   (`study_design`, `sequencing_type`, `statistical_test`, `abundance_in_group_1`, …) are
   decoded against the permissible-value list, so structurally-invalid enums are impossible by
   construction, not caught after the fact.
2. **At assembly (S9)** — `bugsigdb validate` is a **hard gate**: no record leaves the pipeline
   without passing. Its errors feed S10 repair.
3. **At authority boundaries (S6/S7)** — an ID/CURIE is only ever written if an authority tool
   returned it for the proposed string; the resolver's "not found" maps to the schema's tolerated
   unresolved state, never to a guess.

**Reliable taxa/ontology resolution (never hallucinated):** LLM's role is strictly *string
proposal + source grounding*; the *identifier* always comes from NCBI (S6) or OAK/OLS (S7). The
Taxa Verifier (C) additionally requires the proposed name to be present in the cited source. Cache
every resolution keyed by raw string → ID with a synonym table, so the corpus's repeated taxa are
resolved once and re-verified cheaply.

---

## 4. Evaluation harness design

**Goal:** run any architecture blind over the PMC-available gold studies and score it against the
human-curated records, with metrics per the brief, fair alignment, source-type ablation, and cost
control. The harness is architecture-agnostic — it scores the emitted nested-dict record, so A/B/C
are directly comparable.

### 4a. Gold join & corpus

- **Join:** `pmc-map` `data/eval/pmid_pmcid_map.csv` (`study_id,pmid,pmcid,has_pmc`) ⋈
  `studies.csv` (study fields) ⋈ `experiments.csv` (experiment fields incl. `uberon_id`,`efo_id`)
  ⋈ `signatures.csv` (`source`,`abundance_in_group_1`) ⋈ `signatures_taxa.csv`
  (`signature_id,ncbi_id` — the gold taxa-set). Optionally round-trip gold through `bugsigdb load`
  so gold and prediction share the exact nested-dict shape.
- **Eval corpus:** the ~1,732 studies where `has_pmc` is true (73% of ~2,068). The ~27%
  abstract-only studies are a **separate stratum** (abstract-only config only), not dropped —
  they are where the single-taxon happy path lives.
- **Dev/test split:** stratify by (source-type mix, experiment count, has_pmc) and hold out a
  fixed test set (e.g. 15%). Keep a tiny **smoke set** (~20 studies spanning: abstract-only single
  taxon like PMID 19849869; main-table like 21850056; supplement-heavy like 34620922; a
  many-experiment paper) for fast iteration. Never tune on test.

### 4b. Metrics (from the brief, made concrete against the columns)

- **Study-field accuracy (4a of brief):** per-field exact/normalized match on `study_design`
  (+ confusion matrix), `sequencing_type`, thresholds, `mht_correction` (+ confusion matrix).
  Bibliographic fields excluded (PubMed-derived, ~free).
- **Experiment alignment F1 (4b):** no shared IDs → **bipartite match predicted↔gold** (Hungarian
  on a field-overlap score keyed on `body_site`/`condition`/group semantics/`sequencing_type`).
  Report **matched/unmatched counts and an experiment-count delta with over- vs under-segmentation
  reported separately** from field accuracy (a segmentation error must not masquerade as many field
  errors). On matched pairs: per-field accuracy for §1b fields.
- **Signature taxa-set P/R/F1 + Jaccard (4c, headline):** within each matched experiment, align
  signatures by `abundance_in_group_1` (≤2 per experiment), then score the **NCBI-ID sets** (not
  name strings) with precision, recall, F1, and Jaccard. Aggregate **both micro** (all taxa) **and
  macro** (per-signature averaged). Secondary **rank-aware partial credit** (right genus when gold
  is species) as a lenient companion to the strict exact-ID score.
- **Name→ID normalization sub-score:** of gold taxa the pipeline also *found*, what fraction it
  mapped to the correct `ncbi_id` — isolates "missed the taxon" (recall) from "found it, mis-mapped"
  (S6 quality).
- **Direction correctness (4d):** fraction of signatures with correct `abundance_in_group_1` given
  correct Group 0/1 orientation; a flipped orientation shows as systematic inversion.
- **Ontology sub-score:** predicted vs gold `uberon_id`/`efo_id` CURIE match (exact + same-branch
  partial), reusing the ontology plan's V1–V4.

### 4c. Source-type ablation (the load-bearing experiment)

Run each architecture in three input configurations and report every metric per config:

1. **abstract-only** (metadata + abstract),
2. **+full text & main tables** (add `get_full_text_article`),
3. **+supplements & figures** (add S1b/S1c).

Cross-tabulate by gold `source` type (main-table / figure / supplement, from `signatures.source`).
This **quantifies the brief's central claim** — that text-only caps signature recall near the ~16%
main-table share — and tells us exactly what each S1 sub-fetcher buys before we build it.

### 4d. Fair comparison of two agent teams

- Same corpus, same split, same input config, same cost budget, same gold snapshot.
- Report the **aligned score and the alignment quality separately** (never zero a team for
  segmenting differently than the human).
- **Discount known-bad gold:** exclude the ~0.5–2.3% of fields the curated data itself flags
  (missing NCBI ID / label drift) from precision penalties.
- Report **cost & latency per study** alongside accuracy — a 2×-cost team that adds 3 F1 points is
  a real decision, and B/C cost more than A by construction.
- Paired per-study deltas + significance on the test set, not just corpus means.

### 4e. Harness shape (build)

A `bugsigdb eval` command (or `scripts/eval/`) that: loads gold via the join, runs the pipeline
over a study list at a chosen config with a concurrency/cost cap, gates each output through
`bugsigdb validate` (invalid records scored as structural-fail, counted separately), computes 4b
metrics, and writes a per-study JSONL + an aggregate report stratified by source-type and
experiment-count. Deterministic seed + cached MCP/authority responses for reproducibility.

---

## 5. Recommendation & phased build order

### Primary architecture

**Build B (hierarchical orchestrator + specialist subagents) as the core, with C (adversarial
verifier) layered onto the crux stages (S5/S6/direction), and A as the degenerate single-worker
fallback for simple papers.** Rationale against the constraints:

- Only B degrades gracefully on the **1→48 experiment** spread (constraint 3): per-experiment
  isolation + map-reduce (sub-pattern D) turns the hard tail into many small parallel tasks.
- The **figure/supplement recall ceiling** (constraint 2) demands *specialized* S1c/S5 workers
  (table vs supplement vs figure-vision); B's polymorphic Signature Extractor is where that
  specialization naturally lives, rather than being wedged into a single linear step.
- C targets exactly the **hallucination-concentrated** stages (constraint 4) that `validate`
  cannot catch, and B gives it a clean per-worker home.
- A is not thrown away — it *is* B with one experiment worker and no fan-out, so the skeleton and
  the simple-paper path are a strict subset of the target. This sequences risk cleanly.

### Phased roadmap (S ≤1–2d · M ≈3–5d · L ≥1wk)

- **Phase 0 — Eval harness first + walking skeleton (M).** Build §4 (gold join on pmc-map +
  relational CSVs, metrics, source-type ablation scaffold) **before** the pipeline, plus
  Architecture **A** over **abstract-only + main-table** studies only (S0,S1a,S2,S3,S4,S5-table,
  S6,S7,S8,S9). Deliverable: end-to-end schema-valid records on the happy path + a baseline score.
  *Justification:* start narrow where text is sufficient; the harness is what makes every later
  phase measurable. Depends on `feature/pmc-map` landing.
- **Phase 1 — Verifier layer C on S5/S6/direction (M).** Add Taxa Verifier + Direction Verifier +
  bounded repair + unresolved escape hatch. Expect precision/direction gains on the Phase-0 corpus;
  measure with 4b/4d.
- **Phase 2 — Hierarchy B + supplement retrieval (L).** Refactor A→B (Curator-Lead + Experiment
  Workers + map-reduce D); add **S1b supplement acquisition** (OA-package/`oa_file_list` fetch +
  XLSX/CSV/DOCX/PDF parse) + supplement-worker. This is the single biggest recall unlock (~21% of
  signatures) and the hardest engineering. Measure the ablation-2→3 lift.
- **Phase 3 — Figure vision path (L).** Add **S1c figure fetch + vision OCR** (LEfSe bars,
  cladograms, heatmaps) with direction decoding, gated behind the Direction Verifier. Targets the
  ~57% figure-sourced signatures — highest ceiling, highest risk. Ship only when the verifier keeps
  figure-derived precision acceptable.
- **Phase 4 — Hardening (M).** Segmentation Critic, license/rate-limit compliance (JATS license
  block, E-utilities backoff, attribution carry-through), resolver/ontology cache warmers,
  human-in-the-loop review queue for flagged records.

Cost control throughout: cache all MCP/authority calls; cap fan-out concurrency; run the smoke set
on every change and the full corpus only at phase boundaries.

### Top open decisions for the user

1. **How do we fetch supplements & figures?** (constraint 2, the recall ceiling) — PMC OA-package /
   `oa_file_list` for OA articles, publisher-supplement URLs, or Semantic Scholar OA PDFs? This
   gates Phases 2–3 and is entirely un-tooled today. **Biggest branch point.**
2. **Build vs reuse for taxonomy normalization (S6)** — live NCBI E-utilities (rate-limited,
   always-current) vs a bundled local NCBI names/synonym dump (reproducible, CI-safe)? Recommend a
   local dump + cache for eval reproducibility, E-utilities for gap-fill.
3. **Human-in-the-loop vs fully autonomous** — emit every record as `curation_state: Incomplete`
   drafts for human review (recommended given the flagged escape hatches), or aim for autonomous
   `Complete`? Sets the precision bar and whether S10's "flag" is a queue or a hard fail.
4. **Cost/latency budget per study** — bounds B's fan-out width and whether C runs on every stage
   or only figure/supplement-derived claims. Needs a target (e.g. \$ and minutes per study) before
   Phase 2.
5. **Figure vision scope (Phase 3 go/no-go)** — attempt the ~57% figure share (high ceiling, high
   hallucination risk) or cap the autonomous product at abstract+table+supplement (~37% of
   signatures) and route figure-only papers to humans? Decide after seeing Phase-2 ablation numbers.
6. **Ontology validation posture** — inherit the ontology plan's offline-OAK-pinned default;
   confirm CURIE (not UMLS) and whether V3/V4 gate the pipeline or advise.
```
