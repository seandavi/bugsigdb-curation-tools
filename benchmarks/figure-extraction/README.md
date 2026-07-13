# Figure-extraction benchmark (stage 1: eval set)

A curated, figure-type-stratified benchmark for evaluating **vision-based
extraction of microbial signatures from paper figures** — i.e. given a figure
image + its legend, can a model recover the taxa (as NCBI Taxonomy IDs +
names) and their direction of change that a human BugSigDB curator extracted
from that same figure?

This is stage 1: the eval set + retrieval tooling. A later stage runs
extraction (a vision model reading the figure) and scores it against
`manifest.json`'s `gold` field.

## Contents

| Path | Contents |
|------|----------|
| `manifest.json` | 15 entries: one per benchmark figure, with legend, gold taxa/directions, and a stable URL to re-fetch the image. |
| `retrieve.py` | Reusable retrieval logic (pure parsers + thin network functions) — see below. |
| `README.md` | This file. |

Images themselves are **not** committed (`data/` is gitignored). Re-fetch them
with `retrieve.py`'s functions against the `blob_url` in each manifest entry,
or re-run the retrieval recipe from `figure_label_source` if a blob URL goes
stale.

## The gold set

12–15 was the target; **15 studies** made it in, spanning **6 figure-type
buckets** (5 requested + a 6th, `other`, for STAMP-style plots that don't fit
elsewhere):

| figure_type | count | notes |
|---|---|---|
| `lefse_bar_LDA` | 3 | canonical LEfSe/LDA-score ranked bar charts |
| `cladogram` | 3 | circular LEfSe cladograms (2 paired with an LDA bar in the same figure) |
| `heatmap` | 3 | 1 abundance heatmap, 1 fold-change heatmap, 1 dendrogram+heatmap |
| `box_or_violin_with_stats` | 3 | box/violin or box-like (median+IQR) plots per taxon |
| `stacked_bar_composition` | 2 | per-sample/per-group relative-abundance stacked bars |
| `other` | 1 | STAMP-style mean-proportion + 95% CI dot plot |

Every entry's gold taxa were checked against the actual figure image (via the
`Read`/vision tool) before inclusion — see "Rejected candidates" below for
cases that didn't survive this check.

### Figure-type taxonomy (used for classification)

- **`stacked_bar_composition`** — per-sample or per-group 100%-stacked bar
  chart of relative abundance, colored by taxon.
- **`box_or_violin_with_stats`** — box plot, violin plot, or box-like
  median/IQR bar (optionally with jittered points and/or significance
  brackets), one panel or group of bars per taxon.
- **`lefse_bar_LDA`** — horizontal (occasionally vertical) bar chart ranking
  taxa by an effect-size statistic (LEfSe's LDA score, but also ALDEx2 effect
  size, DESeq2/ANCOM-BC log-fold-change, etc.) with bars colored/signed by
  which group is enriched. These are visually near-identical regardless of
  the underlying stats method, so they're grouped together for extraction
  purposes.
- **`cladogram`** — circular/radial taxonomic tree (typically LEfSe's
  `cladogram.py` output) with colored highlighted clades/nodes indicating
  differential abundance.
- **`heatmap`** — a taxon × sample or taxon × group color-coded matrix
  (abundance, z-score, or fold-change), with or without a dendrogram.
- **`volcano_or_ma`** — scatter plot with one axis a fold-change/effect
  size and the other a significance measure (p/q-value), one point per
  taxon. (No example in this set — see "Notes for stage 2" below.)
- **`other`** — anything not fitting cleanly above, e.g. STAMP's
  mean-proportion + 95%-CI dot/bar hybrid plots, or ordination plots (PCoA/
  NMDS/PCA) used as the sole cited figure.

## The retrieval recipe

For a BugSigDB study with PMCID `PMCxxxxx` and a gold figure reference like
`"Figure 2"` or `"figure 3b"`:

1. **Legend + figure filename.** GET the EuropePMC fullTextXML:
   `https://www.ebi.ac.uk/europepmc/webservices/rest/PMCxxxxx/fullTextXML`.
   Parse `<fig>` elements — each has a `<label>` ("Figure 2."), a `<caption>`
   (the legend, tags stripped), and a `<graphic xlink:href="...">` naming the
   image file. `<graphic>` is sometimes a direct child of `<fig>` and
   sometimes nested inside an `<alternatives>` wrapper, so search all
   descendants, not just direct children. Match the BugSigDB `source` cell's
   figure *number* (ignoring panel letters — "figure 3b" → "3") to the `<fig>`
   with that number.
