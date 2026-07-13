"""The curator's own copy of the eval harness's smoke-set study IDs.

Duplicated, deliberately NOT imported, from `bugsigdb_curation.eval.smoke`:
importing `bugsigdb_curation.eval` for any reason is forbidden for a curator
module by the data firewall (§6e), even though `eval.smoke` holds nothing
but a bare list of PMIDs (a work list, not gold content) -- see the plan's
firewall carve-out ("Smoke/dev/test selection uses study IDs only ... never
implies reading their gold") and `tests/test_curator_firewall.py`'s guard
test, which would fail on any `from bugsigdb_curation import eval`-shaped
import here. Keep this list in sync with
`bugsigdb_curation.eval.smoke.smoke_study_ids()` by hand if that module's
anchors/figbench IDs ever change; `tests/test_eval_smoke.py` already guards
the eval side against drift from `benchmarks/figure-extraction/manifest.json`.
"""

from __future__ import annotations

#: Anchors (see `eval.smoke`'s docstring for what each covers): an
#: abstract-only single-taxon happy path, a main-table single-experiment
#: study, a supplement-heavy 48-experiment study, and a 64-experiment tail
#: study.
ANCHOR_STUDY_IDS: tuple[str, ...] = (
    "19849869",
    "21850056",
    "34620922",
    "37864204",
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
