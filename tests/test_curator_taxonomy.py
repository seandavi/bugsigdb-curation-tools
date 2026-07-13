"""Unit tests for `bugsigdb_curation.curator.taxonomy` (S6, general-authority resolver).

Mocks NCBI esearch via `pytest_httpx`. This resolver must never read any
gold/corpus file -- there is no `taxa_csv`/seed constructor arg at all
(unlike `bugsigdb_curation.eval.taxonomy.TaxonomyResolver`, deliberately).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.taxonomy import (
    NCBI_ESEARCH_URL,
    NcbiTaxonomyResolver,
    normalize_taxon_name,
)


def test_normalize_taxon_name_strips_rank_prefix_and_underscores():
    assert normalize_taxon_name("g__Faecalibacterium") == "faecalibacterium"
    assert normalize_taxon_name("s_Escherichia_coli") == "escherichia coli"
    assert normalize_taxon_name("  Bacteroides fragilis  ") == "bacteroides fragilis"


def test_resolve_name_hits_live_esearch_and_caches(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(
            {"db": "taxonomy", "term": "Faecalibacterium prausnitzii", "retmode": "json"}
        ),
        json={"esearchresult": {"idlist": ["853"]}},
    )
    resolver = NcbiTaxonomyResolver(cache_path=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    result = asyncio.run(run())
    assert result == 853
    assert resolver.cache["faecalibacterium prausnitzii"] == 853
    assert "faecalibacterium prausnitzii" not in resolver.unresolved


def test_resolve_name_returns_none_and_marks_unresolved_when_no_hit(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"esearchresult": {"idlist": []}})
    resolver = NcbiTaxonomyResolver(cache_path=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Nonexistentia madeuppii", client=client)

    result = asyncio.run(run())
    assert result is None
    assert "nonexistentia madeuppii" in resolver.unresolved


def test_resolve_name_cache_hit_skips_network(httpx_mock: HTTPXMock):
    # No httpx_mock.add_response registered: any real request would raise.
    resolver = NcbiTaxonomyResolver(cache={"faecalibacterium prausnitzii": 853}, cache_path=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    assert asyncio.run(run()) == 853


def test_cached_none_is_confirmed_unresolved_not_a_fresh_lookup(httpx_mock: HTTPXMock):
    resolver = NcbiTaxonomyResolver(cache={"madeuppia": None}, cache_path=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Madeuppia", client=client)

    assert asyncio.run(run()) is None
    assert "madeuppia" in resolver.unresolved


# --- verify_id (S6's gate on S5b's proposed ids) --------------------------------------------


def test_verify_id_accepts_when_authority_confirms(httpx_mock: HTTPXMock):
    resolver = NcbiTaxonomyResolver(cache={"faecalibacterium prausnitzii": 853}, cache_path=None)

    async def run() -> bool:
        async with httpx.AsyncClient() as client:
            return await resolver.verify_id("Faecalibacterium prausnitzii", 853, client=client)

    assert asyncio.run(run()) is True


def test_verify_id_rejects_when_authority_disagrees(httpx_mock: HTTPXMock):
    resolver = NcbiTaxonomyResolver(cache={"faecalibacterium prausnitzii": 853}, cache_path=None)

    async def run() -> bool:
        async with httpx.AsyncClient() as client:
            # LLM proposed a fabricated/wrong id -- must be rejected.
            return await resolver.verify_id("Faecalibacterium prausnitzii", 999999, client=client)

    assert asyncio.run(run()) is False


def test_verify_id_rejects_unresolvable_name(httpx_mock: HTTPXMock):
    resolver = NcbiTaxonomyResolver(cache={"madeuppia": None}, cache_path=None)

    async def run() -> bool:
        async with httpx.AsyncClient() as client:
            return await resolver.verify_id("Madeuppia", 12345, client=client)

    assert asyncio.run(run()) is False


def test_verify_id_rejects_non_numeric_proposed_id(httpx_mock: HTTPXMock):
    resolver = NcbiTaxonomyResolver(cache={"faecalibacterium prausnitzii": 853}, cache_path=None)

    async def run() -> bool:
        async with httpx.AsyncClient() as client:
            return await resolver.verify_id("Faecalibacterium prausnitzii", "not-a-number", client=client)

    assert asyncio.run(run()) is False


# --- load / save_cache ------------------------------------------------------------------------


def test_load_reads_existing_cache_file(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"faecalibacterium prausnitzii": 853, "madeuppia": None}))

    resolver = NcbiTaxonomyResolver.load(cache_path=cache_path)

    assert resolver.cache == {"faecalibacterium prausnitzii": 853, "madeuppia": None}


def test_load_missing_cache_file_yields_empty_cache(tmp_path: Path):
    resolver = NcbiTaxonomyResolver.load(cache_path=tmp_path / "does_not_exist.json")
    assert resolver.cache == {}


def test_save_cache_round_trips(tmp_path: Path):
    cache_path = tmp_path / "sub" / "cache.json"
    resolver = NcbiTaxonomyResolver(cache={"faecalibacterium prausnitzii": 853}, cache_path=cache_path)

    resolver.save_cache()

    reloaded = NcbiTaxonomyResolver.load(cache_path=cache_path)
    assert reloaded.cache == {"faecalibacterium prausnitzii": 853}


def test_save_cache_is_noop_without_a_path():
    resolver = NcbiTaxonomyResolver(cache={"x": 1}, cache_path=None)
    resolver.save_cache()  # must not raise
