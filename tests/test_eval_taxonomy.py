"""Unit tests for bugsigdb_curation.eval.taxonomy -- the name->taxid resolver.

PR-2: the resolver's local-hit source is the general NCBI `TaxonomyDB`
(`bugsigdb_curation.taxonomy`), not a `taxa.csv`-derived seed map -- most
tests here build one from the shared synthetic-taxdump fixture in
`taxonomy_test_support.py` (also used by `test_taxonomy_db.py` et al.).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.eval.taxonomy import (
    NCBI_ESEARCH_URL,
    TaxonomyResolver,
    genus_token,
    normalize_taxon_name,
)
from bugsigdb_curation.taxonomy.build import build_taxonomy_db
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name as taxonomy_normalize_taxon_name
from bugsigdb_curation.taxonomy.paths import DB_PATH_ENV_VAR
from taxonomy_test_support import (
    TAXID_BACTEROIDES_FRAGILIS,
    TAXID_BACTEROIDES_GENUS,
    TAXID_RETIRED_MERGED_INTO_FRAGILIS,
    write_synthetic_taxdump,
)


@pytest.fixture()
def taxonomy_db(tmp_path: Path) -> TaxonomyDB:
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(
        taxdump_dir, out_path, release="test", source="fixture", build_timestamp="2026-07-14T00:00:00+00:00"
    )
    with TaxonomyDB(out_path) as db:
        yield db


# --- normalize_taxon_name / genus_token ---------------------------------------------------


def test_normalize_strips_double_underscore_rank_prefix():
    assert normalize_taxon_name("g__Faecalibacterium") == "faecalibacterium"


def test_normalize_strips_single_underscore_rank_prefix():
    assert normalize_taxon_name("s_Bacillus_subtilis") == "bacillus subtilis"


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize_taxon_name("  Escherichia   coli  ") == "escherichia coli"


def test_normalize_no_prefix_passthrough():
    assert normalize_taxon_name("Prevotella") == "prevotella"


def test_genus_token_first_word():
    assert genus_token("faecalibacterium prausnitzii") == "faecalibacterium"


def test_genus_token_empty_string():
    assert genus_token("") == ""


# --- Fix 5: normalize_taxon_name parity with bugsigdb_curation.taxonomy.normalize ------------

#: Rank prefixes (double/single-underscored, plus a case-sensitivity probe
#: that must NOT be stripped), underscores, leading/trailing/internal
#: whitespace runs, mixed case, and the empty/whitespace-only strings --
#: mirrors `test_taxonomy_normalize.py::_PARITY_SAMPLE`.
_NORMALIZE_PARITY_SAMPLE = [
    "Faecalibacterium",
    "g__Faecalibacterium",
    "g_Faecalibacterium",
    "s__Escherichia_coli",
    "s_Escherichia coli",
    "k__Bacteria",
    "  Bacteroides   fragilis  ",
    "Escherichia_coli",
    "MiXeD_CaSe",
    "G__Uppercase",  # case-sensitive prefix regex: uppercase G is NOT a prefix
    "t__strain_xyz",
    "no_prefix_here",
    "",
    "   ",
]


@pytest.mark.parametrize("name", _NORMALIZE_PARITY_SAMPLE)
def test_normalize_taxon_name_matches_shared_taxonomy_normalize(name: str):
    """`eval.taxonomy.normalize_taxon_name` is a deliberate duplicate of
    `taxonomy.normalize.normalize_taxon_name` (kept separate, not imported,
    for the data-firewall reasons this module's docstring and
    `taxonomy/normalize.py`'s docstring both explain) -- assert the two stay
    byte-for-byte identical over a shared sample so they can't silently
    desync."""
    assert normalize_taxon_name(name) == taxonomy_normalize_taxon_name(name)


# --- TaxonomyResolver.load / local TaxonomyDB ----------------------------------------------


def test_load_resolves_db_path_and_reads_it(tmp_path, monkeypatch):
    """`.load()`'s real construction path: no `db` given directly, so it
    resolves one via `db_path` (mirrors `NcbiTaxonomyResolver.load`)."""
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(
        taxdump_dir, out_path, release="test", source="fixture", build_timestamp="2026-07-14T00:00:00+00:00"
    )

    resolver = TaxonomyResolver.load(db_path=out_path, cache_path=None)

    assert resolver.resolve_name("Bacteroides") == TAXID_BACTEROIDES_GENUS
    assert resolver.resolve_name("g__Bacteroides") == TAXID_BACTEROIDES_GENUS  # normalization applies


def test_load_with_no_db_found_yields_no_local_resolution(monkeypatch):
    # BUGSIGDB_CACHE_DIR is isolated per-test by conftest.py's autouse
    # fixture, so with no explicit db_path/BUGSIGDB_TAXONOMY_DB there's
    # nothing to find.
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    resolver = TaxonomyResolver.load(cache_path=None)
    assert resolver.db is None
    assert resolver.resolve_name("Bacteroides") is None


def test_load_with_no_cache_path_yields_empty_cache():
    resolver = TaxonomyResolver.load(cache_path=None)
    assert resolver.cache == {}
    assert resolver.cache_path is None


# --- Copilot fix: split "no DB" warning -- unconfigured vs. explicit-but-missing -----------


def test_load_with_nothing_configured_warns_with_generic_message(monkeypatch):
    """No `db_path`, no `BUGSIGDB_TAXONOMY_DB`, and nothing cached -- the
    genuinely-unconfigured case still gets the original generic message,
    not the "configured but missing" one."""
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    with pytest.warns(RuntimeWarning, match=r"^no local taxonomy DB found") as record:
        resolver = TaxonomyResolver.load(cache_path=None)
    assert resolver.db is None
    assert "configured taxonomy DB not found" not in str(record[0].message)


def test_load_with_explicit_missing_db_path_warns_with_actual_path(tmp_path, monkeypatch):
    """An explicit `--taxonomy-db` path that just doesn't exist on disk must
    name that actual path in the warning, not the generic "nothing
    configured" message."""
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    missing = tmp_path / "does_not_exist.duckdb"
    with pytest.warns(RuntimeWarning, match=r"configured taxonomy DB not found at .*does_not_exist\.duckdb"):
        resolver = TaxonomyResolver.load(db_path=missing, cache_path=None)
    assert resolver.db is None


def test_load_with_explicit_missing_env_db_path_warns_with_actual_path(tmp_path, monkeypatch):
    """Same as above, but the explicit configuration comes from
    `BUGSIGDB_TAXONOMY_DB` rather than a direct `db_path` argument."""
    missing = tmp_path / "also_missing.duckdb"
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(missing))
    with pytest.warns(RuntimeWarning, match=r"configured taxonomy DB not found at .*also_missing\.duckdb"):
        resolver = TaxonomyResolver.load(cache_path=None)
    assert resolver.db is None


