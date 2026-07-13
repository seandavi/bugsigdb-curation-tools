"""End-to-end tests for the `bugsigdb validate` CLI command."""

from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from bugsigdb_curation.cli import app

runner = CliRunner()

VALID_STUDY = {
    "pmid": 27409883,
    "citation_mode": "Auto",
    "study_design": ["case-control"],
    "experiments": [
        {
            "host_species": "Homo sapiens",
            "signatures": [
                {
                    "abundance_in_group_1": "increased",
                    "taxa": [{"ncbi_id": 820}],
                }
            ],
        }
    ],
}

VALID_EXPERIMENT = {
    "host_species": "Homo sapiens",
    "signatures": [{"abundance_in_group_1": "increased", "taxa": [{"ncbi_id": 820}]}],
}

VALID_SIGNATURE = {"abundance_in_group_1": "increased", "taxa": [{"ncbi_id": 820}]}


def _write_yaml(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(obj))
    return path


def test_validate_valid_study_exits_zero(tmp_path):
    path = _write_yaml(tmp_path, "study.yaml", VALID_STUDY)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 0, result.output


def test_validate_bad_enum_value_exits_one(tmp_path):
    bad = dict(VALID_STUDY, study_design=["not-a-real-design"])
    path = _write_yaml(tmp_path, "study.yaml", bad)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "not-a-real-design" in result.output


def test_validate_wrong_type_exits_one(tmp_path):
    bad = dict(VALID_STUDY, pmid="not-an-integer")
    path = _write_yaml(tmp_path, "study.yaml", bad)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "not-an-integer" in result.output


def test_validate_missing_required_field_on_experiment(tmp_path):
    bad = {"signatures": [VALID_SIGNATURE]}  # missing required host_species
    path = _write_yaml(tmp_path, "experiment.yaml", bad)
    result = runner.invoke(app, ["validate", str(path), "--target-class", "Experiment"])
    assert result.exit_code == 1
    assert "host_species" in result.output


def test_validate_target_class_experiment_valid(tmp_path):
    path = _write_yaml(tmp_path, "experiment.yaml", VALID_EXPERIMENT)
    result = runner.invoke(app, ["validate", str(path), "--target-class", "Experiment"])
    assert result.exit_code == 0, result.output


def test_validate_target_class_signature_valid(tmp_path):
    path = _write_yaml(tmp_path, "signature.yaml", VALID_SIGNATURE)
    result = runner.invoke(app, ["validate", str(path), "-C", "Signature"])
    assert result.exit_code == 0, result.output


def test_validate_target_class_signature_bad_enum(tmp_path):
    bad = dict(VALID_SIGNATURE, abundance_in_group_1="sideways")
    path = _write_yaml(tmp_path, "signature.yaml", bad)
    result = runner.invoke(app, ["validate", str(path), "-C", "Signature"])
    assert result.exit_code == 1
    assert "sideways" in result.output


def test_validate_multiple_files_mixed_valid_and_invalid(tmp_path):
    good_path = _write_yaml(tmp_path, "good.yaml", VALID_STUDY)
    bad = dict(VALID_STUDY, pmid="oops")
    bad_path = _write_yaml(tmp_path, "bad.yaml", bad)

    result = runner.invoke(app, ["validate", str(good_path), str(bad_path)])

    assert result.exit_code == 1
    assert "good.yaml" in result.output
    assert "bad.yaml" in result.output


def test_validate_format_json_emits_parseable_report(tmp_path):
    bad = dict(VALID_STUDY, study_design=["not-a-real-design"])
    path = _write_yaml(tmp_path, "study.yaml", bad)

    result = runner.invoke(app, ["validate", str(path), "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload[0]["file"].endswith("study.yaml")
    assert payload[0]["valid"] is False
    assert any("not-a-real-design" in p["message"] for p in payload[0]["problems"])


def test_validate_format_json_valid_instance(tmp_path):
    path = _write_yaml(tmp_path, "study.yaml", VALID_STUDY)

    result = runner.invoke(app, ["validate", str(path), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["valid"] is True
    assert payload[0]["problems"] == []


def test_validate_nonexistent_file_exits_two(tmp_path):
    path = tmp_path / "missing.yaml"
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_validate_malformed_yaml_exits_two(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("pmid: [unterminated\n  - broken")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_validate_unknown_target_class_exits_two(tmp_path):
    path = _write_yaml(tmp_path, "study.yaml", VALID_STUDY)
    result = runner.invoke(app, ["validate", str(path), "--target-class", "NotAClass"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "NotAClass" in result.output
