"""End-to-end tests for the `bugsigdb eval score` / `bugsigdb eval gold` CLI commands."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from bugsigdb_curation.cli import app

runner = CliRunner()

STUDY_FIELDS = [
    "study_id", "pmid", "doi", "url", "authors", "title", "journal", "year", "keywords",
    "study_design", "curator", "curated_date", "revision_editor", "state", "reviewer",
]
EXPERIMENT_FIELDS = [
    "experiment_id", "study_id", "experiment_name", "location_of_subjects", "host_species",
    "body_site", "uberon_id", "condition", "efo_id", "group_0_name", "group_1_name",
    "group_1_definition", "group_0_sample_size", "group_1_sample_size", "antibiotics_exclusion",
    "sequencing_type", "variable_region", "sequencing_platform", "data_transformation",
    "statistical_test", "significance_threshold", "mht_correction", "lda_score_above",
    "matched_on", "confounders_controlled_for", "pielou", "shannon", "chao1", "simpson",
    "inverse_simpson", "richness",
]
SIGNATURE_FIELDS = [
    "signature_id", "experiment_id", "signature_name", "source", "description",
    "abundance_in_group_1", "curation_state", "curator", "curated_date", "revision_editor",
    "reviewer",
]
TAXA_FIELDS = ["ncbi_id", "taxon_name"]
SIGNATURES_TAXA_FIELDS = ["signature_id", "ncbi_id"]
PMC_MAP_FIELDS = ["study_id", "pmid", "pmcid", "doi", "has_pmc"]


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def _build_gold_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Two tiny studies: one match target (21850056-style), one abstract-only."""
    relational_dir = tmp_path / "relational"
    relational_dir.mkdir()

    _write_csv(
        relational_dir / "studies.csv",
        STUDY_FIELDS,
        [
            {"study_id": "111", "pmid": "111", "title": "Study One", "study_design": "case-control"},
            {"study_id": "222", "pmid": "222", "title": "Study Two (abstract only)"},
        ],
    )
    _write_csv(
        relational_dir / "experiments.csv",
        EXPERIMENT_FIELDS,
        [
            {
                "experiment_id": "111/Experiment 1",
                "study_id": "111",
                "experiment_name": "Experiment 1",
                "body_site": "Feces",
                "condition": "CRC",
                "group_0_name": "healthy",
                "group_1_name": "CRC",
                "sequencing_type": "16S",
            }
        ],
    )
    _write_csv(
        relational_dir / "signatures.csv",
        SIGNATURE_FIELDS,
        [
            {
                "signature_id": "bsdb:111/1/1",
                "experiment_id": "111/Experiment 1",
                "source": "table 2",
                "abundance_in_group_1": "increased",
                "curation_state": "Complete",
            },
            {
                "signature_id": "bsdb:111/1/2",
                "experiment_id": "111/Experiment 1",
                "source": "table 2",
                "abundance_in_group_1": "decreased",
                "curation_state": "Complete",
            },
        ],
    )
    _write_csv(
        relational_dir / "signatures_taxa.csv",
        SIGNATURES_TAXA_FIELDS,
        [
            {"signature_id": "bsdb:111/1/1", "ncbi_id": "561"},
            {"signature_id": "bsdb:111/1/1", "ncbi_id": "620"},
            {"signature_id": "bsdb:111/1/2", "ncbi_id": "816"},
        ],
    )
    _write_csv(
        relational_dir / "taxa.csv",
        TAXA_FIELDS,
        [
            {"ncbi_id": "561", "taxon_name": "Escherichia coli"},
            {"ncbi_id": "620", "taxon_name": "Shigella"},
            {"ncbi_id": "816", "taxon_name": "Bacteroides fragilis"},
        ],
    )

    pmc_map = tmp_path / "pmid_pmcid_map.csv"
    _write_csv(
        pmc_map,
        PMC_MAP_FIELDS,
        [
            {"study_id": "111", "pmid": "111", "pmcid": "PMC111", "has_pmc": "true"},
            {"study_id": "222", "pmid": "222", "pmcid": "", "has_pmc": "false"},
        ],
    )
    return relational_dir, pmc_map


