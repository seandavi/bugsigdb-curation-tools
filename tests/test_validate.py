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


def test_validate_instance_study_missing_uid_is_invalid():
    # `uid` (not `pmid`) is now the Study identifier, so dropping it must fail.
    bad = {k: v for k, v in VALID_STUDY.items() if k != "uid"}
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert "uid" in problems[0].message


def test_validate_instance_study_without_pmid_but_with_uid_is_valid():
    # The whole point of this change: a PMID-less study (~16 of ~2068 real
    # studies) is valid as long as it has a `uid`.
    no_pmid = {k: v for k, v in VALID_STUDY.items() if k != "pmid"}
    no_pmid["citation_mode"] = "Manual"
    problems = validate_instance(no_pmid, "Study", default_schema_path())
    assert problems == []


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


# --- Problem.path (_extract_path) --------------------------------------------
#
# These lock in the JSON-pointer-ish `path` field derived from the LinkML
# jsonschema plugin's message text, so a future message-format change (which
# would silently break `_extract_path`'s regex) gets caught by a test rather
# than only showing up as a missing `path` in --format json output.


def test_validate_instance_path_for_top_level_field():
    bad = dict(VALID_STUDY, pmid="not-an-integer")
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert problems[0].path == "/pmid"


def test_validate_instance_path_for_list_item():
    bad = dict(VALID_STUDY, study_design=["not-a-real-design"])
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert problems[0].path == "/study_design/0"


def test_validate_instance_path_for_nested_field():
    bad = dict(
        VALID_STUDY,
        experiments=[dict(VALID_STUDY["experiments"][0], host_species=12345)],
    )
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert problems[0].path == "/experiments/0/host_species"


# --- closed=True: unknown/extra properties -----------------------------------
#
# `_get_validator` explicitly wires up `JsonschemaValidationPlugin(closed=True)`
# to catch typo'd/extra field names. These tests exercise that: flipping
# `closed=True` -> `closed=False` should make each of them fail.


def test_validate_instance_rejects_unknown_property_on_study():
    bad = dict(VALID_STUDY, bogus_top_level_field="oops")
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert "bogus_top_level_field" in problems[0].message


def test_validate_instance_rejects_unknown_property_on_nested_experiment():
    bad = dict(
        VALID_STUDY,
        experiments=[dict(VALID_STUDY["experiments"][0], bogus_experiment_field="oops")],
    )
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert "bogus_experiment_field" in problems[0].message
    assert problems[0].path == "/experiments/0"


def test_validate_instance_rejects_unknown_property_on_nested_signature():
    experiment = VALID_STUDY["experiments"][0]
    bad_signature = dict(experiment["signatures"][0], bogus_signature_field="oops")
    bad = dict(VALID_STUDY, experiments=[dict(experiment, signatures=[bad_signature])])
    problems = validate_instance(bad, "Study", default_schema_path())
    assert len(problems) == 1
    assert "bogus_signature_field" in problems[0].message
    assert problems[0].path == "/experiments/0/signatures/0"


def test_validate_instance_rejects_unknown_property_on_experiment_target_class():
    bad = dict(VALID_EXPERIMENT, bogus_field="oops")
    problems = validate_instance(bad, "Experiment", default_schema_path())
    assert len(problems) == 1
    assert "bogus_field" in problems[0].message


def test_validate_instance_rejects_unknown_property_on_signature_target_class():
    bad = dict(VALID_SIGNATURE, bogus_field="oops")
    problems = validate_instance(bad, "Signature", default_schema_path())
    assert len(problems) == 1
    assert "bogus_field" in problems[0].message


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