# --- resolve_name: cache priority + unresolved tracking ------------------------------------


def test_resolve_name_cache_hit_before_local_db(taxonomy_db: TaxonomyDB):
    resolver = TaxonomyResolver(db=taxonomy_db, cache={"bacteroides": 999})
    assert resolver.resolve_name("Bacteroides") == 999  # cache wins over the DB's own 816


def test_resolve_name_falls_back_to_local_db(taxonomy_db: TaxonomyDB):
    resolver = TaxonomyResolver(db=taxonomy_db, cache={})
    assert resolver.resolve_name("Bacteroides") == TAXID_BACTEROIDES_GENUS
    # A DB hit is cached, so a repeat lookup doesn't need the DB again.
    assert resolver.cache["bacteroides"] == TAXID_BACTEROIDES_GENUS


def test_resolve_name_unresolved_is_tracked_and_none_returned():
    resolver = TaxonomyResolver()
    result = resolver.resolve_name("Totally Unknown Organism")
    assert result is None
    assert "totally unknown organism" in resolver.unresolved


def test_resolve_name_cached_none_is_tracked_as_unresolved():
    resolver = TaxonomyResolver(cache={"known miss": None})
    assert resolver.resolve_name("known miss") is None
    assert "known miss" in resolver.unresolved


# --- synonym resolution via the cache -------------------------------------------------------