2. **Image URL.** GET the article HTML,
   `https://pmc.ncbi.nlm.nih.gov/articles/PMCxxxxx/`, with a normal desktop
   browser `User-Agent` (required — PMC's front end otherwise serves a
   different/blocked response). Extract all
   `https://cdn.ncbi.nlm.nih.gov/pmc/blobs/.../<filename>.(jpg|png|gif)` URLs
   and match by filename (suffix match, falling back to stem-only if the
   extension differs — the EuropePMC XML and PMC's HTML occasionally
   disagree, e.g. one source's fullTextXML filename scheme not existing
   verbatim as a CDN blob — see "Retrieval failures" below).
3. **Image bytes.** GET the matched blob URL (same browser `User-Agent`).
4. **OA check.** GET
   `https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=PMCxxxxx` and require
   a `<record license="...">` (not an `<error code="idIsNotOpenAccess">`).
   Only Open Access articles are included; the exact license (`CC BY`,
   `CC BY-NC`, `CC BY-NC-ND`, ...) is recorded per manifest entry — most are
   `CC BY`, a few are `CC BY-NC`/`CC BY-NC-ND` (noted in the table above via
   `manifest.json`'s `license` field; treat non-`CC BY` entries as
   research/internal-eval use only, not further redistribution).

**Important operational note:** concurrent requests to the PMC article-HTML
endpoint are unreliable in this environment — under concurrency ≥2 a
nontrivial fraction of requests silently returned pages with zero matching
CDN blob URLs (not an HTTP error, just an apparently-incomplete/different
response body), while the *same* PMCID fetched sequentially (or on retry)
consistently succeeded. `retrieve.py`'s network functions are single-request
and unopinionated about concurrency; any driver script built on top of them
should serialize the article-HTML fetch (e.g. `asyncio.Semaphore(1)` with a
short delay) rather than parallelizing it, and should retry a `no matching
blob URL` result once before treating it as a real failure.

## `retrieve.py`

Pure parsing/matching functions (no network) — unit tested with tiny inline
XML/HTML fixtures in `tests/test_figbench_retrieve.py`:

- `parse_fulltext_figures(xml_text) -> list[FigureEntry]`
- `normalize_source_label(source) -> str | None` — BugSigDB `source` cell → figure number
- `match_figure(source, figures) -> FigureEntry | None`
- `extract_blob_urls(html_text) -> list[str]`
- `match_filename_to_blob(filename, blob_urls) -> str | None`
- `parse_oa_response(xml_text) -> str | None`
- `build_image_path(images_dir, pmcid, figure_number, filename) -> Path`

Thin network functions (one HTTP call each, `httpx.AsyncClient`-based):
`fetch_fulltext_xml`, `fetch_article_html`, `fetch_oa_status`,
`fetch_image_bytes`.

Run the pure-parser tests: `uv run pytest tests/test_figbench_retrieve.py`.
Run the one opt-in end-to-end network test:
`uv run pytest -m network tests/test_figbench_retrieve.py`.

## `manifest.json` structure

A JSON list; each entry:

```jsonc
{
  "study_id": "...",              // BugSigDB study id (usually the PMID)
  "pmid": "...",
  "pmcid": "PMCxxxxx",
  "license": "CC BY",             // from the OA check
  "study_title": "...",
  "figure_type": "lefse_bar_LDA", // one of the taxonomy buckets above
  "figure_label_source": "...",   // verbatim BugSigDB `source` cell, e.g. "Figure 2A"
  "figure_label_matched": "...",  // matched <fig><label> from fullTextXML, e.g. "Figure 2."
  "figure_filename": "...",       // graphic filename from fullTextXML
  "blob_url": "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/...",
  "image_path": "data/figbench/images/PMCxxxxx_F<n>.jpg",  // local path once re-fetched
  "legend": "...",                // verbatim figure caption (CC-BY-licensed source text — see license field)
  "group_0_name": "...",          // Group 0 (control/reference) name, from the *first* gold signature
  "group_1_name": "...",          // Group 1 (case) name, from the *first* gold signature
  "gold": [
    {
      "signature_id": "bsdb:.../.../...",
      "direction": "increased" | "decreased",  // in Group 1, relative to Group 0
      "group_0_name": "...",      // per-signature — studies with >1 experiment mapped to
      "group_1_name": "...",      //   this figure have different group names per gold entry
      "taxa": [ { "ncbi_id": "...", "name": "..." }, ... ]
    },
    ...
  ]
}
```

