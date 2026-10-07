# BugSigDB Curation Schema (LinkML) and automated curator

[![CI](https://github.com/seandavi/bugsigdb-curation-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/seandavi/bugsigdb-curation-tools/actions/workflows/ci.yml)

A [LinkML](https://linkml.io) representation of the [BugSigDB](https://bugsigdb.org)
curation data model, reverse-engineered from the Semantic MediaWiki + Page Forms
application that curators use, plus the tooling built on it: an **automated de-novo
curator** that turns a PubMed ID into a schema-checked `Study → Experiment → Signature`
record, and an **evaluation harness** that scores those records against the human-curated
BugSigDB corpus.

BugSigDB captures **microbial signatures**: sets of microbial taxa reported as
differentially abundant (DA) between two groups of samples in a published study.

> **Status: research prototype.** The numbers in [Results so far](#results-so-far) come
> from a 19-study smoke set and mostly single runs. Treat them as a floor and a
> direction, not a benchmark. The append-only lab notebook is
> [`docs/LEDGER.md`](docs/LEDGER.md); the draft paper is
> [`paper/bugsigdb-autocuration.qmd`](paper/bugsigdb-autocuration.qmd).

## Contents

- [Workflow at a glance](#workflow-at-a-glance)
- [Methods](#methods)
- [Data model](#data-model)
- [Results so far](#results-so-far)
- [Layout](#layout)
- [Validate / generate](#validate--generate)
- [CLI reference](#cli)
- [Reproducing the pipeline](#reproducing-the-pipeline)

## Workflow at a glance

The system has three parts (Figure 1): **A** builds the curated corpus into a
held-out gold set, **B** is the curator, which sees only a PMID, and **C** scores the
curator's output against the gold and routes drafts to human reviewers. A *data firewall*
separates B from the gold: prediction records flow from B to C, never the reverse.

<a id="fig-workflow"></a>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/figures/workflow-dark.svg">
  <img src="docs/figures/workflow-light.svg" alt="End-to-end workflow: ingest builds the held-out gold; the curator runs stages S0 to S9 from a PMID; the scorer and review packets consume its prediction records." width="100%">
</picture>

**Figure 1. End-to-end workflow.** Lane A (ingest) builds the relational gold tables from
the public BugSigDB export. Lane B (curate) runs the per-PMID pipeline; dashed boxes are
optional stages (`--design split-*` adds S10, `--supplements` adds S1b). Lane C (evaluate)
is the only code that reads gold. Source: [`docs/figures/make_figures.py`](docs/figures/make_figures.py).

## Methods

### 1. Corpus and the held-out gold

`bugsigdb export` downloads the merged CSV dump from
[`waldronlab/bugsigdbexports`](https://github.com/waldronlab/bugsigdbexports). The dump is
denormalised (one row per signature, study and experiment columns repeated).
`bugsigdb split` normalises it into five relational tables, and `bugsigdb pmc-map`
maps PMIDs to PubMed Central IDs so that studies with open full text can be curated and
scored. The current corpus is 2,068 studies; 2,052 have a numeric PMID and about 84% of
those resolve to a PMCID.

| Table | One row per | Key columns |
|-------|-------------|-------------|
| `studies.csv` | study | `study_id`, `pmid`, `doi`, `study_design`, `state` |
| `experiments.csv` | experiment | `experiment_id`, `study_id`, group names and sizes, `body_site`, `condition`, methods, alpha diversity |
| `signatures.csv` | signature | `signature_id`, `experiment_id`, `source`, `abundance_in_group_1` |
| `taxa.csv` | distinct taxon | `ncbi_id`, `taxon_name` |
| `signatures_taxa.csv` | signature × taxon | the join between the two |

*Table 1. Relational gold tables written by `bugsigdb split` (column lists abbreviated; see `src/bugsigdb_curation/split.py`).*

**Data firewall.** The curated records are *held-out ground truth, used for scoring only*.
The curator receives a PMID plus whatever source artifacts it fetches itself. It never sees
a curated field (study design, segmentation, body site, condition, group orientation,
taxa, direction, or source), and its taxonomy lookups use NCBI, not the gold `taxa.csv`.
Curator modules do not import `eval`, and `tests/test_curator_firewall.py` guards the
boundary. This matters because an agent that could read the answer would make every
metric meaningless.

### 2. The curator: a per-PMID stage pipeline

`bugsigdb curate --pmid <PMID>` runs the stages in Table 2. Stages S0–S4, S5a, S8 and S9 are
identical for every design; designs differ only in S5b/S6 and S10 (next section).

| Stage | What it does | Notes |
|-------|--------------|-------|
| S0 resolve | PMID → PMCID, DOI | NCBI ID Converter |
| S1 evidence | Fetch the article as sections, tables and figures | EuropePMC `fullTextXML` for text and tables; PMC article HTML → CDN URLs for figure images. Fully scriptable, no browser. |
| S2 study | Title, authors, journal, year, study design | One LLM call |
| S3 segment | Propose the list of 2-group comparisons ("stubs") the paper reports | One LLM call over the assembled text |
| S4 experiment | Per stub: groups, sample sizes, host, body site, condition, sequencing, statistics | One LLM call per stub; optional body-site → UBERON mapping |
| S5a locate | Choose the table or figure holding the stub's DA result | Keyword regex, or a decision-model ranking with `--decision-model` |
| S5b/S6 extract | Per stub: taxa, direction, NCBI taxon id | Depends on `--design`; ids are *verified* against the taxonomy authority, never trusted from the model |
| S1b supplements | Read the paper's supplementary files and append their experiments | Opt-in (`--supplements`, needs `--decision-model`) |
| S10 verify | Adversarial check of extracted taxa and directions | `split-verify` and `split-panel` only |
| S8 assemble | Build the nested-dict record in the loader's shape | |
| S9 validate | Check against `schema/bugsigdb.yaml` | Failures are recorded, not hidden |

*Table 2. Curator stages. The record keeps provenance (`design`, `flags`, annotations) that is never fed back into extraction.*

Figures are read with a multimodal model: the image and its legend go in together. An
earlier benchmark ([`benchmarks/figure-extraction/`](benchmarks/figure-extraction/))
found taxa-set F1 of about 0.9 for stacked-bar plots, falling to about 0.6 for
cladograms and heatmaps, with errors mostly missed labels rather than invented ones.
One LEfSe figure had every direction flipped, so group orientation is treated as its own
failure mode.

### 3. Three stage designs

`--design` selects how taxa are extracted and checked. All three share the backbone above.

| Design | S5b/S6 (taxa and ids) | S10 (review) |
|--------|----------------------|--------------|
| `fused-lean` (default) | One call extracts taxa and direction *and* proposes NCBI ids; ids are tool-verified | Structural validation only |
| `split-verify` | NER call returns names and direction only; deterministic name → taxid resolution, with an LLM only to disambiguate homonyms | Adversarial verifier on two failure modes: taxon present in source, and direction; bounded repair (≤ 2 rounds) |
| `split-panel` | As `split-verify` | Independent reviewer re-derives the answer from the source in a fresh context; arbitration and bounded repair |

*Table 3. The three curator designs. Anything unresolved after repair is flagged or dropped, never guessed.*

On the 19-study smoke set at a fixed cheap model, `fused-lean` won on both accuracy and
cost (see [Results so far](#results-so-far)), and is the default.

### 4. Taxon resolution

Predicted taxon *names* must become NCBI taxids for both curation and scoring. A local
DuckDB database is built from a pinned NCBI taxdump (`bugsigdb taxonomy build`; the
smoke runs used release `2026-07-01`). Lookups are offline and handle synonyms
(*Propionibacterium acnes* → *Cutibacterium acnes*), rank prefixes such as
`g__Faecalibacterium`, and retired ids through `merged.dmp`. Live NCBI E-utilities is
used only to fill gaps. An earlier live-only resolver was rate-limited (HTTP 429) at
smoke-set scale, which is why the local database exists.

### 5. Optional levers

- **Decision models** (`--decision-model`). Some judgments are bounded: is this sheet a DA
  table, which UBERON term matches this body site. A Cloudflare "decision model" (Clef)
  returns a calibrated probability per option without generating text, at far lower cost
  than a generative call. An offline probe
  ([`benchmarks/decision-probe/RESULTS.md`](benchmarks/decision-probe/RESULTS.md))
  kept the judgments that worked (artifact ranking, supplement-page screening,
  body-site mapping) and rejected the ones that did not (per-taxon direction, figure
  type). Needs `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in `.env`.
- **Supplements** (`--supplements`). Most DA results are in supplementary files that the
  main-text pipeline cannot see. The lever fetches the EuropePMC supplement ZIP, splits it
  into units (one per sheet or PDF page), screens each unit with the decision model, sends
  the routed units to one extraction call, and expands "one-vs-rest" multi-group tables into
  one experiment per group in code. Experiments the main text already reports are dropped.
- **Ground unresolved** (`--ground-unresolved`, `fused-lean` only). Taxa whose model-proposed
  id could not be verified are re-resolved by name against the NCBI authority.

### 6. Evaluation

`bugsigdb eval score` aligns predictions to gold and reports, per study and in aggregate:

1. **Experiment alignment.** Predicted and gold experiments are matched by a Hungarian
   (max-weight bipartite) assignment on field overlap. Unmatched predictions count as
   over-segmentation, unmatched gold as under-segmentation.
2. **Signature alignment.** Within a matched experiment, signatures are paired by taxa-set
   overlap, *not* by the declared direction label. A systematic Group 0/1 flip then shows
   up as low direction accuracy while taxa precision and recall stay high, rather than
   masquerading as a catastrophic taxa failure.
3. **Taxa as taxid sets.** Names are resolved to NCBI taxids and compared as sets
   (precision, recall, F1, Jaccard; micro and macro). Retired ids are canonicalised on
   both sides before comparison.
4. **Cross-tabulation by gold source type** (main table, figure, supplement). A pipeline
   that cannot read supplements would otherwise be dominated by gold it cannot reach.
5. **Coverage counters** for names that fail to resolve, so name-based scores cannot
   quietly shrink.
6. **Known-bad gold discount.** Gold signatures from incomplete curation (blank `State`)
   do not penalise predicted taxa as false positives.

### 7. Human review

Gold is imperfect, and a draft with no gold has no automatic score. `bugsigdb review`
builds one self-contained HTML packet per draft; a BugSigDB curator judges it against the
paper and returns a verdict JSON, which is validated against
[`schema/review_verdict.schema.json`](schema/review_verdict.schema.json) and pinned to the
draft's SHA-256. Details are in [Human review packets](#human-review-packets-bugsigdb-review).

## Data model

A three-level hierarchy, annotated with controlled vocabularies and ontology terms
(Figure 2). The schema is [`schema/bugsigdb.yaml`](schema/bugsigdb.yaml): 6
classes, 63 slots and 12 enums.

<a id="fig-datamodel"></a>
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/figures/data-model-dark.svg">
  <img src="docs/figures/data-model-light.svg" alt="Class diagram: Study contains Experiments, which contain Signatures, which list Taxa. A CurationProvenance mixin and Review records sit alongside." width="100%">
</picture>

**Figure 2. The LinkML data model.** `Study` (one publication, identified by `uid`, the
wiki page name; usually a PMID) contains `Experiment`s (one two-group comparison, Group 0 =
control, Group 1 = case), each containing `Signature`s (taxa that moved one way in Group 1),
each listing `Taxon` nodes. `CurationProvenance` is a mixin carrying curation state and
reviews. Only the slots curators and the agent fill in are shown; the schema has more.

Ontology bindings: condition → EFO/MONDO, body site → UBERON, host species and signature
taxa → NCBI Taxonomy. Ontology-bound slots are string-ranged in the schema, with the binding
documented in comments. Every class, slot, and enum carries a `description` plus
dual-audience `comments`: `CURATOR:` for humans, `AGENT:` for the automated extractor.

## Results so far

Smoke set: 19 studies, `gemini-3.1-flash-lite` (the cheapest multimodal tier), text +
tables + figures, no supplements, single run (ledger L027, L030, L031).

| Source type of gold taxa | Gold taxa | Precision | Recall | F1 |
|--------------------------|----------:|----------:|-------:|---:|
| figure | 260 | 0.78 | 0.30 | 0.43 |
| main table | 51 | 0.71 | 0.10 | 0.17 |
| supplement (unreachable without S1b) | 1,056 | 0.40 | 0.01 | 0.01 |

*Table 4. `fused-lean` taxa-set metrics by gold source type, micro-averaged (L027). Direction accuracy 80.8%; name → id accuracy 100%.*

- **Precision is good, recall is the bottleneck.** About 77% of the smoke set's gold taxa
  live in supplements, and papers with 21 or more experiments are under-segmented by the
  linear single-worker topology (recall about 0.01 there).
- **Design comparison (L030).** Micro F1: `fused-lean` 0.134, `split-panel` 0.031,
  `split-verify` 0.018. The split designs' verifier grounded figure-derived taxa against
  legend text only, which structurally drops correct figure taxa; that was fixed afterwards
  (figure images now reach the verifier) but the comparison was not re-run.
- **A strong model with the evidence in hand does well (L031, n = 1).** Given one
  paper's supplementary PDF directly, `gemini-3.1-pro-preview` scored taxa-set F1 0.83 on a
  48-experiment paper the main-text pipeline scored 0.00 on. Direction accuracy was 11.5%
  (a global orientation mismatch), and 18 multi-group experiments were missed. The PDF was
  hand-fed, so this is a ceiling test, not a pipeline result.
- **Decision-model probe (L032).** Supplement-page screening reached recall 1.0 at
  precision ≥ 0.77, and artifact ranking AUROC 0.96. Per-taxon direction did not work.
  Labels are agent-drafted and unreviewed; n is small.

The first reading is that the low headline F1 is mostly a *retrieval* problem, not a
reasoning problem. Next levers, in expected order of impact: supplement retrieval,
fan-out for many-experiment papers, a model sweep, and a direction-orientation fix.

## Layout

| Path | Contents |
|------|----------|
| `schema/bugsigdb.yaml` | The LinkML schema. 6 classes, 63 slots, 12 controlled-vocabulary enums. |
| `schema/review_verdict.schema.json` | JSON Schema for reviewer verdict files. |
| `src/bugsigdb_curation/` | The `bugsigdb` CLI. `curator/` (pipeline stages), `eval/` (gold join and scorer), `taxonomy/` (DuckDB backend), `review/` (packets and verdicts), plus loader, split, export, validate. |
| `sources/` | Local snapshot of the wiki schema pages the schema was derived from. |
| `benchmarks/` | `figure-extraction/` (vision benchmark) and `decision-probe/` (decision-model probe). |
| `docs/` | `LEDGER.md` (lab notebook), `plans/` (research brief, workflow plan, ontology plan), `workflow.md` (older Mermaid view), `figures/` (README figure generator). |
| `paper/` | Quarto draft of the methods paper. |
| `tests/` | pytest suite. Network-marked tests are deselected by default. |

### `sources/` — the source material

A faithful scrape of the curation schema as encoded at bugsigdb.org (the site is
behind Cloudflare, so pages were pulled via the MediaWiki API at `/w/api.php`):

- `forms/` — Page Forms definitions (input types, mandatory flags, defaults, conditional display).
- `templates/` — how form fields map to stored semantic properties.
- `properties/` — one file per SMW property; datatypes and the tooltip text shown to curators.
- `values/` — snapshots of the controlled-vocabulary value lists (countries, host species, body sites, statistical tests, …).
- `help/` — human curation guidance pages.

## Validate / generate

```bash
uvx --from linkml gen-json-schema schema/bugsigdb.yaml   # -> JSON Schema
uvx --from linkml gen-owl         schema/bugsigdb.yaml   # -> OWL
uvx --from linkml gen-pydantic    schema/bugsigdb.yaml   # -> Pydantic models
```

## CLI

`bugsigdb export` downloads the generated export artifacts (merged CSV dump
and/or GMT signature sets) from the [`waldronlab/bugsigdbexports`](https://github.com/waldronlab/bugsigdbexports)
repo:

```bash
uv run bugsigdb export --list              # see what's available, no download
uv run bugsigdb export                     # full_dump.csv + file_size.csv -> data/exports/
uv run bugsigdb export --select gmt        # GMT signature sets instead
uv run bugsigdb export --select all        # everything
uv run bugsigdb export --ref v1.2.3        # a specific tag/branch instead of devel
uv run bugsigdb export --force             # re-download even if a same-size file exists
```

Existing files are skipped when their size already matches the remote (use
`--force` to override). Downloads stream to disk with bounded concurrency and
a `rich` progress bar; run `uv run bugsigdb export --help` for all options.

`bugsigdb validate` checks one or more curated instance files (YAML or JSON,
each holding a single object or a list of objects) against the LinkML schema,
using the [`linkml` validator](https://linkml.io/linkml/schemas/validation.html):

```bash
uv run bugsigdb validate study.yaml                          # validate as a Study (default)
uv run bugsigdb validate study.yaml other.yaml                # multiple files in one invocation
uv run bugsigdb validate experiment.yaml -C Experiment         # validate against a different class
uv run bugsigdb validate study.yaml --schema my-schema.yaml    # override the schema
uv run bugsigdb validate study.yaml --format json              # machine-readable report
```

Exit codes: `0` if every instance is valid, `1` if any instance fails schema
validation (bad enum value, wrong type, missing required field, …), `2` for
usage/IO errors (file not found, unparseable YAML/JSON, unknown
`--target-class`, bad `--schema` path). Run `uv run bugsigdb validate --help`
for all options.

`bugsigdb load` parses a `full_dump.csv` export (denormalized: one row per
Signature, with Study/Experiment columns repeated) into nested
Study -> Experiment -> Signature records matching `schema/bugsigdb.yaml`'s
slot names:

```bash
uv run bugsigdb load data/exports/full_dump.csv                     # -> YAML on stdout
uv run bugsigdb load data/exports/full_dump.csv --format json       # JSON instead
uv run bugsigdb load data/exports/full_dump.csv -o studies.yaml      # to a file
uv run bugsigdb load data/exports/full_dump.csv --limit 20           # first 20 studies only
```

Studies are keyed by `uid` (the stable `Study` page name, always present);
`pmid` is captured separately as an optional integer when present. Cells are
coerced to the schema's types (ints, floats, bools,
enum values) and multivalued cells are split into lists; blank ("NA") cells
are simply omitted rather than defaulted or invented. `MetaPhlAn taxon names`
and `NCBI Taxonomy IDs` are paired up into `Taxon` records (id + name + rank
+ lineage). The output is plain nested dicts (no `linkml`/`pydantic`
dependency) so it can later be checked structurally, e.g. by a `bugsigdb
validate` command. See `bugsigdb_curation/loader.py` for the exact
column -> slot mapping and delimiter conventions (derived from inspecting the
real dump, not just the wiki docs — some columns disagree with the wiki's
documented delimiters).

`bugsigdb pmc-map` maps curated studies' PMIDs to PubMed Central IDs
(PMCIDs) via the [NCBI PMC ID Converter API](https://www.ncbi.nlm.nih.gov/pmc/tools/id-converter-api/),
producing a gold/eval set for de-novo curation workflows (which typically
need PMC full text, not just a PubMed abstract):

```bash
uv run bugsigdb pmc-map                                  # data/exports/relational/studies.csv -> data/eval/pmid_pmcid_map.csv
uv run bugsigdb pmc-map --input studies.csv --output map.csv
uv run bugsigdb pmc-map --email me@example.org            # NCBI etiquette for unauthenticated use
uv run bugsigdb pmc-map --limit 50                        # first 50 distinct PMIDs only, for testing
```

Reads distinct numeric PMIDs from a relational `studies.csv` (as produced by
`bugsigdb split`; the 16 or so studies with no PMID at all are skipped),
queries idconv in batches of 200 with bounded (3-way) concurrency, and
writes `study_id,pmid,pmcid,doi,has_pmc` — one row per study, so studies
that share a PMID both appear. A coverage summary (`N PMIDs: M with PMCID
(X%), K without.`) is printed to stderr. Of the current 2068 curated
studies, 2052 have a numeric PMID, of which about 84% resolve to a PMCID.

`--limit` truncates the *distinct PMID* list before querying, not the study
rows — so any study row whose PMID falls outside that truncated subset is
excluded from the output CSV (its PMID was simply never queried). When that
happens, a `Note: N study row(s) excluded (PMID outside --limit).` line is
printed to stderr so the row-count drop isn't silent.

`bugsigdb split` normalises a flat `full_dump.csv` into the five relational CSVs
described in Table 1 (default `data/exports/relational/`). These tables are the held-out
gold: only `eval score` and `eval gold` read them.

```bash
uv run bugsigdb split                                        # data/exports/full_dump.csv -> data/exports/relational/
uv run bugsigdb split --input dump.csv --output-dir out/
```

### De-novo curation (`bugsigdb curate`)

`bugsigdb curate` takes a PMID and writes a schema-checked prediction record in exactly
the shape `eval score` consumes. It never takes a gold path. See [Methods](#methods) for
what each stage and flag does.

```bash
uv run bugsigdb curate --pmid 21850056 -o pred.json                  # Gemini via LiteLLM, fused-lean
uv run bugsigdb curate --pmid 21850056 --mock                        # mock LLM: no model key needed (still fetches the paper)
uv run bugsigdb curate --pmid 21850056 --design split-verify         # or split-panel
uv run bugsigdb curate --smoke -o preds/                             # the curator's ~20-study smoke set
uv run bugsigdb curate --pmid 34620922 --decision-model clef --supplements -o pred.json
```

A model key for LiteLLM's `gemini/` provider (`GOOGLE_API_KEY` or `GEMINI_API_KEY`) goes
in `.env`; an optional `NCBI_API_KEY` raises the NCBI rate limit. When the run produces
sidecar annotations (decision-model calls, body-site → UBERON), they are written to
`<out>.annotations.json` next to the prediction. `--taxonomy-db` / `--taxonomy-release`
choose the local taxonomy database (below); without one the curator falls back to live
NCBI.

### Taxonomy backend (`bugsigdb taxonomy`)

```bash
uv run bugsigdb taxonomy build --download --release 2026-07-01     # NCBI taxdump -> DuckDB (cached under XDG cache)
uv run bugsigdb taxonomy build --taxdump taxdump.tar.gz --release 2026-07-01
uv run bugsigdb taxonomy lookup "Propionibacterium acnes"          # name -> tax_id (synonyms, merged ids)
uv run bugsigdb taxonomy lookup --taxid 1747                       # tax_id -> name + lineage
```

The database path resolves as `--db` > `BUGSIGDB_TAXONOMY_DB` > newest cached
`ncbi-taxdump-*.duckdb`.

### Scoring (`bugsigdb eval`)

```bash
uv run bugsigdb eval score --pred preds/ --out report/ --smoke      # -> scores.jsonl, report.md, report.html
uv run bugsigdb eval gold --smoke -o gold.yaml                      # dump gold in the prediction shape
```

`score` reads the relational gold (`--relational`) and the PMID → PMCID map
(`--pmc-map`); see [Evaluation](#6-evaluation) for the metrics.

### Supplement inspection (`bugsigdb supplements`)

```bash
uv run bugsigdb supplements --pmid 34620922                  # list a paper's supplementary files
uv run bugsigdb supplements --pmcid PMC8000000 --dump out/   # also write raw + parsed text
```

Read-only and standalone: it uses public EuropePMC/NCBI data and touches no gold. The
extraction path is `curate --supplements`.

### Human review packets (`bugsigdb review`)

Drafts written by `bugsigdb curate` have no gold, so BugSigDB curators' verdicts
are the evaluation. A **review packet** is one self-contained `.html` file (inline
CSS/JS, no network requests): the reviewer opens it, judges the draft against the
paper, clicks *Export*, and sends back one JSON file.

```bash
# 1. build a packet (+ <pmid>.manifest.json pinning the draft's sha256)
uv run bugsigdb review packet --pred preds/21850056.json --out packets/ \
    --model-label gemini-3-pro --design-label split-verify
#    picks up preds/21850056.annotations.json automatically; --offline skips fetching evidence,
#    --evidence-dir DIR caches it (complete fetches only; --refresh-evidence refetches),
#    --pmcid overrides the PMID->PMCID lookup
# 2. file returned verdicts (validated against schema/review_verdict.schema.json)
uv run bugsigdb review ingest ~/Downloads/verdicts_21850056_*.json --manifests packets/   # -> data/reviews/<pmid>/
# 3. aggregate
uv run bugsigdb review report --reviews data/reviews --out report.md
```

Figure images are embedded only when the article's EuropePMC licence is CC BY or
CC0 (each image under ~1.5 MB, all images together under ~6 MB); otherwise the packet
shows the legend and a link. `ingest` refuses verdicts whose `draft_sha256` differs
from the manifest, and refuses to overwrite a different file already filed for the
same reviewer and second, unless `--force`; re-ingesting an identical file is a no-op. Verdicts contain reviewer names/emails: `data/` is git-ignored.

## Reproducing the pipeline

```bash
uv sync
uv run bugsigdb export                                   # 1. download full_dump.csv
uv run bugsigdb split                                    # 2. -> relational gold tables
uv run bugsigdb pmc-map                                  # 3. -> data/eval/pmid_pmcid_map.csv
uv run bugsigdb taxonomy build --download --release 2026-07-01   # 4. local taxonomy DB
uv run bugsigdb curate --smoke -o preds/                 # 5. curate the smoke set (needs a model key)
uv run bugsigdb eval score --pred preds/ --out report/ --smoke   # 6. score against gold
uv run pytest                                            # offline test suite
```

`data/` is git-ignored: it holds the export, gold tables, run outputs and reviewer
verdicts (which contain reviewer names and emails). Each run's results in
[`docs/LEDGER.md`](docs/LEDGER.md) are anchored to a commit and the pinned taxonomy release.
To regenerate the README figures: `python docs/figures/make_figures.py` (stdlib only).

## License

Schema released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/),
consistent with BugSigDB.

PDF reading (supplement pages: text, size, JPEG render) goes through `bugsigdb_curation.pdf`, built on
[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (BSD-3-Clause / Apache-2.0) and Pillow (MIT-CMU). It
replaced PyMuPDF, whose AGPL-3.0 licence (or commercial licence) would have made the whole project copyleft.
