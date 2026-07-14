"""Unit tests for `bugsigdb_curation.curator.reconcile` (S6-reconcile, split A1's
deterministic TaxonomyDB resolution + model-gated disambiguation).

Uses the shared synthetic-taxdump fixture (`tests/taxonomy_test_support.py`,
also used by `test_taxonomy_db.py`/`test_curator_pipeline_e2e.py`): a real
built `TaxonomyDB` wired into a `NcbiTaxonomyResolver`, entirely offline
except where a test explicitly exercises the live gap-fill path (mocked via
`httpx_mock`, same pattern as `test_curator_taxonomy.py`).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.ner import NamedTaxon
from bugsigdb_curation.curator.reconcile import reconcile_names, resolve_one_name
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import NCBI_ESEARCH_URL, NcbiTaxonomyResolver
from bugsigdb_curation.taxonomy.build import build_taxonomy_db
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from taxonomy_test_support import (
    TAXID_BACTEROIDES_FRAGILIS,
    TAXID_MORGANELLA_A,
    TAXID_MORGANELLA_B,
    write_synthetic_taxdump,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def taxonomy_db(tmp_path) -> TaxonomyDB:
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    db_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(taxdump_dir, db_path, release="test", source="fixture", build_timestamp="2026-07-14T00:00:00+00:00")
    db = TaxonomyDB(db_path)
    yield db
    db.close()


def _resolver(db: TaxonomyDB) -> NcbiTaxonomyResolver:
    return NcbiTaxonomyResolver(cache={}, cache_path=None, db=db)


# --- resolve_one_name --------------------------------------------------------------------------


def test_resolve_one_name_uses_local_db_for_unambiguous_hit_no_network(taxonomy_db: TaxonomyDB):
    resolver = _resolver(taxonomy_db)
    model = MockModel()  # never called: no ambiguity, no gap-fill

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name(
                "Bacteroides fragilis", model=model, resolver=resolver, client=client, source_context=""
            )

    tax_id, disambiguated = _run(run())
    assert tax_id == TAXID_BACTEROIDES_FRAGILIS
    assert disambiguated is False
    assert model.calls == []


def test_resolve_one_name_disambiguates_ambiguous_homonym_using_source_context(taxonomy_db: TaxonomyDB):
    """"Morganella" is a true homonym in the fixture (two distinct tax_ids,
    500 and 600) -- resolution must go through one disambiguation model call
    constrained to those two candidates."""
    resolver = _resolver(taxonomy_db)
    model = MockModel(responses={"taxon_disambiguate": {"chosen_tax_id": TAXID_MORGANELLA_B}})

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name(
                "Morganella", model=model, resolver=resolver, client=client, source_context="host_species: Homo sapiens"
            )

    tax_id, disambiguated = _run(run())
    assert tax_id == TAXID_MORGANELLA_B
    assert disambiguated is True
    assert len(model.calls) == 1
    prompt_text = model.calls[0]["messages"][0]["content"][0]["text"]
    assert str(TAXID_MORGANELLA_A) in prompt_text
    assert str(TAXID_MORGANELLA_B) in prompt_text
    assert "Homo sapiens" in prompt_text


def test_resolve_one_name_disambiguation_never_guesses_outside_candidates(taxonomy_db: TaxonomyDB):
    """A disambiguation response outside the candidate set is rejected --
    "never guess" applies here exactly as it does to fused-lean's id
    verification gate."""
    resolver = _resolver(taxonomy_db)
    model = MockModel(responses={"taxon_disambiguate": {"chosen_tax_id": 999999}})

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name("Morganella", model=model, resolver=resolver, client=client, source_context="")

    tax_id, disambiguated = _run(run())
    assert tax_id is None
    assert disambiguated is True


def test_resolve_one_name_disambiguation_declines_stays_unresolved(taxonomy_db: TaxonomyDB):
    resolver = _resolver(taxonomy_db)
    model = MockModel(responses={"taxon_disambiguate": {"chosen_tax_id": None}})

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name("Morganella", model=model, resolver=resolver, client=client, source_context="")

    tax_id, _disambiguated = _run(run())
    assert tax_id is None


def test_resolve_one_name_falls_through_to_live_gapfill_on_local_miss(taxonomy_db: TaxonomyDB, httpx_mock: HTTPXMock):
    resolver = _resolver(taxonomy_db)
    model = MockModel()  # not called: no ambiguity involved
    httpx_mock.add_response(
        url=httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(
            {"db": "taxonomy", "term": "some novel taxon", "retmode": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"esearchresult": {"idlist": ["424242"]}},
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name(
                "Some Novel Taxon", model=model, resolver=resolver, client=client, source_context=""
            )

    tax_id, disambiguated = _run(run())
    assert tax_id == 424242
    assert disambiguated is False


def test_resolve_one_name_ambiguous_disambiguation_is_not_cached_across_contexts(taxonomy_db: TaxonomyDB):
    """Homonym disambiguation depends on `source_context` (per-experiment
    host/body-site/lineage cues), which differs call to call -- so its
    outcome must never be written into the shared `resolver.cache`/
    `.unresolved`. Two calls with the SAME resolver but DIFFERENT
    source_context must each issue their own disambiguation model call
    (never served from a cached first-caller decision) and may legitimately
    land on different tax_ids."""
    resolver = _resolver(taxonomy_db)
    model = MockModel(
        responses={
            "taxon_disambiguate": lambda messages: {
                "chosen_tax_id": TAXID_MORGANELLA_A
                if "gut" in messages[0]["content"][0]["text"]
                else TAXID_MORGANELLA_B
            }
        }
    )

    async def run():
        async with httpx.AsyncClient() as client:
            first = await resolve_one_name(
                "Morganella", model=model, resolver=resolver, client=client, source_context="body_site: gut"
            )
            second = await resolve_one_name(
                "Morganella", model=model, resolver=resolver, client=client, source_context="body_site: skin"
            )
            return first, second

    (tax_id_a, disambiguated_a), (tax_id_b, disambiguated_b) = _run(run())

    assert tax_id_a == TAXID_MORGANELLA_A
    assert tax_id_b == TAXID_MORGANELLA_B
    assert disambiguated_a is True
    assert disambiguated_b is True
    disambiguation_calls = [c for c in model.calls if c["stage"] == "taxon_disambiguate"]
    assert len(disambiguation_calls) == 2  # the second call was NOT served from a shared-resolver cache hit

    # Neither the chosen id nor an "unresolved" marker for the declined-
    # elsewhere case may leak into the shared, batch-wide/persisted resolver
    # state -- that decision is local to (name, source_context).
    assert "morganella" not in resolver.cache
    assert "morganella" not in resolver.unresolved


def test_resolve_one_name_declined_disambiguation_does_not_pollute_unresolved(taxonomy_db: TaxonomyDB):
    """A declined disambiguation (`chosen_tax_id: null`) means "undecidable
    in this context", not "confirmed no hit anywhere" -- `resolver.unresolved`
    (per `curator.taxonomy`'s docstring) is reserved for the latter, so a
    decline must not be added to it."""
    resolver = _resolver(taxonomy_db)
    model = MockModel(responses={"taxon_disambiguate": {"chosen_tax_id": None}})

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name("Morganella", model=model, resolver=resolver, client=client, source_context="")

    tax_id, _disambiguated = _run(run())

    assert tax_id is None
    assert "morganella" not in resolver.unresolved
    assert "morganella" not in resolver.cache


def test_resolve_one_name_uses_cache_before_db_or_network(taxonomy_db: TaxonomyDB):
    resolver = NcbiTaxonomyResolver(cache={"bacteroides fragilis": 12345}, cache_path=None, db=taxonomy_db)
    model = MockModel()

    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve_one_name(
                "Bacteroides fragilis", model=model, resolver=resolver, client=client, source_context=""
            )

    tax_id, _ = _run(run())
    assert tax_id == 12345  # cache wins even though the local DB has a different id for this name


# --- reconcile_names ----------------------------------------------------------------------------


def test_reconcile_names_groups_by_direction_and_resolves_via_authority(
    taxonomy_db: TaxonomyDB, httpx_mock: HTTPXMock
):
    resolver = _resolver(taxonomy_db)
    model = MockModel()
    # "Some Novel Taxon" is a local-DB miss -> live gap-fill; mock an empty
    # esearch hit so the unresolved-but-kept path is deterministic/offline
    # (no real NCBI network call in a unit test).
    httpx_mock.add_response(
        url=httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(
            {"db": "taxonomy", "term": "some novel taxon", "retmode": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"esearchresult": {"idlist": []}},
    )
    names = [
        NamedTaxon(name="Bacteroides fragilis", direction="increased"),
        NamedTaxon(name="Some Novel Taxon", direction="decreased"),
    ]

    async def run():
        async with httpx.AsyncClient() as client:
            return await reconcile_names(names, model=model, resolver=resolver, client=client, source_context="")

    signatures = _run(run())

    assert {s.direction for s in signatures} == {"increased", "decreased"}
    increased = next(s for s in signatures if s.direction == "increased")
    assert increased.taxa[0].taxon_name == "Bacteroides fragilis"
    assert increased.taxa[0].ncbi_id == TAXID_BACTEROIDES_FRAGILIS
    decreased = next(s for s in signatures if s.direction == "decreased")
    assert decreased.taxa[0].ncbi_id is None  # unresolved, kept by name -- never dropped
    assert decreased.taxa[0].taxon_name == "Some Novel Taxon"


def test_reconcile_names_dispatches_disambiguation_only_for_the_ambiguous_name(taxonomy_db: TaxonomyDB):
    resolver = _resolver(taxonomy_db)
    model = MockModel(responses={"taxon_disambiguate": {"chosen_tax_id": TAXID_MORGANELLA_A}})
    names = [
        NamedTaxon(name="Bacteroides fragilis", direction="increased"),  # unambiguous
        NamedTaxon(name="Morganella", direction="increased"),  # ambiguous
    ]

    async def run():
        async with httpx.AsyncClient() as client:
            return await reconcile_names(names, model=model, resolver=resolver, client=client, source_context="")

    signatures = _run(run())

    assert len(model.calls) == 1  # only the ambiguous name triggered a disambiguation call
    assert model.calls[0]["stage"] == "taxon_disambiguate"
    taxa_by_name = {t.taxon_name: t.ncbi_id for sig in signatures for t in sig.taxa}
    assert taxa_by_name["Bacteroides fragilis"] == TAXID_BACTEROIDES_FRAGILIS
    assert taxa_by_name["Morganella"] == TAXID_MORGANELLA_A
