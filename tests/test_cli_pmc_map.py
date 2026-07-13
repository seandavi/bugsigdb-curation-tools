"""End-to-end tests for the `bugsigdb pmc-map` CLI command, HTTP fully mocked."""

from __future__ import annotations

import csv
from pathlib import Path

from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.pmc_map import IDCONV_URL

runner = CliRunner()


def _write_studies_csv(tmp_path: Path) -> Path:
    path = tmp_path / "studies.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["study_id", "pmid", "doi"])
        writer.writeheader()
        writer.writerow({"study_id": "Study 1", "pmid": "1", "doi": "10.1/a"})
        writer.writerow({"study_id": "Study 2", "pmid": "", "doi": "10.1/b"})  # skipped, no PMID
        writer.writerow({"study_id": "Study 3", "pmid": "2", "doi": "10.1/c"})
    return path


def test_pmc_map_writes_csv_and_prints_summary(tmp_path, httpx_mock: HTTPXMock):
    input_file = _write_studies_csv(tmp_path)
    output_file = tmp_path / "out" / "map.csv"

    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "1,2",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "test@example.com",
        },
        json={
            "status": "ok",
            "records": [
                {"pmid": "1", "pmcid": "PMC1", "doi": "10.1/a"},
                {"pmid": "2", "status": "error", "errmsg": "Identifier not found in PMC"},
            ],
        },
    )

    result = runner.invoke(
        app,
        [
            "pmc-map",
            "--input",
            str(input_file),
            "--output",
            str(output_file),
            "--email",
            "test@example.com",
        ],
    )

    assert result.exit_code == 0, result.output
    assert output_file.exists()

    with open(output_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows == [
        {"study_id": "Study 1", "pmid": "1", "pmcid": "PMC1", "doi": "10.1/a", "has_pmc": "true"},
        {"study_id": "Study 3", "pmid": "2", "pmcid": "", "doi": "", "has_pmc": "false"},
    ]

    assert "2 PMIDs: 1 with PMCID (50.0%), 1 without." in result.output


def test_pmc_map_limit_only_queries_first_n_pmids(tmp_path, httpx_mock: HTTPXMock):
    input_file = _write_studies_csv(tmp_path)
    output_file = tmp_path / "map.csv"

    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "1",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "seandavi@gmail.com",
        },
        json={"status": "ok", "records": [{"pmid": "1", "pmcid": "PMC1"}]},
    )

    result = runner.invoke(
        app,
        ["pmc-map", "--input", str(input_file), "--output", str(output_file), "--limit", "1"],
    )

    assert result.exit_code == 0, result.output
    with open(output_file, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Study 3 (pmid=2) was excluded from the query by --limit, so its row is dropped.
    assert [r["study_id"] for r in rows] == ["Study 1"]
    assert "1 PMIDs: 1 with PMCID (100.0%), 0 without." in result.output
    assert "Note: 1 study row(s) excluded (PMID outside --limit)." in result.output


def test_pmc_map_missing_input_file_errors(tmp_path):
    result = runner.invoke(
        app, ["pmc-map", "--input", str(tmp_path / "nope.csv"), "--output", str(tmp_path / "out.csv")]
    )
    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_pmc_map_default_email_is_seandavi(tmp_path, httpx_mock: HTTPXMock):
    input_file = _write_studies_csv(tmp_path)
    output_file = tmp_path / "map.csv"

    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "1,2",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "seandavi@gmail.com",
        },
        json={"status": "ok", "records": [{"pmid": "1", "pmcid": "PMC1"}, {"pmid": "2"}]},
    )

    result = runner.invoke(app, ["pmc-map", "--input", str(input_file), "--output", str(output_file)])
    assert result.exit_code == 0, result.output