Some studies contributed multiple BugSigDB signatures/experiments to the
*same* figure (e.g. `study_id 33804656` has 8 gold signatures across 4
experiments, all sourced from Figure 5's four cladogram panels) — the
top-level `group_0_name`/`group_1_name` reflect only the first gold entry;
downstream code that needs per-comparison group names should read them off
each `gold[i]`, not the top-level fields.

## Candidate pool and downselection

Starting pool: joined `signatures.csv` → `experiments.csv` → `studies.csv` →
`data/eval/pmid_pmcid_map.csv`, keeping studies where (a) ≥80% of a study's
signatures have a `source` starting with "figure", (b) `has_pmc == true`, and
(c) the study's figure-sourced gold-taxa count is in [4, 40] — **515
candidates**. Restricting further to studies whose figure-sourced signatures
all cite a *single* figure number (no multi-figure/table/text combos) gave
**174 clean candidates**, spread from 2013 to 2026 by PMCID. A stratified
sample of 42 was drawn across that range and run through the retrieval
recipe; **36 retrieved successfully** (OA + fig-label match + image
download). Each of the 36 images was viewed and classified by figure type,
with gold taxa cross-checked against what's actually visible in the image;
**15** were kept for the final set (see distribution above).

### Rejected candidates

- **PMID 27912057, 30153231, 32010563, 35017199, 38294805** — failed the OA
  check (`idIsNotOpenAccess`).
- **PMID 26151645 (PMC4681870)** — fig-label matched and the article HTML had
  plenty of CDN blob URLs, but none matched the fullTextXML's graphic
  filename (EuropePMC's filename, e.g.
  `41396_2016_Article_BFismej201599_Fig2_HTML.jpg`, doesn't correspond to any
  PMC-hosted blob — PMC's actual files use a different, shorter naming
  scheme, e.g. `ismej201599f2.jpg`, for this particular publisher/journal).
  Not pursued further since equally good alternatives existed.
- **PMID 28449715 (PMC5408370), Figure 1C** — retrieved fine, but panel C's
  bar chart has no legible taxon-name axis labels at the resolution the
  figure was published at, so the gold taxa can't be visually confirmed
  against the image. Excluded for gold-mismatch risk.
- **PMID 32490229 (PMC7262409), Figure 4a** — retrieved fine (clean LEfSe
  bar), but several taxon labels are visibly truncated in the source image
  (e.g. bare `g_`/`f_` prefixes with no name following), and its license is
  the more restrictive `CC BY-NC-ND`. Not selected in favor of cleaner,
  more-open alternatives.
- 20 further successfully-retrieved candidates were simply not needed once
  15 with good type coverage were selected; none were excluded for quality
  reasons (they're reasonable alternates/replacements if any selected entry
  turns out to be problematic in stage 2).

## Notes for stage 2 (extraction/scoring)

- **No `volcano_or_ma` example** made it into the final 15 — none of the 36
  successfully retrieved figures was a genuine fold-change-vs-significance
  scatter plot (the closest analogues, DESeq2/ANCOM-BC log-fold-change *bar*
  charts, were bucketed under `lefse_bar_LDA`/`other` instead, since they
  have no second/significance axis). If a true volcano/MA-plot example is
  wanted, it'll need a fresh retrieval pass — none of the rejected/spare
  candidates above is one either.
- **Multi-panel figures are the norm, not the exception.** 13 of the 15
  entries are multi-panel figures (`source_label` like "Figure 2C" or
  "Figure 4c") where the BugSigDB curator's cited panel is only one part of
  a larger image; the *whole* figure image is what's stored (PMC/EuropePMC
  don't expose sub-panel crops), so a stage-2 extractor will see — and must
  correctly ignore — sibling panels (PCoA plots, alpha-diversity boxplots,
  correlation heatmaps unrelated to the gold taxa, etc.) that aren't the
  cited one. `figure_label_source` tells you which panel is actually gold.
- **Taxa are given as NCBI IDs + names already** (`gold[].taxa[].ncbi_id`/
  `name`) — no name→ID normalization needed downstream; BugSigDB's own
  curation already did that mapping. Some `ncbi_id` values are for
  higher-rank clades (e.g. phylum/family-level placeholders like
  `k__Bacillati`), not just species/strain — worth checking rank when
  scoring.
- **Direction isn't always symmetric two-way.** A few entries (e.g.
  `study_id 34611216`/PMC8492659) have only one BugSigDB signature (all
  taxa "decreased" in Group 1) even though the figure shows all bars
  pointing one way — there's no "increased" counterpart to score against
  for that figure. Don't assume every entry has both directions represented.
- **Multiple experiments per figure.** `study_id 33804656` (8 gold
  signatures / 4 experiments, all citing Figure 5's four panels) and
  `study_id 30675188` (4 gold signatures / 3 experiments, all citing Figure
  3's three panels) require matching *which panel* each gold signature came
  from — the panel-to-experiment mapping isn't explicit in `gold[]` and may
  need to be inferred from the legend text (which describes each panel) when
  scoring per-panel accuracy rather than whole-figure accuracy.
- **License mix.** 13/15 entries are `CC BY`; 2 (`study_id 32753953`,
  `study_id 35387878`) are `CC BY-NC`. Fine for an internal eval set; flag if
  this benchmark or its images are ever redistributed externally.
