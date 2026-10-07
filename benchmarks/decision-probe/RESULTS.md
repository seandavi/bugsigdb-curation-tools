# Decision-model probe (issue #19, Phase 0b) — results

Offline probe of **Cloudflare Clef / Clef-flash** (System One decision models) on the curator's bounded
routing / screening / verification judgments, scored against the held-out gold. No pipeline wiring.
Run date **2026-10-06**; one run per (experiment × model); both models on every experiment.
Scorer-side: lives under `benchmarks/`, reads gold, and nothing in `src/bugsigdb_curation/curator/`
imports it. The seam itself is `src/bugsigdb_curation/decision.py` (merged in #20).

> **Read this first — what the numbers can and cannot support.**
> * **Hand labels are agent-drafted, not human-reviewed** (`labels/p1_*.yaml`, headers say so). The 34620922
>   page labels were written *before* running (from page text + the L031 decomposition); the 37864204 labels
>   were cross-checked against the supplementary tables the gold cites (S3/S4/S7/S10), so they are **not**
>   gold-independent. Treat P1/P4 as a smoke until Sean reviews them.
> * **Small n.** P1: 47 pages, 14 sheets, 17 files from two papers. P4: 24 DA units. P2 assignment: 27.
>   Figure benchmark: 15 figures. No confidence intervals were computed; differences of a few points are noise.
> * **Oracle-stub conditions** (gold experiments / groups / taxa as the option set) isolate each judgment from
>   S3/S5b extraction error; production will be harder.
> * **Gold-derived negatives are noisy** (P2 `is_da`): an uncited artifact may still hold DA results.
> * **P5 labels are already-normalized vocabulary** ("Feces"), so it is easier than production S4 free text.
> * 17 of 19 smoke papers have bundles: 19849869 has no PMC full text; 21850056's EuropePMC `fullTextXML`
>   returned HTTP 500 on every retry today. Figure *images* were unavailable for ~half the bundles (PMC blob
>   URL unresolved), so P2 uses legend/caption text; figure type and direction use the 15 manifest images.
> * One run each: Clef is nominally deterministic but run-to-run variance was not measured.

## Verdicts (proposed gates from the issue)

| Decision point | Gate | Result | Verdict |
|---|---|---|---|
| **P1 supplement screening** | R ≥ 0.95 at P ≥ 0.6 on pages/sheets | Pages: R = 1.0, P = 1.0 at the R = 1.0 threshold (both models, image and text); P 0.77–0.91 at τ = 0.5. Sheets: same. File manifests (pre-download): clef R = 1.0 at P = 0.80; clef-flash only P = 0.50. | **GO** for pages and sheets (either model); **GO with clef only** for file-level pre-screening. Deploy at **τ = 0.5** (R = 1.0, P ≥ 0.77 on pages — cheap to over-include). 20/47 pages and 4/14 sheets would reach the strong model. |
| **P2 `is_da`** | beats the regex | AUROC 0.96 / 0.94 vs the regex's single operating point P 0.39 / R 0.65; clef P 0.60 / R 0.90 at 0.5. | **GO as a ranker** (clearly beats S5a's regex). At R = 1.0 precision is still only 0.54, so it orders candidates rather than gating alone. |
| **P2 assignment** | ≥ 0.8 with usable coverage at conf ≥ 0.7 | 27 artifacts over 38–48 options: clef top-1-in-gold-set 0.70 (0.71 on main-text); flash 0.45 (0.71). Confidence never reached 0.7 (probability mass is spread over many options); at conf ≥ 0.1 clef was 10/10 correct (50 % coverage) on supplement pages. | **NO-GO as specified; promising for clef.** Re-test with candidate shortlists (≤ 10 options) and a larger n before deciding. |
| **P2 figure type** | — (informational) | 0.53 / 0.60 on 15 figures; image barely helps (0.47 / 0.40 without). | **NO-GO** — not usable as a router. |
| **P3 orientation** | per-taxon ≥ 0.9 on benchmark figures; flipped LEfSe figure caught | Figures: 0.68 / 0.70. Only LEfSe-style bar charts reach 0.95 / 0.88 (n = 41 taxa, 3 figures). 34620922 table pages: **text 0.51 (chance)**, **image 0.76 (clef) / 0.66 (flash)**; signature-majority 0.83 (clef, image). | **NO-GO** as a general verifier. A *conditional* use survives: clef + page/figure image, accept only at confidence ≥ 0.5 (0.91–0.93 accuracy at 23–35 % coverage). The image is essential (text-only is chance). |
| **P4 arity** | ≥ 0.9 on labelled tables | 4-way arity accuracy 0.25–0.80 (n = 24): fails, because the label scheme conflates "page of many 2-group tables" with "all-pairwise". But the decision the pipeline needs — *does this table need a one-vs-rest decomposition?* — was **3/3 with 0 false positives** on the supplement pages in all four (model × text/image) runs. | **Narrow GO** for one-vs-rest detection (smoke, n = 3 positives); **NO-GO** for 4-way arity. Needs more positives (second big paper) before relying on it. |
| **P5 ontology — body_site** | recall@10 ≥ 0.9 and choice acc ≥ 0.9 | recall@10 0.995; acc given present 0.946 (clef) / 0.976 (flash); experiment-weighted 0.99; at conf ≥ 0.7: 0.99 at 57–69 % coverage. | **GO** (with the easy-vocabulary caveat). |
| **P5 ontology — condition** | same | recall@10 **0.896** (just under the gate); acc 0.861 / 0.874; at conf ≥ 0.7: 0.97–0.98 at 52 % coverage. Misses are mostly gold IDs outside efo/mondo/hp (OBA 0.17, NCBITAXON 0, CHEBI 0.73 recall); many "errors" look like near-equivalent terms in two ontologies (e.g. *Anemia*: gold MONDO, picked HP; not verified term-by-term). | **NO-GO as is**; widen the searched ontologies (oba, chebi, ncbitaxon, go) and score equivalence classes, then re-run. Coverage-at-confidence ≥ 0.7 is already useful. |

