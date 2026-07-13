"""The eval harness's curated smoke set: ~20 studies spanning source types,
`has_pmc` status, and experiment-count scale, for fast iteration without
scoring the full ~2,068-study gold corpus.

Anchors (plan §4a), verified to exist in the real gold export:

* ``19849869`` -- abstract-only (``has_pmc=false``), the single-taxon happy path.
* ``21850056`` -- main-table sourced, a single experiment (the worked example
  in the task brief: 1 experiment, 2 signatures, source ``"table 2"``).
* ``34620922`` -- supplement-heavy AND a "many comparisons" anchor: 48
  experiments in the corpus, most signatures sourced from supplementary tables.
* ``37864204`` -- the experiment-count tail with real content: 64
  experiments, all 64 with a curated taxa set (unlike the corpus's single
  largest study, PMID 34963452 with 200 experiments, which turned out on
  inspection to be a curation stub -- identical body_site/condition across
  all 200 experiments and zero taxa in any of them, so nothing in the gold
  itself can disambiguate its experiments; deliberately not used as an
  anchor since a smoke-set study should exercise real signal).

Plus the 15 figure-typed PMIDs from
`benchmarks/figure-extraction/manifest.json`'s ``pmid`` field (figures are the
largest signature-source bucket corpus-wide, ~57% per the plan's constraint
2), reused verbatim so the figure-extraction benchmark and this eval harness
score the same studies. Kept as a static constant here (rather than reading
the manifest at import time) so this module has no filesystem dependency;
`tests/test_eval_smoke.py` cross-checks it against the live manifest to guard
against drift.
"""

from __future__ import annotations

from bugsigdb_curation.eval.gold import GoldStudy

ANCHOR_STUDY_IDS: tuple[str, ...] = (
    "19849869",  # abstract-only, has_pmc=false
    "21850056",  # main-table, single experiment
    "34620922",  # supplement-heavy, 48 experiments
    "37864204",  # experiment-count tail with real content, 64 experiments
)

#: The 15 PMIDs from `benchmarks/figure-extraction/manifest.json`.
FIGBENCH_STUDY_IDS: tuple[str, ...] = (
    "30854760",
    "32552447",
    "31215600",
    "33167991",
    "34090383",
    "24192039",
    "32753953",
    "38387693",
    "29459704",
    "33804656",
    "41670382",
    "35233023",
    "35387878",
    "40943094",
    "30675188",
)


def smoke_study_ids() -> list[str]:
    """The full smoke set: anchors + figbench studies, de-duplicated, order preserved."""
    seen: set[str] = set()
    ids: list[str] = []
    for study_id in (*ANCHOR_STUDY_IDS, *FIGBENCH_STUDY_IDS):
        if study_id not in seen:
            seen.add(study_id)
            ids.append(study_id)
    return ids


def select_smoke(gold: dict[str, GoldStudy]) -> dict[str, GoldStudy]:
    """Filter a full `study_id -> GoldStudy` gold dict down to the smoke set.

    Silently drops any smoke id absent from `gold` (e.g. a partial/test
    relational export) rather than raising -- `missing_smoke_ids` is
    available to check for that explicitly.
    """
    ids = smoke_study_ids()
    return {study_id: gold[study_id] for study_id in ids if study_id in gold}


def missing_smoke_ids(gold: dict[str, GoldStudy]) -> list[str]:
    """Smoke-set study ids absent from `gold` (empty for the real corpus)."""
    return [study_id for study_id in smoke_study_ids() if study_id not in gold]
