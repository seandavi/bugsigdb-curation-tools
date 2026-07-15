"""End-to-end tests for the `bugsigdb supplements` CLI command, HTTP fully mocked.

Standalone command (not wired into `bugsigdb curate`) -- see
`bugsigdb_curation.supplements`'s module docstring.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.pmc_map import IDCONV_URL
from bugsigdb_curation.supplements import EUROPEPMC_SUPPLEMENTARY_FILES_URL

runner = CliRunner()


def _build_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_supplements_by_pmcid_prints_table_and_dumps_files(tmp_path: Path, httpx_mock: HTTPXMock):
    zip_bytes = _build_zip({"a.pdf": b"%PDF-1.4 fake", "b.csv": b"x,y\n1,2\n"})
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC8497572"),
        content=zip_bytes,
        headers={"Content-Type": "application/zip"},
    )
    dump_dir = tmp_path / "dump"

    result = runner.invoke(
        app, ["supplements", "--pmcid", "PMC8497572", "--dump", str(dump_dir)]
    )

    assert result.exit_code == 0, result.output
    assert "a.pdf" in result.output
    assert "b.csv" in result.output
    assert "pdf" in result.output
    assert "csv" in result.output
    assert (dump_dir / "a.pdf").read_bytes() == b"%PDF-1.4 fake"
    assert (dump_dir / "b.csv").read_bytes() == b"x,y\n1,2\n"
    assert (dump_dir / "b.csv.txt").read_text() == "x\ty\n1\t2"
    assert not (dump_dir / "a.pdf.txt").exists()  # pdf has no text rendering


def test_supplements_by_pmid_resolves_pmcid_first(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {
                "ids": "21850056",
                "idtype": "pmid",
                "format": "json",
                "tool": "bugsigdb-curation",
                "email": DEFAULT_EMAIL,
            }
        ),
        json={
            "status": "ok",
            "records": [{"pmid": "21850056", "pmcid": "PMC3123456", "doi": "10.1/x"}],
        },
    )
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC3123456"),
        content=_build_zip({"a.pdf": b"data"}),
        headers={"Content-Type": "application/zip"},
    )

    result = runner.invoke(app, ["supplements", "--pmid", "21850056"])

    assert result.exit_code == 0, result.output
    assert "PMC3123456" in result.output
    assert "a.pdf" in result.output


def test_supplements_pmid_with_no_pmc_record_prints_warning_and_exits_zero(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={"status": "ok", "records": [{"pmid": "19849869", "live": "false"}]},
    )

    result = runner.invoke(app, ["supplements", "--pmid", "19849869"])

    assert result.exit_code == 0, result.output
    assert "no pmcid" in result.output.lower() or "nothing to fetch" in result.output.lower()


def test_supplements_no_files_found(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC0000000"), status_code=404
    )

    result = runner.invoke(app, ["supplements", "--pmcid", "PMC0000000"])

    assert result.exit_code == 0, result.output
    assert "no supplementary files" in result.output.lower()


def test_supplements_requires_exactly_one_of_pmid_or_pmcid():
    result = runner.invoke(app, ["supplements"])
    assert result.exit_code == 2

    result = runner.invoke(app, ["supplements", "--pmid", "1", "--pmcid", "PMC1"])
    assert result.exit_code == 2