def _write_prediction(path: Path) -> None:
    prediction = {
        "study_id": "111",
        "experiments": [
            {
                "experiment_id": "111/Experiment 1",
                "body_site": ["Feces"],
                "condition": ["CRC"],
                "group_0_name": "healthy",
                "group_1_name": "CRC",
                "sequencing_type": "16S",
                "signatures": [
                    {"abundance_in_group_1": "increased", "taxa": [{"ncbi_id": 561}, {"ncbi_id": 620}]},
                    {"abundance_in_group_1": "decreased", "taxa": [{"ncbi_id": 816}]},
                ],
            }
        ],
    }
    path.write_text(json.dumps(prediction))


def _write_malformed_prediction(path: Path) -> None:
    """A prediction whose taxon is a bare string instead of a taxon dict --
    triggers `AttributeError` in `taxonomy.resolve_taxon`'s `taxon.get(...)`
    (a real malformed-pipeline-output shape, not a synthetic one)."""
    prediction = {
        "study_id": "222",
        "experiments": [
            {
                "body_site": ["Feces"],
                "signatures": [
                    {"abundance_in_group_1": "increased", "taxa": ["Escherichia coli"]},
                ],
            }
        ],
    }
    path.write_text(json.dumps(prediction))


def test_eval_score_end_to_end_writes_jsonl_md_html(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir()
    _write_prediction(pred_dir / "111.json")

    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "eval", "score",
            "--pred", str(pred_dir),
            "--relational", str(relational_dir),
            "--pmc-map", str(pmc_map),
            "--out", str(out_dir),
            "--taxonomy-cache", str(tmp_path / "cache.json"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (out_dir / "scores.jsonl").exists()
    assert (out_dir / "report.md").exists()
    assert (out_dir / "report.html").exists()

    lines = (out_dir / "scores.jsonl").read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    # Both gold studies are scored -- 222 has no prediction, so it's scored
    # as a full miss (Blocker 2 / §4d "same corpus, same split") AND listed
    # in its own missing-prediction bucket, rather than silently dropped.
    study_scores = {r["study_id"]: r for r in records if r["record_type"] == "study_score"}
    assert set(study_scores) == {"111", "222"}
    assert study_scores["111"]["micro_taxa"]["f1"] == 1.0
    assert study_scores["111"]["direction_correct"] == study_scores["111"]["direction_total"] == 2

    missing = [r for r in records if r["record_type"] == "missing_prediction"]
    assert [r["study_id"] for r in missing] == ["222"]

    md = (out_dir / "report.md").read_text()
    assert "## Missing predictions" in md
    assert "- 222" in md


def test_eval_score_isolates_per_study_scoring_errors(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir()
    _write_prediction(pred_dir / "111.json")  # well-formed
    _write_malformed_prediction(pred_dir / "222.json")  # taxon is a bare string, not a dict

    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "eval", "score",
            "--pred", str(pred_dir),
            "--relational", str(relational_dir),
            "--pmc-map", str(pmc_map),
            "--out", str(out_dir),
            "--taxonomy-cache", str(tmp_path / "cache.json"),
        ],
    )

    # The batch completes despite one malformed prediction -- it must not
    # abort the whole corpus run with no partial report.
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in (out_dir / "scores.jsonl").read_text().strip().splitlines()]
    study_scores = {r["study_id"] for r in records if r["record_type"] == "study_score"}
    assert study_scores == {"111"}  # 222 raised while scoring, so it's not a study_score record

    errors = [r for r in records if r["record_type"] == "scoring_error"]
    assert [e["study_id"] for e in errors] == ["222"]
    assert "AttributeError" in errors[0]["error"]

    md = (out_dir / "report.md").read_text()
    assert "## Scoring errors" in md
    assert "222" in md


