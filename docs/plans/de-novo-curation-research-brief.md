# De Novo BugSigDB Curation from PMC/PubMed — Feasibility Research Brief

**Status:** Research/feasibility only. No schema/loader/validator changes. Input to a downstream
planner that will design the multi-agent workflow.
**Author:** research analyst pass, 2026-07-13
**Question:** Given a PMID/PMCID, can an automated pipeline extract a BugSigDB record (Study →
Experiments → Signatures) matching `schema/bugsigdb.yaml`? Where are the hard limits?

**Sources grounded against:** `schema/bugsigdb.yaml`; `sources/help/*.wiki`;
`docs/plans/ontology-integration-plan.md`; the relational export
(`data/exports/relational/*.csv`, snapshot 2026-07-13, 2,068 studies / 8,942 experiments /
14,156 signatures / 110,187 signature–taxon links); and live probes of real articles via the
PubMed/PMC MCP tools. Article structure below is characterized, not reproduced (copyright).

> Attribution note: article full text/metadata below was retrieved via PubMed/PMC. The
> `get_full_text_article` and `get_article_metadata` tools emit a mandatory legal notice
> requiring PubMed attribution and inline DOI links whenever their content is *presented*. Any
> pipeline surfacing fetched text to users must carry that attribution.

---

## 0. TL;DR for the planner

- **Study-level metadata is essentially free.** With a PMID, PubMed/PMC returns clean title,
  authors, journal, year, keywords, abstract, DOI. Only `study_design` needs inference. This
  layer is close to solved.
- **Experiment-level metadata is mostly recoverable from Methods prose**, but requires
  segmentation judgment (how many experiments?) and ontology mapping (body_site→UBERON,
  condition→EFO/MONDO). Medium difficulty, mostly tractable.
- **Signatures are the bottleneck and the reason full automation is hard.** The actual
  differential-abundance taxa lists live disproportionately in **figures (images, ~57% of
  signatures)** and **supplementary files (~21%)**, with only ~16% in main-body tables. The PMC
  full-text tool returns body prose + main tables but **not supplementary files and not figure
  contents** — so for a large class of papers the signature payload is simply not in what we can
  fetch through this channel.
- **Full-text access is ~73%** (44/60 sampled study PMIDs have a PMCID). The other ~27% are
  abstract-only through this route. Simple studies (1–2 taxa) are often recoverable from the
  abstract even without full text; rich studies are not.

---

## 1. What a curated record must contain, decomposed by level (with difficulty)

Difficulty scale: **trivial** (from PubMed record) · **methods-text** (inferable from Methods
prose) · **results/supplement** (needs the differential-abundance result, often figure/supplement)
· **ontology** (needs controlled-vocab/ontology mapping) · **judgment** (curator judgment call).

### 1a. Study (one publication → one Study; `uid` is the key, not `pmid`)

| Field | Mandatory | Difficulty | Notes |
| --- | --- | --- | --- |
| `uid` | yes (identifier) | trivial | Page name; for automation, use the PMID or `Study_<id>`. |
| `pmid`, `doi`, `uri` | no | trivial | Direct from the PubMed record / ID converter. |
| `citation_mode` | yes (default Auto) | trivial | Auto when a PMID exists → PubMed fills bibliography. |
| `title`,`authors`,`journal`,`year`,`pages`,`first_page`,`keywords`,`abstract` | no (Auto fills) | trivial | Only needed in Manual mode (no-PMID studies, ~0.8%). |
| `study_design` | de-facto yes | methods-text + **judgment** | Fixed vocab (7 values). `Help_Study_Design.wiki` shows the discriminators are subtle: case-control vs cross-sectional vs nested case-control vs prospective cohort vs longitudinal all hinge on *how subjects were selected*, not just wording. Real error source. |

Verdict: **near-trivial except study_design**, which is a genuine classification task.

### 1b. Experiment (one two-group comparison; Group 0 = control, Group 1 = case)

