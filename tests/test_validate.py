"""Unit tests for bugsigdb_curation.validate — the pure/testable validation logic."""

from __future__ import annotations

import json

import pytest

from bugsigdb_curation.validate import (
    InstanceResult,
    Problem,
    ValidationInputError,
    default_schema_path,
    load_instances,
    validate_file,
    validate_instance,
)

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
    "signatures": [
        {
            "abundance_in_group_1": "increased",
            "taxa": [{"ncbi_id": 820}],
        }
    ],
}

VALID_SIGNATURE = {
    "abundance_in_group_1": "increased",
    "taxa": [{"ncbi_id": 820}],
}


# --- default_schema_path ---------------------------------------------------


def test_default_schema_path_resolves_to_existing_readable_schema():
    path = default_schema_path()
    assert path.is_file()
    text = path.read_text()
    assert "name: bugsigdb" in text


# --- load_instances ---------------------------------------------------------


def test_load_instances_single_object_yaml(tmp_path):
    path = tmp_path / "study.yaml"
    path.write_text("pmid: 123\ncitation_mode: Auto\n")
    instances = load_instances(path)
    assert instances == [{"pmid": 123, "citation_mode": "Auto"}]


def test_load_instances_list_yaml(tmp_path):
    path = tmp_path / "studies.yaml"
    path.write_text("- pmid: 1\n- pmid: 2\n")
    instances = load_instances(path)
    assert instances == [{"pmid": 1}, {"pmid": 2}]


def test_load_instances_json_file(tmp_path):
    path = tmp_path / "study.json"
    path.write_text(json.dumps({"pmid": 123}))
    instances = load_instances(path)
    assert instances == [{"pmid": 123}]


def test_load_instances_json_list_file(tmp_path):
    path = tmp_path / "studies.json"
    path.write_text(json.dumps([{"pmid": 1}, {"pmid": 2}]))
    instances = load_instances(path)
    assert instances == [{"pmid": 1}, {"pmid": 2}]


def test_load_instances_nonexistent_file_raises(tmp_path):
    path = tmp_path / "missing.yaml"
    with pytest.raises(ValidationInputError, match="not found"):
        load_instances(path)


def test_load_instances_malformed_yaml_raises(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("pmid: [unterminated\n  - broken")
    with pytest.raises(ValidationInputError):
        load_instances(path)


def test_load_instances_rejects_non_object_non_list(tmp_path):
    path = tmp_path / "scalar.yaml"
    path.write_text("just a string\n")
    with pytest.raises(ValidationInputError):
        load_instances(path)


# --- validate_instance --------------------------------------------------------


def test_validate_instance_valid_study_has_no_problems():
    problems = validate_instance(VALID_STUDY, "Study", default_schema_path())
    assert problems == []


def test_validate_instance_bad_enum_value():
    bad = dict(VALID_STUDY, study_design=["not-a-real-design"])
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert isinstance(problems[0], Problem)
    assert problems[0].severity == "ERROR"
    assert "not-a-real-design" in problems[0].message
    assert problems[0].instantiates == "Study"


def test_validate_instance_wrong_type():
    bad = dict(VALID_STUDY, pmid="not-an-integer")
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert "not-an-integer" in problems[0].message


def test_validate_instance_missing_required_field_on_experiment():
    bad = {"signatures": [VALID_SIGNATURE]}  # host_species is required on Experiment
    problems = validate_instance(bad, "Experiment", default_schema_path())
    assert any("host_species" in p.message for p in problems)


def test_validate_instance_target_class_experiment_valid():
    problems = validate_instance(VALID_EXPERIMENT, "Experiment", default_schema_path())
    assert problems == []


def test_validate_instance_target_class_signature_valid():
    problems = validate_instance(VALID_SIGNATURE, "Signature", default_schema_path())
    assert problems == []


def test_validate_instance_target_class_signature_bad_enum():
    bad = dict(VALID_SIGNATURE, abundance_in_group_1="sideways")
    problems = validate_instance(bad, "Signature", default_schema_path())
    assert len(problems) == 1
    assert "sideways" in problems[0].message


def test_validate_instance_unknown_target_class_raises():
    with pytest.raises(ValidationInputError, match="NotAClass"):
        validate_instance(VALID_STUDY, "NotAClass", default_schema_path())


def test_validate_instance_unknown_schema_path_raises(tmp_path):
    with pytest.raises(ValidationInputError):
        validate_instance(VALID_STUDY, "Study", tmp_path / "nonexistent.yaml")


# --- validate_file ------------------------------------------------------------


def test_validate_file_valid_single_object(tmp_path):
    path = tmp_path / "study.yaml"
    path.write_text(_to_yaml(VALID_STUDY))
    results = validate_file(path, "Study", default_schema_path())
    assert len(results) == 1
    assert isinstance(results[0], InstanceResult)
    assert results[0].valid
    assert results[0].problems == []


def test_validate_file_multiple_objects_mixed_validity(tmp_path):
    bad = dict(VALID_STUDY, pmid="oops")
    path = tmp_path / "studies.yaml"
    path.write_text(_to_yaml([VALID_STUDY, bad]))
    results = validate_file(path, "Study", default_schema_path())
    assert len(results) == 2
    assert results[0].valid
    assert not results[1].valid


def _to_yaml(obj) -> str:
    import yaml

    return yaml.safe_dump(obj)
