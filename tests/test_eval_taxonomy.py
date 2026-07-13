"""Unit tests for bugsigdb_curation.eval.taxonomy -- the name->taxid resolver."""

from __future__ import annotations

import asyncio
import csv
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


def _write_taxa_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ncbi_id", "taxon_name"])
        writer.writerows(rows)


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


# --- TaxonomyResolver.load / seed map -----------------------------------------------------


def test_load_builds_seed_map_from_taxa_csv(tmp_path):
    taxa_csv = tmp_path / "taxa.csv"
    _write_taxa_csv(taxa_csv, [("561", "Escherichia coli"), ("620", "Shigella")])

    resolver = TaxonomyResolver.load(taxa_csv=taxa_csv, cache_path=None)

    assert resolver.resolve_name("Escherichia coli") == 561
    assert resolver.resolve_name("g__Shigella") == 620  # normalization applies


def test_load_with_missing_taxa_csv_yields_empty_seed(tmp_path):
    resolver = TaxonomyResolver.load(taxa_csv=tmp_path / "nope.csv", cache_path=None)
    assert resolver.seed == {}


def test_load_with_no_cache_path_yields_empty_cache():
    resolver = TaxonomyResolver.load(taxa_csv=None, cache_path=None)
    assert resolver.cache == {}
    assert resolver.cache_path is None


# --- resolve_name: cache priority + unresolved tracking ------------------------------------


def test_resolve_name_cache_hit_before_seed():
    resolver = TaxonomyResolver(seed={"lactobacillus": 100}, cache={"lactobacillus": 999})
    assert resolver.resolve_name("Lactobacillus") == 999


def test_resolve_name_falls_back_to_seed():
    resolver = TaxonomyResolver(seed={"lactobacillus": 100}, cache={})
    assert resolver.resolve_name("Lactobacillus") == 100


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


def test_resolve_taxon_resolves_taxon_name_when_no_id():
    resolver = TaxonomyResolver(seed={"escherichia coli": 561})
    assert resolver.resolve_taxon({"taxon_name": "Escherichia coli"}) == 561


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

    resolver = TaxonomyResolver.load(taxa_csv=None, cache_path=cache_path)
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