| Field | Difficulty | Notes |
| --- | --- | --- |
| (segmentation: how many Experiments?) | **judgment** | One paper → N experiments, one per distinct 2-group comparison (per body site / cohort / timepoint / method). This is the hardest experiment-level decision and drives everything downstream. Probed studies ranged from **1** (PMID 19849869, 21850056) to **48** experiments (PMID 34620922). |
| `location_of_subjects` | methods-text + ontology(country list) | Recruitment country, not author affiliation. ~197-value list. |
| `host_species` | methods-text (mandatory; sentinel `Not specified`) | Usually obvious (`Homo sapiens`); closed ~128-value list. |
| `body_site` | **ontology (UBERON)** | Map sampled site → Glossary label; 99.5% UBERON coverage exists in curated data. Prefer specific site. |
| `condition` | **ontology (EFO/MONDO/…)** | The phenotype defining the case group. "EFO ID" column actually spans 15+ ontologies (MONDO dominant). Multi-valued. |
| `group_0_name`/`group_1_name` | methods-text | Short arm labels; must fix Group 0/1 orientation correctly — all directions depend on it. |
| `group_1_definition` | methods-text | Diagnostic/inclusion criteria of the case arm. (`group_0_definition` is not on the form — do not fabricate.) |
| `group_0_sample_size`/`group_1_sample_size` | methods-text/results | Per-comparison N (can differ from study N); sometimes only in a table. |
| `antibiotics_exclusion` | methods-text | Often absent → leave blank. |
| `sequencing_type` | methods-text | 16S/18S/WMS/ITS/PCR. |
| `variable_region_lower/upper_bound` | methods-text | 16S only; e.g. "V3–V4" → 3,4. |
| `sequencing_platform` | methods-text + vocab | Map instrument → enum (MiSeq→Illumina, etc.). |
| `data_transformation` | methods-text + judgment | Inferred from abundance representation; `Help_Data_Transformations.wiki` pairs it with tests. Frequently implicit. |
| `statistical_test` | methods-text + vocab | Multi-valued; ~49-value vocab; match exact spelling (LEfSe, DESeq2, Mann-Whitney (Wilcoxon), ANCOM-BC). Flag unknown tools rather than approximating. |
| `significance_threshold`, `lda_score_above` | methods-text | Numeric cutoffs; LDA only for LEfSe. |
| `mht_correction` | methods-text + **judgment** | `Help_MHT_correction.wiki`: inferred from naming a method / q-values. DESeq2 corrects by default, LEfSe does not — implicit knowledge. If both corrected+uncorrected reported, curate the corrected list. |
| `matched_on` / `confounders_controlled_for` | methods-text + vocab | Design-stage matching vs analysis-stage adjustment (distinct!). ~289-value shared list. |
| `pielou`/`shannon`/`chao1`/`simpson`/`inverse_simpson`/`richness`/`faith` | results (direction) + judgment | Whole-community alpha-diversity **direction** in Group 1 vs Group 0 (increased/decreased/unchanged). Only when the paper reports a between-group test for that metric. Often in a figure/results sentence. |

Verdict: **mostly methods-text, tractable**, but with two structural judgment calls (experiment
segmentation, MHT inference) and heavy ontology mapping (body_site, condition).

### 1c. Signature (one direction of change within an experiment)

| Field | Difficulty | Notes |
| --- | --- | --- |
| `abundance_in_group_1` | results + **judgment** | increased/decreased in Group 1 vs Group 0. Two Signatures per comparison (up + down). Orientation errors flip the whole signature. |
| `source` | results/supplement | Exact figure/table/supplement the taxa came from (e.g. "Table 2", "Figure 3B", "Supplementary Table S4"). |
| `description` | optional | Free text. |
| `taxa` (→ `Taxon.ncbi_id`) | **results/supplement + ontology(NCBI) + judgment** | The core payload: the *set* of taxa in that direction, each resolved to an NCBI Taxonomy integer ID. This is where automation is hardest (see §3). |

Verdict: **the crux.** Everything else is metadata; this is the scientific content and the part
most often not in fetchable text.

---

## 2. Input availability & retrievability (grounded probes)

### 2a. Full-text access rate
- **73% (44/60) of randomly sampled BugSigDB study PMIDs have a PMCID** (potential PMC full text).
  The remaining ~27% are **abstract + metadata only** through the PubMed/PMC tools. Older
  (pre-~2012) and some subscription-journal papers skew toward no-PMCID.
- PMCID presence is a necessary but **not sufficient** signal for programmatic reuse: it indicates
  PMC hosting, not necessarily an open/redistributable license. (The dedicated
  `get_copyright_status` tool returned HTTP 500 on every call during probing — currently
  unreliable; OA/license is more robustly read from the JATS license block or DOI/publisher.)

### 2b. Structure quality of retrieved full text (good, with caveats)
Probed `get_full_text_article` on OA articles:
- **PMC3507793 (PLOS ONE)** and **PMC3260502-class** papers: Introduction / Methods / Results /
  Discussion / Abstract come back as **clean, section-labeled prose**, and **main-body tables are
  rendered inline as text** (e.g. full numeric Table 1–4 content was present). Main tables are
  therefore extractable through this channel.
