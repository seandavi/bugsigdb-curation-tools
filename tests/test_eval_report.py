"""Unit tests for bugsigdb_curation.eval.report -- JSONL/markdown/HTML reports."""

from __future__ import annotations

import json

from bugsigdb_curation.eval.gold import GoldExperiment, GoldSignature, GoldStudy, source_type, to_nested_dict
from bugsigdb_curation.eval.report import render_html, render_markdown, write_jsonl, write_reports
from bugsigdb_curation.eval.score import aggregate_scores, score_study
from bugsigdb_curation.eval.taxonomy import TaxonomyResolver


def _sample_scores():
    signature = GoldSignature(
        signature_id="s1",
        experiment_id="1/Experiment 1",
        source="table 1",
        source_type=source_type("table 1"),
        direction="increased",
        taxa=frozenset({1, 2}),
        curation_state="Complete",
    )
    experiment = GoldExperiment(
        experiment_id="1/Experiment 1",
        study_id="1",
        experiment_name="Experiment 1",
        location_of_subjects=(),
        host_species=None,
        body_site=("Feces",),
        uberon_id=None,
        condition=("CRC",),
        efo_id=None,
        group_0_name="healthy",
        group_1_name="CRC",
        group_1_definition=None,
        group_0_sample_size=None,
        group_1_sample_size=None,
        sequencing_type="16S",
        statistical_test=(),
        mht_correction=None,
        signatures=(signature,),
    )
    gold = GoldStudy(
        study_id="1",
        pmid="1",
        doi=None,
        title=None,
        journal=None,
        year=None,
        study_design=(),
        pmcid=None,
        has_pmc=True,
        experiments=(experiment,),
    )
    pred = to_nested_dict(gold)
    resolver = TaxonomyResolver()
    scores = [score_study(gold, pred, resolver)]
    return scores, aggregate_scores(scores)


def test_write_jsonl_one_line_per_study(tmp_path):
    scores, _ = _sample_scores()
    path = write_jsonl(scores, tmp_path / "out" / "scores.jsonl")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["study_id"] == "1"
    assert "micro_taxa" in record
    assert record["micro_taxa"]["f1"] == 1.0


def test_render_markdown_contains_expected_sections():
    scores, aggregate = _sample_scores()
    md = render_markdown(scores, aggregate)

    assert "# BugSigDB Eval Harness Report" in md
    assert "## By source type" in md
    assert "## By experiment-count bucket" in md
    assert "## Per-study" in md
    assert "| 1 |" in md  # the study_id row
    assert "main-table" in md


def test_render_markdown_nonempty_and_deterministic():
    scores, aggregate = _sample_scores()
    md1 = render_markdown(scores, aggregate)
    md2 = render_markdown(scores, aggregate)
    assert md1 == md2
    assert len(md1) > 100


def test_render_html_is_self_contained_and_has_expected_sections():
    scores, aggregate = _sample_scores()
    html = render_html(scores, aggregate)

    assert html.strip().startswith("<!doctype html>")
    assert "<style>" in html  # inline CSS, no external stylesheet link
    assert "http://" not in html and "https://" not in html  # no external assets
    assert "By source type" in html
    assert "By experiment-count bucket" in html
    assert "Per-study" in html
    assert "<td>1</td>" in html  # the study_id cell


def test_write_reports_writes_all_three_files(tmp_path):
    scores, aggregate = _sample_scores()
    paths = write_reports(scores, aggregate, tmp_path / "reports")

    assert paths["jsonl"].exists()
    assert paths["md"].exists()
    assert paths["html"].exists()
    assert paths["jsonl"].stat().st_size > 0
    assert paths["md"].stat().st_size > 0
    assert paths["html"].stat().st_size > 0