**Model axis.** clef-flash (9B, ~2.7× cheaper) matches clef on text/tabular judgments (sheets, pages-as-text,
ontology) and is sometimes better calibrated (Brier), but is clearly worse when an image or a long option list
matters (file screening, direction, assignment). Latency 0.35–1.2 s per call either way.

**Cost.** The *entire* probe — ~1,000 calls on each model — cost about **$0.39 (clef) / $0.15 (clef-flash)** at
list price (input tokens only; output tokens are 0). Per-experiment table below.

**Surprises worth knowing.**
* The Workers AI **pre-flight token estimate scales with encoded image bytes**: a 0.8 MB PNG page was rejected
  as ~274k tokens against a 65k context, while pages re-encoded as ≤ 200 KB JPEGs averaged ~1.6k tokens per call. The probe encodes
  pages as size-capped JPEGs. Any production caller needs the same guard.
* Query *phrasing* matters: in the live smoke, clef-flash scored an obvious DA table at p = 0.29 for a
  `noul` yes/no, yet picked the right option at 0.99 in a `choice`. Prefer `choice` over `noul` where possible.
* The supplementary-files ZIP for 37864204 is 262 MB (two videos) and took ~11 min from EuropePMC; the supplement
  fetcher (#18) should stream/skip large non-text members.

## Metrics

### P1 – supplement screening (`has_da_results`)

| unit | model | n (pos) | AUROC | AUPRC | Brier | P / R @0.5 | R=1.0: τ, P, routed | cost |
|---|---|---|---|---|---|---|---|---|
| supp_files | clef | 17 (4) | 0.92 | 0.68 | 0.243 | 0.50 / 1.00 | 0.913, 0.80, 5/17 | 15,430 tok / 577 ms |
| supp_files | clef-flash | 17 (4) | 0.73 | 0.39 | 0.207 | 0.40 / 0.50 | 0.327, 0.50, 8/17 | 15,430 tok / 822 ms |
| supp_sheets | clef | 14 (4) | 1.00 | 1.00 | 0.036 | 0.80 / 1.00 | 0.984, 1.00, 4/14 | 26,178 tok / 1135 ms |
| supp_sheets | clef-flash | 14 (4) | 1.00 | 1.00 | 0.009 | 1.00 / 1.00 | 0.959, 1.00, 4/14 | 26,178 tok / 499 ms |
| supp_pages | clef | 47 (20) | 1.00 | 1.00 | 0.069 | 0.77 / 1.00 | 0.978, 1.00, 20/47 | 76,836 tok / 759 ms |
| supp_pages | clef-flash | 47 (20) | 1.00 | 1.00 | 0.033 | 0.91 / 1.00 | 0.925, 1.00, 20/47 | 76,836 tok / 557 ms |
| supp_pages_txt | clef | 47 (20) | 1.00 | 1.00 | 0.051 | 0.83 / 1.00 | 0.979, 1.00, 20/47 | 91,004 tok / 603 ms |
| supp_pages_txt | clef-flash | 47 (20) | 1.00 | 1.00 | 0.041 | 0.87 / 1.00 | 0.863, 1.00, 20/47 | 91,004 tok / 396 ms |

### P4 – arity

| unit | model | n DA units | arity acc | multi recall | two_group recall | one-vs-rest TP / gold / FP | n_groups within ½ level |
|---|---|---|---|---|---|---|---|
| supp_sheets | clef | 4 | 0.25 | 0.00 | 1.00 | 0 / 0 / 0 | 0.25 |
| supp_sheets | clef-flash | 4 | 0.50 | 0.33 | 1.00 | 0 / 0 / 0 | 0.25 |
| supp_pages | clef | 20 | 0.55 | 1.00 | 0.47 | 3 / 3 / 0 | 0.55 |
| supp_pages | clef-flash | 20 | 0.65 | 1.00 | 0.59 | 3 / 3 / 0 | 0.50 |
| supp_pages_txt | clef | 20 | 0.50 | 1.00 | 0.41 | 3 / 3 / 0 | 0.50 |
| supp_pages_txt | clef-flash | 20 | 0.80 | 1.00 | 0.76 | 3 / 3 / 0 | 0.55 |

### P2 – DA-artifact detection vs the S5a regex

| model | AUROC | AUPRC | P / R @0.5 | R=1.0: τ, P | regex P / R | cost |
|---|---|---|---|---|---|---|
| clef | 0.96 | 0.79 | 0.60 / 0.90 | 0.248, 0.54 | 0.39 / 0.65 | 56,464 tok / 523 ms |
| clef-flash | 0.94 | 0.69 | 0.57 / 0.80 | 0.148, 0.53 | 0.39 / 0.65 | 56,464 tok / 346 ms |

### P2 – figure type (15 figbench figures)

| model | with image | without image |
|---|---|---|
| clef | 0.53 | 0.47 |
| clef-flash | 0.60 | 0.40 |

### P2 – artifact → experiment assignment (oracle-stub option sets)

| set | model | n | top-1 in gold set | accuracy at conf ≥0.1 (coverage) | options |
|---|---|---|---|---|---|
| main-text artifacts (7) | clef | 7 | 0.71 | – | ~38 |
| main-text artifacts (7) | clef-flash | 7 | 0.71 | – | ~38 |
| 34620922 DA pages | clef | 20 | 0.70 | 1.00 (0.50) | 48 |
| 34620922 DA pages | clef-flash | 20 | 0.45 | 0.44 (0.80) | 48 |

### P3 – direction (per-taxon `increased in group_1?`)

| set | model | taxa | accuracy | AUROC | signature-majority | acc at confidence ≥ 0.5 (coverage) | cost |
|---|---|---|---|---|---|---|---|
| 15 figbench figures (image) | clef | 249 | 0.68 | 0.77 | 0.65 | 0.93 (0.23) | 122,528 tok / 843 ms |
| 15 figbench figures (image) | clef-flash | 249 | 0.70 | 0.77 | 0.72 | 0.87 (0.38) | 122,528 tok / 530 ms |
| same, image withheld (control) | clef | 249 | 0.55 | 0.56 | 0.55 | 0.76 (0.10) | 72,570 tok / 442 ms |
| same, image withheld (control) | clef-flash | 249 | 0.58 | 0.60 | 0.57 | 0.64 (0.23) | 72,570 tok / 328 ms |
| 34620922 table pages, text | clef | 793 | 0.51 | 0.55 | 0.54 | 0.69 (0.16) | 257,412 tok / 893 ms |
| 34620922 table pages, text | clef-flash | 793 | 0.51 | 0.52 | 0.37 | 0.54 (0.24) | 257,412 tok / 468 ms |
| 34620922 table pages, image | clef | 793 | 0.76 | 0.84 | 0.83 | 0.91 (0.35) | 181,106 tok / 1156 ms |
| 34620922 table pages, image | clef-flash | 793 | 0.66 | 0.73 | 0.58 | 0.84 (0.19) | 181,106 tok / 545 ms |

#### P3 by figure type (figbench, with image)

| type | n taxa | clef | clef-flash |
|---|---|---|---|
| box_or_violin | 22 | 0.64 | 0.50 |
| cladogram | 104 | 0.55 | 0.65 |
| heatmap | 58 | 0.71 | 0.71 |
| lefse_lda_bar | 41 | 0.95 | 0.88 |
| other | 13 | 0.54 | 0.69 |
| stacked_bar_composition | 11 | 1.00 | 0.91 |

### P5 – ontology

| field | model | units | retrieval recall@10 | choice acc (gold present) | exp-weighted acc | none-of-these when absent | acc @conf ≥0.7 (coverage) | cost |
|---|---|---|---|---|---|---|---|---|
| body_site | clef | 206 | 0.995 | 0.946 | 0.991 | – | 0.991 (0.57) | 119,106 tok / 472 ms |
| body_site | clef-flash | 206 | 0.995 | 0.976 | 0.993 | – | 0.993 (0.69) | 119,106 tok / 419 ms |
| condition | clef | 910 | 0.896 | 0.861 | 0.862 | 0.20 | 0.974 (0.52) | 512,054 tok / 432 ms |
| condition | clef-flash | 910 | 0.896 | 0.874 | 0.858 | 0.15 | 0.983 (0.52) | 512,054 tok / 450 ms |

### Cost summary (input tokens × list price; output is 0)

| experiment | clef $ | clef-flash $ |
|---|---|---|
| figbench | 0.0294 | 0.0110 |
| figbench_noimage | 0.0174 | 0.0065 |
| supp_files | 0.0037 | 0.0014 |
| supp_sheets | 0.0063 | 0.0024 |
| supp_pages | 0.0184 | 0.0069 |
| supp_pages_txt | 0.0218 | 0.0082 |
| locate | 0.0136 | 0.0051 |
| locate_pages | 0.0194 | 0.0073 |
| direction_pages | 0.0618 | 0.0232 |
| direction_pages_img | 0.0435 | 0.0163 |
| ontology_body_site | 0.0286 | 0.0107 |
| ontology_condition | 0.1229 | 0.0461 |
| **total** | 0.387 | 0.145 |

`R=1.0` columns give the highest threshold that recovers every positive, with the precision it buys and the
number of units routed. P1 `Brier` is the calibration check (lower is better). Costs are input tokens × list
price ($0.24/M clef, $0.09/M clef-flash).

## Method notes and deviations from the issue

* **Units and questions** follow the issue; each experiment is one module in `experiments/`. P1 files are judged
  from `{filename, media_type, label, caption}` only; sheets from the header + first 30 rows; pages from one
  100-dpi JPEG (and, separately, the extracted page text). Pages are read through `bugsigdb_curation.pdf`
  (pypdfium2). The archived runs predate that switch (they used PyMuPDF): a re-run sees slightly different page text
  and JPEG bytes, so expect small shifts in the page experiments.
* **P3 on the L031 failure mode** (`direction_pages*`) is an addition: it asks the per-taxon question on the
  34620922 supplement table pages for the 71 gold signatures that cite a captioned supplementary table, with
  the gold experiment's groups. L031's generative extractor scored 11.5 % direction accuracy on these tables.
  Only the first page of a table is shown; pairwise pages carry several sub-tables.
* **P3 orientation `choice`** (per-figure "what does positive/enriched mean") was asked but is *not* scored
  against gold: gold has no per-figure orientation label. Its answers are stored in each `figbench` summary and
  were unstable across signatures of the same figure.
* **P4 main-text tables** (second half of the issue's P4 unit) were not run: no arity labels were written for the
  main-text DA tables/figures in the bundles (20 DA-cited artifacts), so P4 rests on supplement pages/sheets only.
* **P5 context** is the study *title* (from `studies.csv`), not the abstract. Candidates: OLS4 search
  (`uberon`; `efo,mondo,hp`), top 10, in OLS rank order. Labels with commas / multiple ids (127 body-site
  combos) and the 13 ambiguous body-site labels were excluded.
* **P2 assignment** uses the gold experiments as options (oracle-stub). The gold set for an artifact is every
  experiment citing it, so top-1 is judged against a set (mean size 4.2 on supplement pages).
* **Not done:** Jev (no key); repeats/CIs; the issue's per-decision follow-up issues (filed after review of this
  file); human review of labels.

## Reproduce

```bash
uv run python benchmarks/decision-probe/probe_retrieve.py            # bundles, figures, supplements -> data/decision-probe/
uv run python benchmarks/decision-probe/probe_retrieve.py figbench   # the 15 manifest figures
uv run python benchmarks/decision-probe/run.py <experiment>    # both models; --model clef|clef-flash
uv run python benchmarks/decision-probe/rescore.py             # re-score archived results, no API
uv run python benchmarks/decision-probe/report.py              # print the tables above
```

Experiments: `figbench`, `figbench_noimage`, `supp_files`, `supp_sheets`, `supp_pages`, `supp_pages_txt`,
`locate`, `locate_pages`, `direction_pages`, `direction_pages_img`, `ontology_body_site`, `ontology_condition`.
Needs `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN` in `.env`. Raw API archives
(`runs/<date>_<model>/<experiment>.jsonl`) are gitignored; `*.results.json` (per-item answers) and
`*.summary.json` are tracked.