- **Caveat — italic taxon names get stripped.** In PMC8497572 the running Results text had genus
  and host-species names (rendered in the source as italics) **dropped**, producing sentences like
  "…NK4A21 group had significantly higher relative abundance in the rectum of ___ and ___ was
  higher in the cecum of ___." Since taxon names are exactly what we need, this is a material
  extraction artifact of the text channel — inline-prose taxon mentions are unreliable.

### 2c. The supplementary-data problem (evidence)
- **PMC8497572 / PMID 34620922** (Sci Rep, wild rodents, CC-BY): BugSigDB curated **48
  experiments and 74 signatures, and 100% of those signatures cite Supplementary Tables**. The
  full-text tool returned the entire body narrative — but the Supplementary Tables were **not
  included**; the article's tail was literally just a "Supplementary Information" placeholder. The
  Results prose only *references* "Supplementary Table _" (numbers stripped) and never lists the
  taxa. **Net: for this paper the tool surfaces ~0% of the actual signature payload.** A pipeline
  restricted to this channel could reconstruct the study/experiment scaffold but essentially none
  of the 74 taxa-sets.
- This generalizes. Across the whole export (14,156 signatures), the `source` field breaks down
  roughly as: **~57% figures, ~21% supplementary items, ~16% main tables, ~3% empty.** So the
  differential-abundance results a curator transcribes live predominantly in **images (figures)**
  and **supplementary files** — the two things the PMC text API does *not* give you — and only a
  minority in main-body tables (which it does).
- **Implication:** de novo signature extraction needs a **separate supplementary-file acquisition
  path** (PMC OA package / `oa_file_list` / publisher supplement URLs → download and parse
  XLSX/CSV/DOCX/PDF) **and a figure-understanding path** (vision OCR of LEfSe bar plots /
  cladograms / heatmaps), neither of which is covered by `get_full_text_article`.

