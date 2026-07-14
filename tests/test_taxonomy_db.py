"""Unit tests for `bugsigdb_curation.taxonomy.db.TaxonomyDB` (resolve/lineage/rank/genus_of).

Built each test from the synthetic fixture in `taxonomy_test_support.py`
via `build_taxonomy_db` -- exercises the resolver against a real (if tiny)
DuckDB file, not a mock.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bugsigdb_curation.taxonomy.build import build_taxonomy_db
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from taxonomy_test_support import (
    TAXID_ALCALIGENES_WITH_PROVIDENCIA_SYNONYM,
    TAXID_BACTEROIDES_FRAGILIS,
    TAXID_BACTEROIDES_GENUS,
    TAXID_MORGANELLA_A,
    TAXID_MORGANELLA_B,
    TAXID_PROVIDENCIA_SCIENTIFIC,
    TAXID_ROOT,
    write_synthetic_taxdump,
)


@pytest.fixture()
def taxonomy_db(tmp_path: Path):
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(
        taxdump_dir,
        out_path,
        release="test",
        source="fixture",
        build_timestamp="2026-07-14T00:00:00+00:00",
    )
    with TaxonomyDB(out_path) as db:
        yield db


def test_resolve_scientific_name_hit(taxonomy_db: TaxonomyDB):
    resolution = taxonomy_db.resolve("Bacteroides")
    assert resolution is not None
    assert resolution.tax_id == TAXID_BACTEROIDES_GENUS
    assert resolution.name_class == "scientific name"
    assert resolution.matched_name_txt == "Bacteroides"
    assert resolution.rank == "genus"
    assert resolution.ambiguous is False
    assert resolution.candidates == (TAXID_BACTEROIDES_GENUS,)


def test_resolve_synonym_resolves_to_same_tax_id_as_scientific_name(taxonomy_db: TaxonomyDB):
    """The crux case: a synonym ("Bacteroidus") must resolve to the exact
    same tax_id as the canonical scientific name ("Bacteroides") it's a
    synonym of."""
    canonical = taxonomy_db.resolve("Bacteroides")
    synonym = taxonomy_db.resolve("Bacteroidus")
    assert canonical is not None
    assert synonym is not None
    assert synonym.tax_id == canonical.tax_id == TAXID_BACTEROIDES_GENUS
    assert synonym.name_class == "synonym"


@pytest.mark.parametrize(
    "query",
    [
        "bacteroides",
        "BACTEROIDES",
        "  Bacteroides  ",
        "Bacteroides",
        "g__Bacteroides",
        "g_Bacteroides",
    ],
)
def test_resolve_is_case_whitespace_underscore_and_rank_prefix_insensitive(
    taxonomy_db: TaxonomyDB, query: str
):
    resolution = taxonomy_db.resolve(query)
    assert resolution is not None
    assert resolution.tax_id == TAXID_BACTEROIDES_GENUS


def test_resolve_normalizes_underscore_separated_species_name(taxonomy_db: TaxonomyDB):
    resolution = taxonomy_db.resolve("Bacteroides_fragilis")
    assert resolution is not None
    assert resolution.tax_id == TAXID_BACTEROIDES_FRAGILIS


def test_resolve_homonym_is_ambiguous_with_deterministic_pick_and_candidates(taxonomy_db: TaxonomyDB):
    resolution = taxonomy_db.resolve("Morganella")
    assert resolution is not None
    assert resolution.ambiguous is True
    assert resolution.candidates == (TAXID_MORGANELLA_A, TAXID_MORGANELLA_B)
    # Deterministic pick: the smaller tax_id among the (both scientific-name) candidates.
    assert resolution.tax_id == TAXID_MORGANELLA_A


def test_resolve_homonym_pick_is_deterministic_across_repeated_calls(taxonomy_db: TaxonomyDB):
    picks = {taxonomy_db.resolve("Morganella").tax_id for _ in range(5)}
    assert picks == {TAXID_MORGANELLA_A}


def test_resolve_cross_class_homonym_prefers_the_scientific_name_row(taxonomy_db: TaxonomyDB):
    """The crux ambiguity case a same-class-only homonym fixture can't catch:
    "Providencia" is both a *synonym* of tax_id 850 (`Alcaligenes`) and the
    *scientific name* of tax_id 860. 850 < 860, so a naive
    smallest-tax_id-wins tie-break would (wrongly) return 850's synonym row.
    `resolve()` must still pick 860 -- the scientific-name-class row --
    while still surfacing both tax_ids as ambiguous candidates."""
    resolution = taxonomy_db.resolve("Providencia")
    assert resolution is not None
    assert resolution.ambiguous is True
    assert resolution.candidates == (
        TAXID_ALCALIGENES_WITH_PROVIDENCIA_SYNONYM,
        TAXID_PROVIDENCIA_SCIENTIFIC,
    )
    assert resolution.tax_id == TAXID_PROVIDENCIA_SCIENTIFIC
    assert resolution.name_class == "scientific name"
    assert resolution.matched_name_txt == "Providencia"


def test_resolve_unknown_name_returns_none(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.resolve("Not A Real Taxon Whatsoever") is None


def test_rank(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.rank(TAXID_BACTEROIDES_GENUS) == "genus"
    assert taxonomy_db.rank(TAXID_BACTEROIDES_FRAGILIS) == "species"
    assert taxonomy_db.rank(999999) is None


def test_genus_of_species_returns_its_genus(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.genus_of(TAXID_BACTEROIDES_FRAGILIS) == TAXID_BACTEROIDES_GENUS


def test_genus_of_a_genus_returns_itself(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.genus_of(TAXID_BACTEROIDES_GENUS) == TAXID_BACTEROIDES_GENUS


def test_genus_of_unknown_tax_id_returns_none(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.genus_of(999999) is None


def test_lineage_species_is_root_first_self_last(taxonomy_db: TaxonomyDB):
    lineage = taxonomy_db.lineage(TAXID_BACTEROIDES_FRAGILIS)
    tax_ids = [row[0] for row in lineage]
    assert tax_ids == [TAXID_ROOT, 2, 200, TAXID_BACTEROIDES_GENUS, TAXID_BACTEROIDES_FRAGILIS]
    ranks = [row[1] for row in lineage]
    assert ranks == ["no rank", "superkingdom", "phylum", "genus", "species"]
    names = [row[2] for row in lineage]
    assert names[-1] == "Bacteroides fragilis"
    assert names[0] == "root"


def test_lineage_of_root_is_a_single_self_row(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.lineage(TAXID_ROOT) == [(TAXID_ROOT, "no rank", "root")]


def test_lineage_of_unknown_tax_id_is_empty(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.lineage(999999) == []


def test_scientific_name(taxonomy_db: TaxonomyDB):
    assert taxonomy_db.scientific_name(TAXID_BACTEROIDES_GENUS) == "Bacteroides"
    assert taxonomy_db.scientific_name(999999) is None


def test_meta_round_trips_provenance(taxonomy_db: TaxonomyDB):
    meta = taxonomy_db.meta()
    assert meta["release"] == "test"
    assert meta["source"] == "fixture"


def test_taxonomy_db_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        TaxonomyDB(tmp_path / "does_not_exist.duckdb")


def test_taxonomy_db_missing_meta_table_raises_clear_error(tmp_path: Path):
    """A DB with `names`/`nodes` but no `meta` table at all (e.g. a build
    that died before `CREATE TABLE meta`, or any other hand-crafted/corrupt
    file) must raise a clear error on open -- not open silently-empty and
    have every `resolve()` call return `None`, indistinguishable from
    "unknown name"."""
    out_path = tmp_path / "incomplete.duckdb"
    con = duckdb.connect(str(out_path))
    con.execute("CREATE TABLE names (tax_id BIGINT, name_txt VARCHAR, name_class VARCHAR, name_norm VARCHAR)")
    con.execute("CREATE TABLE nodes (tax_id BIGINT PRIMARY KEY, parent_tax_id BIGINT, rank VARCHAR)")
    con.close()

    with pytest.raises(ValueError, match="missing table"):
        TaxonomyDB(out_path)


def test_taxonomy_db_empty_meta_table_raises_clear_error(tmp_path: Path):
    """A DB with a `meta` table present but empty (created but never
    populated) is just as much an incomplete build as a missing table, and
    must raise the same kind of clear error."""
    out_path = tmp_path / "empty_meta.duckdb"
    con = duckdb.connect(str(out_path))
    con.execute("CREATE TABLE names (tax_id BIGINT, name_txt VARCHAR, name_class VARCHAR, name_norm VARCHAR)")
    con.execute("CREATE TABLE nodes (tax_id BIGINT PRIMARY KEY, parent_tax_id BIGINT, rank VARCHAR)")
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.close()

    with pytest.raises(ValueError, match="empty meta table"):
        TaxonomyDB(out_path)


def test_taxonomy_db_corrupt_file_raises_duckdb_error(tmp_path: Path):
    """A non-DuckDB byte blob at the path (a truncated/corrupt build, or a
    DB built with an incompatible DuckDB version) raises `duckdb.Error`
    (`duckdb.IOException`) from `duckdb.connect()` itself -- distinct from
    the `FileNotFoundError`/`ValueError` this module raises directly. Fix 1
    depends on this being a `duckdb.Error` subclass, not a `ValueError`."""
    bad_path = tmp_path / "corrupt.duckdb"
    bad_path.write_bytes(b"not a real duckdb file, just some garbage bytes\x00\x01\x02" * 10)

    with pytest.raises(duckdb.Error):
        TaxonomyDB(bad_path)


def test_taxonomy_db_close_is_idempotent(tmp_path: Path):
    """Fix 4: `close()` must tolerate being called more than once -- a
    caller-supplied resolver and a `with`-block `__exit__` (or two
    independent teardown paths) can both end up calling it on the same
    handle."""
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(
        taxdump_dir, out_path, release="test", source="fixture", build_timestamp="2026-07-14T00:00:00+00:00"
    )
    db = TaxonomyDB(out_path)
    db.close()
    db.close()  # must not raise
