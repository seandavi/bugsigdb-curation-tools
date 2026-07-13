"""End-to-end tests for the `bugsigdb load` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from bugsigdb_curation.cli import app

runner = CliRunner()

FIXTURE = Path(__file__).parent / "data" / "full_dump_sample.csv"


def test_load_json_limit_one_produces_expected_structure():
    result = runner.invoke(app, ["load", str(FIXTURE), "--format", "json", "--limit", "1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    study = payload[0]
    assert study["pmid"] == 100001
    assert len(study["experiments"]) == 2
    assert "signatures" in study["experiments"][0]

    assert "1 studies, 2 experiments, 3 signatures" in result.stderr


def test_load_yaml_default_format_all_studies():
    result = runner.invoke(app, ["load", str(FIXTURE)])

    assert result.exit_code == 0, result.output
    payload = yaml.safe_load(result.stdout)
    assert len(payload) == 2
    assert "2 studies, 3 experiments, 6 signatures" in result.stderr


def test_load_writes_to_output_file(tmp_path):
    out = tmp_path / "out.yaml"
    result = runner.invoke(app, ["load", str(FIXTURE), "--output", str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    payload = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert len(payload) == 2
    # Nothing but the summary should go to stderr; stdout stays clean when writing to a file.
    assert "studies," in result.stderr


def test_load_missing_file_returns_error():
    result = runner.invoke(app, ["load", "tests/data/does_not_exist.csv"])
    assert result.exit_code != 0
