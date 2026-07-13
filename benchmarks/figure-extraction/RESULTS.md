# Figure-extraction benchmark — results (round 1)

**Question:** can a vision model recover the microbial signature (taxa + direction)
that BugSigDB curators extracted from a paper **figure**, given the figure image +
its legend?

**Set:** 15 real BugSigDB studies whose signatures were figure-sourced, spanning 6
figure types (see `manifest.json` / `README.md`). Gold = the taxa (NCBI IDs) and
directions BugSigDB curated. Condition tested: **figure image + legend** (the
realistic pipeline). Extraction was **blind** — five independent sonnet vision
agents saw only `blind_inputs.json` (image path + legend + group names), never the
gold. Scored by `score.py` (`uv run python benchmarks/figure-extraction/score.py`).

Direction is expressed relative to Group 1 (case/exposed): "increased" = higher in
Group 1. Taxa scored two ways: **strict** (exact normalized name) and **genus**
(collapse to genus token). Direction accuracy is over strictly-matched taxa.

## Headline: by figure type

| figure type | n | strict F1 | genus F1 | direction acc |
|---|---:|---:|---:|---:|
| stacked_bar_composition | 2 | 0.91 | 0.91 | 7/7 (100%) |
| lefse_bar_LDA | 3 | 0.75 | 0.80 | 18/33 (55%)\* |
| heatmap | 3 | 0.68 | 0.67 | 38/43 (88%) |
| box_or_violin_with_stats | 3 | 0.62 | 0.71 | 11/11 (100%) |
| cladogram | 3 | 0.59 | 0.57 | 46/49 (94%) |
| other (STAMP) | 1 | 0.43 | 0.71 | 3/3 (100%) |

\* The lefse direction number is dragged down entirely by one figure (PMC7342497),
which flipped **every** direction — see finding 2.

Per-figure numbers are in `results.json`.

## Findings

1. **Retrieval is fully solved and scriptable.** Figure image + legend for OA
   articles come from: EuropePMC REST `fullTextXML` (legends + figure filenames) +
   the PMC article HTML (`cdn.ncbi.nlm.nih.gov/pmc/blobs/…` image URLs) + a plain
   GET of the blob. No browser, no NCBI FTP, no login. (`retrieve.py`.)

2. **Taxa recall degrades predictably with figure complexity.** Clean labeled
   charts (stacked bar, well-drawn LEfSe LDA bars) reach ~0.8–1.0 F1; dense
   cladograms, small multi-panel box plots, and gradient heatmaps land ~0.5–0.7.
   The dominant error is **recall** (missing small/compressed labels), not
   fabrication — precision is generally ≥ recall.

3. **Direction/orientation is a distinct, sometimes catastrophic failure mode.**
   Most figures get direction right (88–100%) once taxa are found, BUT one LEfSe
   figure (PMC7342497) identified all 15 taxa perfectly and inverted **all 15
   directions** — a systematic group→color mapping flip. Direction correctness is
   not implied by taxa correctness and must be evaluated (and defended) separately.

4. **Legends reliably locate the differential-abundance panel.** Every extractor
   used the legend to pick the right panel in these multi-panel figures (13/15 are
   multi-panel). This supports the "find the figure/panel via legend" hypothesis —
   no process-of-elimination needed.

5. **Nomenclature drift is the next accuracy ceiling, not model quality.** Gold uses
   current NCBI names; figures use whatever the authors drew (Actinobacteria vs
   Actinomycetota; Propionibacterium vs Cutibacterium). String matching misses
   these. A real pipeline needs NCBI synonym/lineage resolution — which is also
   required for the schema (taxa must be NCBI IDs) regardless.

6. **Measurement caveat (fixed during this round):** the first scoring pass gave a
   cladogram figure 0.00 purely because the scorer only stripped double-underscore
   rank prefixes (`c__`) while LEfSe labels use single underscore (`c_`). Fixed and
   unit-tested (`tests/test_figbench_score.py`). Lesson: normalization bugs can
   masquerade as model failures; the scorer needs tests as much as the extractor.

## Limitations / for round 2

- n=15 (medium), 1–3 figures/type; treat numbers as directional, not precise.
- Only the **figure+legend** condition was run. Ablations (figure-only,
  legend-only, legend→figure selection) and prompt/model variants are hooks left
  for later (the blind-input harness supports them).
- Scoring is string-based; add NCBI synonym resolution for a fairer taxa score and
  to quantify how much of the gap is nomenclature vs genuine miss.
- Directionality deserves a dedicated adversarial verifier in the real pipeline
  (finding 3).
