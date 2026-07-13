"""End-to-end tests for the `bugsigdb validate` CLI command."""

from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from bugsigdb_curation.cli import app

runner = CliRunner()

VALID_STUDY = {
    "uid": "27409883",
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


def test_validate_study_missing_uid_exits_one(tmp_path):
    bad = {k: v for k, v in VALID_STUDY.items() if k != "uid"}
    path = _write_yaml(tmp_path, "study.yaml", bad)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "uid" in result.output


def test_validate_study_without_pmid_but_with_uid_exits_zero(tmp_path):
    # The whole point of this change: a PMID-less study is valid as long as
    # it carries a `uid`.
    no_pmid = {k: v for k, v in VALID_STUDY.items() if k != "pmid"}
    no_pmid["citation_mode"] = "Manual"
    path = _write_yaml(tmp_path, "study.yaml", no_pmid)
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


# --- rich markup injection (bracket-containing user/file-derived strings) ---
#
# `rich` interprets "[...]"/"[/...]" as markup tags. A lone unmatched *opening*
# tag like "[bracket]" is silently swallowed by rich (a quieter but separate
# data-loss bug), but an unmatched *closing* tag like "[/x]" raises
# `rich.errors.MarkupError` from inside `console.print()` — these tests use
# that "[/x]" shape (rather than plain "[...]") to actually reproduce the
# crash. Pre-fix, the nonexistent-file, malformed-YAML, and unknown-target-
# class error paths print such strings (filename, YAML parser message,
# target-class value) directly without `rich.markup.escape()`, so the
# MarkupError propagates as an uncaught exception (a traceback and exit code
# 1) instead of the contracted, clean exit code 2.


def test_validate_nonexistent_file_with_brackets_exits_two_cleanly(tmp_path):
    path = tmp_path / "missing[/x].yaml"
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_validate_unknown_target_class_with_brackets_exits_two_cleanly(tmp_path):
    path = _write_yaml(tmp_path, "study.yaml", VALID_STUDY)
    result = runner.invoke(app, ["validate", str(path), "--target-class", "Not[/x]Class"])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


def test_validate_malformed_yaml_with_brackets_exits_two_cleanly(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("pmid: [/x]unterminated\n  - broken")
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)


# --- closed=True: unknown/extra properties -----------------------------------


def test_validate_extra_property_on_study_exits_one(tmp_path):
    bad = dict(VALID_STUDY, bogus_top_level_field="oops")
    path = _write_yaml(tmp_path, "study.yaml", bad)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "bogus_top_level_field" in result.output


def test_validate_extra_property_on_nested_experiment_exits_one(tmp_path):
    bad_experiment = dict(VALID_STUDY["experiments"][0], bogus_experiment_field="oops")
    bad = dict(VALID_STUDY, experiments=[bad_experiment])
    path = _write_yaml(tmp_path, "study.yaml", bad)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "bogus_experiment_field" in result.output


def test_validate_extra_property_on_nested_signature_exits_one(tmp_path):
    experiment = VALID_STUDY["experiments"][0]
    bad_signature = dict(experiment["signatures"][0], bogus_signature_field="oops")
    bad = dict(VALID_STUDY, experiments=[dict(experiment, signatures=[bad_signature])])
    path = _write_yaml(tmp_path, "study.yaml", bad)
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 1
    assert "bogus_signature_field" in result.output
