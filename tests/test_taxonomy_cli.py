"""End-to-end tests for `bugsigdb taxonomy build`/`bugsigdb taxonomy lookup`.

Fully offline (synthetic fixture from `taxonomy_test_support.py`, no network).
`BUGSIGDB_CACHE_DIR` is always monkeypatched into `tmp_path` so these tests
never touch the real `~/.cache`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from taxonomy_test_support import (
    TAXID_BACTEROIDES_FRAGILIS,
    TAXID_BACTEROIDES_GENUS,
    write_synthetic_taxdump,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)


def test_build_writes_db_to_default_xdg_cache_path(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")

    result = runner.invoke(app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "2026-07-14"])

    assert result.exit_code == 0, result.output
    expected_out = tmp_path / "cache" / "taxonomy" / "ncbi-taxdump-2026-07-14.duckdb"
    assert expected_out.exists()
    assert "Built taxonomy DB" in result.output


def test_build_respects_explicit_out_path(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "custom" / "mine.duckdb"

    result = runner.invoke(
        app,
        ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1", "--out", str(out_path)],
    )

    assert result.exit_code == 0, result.output
    assert out_path.exists()


def test_build_requires_taxdump_unless_download(tmp_path: Path):
    result = runner.invoke(app, ["taxonomy", "build", "--release", "r1"])
    assert result.exit_code == 2
    assert "--taxdump is required" in result.output


def test_build_missing_taxdump_path_errors(tmp_path: Path):
    result = runner.invoke(
        app, ["taxonomy", "build", "--taxdump", str(tmp_path / "nope"), "--release", "r1"]
    )
    assert result.exit_code == 1


def test_build_then_lookup_round_trip_by_name(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    build_result = runner.invoke(
        app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1"]
    )
    assert build_result.exit_code == 0, build_result.output

    lookup_result = runner.invoke(app, ["taxonomy", "lookup", "Bacteroides"])
    assert lookup_result.exit_code == 0, lookup_result.output
    assert str(TAXID_BACTEROIDES_GENUS) in lookup_result.output
    assert "scientific name" in lookup_result.output


def test_lookup_normalizes_rank_prefixed_query(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    runner.invoke(app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1"])

    result = runner.invoke(app, ["taxonomy", "lookup", "g__Bacteroides"])
    assert result.exit_code == 0, result.output
    assert str(TAXID_BACTEROIDES_GENUS) in result.output


def test_lookup_flags_ambiguous_homonym(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    runner.invoke(app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1"])

    result = runner.invoke(app, ["taxonomy", "lookup", "Morganella"])
    assert result.exit_code == 0, result.output
    assert "ambiguous" in result.output.lower()
    assert "500" in result.output and "600" in result.output


def test_lookup_unknown_name_exits_nonzero(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    runner.invoke(app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1"])

    result = runner.invoke(app, ["taxonomy", "lookup", "Not A Real Taxon"])
    assert result.exit_code == 1


def test_lookup_by_taxid_prints_lineage(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    runner.invoke(app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1"])

    result = runner.invoke(app, ["taxonomy", "lookup", "--taxid", str(TAXID_BACTEROIDES_FRAGILIS)])
    assert result.exit_code == 0, result.output
    assert "Bacteroides fragilis" in result.output
    assert str(TAXID_BACTEROIDES_GENUS) in result.output  # genus ancestor appears in the lineage table


def test_lookup_requires_exactly_one_of_name_or_taxid(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    runner.invoke(app, ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1"])

    neither = runner.invoke(app, ["taxonomy", "lookup"])
    assert neither.exit_code == 2

    both = runner.invoke(app, ["taxonomy", "lookup", "Bacteroides", "--taxid", str(TAXID_BACTEROIDES_GENUS)])
    assert both.exit_code == 2


def test_lookup_with_no_db_anywhere_errors_cleanly(tmp_path: Path):
    result = runner.invoke(app, ["taxonomy", "lookup", "Bacteroides"])
    assert result.exit_code == 2
    assert "no --db path" in result.output or "no cached" in result.output.lower()


def test_lookup_db_flag_overrides_default(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    custom_out = tmp_path / "explicit.duckdb"
    build_result = runner.invoke(
        app,
        ["taxonomy", "build", "--taxdump", str(taxdump_dir), "--release", "r1", "--out", str(custom_out)],
    )
    assert build_result.exit_code == 0, build_result.output

    # No default DB exists (build wrote to --out, not the cache root), but
    # --db must still find it directly.
    result = runner.invoke(app, ["taxonomy", "lookup", "Bacteroides", "--db", str(custom_out)])
    assert result.exit_code == 0, result.output
    assert str(TAXID_BACTEROIDES_GENUS) in result.output