### 2d. Contrasting simple cases
- **PMID 19849869** (Br J Nutr, obese Indian children): **no PMCID** → no PMC full text. Yet
  BugSigDB curated exactly 1 experiment / 1 signature / 1 taxon (*Faecalibacterium prausnitzii*
  increased in obese; qPCR; source "Figure 1"), and that single result is **stated verbatim in the
  PubMed abstract** ("Levels of Faecalibacterium prausnitzii were significantly higher in obese
  children…, P = 0.0253"). So small/targeted-taxon studies are frequently recoverable from the
  **abstract alone**, even with zero full-text access.
- **PMID 21850056** (ISME J, PMC3260502, colorectal cancer): 1 experiment / 2 signatures / 11
  taxa, all sourced from **"table 2"** — the recoverable-from-main-table happy path. Taxa are
  genus-level; in the export they carry greengenes-style rank prefixes (`g__Escherichia`) already
  resolved to NCBI IDs (562, etc.).

> Note: the task's suggested example PMID **23209786 is not actually a BugSigDB study** in this
> export (it is an iron-deficiency diagnostic paper, PMC3507793) — used here only as a full-text
> *structure* probe. Substituted 21850056 and 34620922 as real BugSigDB studies.

---

## 3. The signature-extraction bottleneck (why this is the crux)

Producing one Signature requires four chained sub-tasks, each with its own failure mode:

1. **Locate the differential-abundance result.** Per §2c it is usually in a figure or a
   supplement, not fetchable prose. This is the dominant blocker: if you can't get the source, no
   downstream step matters. Requires supplement download + spreadsheet/PDF parsing and/or vision
   extraction from figures.
2. **Assign direction (`abundance_in_group_1`) against the right Group 0/1 orientation.** Papers
   describe enrichment in whichever group they like ("higher in controls", "enriched in cases");
   the pipeline must normalize to Group 1 = case/exposed. LEfSe plots encode direction by
   color/side — trivial for a human, error-prone for OCR.
3. **Normalize taxon names → NCBI Taxonomy IDs.** Names appear with rank prefixes (`g__`, `s__`),
   abbreviations (*F. prausnitzii*), OTU/ASV codes, ambiguous/relabelled clades ("Clostridium
   cluster XIVa", "Lachnospiraceae NK4A214 group"), synonyms, misspellings, and greengenes/SILVA
   nomenclature. Requires a resolver (NCBI E-utilities / a taxonomy service) with synonym handling.
   The curated corpus is 100% NCBI-mapped (9,274 taxa, 0 missing) — so a *gold* mapping exists, but
   the source strings are messy and the schema explicitly tolerates "missing NCBI ID" as a
   cleanup state, meaning even humans don't always resolve everything.
4. **Segment into the right number of signatures/experiments.** One paper commonly reports **many**
   comparisons (probed: up to 48 experiments / 74 signatures). Over-segmentation (splitting one
   comparison into several) and under-segmentation (merging distinct body-site/cohort comparisons)
   both produce wrong records and wreck evaluation alignment (§4).

Hallucination risk is concentrated here: an LLM asked for "the taxa that increased" will readily
invent plausible genus names or fabricate NCBI IDs. Every emitted `ncbi_id` must be **verified
against NCBI**, and every taxon should be traceable to a cited `source`.

---

## 4. Evaluation methodology (use existing curated records as gold)

The 2,068 existing BugSigDB records are the gold standard. A parallel PMID→PMCID map lets us fetch
the same articles the humans curated, run the pipeline blind, and score against the curated record.
Structure metrics by level, because the alignment problem differs at each.

### 4a. Study-metadata accuracy (easy to score)
- Per-field exact/normalized match for `study_design`, `sequencing_type`, thresholds, booleans;
  field-level accuracy + confusion matrices (esp. `study_design`, `mht_correction`). Bibliographic
  fields are ~free and can be excluded or spot-checked.

### 4b. Experiment alignment/matching (the hard scaffolding metric)
- Predicted vs gold experiments must be **matched before scoring** (no shared IDs). Match on a key
  of (body_site, condition, group_0/1 semantics, sequencing_type). Report matched/ unmatched
  counts, and treat **over-/under-segmentation explicitly**: an experiment-count delta and an
  alignment F1 (bipartite matching; e.g. Hungarian on field-overlap score). Then, on matched
  pairs, per-field accuracy for the §1b fields. Pitfall: a single segmentation error cascades into
  many apparent field errors — report segmentation separately from field accuracy.

### 4c. Signature set-similarity (the headline metric)
- For each matched experiment, align predicted↔gold signatures by direction
  (`abundance_in_group_1`) — usually ≤2 per experiment, so alignment is easy once experiments are
  matched. Then score the **taxa set** per signature:
  - **Precision / Recall / F1** on NCBI-ID sets, and **Jaccard** on the ID sets.
  - Score **on resolved NCBI IDs**, not name strings, to make it taxonomy-normalization-invariant.
  - Add a **taxonomy-normalization sub-score**: of gold taxa, what fraction did the pipeline
    resolve to the correct NCBI ID from the source string (name→ID accuracy), separated from
    recall of *finding* the taxon at all. This isolates "missed the taxon" from "found it,
    mis-mapped the ID."
  - Consider **rank-aware partial credit** (predicting the correct genus when gold is at species,
    or vice versa) as a secondary lenient score, but keep a strict exact-ID score as primary.
- Aggregate micro (over all taxa) and macro (per signature, then averaged) — micro is dominated by
  a few large signatures; report both.

### 4d. Direction correctness
- Fraction of signatures with the correct direction given correct Group 0/1 orientation; a flipped
  orientation should be visible as systematic direction inversion.

### 4e. Pitfalls to bake into the harness
- **Partial credit / alignment ambiguity:** a good pipeline that segments differently than the
  human can be unfairly zeroed. Always report the *aligned* score and the *alignment quality*
  separately.
- **Gold is imperfect:** curated data has known "missing NCBI ID" and label-drift cleanup states
  (see `ontology-integration-plan.md` §1c). Do not treat gold as ground truth for the ~0.5–2.3% of
  fields it itself flags.
- **Source-restricted evaluation:** score a "text-only" configuration vs a "text+supplement+figure"
  configuration to quantify exactly how much the supplement/figure channels buy (per §2c the
  ceiling for text-only is low on figure/supplement-sourced signatures).
- **Stratify by difficulty:** report metrics split by source type (main-table vs figure vs
  supplement) and by experiment count, since feasibility varies wildly across these.

---

## 5. Constraints & risks for an automated pipeline

- **Supplement access is the #1 constraint.** ~21% of signatures (and whole studies like PMID
  34620922) are supplement-only, and the primary full-text tool does not return supplements. Needs
  a dedicated PMC OA-package / `oa_file_list` / publisher-supplement fetch + XLSX/CSV/DOCX/PDF
  parsing layer. Without it, a large minority of signatures are unreachable.
- **Figures are images.** ~57% of signatures are figure-sourced (LEfSe bar plots, cladograms,
  heatmaps). Requires vision/OCR extraction with direction decoding from color/axis — accuracy and
  hallucination are open risks.
- **~27% of studies have no PMC full text** at all (abstract-only via this route). Some are
  recoverable from the abstract (single-taxon studies); rich ones are not without the publisher PDF
  (copyright-gated).
- **Copyright / licensing at scale.** PMCID ≠ redistributable. Full-text/metadata tools carry a
  mandatory attribution+DOI-link legal notice. A scaled pipeline must track per-article license
  (JATS license block), respect NCBI E-utilities/PMC **rate limits** (batch, back off), and avoid
  redistributing non-OA text. The `get_copyright_status` tool was **unavailable (HTTP 500)** during
  probing — do not depend on it; read license from JATS/DOI.
- **Hallucinated taxa / fabricated NCBI IDs.** Must verify every `ncbi_id` against NCBI and require
  a cited `source`; never accept an unverified integer.
- **Taxonomy normalization drift.** Greengenes/SILVA names, OTU/ASV codes, obsolete/merged NCBI
  IDs, ambiguous clade names. Needs a synonym-aware resolver and an "unresolved" escape hatch
  (mirrors the human "missing NCBI ID" state).
- **Ontology mapping/drift for body_site (UBERON) and condition (EFO/MONDO/15+ ontologies).**
  Map to existing BugSigDB Glossary labels; multi-valued; condition spans many ontologies. See
  `ontology-integration-plan.md` for the coverage/validation approach (V1–V4 checks; ~99% body_site
  and ~98% condition mappable, but resolution/obsolescence/label-consistency must be validated).
- **Papers with dozens of comparisons.** Experiment/signature segmentation blows up (48 experiments
  observed). High risk of over/under-segmentation; needs explicit comparison-enumeration logic.
- **Implicit/non-standard reporting.** `mht_correction` (DESeq2 auto-corrects, LEfSe doesn't),
  `data_transformation`, and Group 0/1 orientation are often implicit — require domain rules, not
  just extraction.

---

## 6. Relevant tooling in this environment

- **PubMed/PMC MCP** (used here): `convert_article_ids` (PMID↔PMCID↔DOI — the gold-fetch enabler),
  `get_article_metadata` (title/abstract/authors/MeSH/keywords — powers Study level + abstract-only
  fallback), `get_full_text_article` (body prose + main tables inline; **no supplements, strips
  italic taxon names**), `get_copyright_status` (currently 500), `search_articles`. Solid for
  Study/Experiment scaffolding; insufficient alone for Signatures.
- **Semantic Scholar MCP** (`snippet_search`, `paper_details`, `paper_batch_details`): useful for
  abstracts/TLDRs and sometimes open-access PDF URLs; a secondary metadata/abstract source and a
  possible route to publisher OA PDFs for the ~27% without PMCID.
- **`bugsigdb validate` CLI** (`src/bugsigdb_curation/cli.py` `validate` command, over
  `schema/bugsigdb.yaml`): validates any emitted record against the LinkML schema (enum membership,
  required fields, types) — the natural **structural gate** on pipeline output before scoring.
  `load` and `export` commands also exist for round-tripping the export.
- **Ontology integration plan** (`docs/plans/ontology-integration-plan.md`): the mapping/validation
  design for body_site/condition/taxa CURIEs (coverage numbers, V1–V4 validation, offline OAK vs
  online OLS4). Directly informs the ontology-mapping agents and the eval's normalization scoring.

---

## Appendix — probes actually run (2026-07-13)

| PMID | PMCID | OA/journal | Curated (exp/sig/taxa) | Signature source | Retrievability finding |
| --- | --- | --- | --- | --- | --- |
| 19849869 | none | Br J Nutr (closed) | 1 / 1 / 1 | Figure 1 | No PMC full text; **single result present in abstract** → recoverable abstract-only. |
| 21850056 | PMC3260502 | ISME J (OA) | 1 / 2 / 11 | table 2 | Main tables retrievable inline → recoverable happy path. |
| 34620922 | PMC8497572 | Sci Rep (CC-BY) | 48 / 74 / many | Supplementary Tables (100%) | **Supplements not returned by full-text tool; italic taxon names stripped from prose → ~0% of signature payload recoverable via this channel.** |
| 23209786 | PMC3507793 | PLOS ONE (OA) | *not a BugSigDB study* | — | Used only as full-text structure probe: sections + Tables 1–4 inline, no supplements. |
| (60-PMID sample) | 44 have PMCID | — | — | — | **~73% full-text access rate; ~27% abstract-only.** |
| corpus (14,156 sigs) | — | — | — | ~57% figure / ~21% supplement / ~16% table / ~3% empty | Signature payload is mostly in images + supplements. |
