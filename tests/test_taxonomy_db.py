"""Unit tests for `bugsigdb_curation.taxonomy.db.TaxonomyDB` (resolve/lineage/rank/genus_of).

Built each test from the synthetic fixture in `taxonomy_test_support.py`
via `build_taxonomy_db` -- exercises the resolver against a real (if tiny)
DuckDB file, not a mock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bugsigdb_curation.taxonomy.build import build_taxonomy_db
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from taxonomy_test_support import (
    TAXID_BACTEROIDES_FRAGILIS,
    TAXID_BACTEROIDES_GENUS,
    TAXID_MORGANELLA_A,
    TAXID_MORGANELLA_B,
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
