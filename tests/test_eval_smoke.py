"""Unit tests for bugsigdb_curation.eval.smoke -- the curated smoke set."""

from __future__ import annotations

import json
from pathlib import Path

from bugsigdb_curation.eval.gold import GoldStudy
from bugsigdb_curation.eval.smoke import (
    ANCHOR_STUDY_IDS,
    FIGBENCH_STUDY_IDS,
    missing_smoke_ids,
    select_smoke,
    smoke_study_ids,
)

_MANIFEST_PATH = Path(__file__).parent.parent / "benchmarks" / "figure-extraction" / "manifest.json"


def _stub_gold_study(study_id: str) -> GoldStudy:
    return GoldStudy(
        study_id=study_id,
        pmid=study_id,
        doi=None,
        title=None,
        journal=None,
        year=None,
        study_design=(),
        pmcid=None,
        has_pmc=True,
        experiments=(),
    )


def test_smoke_study_ids_deduplicated_and_ordered():
    ids = smoke_study_ids()
    assert len(ids) == len(set(ids))
    assert ids[: len(ANCHOR_STUDY_IDS)] == list(ANCHOR_STUDY_IDS)


def test_smoke_study_ids_includes_all_anchors_and_figbench_ids():
    ids = set(smoke_study_ids())
    assert set(ANCHOR_STUDY_IDS) <= ids
    assert set(FIGBENCH_STUDY_IDS) <= ids


def test_smoke_set_size_is_about_twenty():
    assert 15 <= len(smoke_study_ids()) <= 25


def test_select_smoke_filters_and_drops_missing():
    gold = {sid: _stub_gold_study(sid) for sid in ANCHOR_STUDY_IDS[:2]}
    selected = select_smoke(gold)
    assert set(selected) == set(ANCHOR_STUDY_IDS[:2])


def test_missing_smoke_ids_reports_absent_ids():
    gold = {ANCHOR_STUDY_IDS[0]: _stub_gold_study(ANCHOR_STUDY_IDS[0])}
    missing = missing_smoke_ids(gold)
    assert ANCHOR_STUDY_IDS[0] not in missing
    assert ANCHOR_STUDY_IDS[1] in missing


def test_figbench_ids_match_live_manifest_pmids():
    # Guards drift between the hardcoded FIGBENCH_STUDY_IDS constant and the
    # actual benchmarks/figure-extraction/manifest.json it was sourced from.
    if not _MANIFEST_PATH.exists():
        return  # manifest not present in this checkout; nothing to cross-check
    manifest = json.loads(_MANIFEST_PATH.read_text())
    manifest_pmids = {entry["pmid"] for entry in manifest}
    assert manifest_pmids == set(FIGBENCH_STUDY_IDS)


def test_anchors_are_not_figbench_ids():
    assert set(ANCHOR_STUDY_IDS).isdisjoint(FIGBENCH_STUDY_IDS)
