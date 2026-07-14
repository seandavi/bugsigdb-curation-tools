"""Unit tests for `bugsigdb_curation.taxonomy.build` (`.dmp` parsing + DB build).

Uses the synthetic fixture in `taxonomy_test_support.py` -- fully offline,
no real taxdump required.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import duckdb
import pytest

import httpx

from bugsigdb_curation.taxonomy.build import (
    build_taxonomy_db,
    download_taxdump,
    extract_taxdump,
    find_dmp_files,
    parse_names,
    parse_nodes,
)
from taxonomy_test_support import (
    EXPECTED_NAMES_ROWS,
    EXPECTED_NODES_ROWS,
    TAXID_BACTEROIDES_GENUS,
    write_malformed_taxdump_duplicate_node_pk,
    write_synthetic_taxdump,
)

BUILD_TIMESTAMP = "2026-07-14T00:00:00+00:00"


def test_parse_names_reads_all_fields(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    rows = list(parse_names(taxdump_dir / "names.dmp"))
    assert len(rows) == EXPECTED_NAMES_ROWS
    tax_id, name_txt, name_class, name_norm = next(r for r in rows if r[1] == "Bacteroides")
    assert tax_id == TAXID_BACTEROIDES_GENUS
    assert name_class == "scientific name"
    assert name_norm == "bacteroides"


def test_parse_nodes_reads_first_three_fields_only(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    rows = list(parse_nodes(taxdump_dir / "nodes.dmp"))
    assert len(rows) == EXPECTED_NODES_ROWS
    genus_row = next(r for r in rows if r[0] == TAXID_BACTEROIDES_GENUS)
    assert genus_row == (TAXID_BACTEROIDES_GENUS, 200, "genus")


def test_find_dmp_files_locates_both(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    names_path, nodes_path = find_dmp_files(taxdump_dir)
    assert names_path.name == "names.dmp"
    assert nodes_path.name == "nodes.dmp"


def test_find_dmp_files_searches_nested_subdirectory(tmp_path: Path):
    nested = tmp_path / "extracted" / "taxdump_inner"
    write_synthetic_taxdump(nested)
    names_path, nodes_path = find_dmp_files(tmp_path / "extracted")
    assert names_path.parent == nested


def test_find_dmp_files_missing_raises(tmp_path: Path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        find_dmp_files(empty_dir)


def test_build_taxonomy_db_from_directory_writes_tables_and_meta(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "out.duckdb"

    stats = build_taxonomy_db(
        taxdump_dir,
        out_path,
        release="2026-07-14",
        source="unit-test-fixture",
        build_timestamp=BUILD_TIMESTAMP,
    )

    assert stats.names_rows == EXPECTED_NAMES_ROWS
    assert stats.nodes_rows == EXPECTED_NODES_ROWS
    assert stats.out_path == out_path
    assert out_path.exists()

    con = duckdb.connect(str(out_path), read_only=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM names").fetchone()[0] == EXPECTED_NAMES_ROWS
        assert con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == EXPECTED_NODES_ROWS

        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        assert meta["release"] == "2026-07-14"
        assert meta["source"] == "unit-test-fixture"
        assert meta["build_timestamp"] == BUILD_TIMESTAMP
        assert meta["names_rows"] == str(EXPECTED_NAMES_ROWS)
        assert meta["nodes_rows"] == str(EXPECTED_NODES_ROWS)
        assert len(meta["names_dmp_sha256"]) == 64  # sha256 hex digest
        assert len(meta["nodes_dmp_sha256"]) == 64
    finally:
        con.close()


def test_build_taxonomy_db_overwrites_existing_out_path(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "out.duckdb"
    out_path.write_bytes(b"not a real duckdb file")

    stats = build_taxonomy_db(
        taxdump_dir, out_path, release="r1", source="fixture", build_timestamp=BUILD_TIMESTAMP
    )
    assert stats.names_rows == EXPECTED_NAMES_ROWS


def test_build_taxonomy_db_mid_build_failure_leaves_no_out_path_or_tmp_file(tmp_path: Path):
    """No pre-existing `out_path`: a mid-build failure (PRIMARY KEY violation
    on `nodes`) must leave `out_path` absent -- never a corrupt/empty DB --
    and must not leave any `.tmp-*` staging file behind."""
    taxdump_dir = write_malformed_taxdump_duplicate_node_pk(tmp_path / "taxdump")
    out_path = tmp_path / "out.duckdb"

    with pytest.raises(duckdb.Error):
        build_taxonomy_db(
            taxdump_dir, out_path, release="r1", source="fixture", build_timestamp=BUILD_TIMESTAMP
        )

    assert not out_path.exists()
    assert list(tmp_path.glob("out.duckdb.tmp-*")) == []


def test_build_taxonomy_db_mid_build_failure_preserves_existing_good_db(tmp_path: Path):
    """A pre-existing good `out_path` must be byte-for-byte unchanged after a
    mid-build failure targeting the same path -- the old DB is never
    unlinked/replaced until the new build has fully succeeded."""
    good_taxdump_dir = write_synthetic_taxdump(tmp_path / "good_taxdump")
    out_path = tmp_path / "out.duckdb"
    build_taxonomy_db(
        good_taxdump_dir, out_path, release="good-release", source="good-fixture", build_timestamp=BUILD_TIMESTAMP
    )
    good_bytes = out_path.read_bytes()

    bad_taxdump_dir = write_malformed_taxdump_duplicate_node_pk(tmp_path / "bad_taxdump")
    with pytest.raises(duckdb.Error):
        build_taxonomy_db(
            bad_taxdump_dir, out_path, release="bad-release", source="bad-fixture", build_timestamp=BUILD_TIMESTAMP
        )

    assert out_path.read_bytes() == good_bytes
    con = duckdb.connect(str(out_path), read_only=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        assert meta["release"] == "good-release"
    finally:
        con.close()
    assert list(tmp_path.glob("out.duckdb.tmp-*")) == []


def test_build_taxonomy_db_from_tar_gz_archive(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    archive_path = tmp_path / "taxdump.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(taxdump_dir / "names.dmp", arcname="names.dmp")
        tf.add(taxdump_dir / "nodes.dmp", arcname="nodes.dmp")

    out_path = tmp_path / "out.duckdb"
    stats = build_taxonomy_db(
        archive_path,
        out_path,
        release="r1",
        source="fixture-archive",
        build_timestamp=BUILD_TIMESTAMP,
        extract_dir=tmp_path / "extracted",
    )
    assert stats.names_rows == EXPECTED_NAMES_ROWS
    assert stats.nodes_rows == EXPECTED_NODES_ROWS


def test_build_taxonomy_db_from_zip_archive_uses_temp_extract_dir(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    archive_path = tmp_path / "taxdump.zip"
    with zipfile.ZipFile(archive_path, "w") as zf:
        zf.write(taxdump_dir / "names.dmp", arcname="names.dmp")
        zf.write(taxdump_dir / "nodes.dmp", arcname="nodes.dmp")

    out_path = tmp_path / "out.duckdb"
    # No extract_dir given -- build_taxonomy_db must fall back to its own
    # temporary directory (and clean it up) rather than erroring.
    stats = build_taxonomy_db(
        archive_path, out_path, release="r1", source="fixture-zip", build_timestamp=BUILD_TIMESTAMP
    )
    assert stats.names_rows == EXPECTED_NAMES_ROWS


def test_extract_taxdump_rejects_unsupported_archive_type(tmp_path: Path):
    bogus = tmp_path / "taxdump.rar"
    bogus.write_bytes(b"not really an archive")
    with pytest.raises(ValueError, match="unsupported archive type"):
        extract_taxdump(bogus, tmp_path / "extracted")


# --- opt-in real-network test -----------------------------------------------------------
#
# Deselected by default (`-m 'not network'` in pyproject.toml's addopts), and
# not exercised by anything else in this file -- the local-path build route
# above is the tested, offline path. This is the one place the package
# actually reaches the network; it must skip (not fail/error) when egress is
# blocked, since that's expected in sandboxed dev/CI environments.


@pytest.mark.network
def test_download_taxdump_real_network(tmp_path: Path):
    dest = tmp_path / "taxdump.tar.gz"
    try:
        download_taxdump(dest, timeout=30.0)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.HTTPStatusError) as exc:
        pytest.skip(f"network unavailable or NCBI unreachable: {exc}")

    assert dest.exists()
    assert dest.stat().st_size > 0
    # A real gzip'd tar starts with the gzip magic bytes.
    assert dest.read_bytes()[:2] == b"\x1f\x8b"