def test_synonym_resolution_via_shared_cache_entry():
    # Propionibacterium acnes was reclassified as Cutibacterium acnes; the seed
    # map (built straight from taxa.csv, which only knows the corpus's own
    # curated spelling) can't unify them on its own, but seeding the cache
    # with both normalized names pointing at the same taxid does.
    shared_id = 1747
    resolver = TaxonomyResolver(
        cache={
            "propionibacterium acnes": shared_id,
            "cutibacterium acnes": shared_id,
        }
    )
    assert resolver.resolve_name("Propionibacterium acnes") == shared_id
    assert resolver.resolve_name("Cutibacterium acnes") == shared_id


# --- resolve_taxon: id passthrough vs name resolution ---------------------------------------


def test_resolve_taxon_passes_through_int_ncbi_id():
    resolver = TaxonomyResolver()
    assert resolver.resolve_taxon({"ncbi_id": 561, "taxon_name": "should be ignored"}) == 561


def test_resolve_taxon_passes_through_numeric_string_ncbi_id():
    resolver = TaxonomyResolver()
    assert resolver.resolve_taxon({"ncbi_id": "561"}) == 561


def test_resolve_taxon_resolves_taxon_name_when_no_id(taxonomy_db: TaxonomyDB):
    resolver = TaxonomyResolver(db=taxonomy_db)
    assert resolver.resolve_taxon({"taxon_name": "Bacteroides"}) == TAXID_BACTEROIDES_GENUS


def test_resolve_taxon_returns_none_when_nothing_to_resolve():
    resolver = TaxonomyResolver()
    assert resolver.resolve_taxon({}) is None


def test_resolve_taxon_bool_ncbi_id_is_not_treated_as_int():
    # bool is an int subclass in Python; guard against a stray `True`/`False`
    # leaking through as a taxid.
    resolver = TaxonomyResolver()
    assert resolver.resolve_taxon({"ncbi_id": True}) is None


# --- genus_of_id --------------------------------------------------------------------------


def test_genus_of_id_uses_reverse_name_lookup():
    resolver = TaxonomyResolver(id_to_name={561: "escherichia coli"})
    assert resolver.genus_of_id(561) == "escherichia"


def test_genus_of_id_unknown_id_returns_none():
    resolver = TaxonomyResolver()
    assert resolver.genus_of_id(999999) is None


def test_genus_of_id_falls_back_to_local_db_scientific_name(taxonomy_db: TaxonomyDB):
    """PR-2: `genus_of_id` no longer needs a bulk `taxa.csv` reverse map --
    it falls back to the local `TaxonomyDB`'s own scientific name for ANY
    tax_id the DB knows, even one this resolver never resolved a prediction
    against."""
    resolver = TaxonomyResolver(db=taxonomy_db)
    assert resolver.genus_of_id(TAXID_BACTEROIDES_FRAGILIS) == "bacteroides"
    # Backfilled into id_to_name as a side effect, so a repeat call is free.
    assert resolver.id_to_name[TAXID_BACTEROIDES_FRAGILIS] == "bacteroides fragilis"


def test_name_of_id_prefers_id_to_name_over_db(taxonomy_db: TaxonomyDB):
    """A manually-seeded/previously-resolved `id_to_name` entry wins over
    the DB's own name for the same id (mirrors cache-over-DB priority in
    `resolve_name`)."""
    resolver = TaxonomyResolver(db=taxonomy_db, id_to_name={TAXID_BACTEROIDES_GENUS: "overridden name"})
    assert resolver.name_of_id(TAXID_BACTEROIDES_GENUS) == "overridden name"