def test_eval_score_is_deterministic_across_runs(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir()
    _write_prediction(pred_dir / "111.json")

    outputs = []
    for i in range(2):
        out_dir = tmp_path / f"out{i}"
        result = runner.invoke(
            app,
            [
                "eval", "score",
                "--pred", str(pred_dir),
                "--relational", str(relational_dir),
                "--pmc-map", str(pmc_map),
                "--out", str(out_dir),
                "--taxonomy-cache", str(tmp_path / f"cache{i}.json"),
            ],
        )
        assert result.exit_code == 0, result.output
        outputs.append((out_dir / "scores.jsonl").read_text())

    assert outputs[0] == outputs[1]


def test_eval_score_missing_relational_dir_errors(tmp_path):
    result = runner.invoke(
        app,
        ["eval", "score", "--pred", str(tmp_path), "--relational", str(tmp_path / "nope"), "--out", str(tmp_path / "out")],
    )
    assert result.exit_code == 1
    # Normalize whitespace: rich word-wraps the (possibly long) path in the error
    # message at the console width, which can split "does not exist" across a newline
    # on CI runners with long tmp paths. The intent is only that the path is reported missing.
    assert "does not exist" in " ".join(result.output.split())


def test_eval_score_missing_pred_path_errors(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    result = runner.invoke(
        app,
        [
            "eval", "score",
            "--pred", str(tmp_path / "nope.json"),
            "--relational", str(relational_dir),
            "--pmc-map", str(pmc_map),
            "--out", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 1
    # Normalize whitespace: rich word-wraps the (possibly long) path in the error
    # message at the console width, which can split "does not exist" across a newline
    # on CI runners with long tmp paths. The intent is only that the path is reported missing.
    assert "does not exist" in " ".join(result.output.split())


def test_eval_score_writes_unresolved_taxa_diagnostic_file(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir()
    prediction = {
        "study_id": "111",
        "experiments": [
            {
                "experiment_id": "111/Experiment 1",
                "body_site": ["Feces"],
                "condition": ["CRC"],
                "group_0_name": "healthy",
                "group_1_name": "CRC",
                "sequencing_type": "16S",
                "signatures": [
                    {
                        "abundance_in_group_1": "increased",
                        # 561 resolves via ncbi_id; the second taxon is only a
                        # name the resolver has no seed/cache entry for.
                        "taxa": [{"ncbi_id": 561}, {"taxon_name": "Some Unresolvable Organism"}],
                    },
                ],
            }
        ],
    }
    (pred_dir / "111.json").write_text(json.dumps(prediction))

    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "eval", "score",
            "--pred", str(pred_dir),
            "--relational", str(relational_dir),
            "--pmc-map", str(pmc_map),
            "--out", str(out_dir),
            "--taxonomy-cache", str(tmp_path / "cache.json"),
        ],
    )
    assert result.exit_code == 0, result.output

    unresolved_path = out_dir / "unresolved_taxa.txt"
    assert unresolved_path.exists()
    assert "some unresolvable organism" in unresolved_path.read_text()


def test_eval_gold_dumps_nested_shape(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    output_file = tmp_path / "gold.yaml"

    result = runner.invoke(
        app,
        [
            "eval", "gold",
            "--relational", str(relational_dir),
            "--pmc-map", str(pmc_map),
            "--full",  # dump everything, not just the (real-corpus) smoke set
            "--output", str(output_file),
        ],
    )

    assert result.exit_code == 0, result.output
    studies = yaml.safe_load(output_file.read_text())
    assert {s["study_id"] for s in studies} == {"111", "222"}
    study_111 = next(s for s in studies if s["study_id"] == "111")
    assert len(study_111["experiments"]) == 1
    assert len(study_111["experiments"][0]["signatures"]) == 2


def test_eval_gold_missing_relational_dir_errors(tmp_path):
    result = runner.invoke(app, ["eval", "gold", "--relational", str(tmp_path / "nope")])
    assert result.exit_code == 1
    # Normalize whitespace: rich word-wraps the (possibly long) path in the error
    # message at the console width, which can split "does not exist" across a newline
    # on CI runners with long tmp paths. The intent is only that the path is reported missing.
    assert "does not exist" in " ".join(result.output.split())


def test_eval_score_prediction_matched_by_filename_stem_when_no_id_key(tmp_path):
    relational_dir, pmc_map = _build_gold_fixture(tmp_path)
    pred_dir = tmp_path / "predictions"
    pred_dir.mkdir()
    # No "study_id"/"uid" key in the body -- must be matched via the filename.
    prediction = {
        "experiments": [
            {
                "body_site": ["Feces"],
                "condition": ["CRC"],
                "group_0_name": "healthy",
                "group_1_name": "CRC",
                "sequencing_type": "16S",
                "signatures": [
                    {"abundance_in_group_1": "increased", "taxa": [{"ncbi_id": 561}]},
                ],
            }
        ]
    }
    (pred_dir / "111.json").write_text(json.dumps(prediction))

    out_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "eval", "score",
            "--pred", str(pred_dir),
            "--relational", str(relational_dir),
            "--pmc-map", str(pmc_map),
            "--out", str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    lines = (out_dir / "scores.jsonl").read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    study_ids = {r["study_id"] for r in records if r["record_type"] == "study_score"}
    # 222 has no prediction file but is still scored (as a full miss) --
    # both gold studies show up as study_score records.
    assert study_ids == {"111", "222"}
