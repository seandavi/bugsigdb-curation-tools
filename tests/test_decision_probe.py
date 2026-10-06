"""Offline tests for the decision-probe's pure helpers (metrics, source parsing, scoring).

No network, no data/ dependency: the probe's experiments need cached bundles, but their scoring
functions and parsers are pure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[1] / "benchmarks" / "decision-probe"
sys.path.insert(0, str(PROBE))

import common  # noqa: E402
from experiments import _screen, figbench, locate  # noqa: E402

from bugsigdb_curation.decision import MockDecisionModel, NoulAnswer  # noqa: E402


def test_auroc_perfect_inverted_and_ties():
    assert common.auroc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert common.auroc([0, 0, 1, 1], [0.9, 0.8, 0.2, 0.1]) == 0.0
    assert common.auroc([0, 1], [0.5, 0.5]) == 0.5
    assert common.auroc([1, 1], [0.2, 0.9]) is None


def test_auprc_and_brier():
    assert common.auprc([0, 1, 1], [0.1, 0.9, 0.8]) == 1.0
    assert common.auprc([0, 0], [0.1, 0.2]) is None
    assert common.brier([1, 0], [1.0, 0.0]) == 0.0
    assert common.brier([1, 0], [0.0, 1.0]) == 1.0


def test_threshold_for_recall_picks_highest_threshold_meeting_target():
    y = [1, 1, 0, 1, 0]
    p = [0.9, 0.8, 0.7, 0.6, 0.1]
    at = common.threshold_for_recall(y, p, 1.0)
    assert at and at["tau"] == 0.6 and at["recall"] == 1.0 and at["precision"] == 0.75
    assert common.threshold_for_recall(y, p, 0.6)["tau"] == 0.8


def test_coverage_at_confidence_accuracy_and_coverage():
    rows = common.coverage_at_confidence([True, False, True, True], [0.9, 0.2, 0.8, 0.4], (0.0, 0.5))
    assert rows[0] == {"tau": 0.0, "coverage": 1.0, "accuracy": 0.75, "n": 4}
    assert rows[1]["coverage"] == 0.5 and rows[1]["accuracy"] == 1.0


@pytest.mark.parametrize(
    "source, expected",
    [
        ("Figure 2", {("figure", "2")}),
        ("Fig. 1C", {("figure", "1")}),
        ("Table 2, Table 1", {("table", "2"), ("table", "1")}),
        ("Supplementary Table S7 and Figure 3", {("figure", "3")}),
        ("Table S4, Fig 3, Supplementary Table S7", {("figure", "3")}),
        ("Supplementary Table 10", set()),
        ("Table 1& S3", {("table", "1")}),
        (None, set()),
    ],
)
def test_cited_artifacts_ignores_supplementary_citations(source, expected):
    assert locate.cited_artifacts(source) == expected


def test_figbench_clean_taxon_and_chunking():
    assert figbench.clean_taxon("s__Gardnerella_vaginalis") == "Gardnerella vaginalis"
    sig = {"group_0_name": "a", "group_1_name": "b", "direction": "decreased", "taxa": [{"name": f"g__T{i}"} for i in range(130)]}
    chunks = figbench.taxon_questions(sig)
    assert [len(qs) for qs, _ in chunks] == [60, 60, 10]
    assert all(v is False for _, truth in chunks for v in truth.values())


def _unit(uid, da, kind, arity=None, n=None):
    label = {"has_da_results": da, "content_kind": kind}
    if arity:
        label |= {"arity": arity, "n_groups": n}
    return {"id": uid, "state": {"x": uid}, "images": [], "label": label}


def test_screen_runs_through_the_seam_and_scores():
    from bugsigdb_curation.decision import ChoiceAnswer, ScoreAnswer

    def answers(state, questions):
        da = state["x"].startswith("da")
        return {
            "has_da_results": NoulAnswer(0.9 if da else 0.1),
            "content_kind": ChoiceAnswer("da_taxa_table" if da else "other", {"da_taxa_table": 0.5, "other": 0.5}, 0.8),
            "arity": ChoiceAnswer("multi_group_one_vs_rest" if state["x"] == "da1" else "two_group", {"two_group": 1.0}, 0.9),
            "n_groups": ScoreAnswer(1.0, {"0": 0.5, "1": 0.5}, 0.5),
        }

    units = [
        _unit("da1", True, "da_taxa_table", "multi_group_one_vs_rest", 3),
        _unit("da2", True, "da_taxa_table", "two_group", 2),
        _unit("no1", False, "other"),
        _unit("no2", False, "other"),
    ]
    model = MockDecisionModel({"s:da1": answers, "s:da2": answers, "s:no1": answers, "s:no2": answers})
    results = asyncio.run(_screen.screen(model, units, stage="s"))
    summary = _screen.score(results)
    assert summary["has_da_results"]["auroc"] == 1.0
    assert summary["content_kind"]["accuracy"] == 1.0
    assert summary["arity"]["accuracy"] == 1.0
    assert summary["arity"]["one_vs_rest_detection"] == {"gold_positive": 1, "true_positive": 1, "false_positive": 0}