# --- canonical_id / merged.dmp canonicalization ---------------------------------------------


def test_canonical_id_delegates_to_db(taxonomy_db: TaxonomyDB):
    resolver = TaxonomyResolver(db=taxonomy_db)
    assert resolver.canonical_id(TAXID_RETIRED_MERGED_INTO_FRAGILIS) == TAXID_BACTEROIDES_FRAGILIS
    assert resolver.canonical_id(TAXID_BACTEROIDES_FRAGILIS) == TAXID_BACTEROIDES_FRAGILIS


def test_canonical_id_identity_when_no_db():
    resolver = TaxonomyResolver()
    assert resolver.canonical_id(TAXID_RETIRED_MERGED_INTO_FRAGILIS) == TAXID_RETIRED_MERGED_INTO_FRAGILIS


def test_name_of_id_resolves_retired_id_via_canonicalization(taxonomy_db: TaxonomyDB):
    """A retired gold tax_id (999, merged into Bacteroides fragilis's 817)
    must resolve to the CURRENT node's scientific name -- `name_of_id`
    delegates to `TaxonomyDB.scientific_name`, which canonicalizes
    internally (see `db.py`)."""
    resolver = TaxonomyResolver(db=taxonomy_db)
    assert resolver.name_of_id(TAXID_RETIRED_MERGED_INTO_FRAGILIS) == "bacteroides fragilis"


def test_genus_of_id_resolves_retired_id_via_canonicalization(taxonomy_db: TaxonomyDB):
    resolver = TaxonomyResolver(db=taxonomy_db)
    assert resolver.genus_of_id(TAXID_RETIRED_MERGED_INTO_FRAGILIS) == "bacteroides"


# --- add_resolution / save_cache -----------------------------------------------------------


def test_add_resolution_updates_cache_and_clears_unresolved():
    resolver = TaxonomyResolver()
    resolver.resolve_name("mystery organism")
    assert "mystery organism" in resolver.unresolved

    resolver.add_resolution("Mystery Organism", 42)
    assert resolver.cache["mystery organism"] == 42
    assert "mystery organism" not in resolver.unresolved


def test_save_cache_writes_json(tmp_path):
    cache_path = tmp_path / "sub" / "cache.json"
    resolver = TaxonomyResolver(cache={"escherichia coli": 561})
    resolver.save_cache(cache_path)

    assert json.loads(cache_path.read_text()) == {"escherichia coli": 561}


def test_save_cache_noop_without_a_path():
    resolver = TaxonomyResolver(cache={"x": 1}, cache_path=None)
    resolver.save_cache()  # should not raise


def test_load_then_save_cache_round_trip(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"prevotella": 838, "unknown thing": None}))

    resolver = TaxonomyResolver.load(cache_path=cache_path)
    assert resolver.cache == {"prevotella": 838, "unknown thing": None}
    assert resolver.resolve_name("Prevotella") == 838
    assert resolver.resolve_name("unknown thing") is None


# --- resolve_name_online (network, mocked) --------------------------------------------------


@pytest.mark.network
def test_resolve_name_online_caches_result(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=NCBI_ESEARCH_URL,
        match_params={"db": "taxonomy", "term": "Escherichia coli", "retmode": "json"},
        json={"esearchresult": {"idlist": ["562"]}},
    )
    resolver = TaxonomyResolver()

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name_online("Escherichia coli", client)

    result = asyncio.run(run())
    assert result == 562
    assert resolver.cache["escherichia coli"] == 562


@pytest.mark.network
def test_resolve_name_online_no_hits_caches_none(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=NCBI_ESEARCH_URL,
        match_params={"db": "taxonomy", "term": "Nonexistent Organism", "retmode": "json"},
        json={"esearchresult": {"idlist": []}},
    )
    resolver = TaxonomyResolver()

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name_online("Nonexistent Organism", client)

    result = asyncio.run(run())
    assert result is None
    assert resolver.cache["nonexistent organism"] is None
