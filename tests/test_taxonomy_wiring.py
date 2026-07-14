"""PR-2 wiring tests: the curator's `NcbiTaxonomyResolver` and the eval
scorer's `TaxonomyResolver`, both resolving through the local `TaxonomyDB`
before (or instead of) touching the network/gold.

Builds a real (if tiny) `.duckdb` from the shared synthetic-taxdump fixture
(`taxonomy_test_support.py`) -- the same fixture `test_taxonomy_db.py` et al.
use -- rather than mocking `TaxonomyDB` itself, so these tests exercise the
actual local-first resolution path end to end. Network calls are mocked via
`pytest_httpx`; a test with no registered mock fails loudly on any real
request, which is exactly how "no network for a local hit" is proven below.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import NCBI_ESEARCH_URL, TOOL_NAME, NcbiTaxonomyResolver
from bugsigdb_curation.eval.gold import GoldExperiment, GoldSignature, GoldStudy, source_type
from bugsigdb_curation.eval.score import score_study
from bugsigdb_curation.eval.taxonomy import TaxonomyResolver
from bugsigdb_curation.taxonomy.build import build_taxonomy_db
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from bugsigdb_curation.taxonomy.paths import DB_PATH_ENV_VAR
from taxonomy_test_support import (
    TAXID_BACTEROIDES_FRAGILIS,
    TAXID_CUTIBACTERIUM_ACNES,
    TAXID_FAECALIBACTERIUM,
    TAXID_FIRMICUTES,
    TAXID_RETIRED_MERGED_INTO_FRAGILIS,
    write_synthetic_taxdump,
)


@pytest.fixture()
def built_db_path(tmp_path: Path) -> Path:
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    out_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(
        taxdump_dir, out_path, release="test", source="fixture", build_timestamp="2026-07-14T00:00:00+00:00"
    )
    return out_path


def _esearch_url(term: str, **extra: str) -> httpx.URL:
    params = {"db": "taxonomy", "term": term, "retmode": "json", "tool": TOOL_NAME, "email": DEFAULT_EMAIL, **extra}
    return httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(params)


# --- curator: local-first resolution, no network for a local hit ----------------------------


@pytest.mark.parametrize(
    ("query", "expected_tax_id"),
    [
        # The exact taxa a real `curate --smoke` run got HTTP 429s on before
        # the local TaxonomyDB was wired in (see curator/taxonomy.py's
        # module docstring).
        ("Faecalibacterium", TAXID_FAECALIBACTERIUM),
        ("g__Faecalibacterium", TAXID_FAECALIBACTERIUM),  # MetaPhlAn rank-prefix
        ("Firmicutes", TAXID_FIRMICUTES),
        ("Bacteroides fragilis", TAXID_BACTEROIDES_FRAGILIS),
        # Reclassification synonym: NCBI's own taxdump unifies these, no
        # cache seeding or network gap-fill required.
        ("Propionibacterium acnes", TAXID_CUTIBACTERIUM_ACNES),
    ],
)
def test_curator_resolves_real_taxa_locally_without_network(
    built_db_path: Path, httpx_mock: HTTPXMock, query: str, expected_tax_id: int
) -> None:
    # No httpx_mock.add_response registered at all: any real request raises.
    with TaxonomyDB(built_db_path) as db:
        resolver = NcbiTaxonomyResolver(cache_path=None, db=db)

        async def run() -> int | None:
            async with httpx.AsyncClient() as client:
                return await resolver.resolve_name(query, client=client)

        assert asyncio.run(run()) == expected_tax_id


def test_curator_verify_id_uses_local_db_too(built_db_path: Path, httpx_mock: HTTPXMock) -> None:
    """`verify_id` (S6's gate on S5b's proposed ids) goes through
    `resolve_name`, so it's local-DB-first too -- no network for a proposal
    the local DB can confirm."""
    with TaxonomyDB(built_db_path) as db:
        resolver = NcbiTaxonomyResolver(cache_path=None, db=db)

        async def run() -> bool:
            async with httpx.AsyncClient() as client:
                return await resolver.verify_id("Faecalibacterium", TAXID_FAECALIBACTERIUM, client=client)

        assert asyncio.run(run()) is True


# --- curator: gap-fill only for a genuine local-DB miss --------------------------------------


def test_curator_gap_fills_live_only_for_the_local_miss(built_db_path: Path, httpx_mock: HTTPXMock) -> None:
    """"Escherichia coli" isn't in the fixture DB -- must gap-fill via a live
    esearch. Exactly one mock is registered (for that miss); if the local
    hit ("Faecalibacterium") had *also* reached the network, pytest_httpx
    would raise on the unmocked/duplicate request instead of this passing."""
    httpx_mock.add_response(url=_esearch_url("escherichia coli"), json={"esearchresult": {"idlist": ["562"]}})

    with TaxonomyDB(built_db_path) as db:
        resolver = NcbiTaxonomyResolver(cache_path=None, db=db)

        async def run() -> tuple[int | None, int | None]:
            async with httpx.AsyncClient() as client:
                local_hit = await resolver.resolve_name("Faecalibacterium", client=client)
                gap_filled = await resolver.resolve_name("Escherichia coli", client=client)
                return local_hit, gap_filled

        local_hit, gap_filled = asyncio.run(run())

    assert local_hit == TAXID_FAECALIBACTERIUM
    assert gap_filled == 562
    # The gap-filled name is cached too, exactly like a local hit.
    assert resolver.cache["escherichia coli"] == 562


# --- curator: no DB configured -> live-only, never crashes -----------------------------------


def test_curator_load_with_no_db_found_warns_once_and_falls_back_to_live(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.load()`'s real construction path: with no `--taxonomy-db`/
    `BUGSIGDB_TAXONOMY_DB` and no cached DB (conftest.py's autouse fixture
    isolates `BUGSIGDB_CACHE_DIR` per-test, so there's nothing to find),
    resolution must not crash -- it falls back to live-only, with a
    one-time warning."""
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    httpx_mock.add_response(url=_esearch_url("faecalibacterium"), json={"esearchresult": {"idlist": ["853"]}})

    with pytest.warns(RuntimeWarning, match="no local taxonomy DB"):
        resolver = NcbiTaxonomyResolver.load(cache_path=None)

    assert resolver.db is None

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium", client=client)

    assert asyncio.run(run()) == 853


def test_curator_load_with_corrupt_db_warns_and_falls_back_to_live(
    tmp_path: Path, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 1: a corrupt/truncated `.duckdb` (or one built by an incompatible
    DuckDB version) raises `duckdb.IOException` from `TaxonomyDB.__init__`'s
    `duckdb.connect()` -- a `duckdb.Error` subclass, NOT a `ValueError`.
    Pre-fix, `_load_optional_taxonomy_db` only caught
    `(FileNotFoundError, ValueError)`, so this propagated and crashed the
    whole `curate` run instead of degrading gracefully like every other
    "no usable local DB" case."""
    bad_db = tmp_path / "corrupt.duckdb"
    bad_db.write_bytes(b"not a real duckdb file, just some garbage bytes\x00\x01\x02" * 10)
    monkeypatch.setenv("BUGSIGDB_TAXONOMY_DB", str(bad_db))
    httpx_mock.add_response(url=_esearch_url("faecalibacterium"), json={"esearchresult": {"idlist": ["853"]}})

    with pytest.warns(RuntimeWarning, match="failed to open local taxonomy DB"):
        resolver = NcbiTaxonomyResolver.load(cache_path=None)

    assert resolver.db is None  # degraded, not crashed

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium", client=client)

    assert asyncio.run(run()) == 853


def test_curator_load_with_explicit_missing_db_path_warns_with_actual_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copilot fix: an explicit `--taxonomy-db`/`BUGSIGDB_TAXONOMY_DB` path
    that doesn't exist on disk must name that actual path in the warning --
    a config/typo problem, distinct from "nothing configured at all"."""
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    missing = tmp_path / "does_not_exist.duckdb"

    with pytest.warns(RuntimeWarning, match=r"configured taxonomy DB not found at .*does_not_exist\.duckdb"):
        resolver = NcbiTaxonomyResolver.load(cache_path=None, db_path=missing)

    assert resolver.db is None


def test_curator_load_with_explicit_missing_env_db_path_warns_with_actual_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "also_missing.duckdb"
    monkeypatch.setenv(DB_PATH_ENV_VAR, str(missing))

    with pytest.warns(RuntimeWarning, match=r"configured taxonomy DB not found at .*also_missing\.duckdb"):
        resolver = NcbiTaxonomyResolver.load(cache_path=None)

    assert resolver.db is None


def test_curator_bare_constructor_with_db_none_resolves_live_only(httpx_mock: HTTPXMock) -> None:
    """A resolver directly constructed with `db=None` (the dataclass
    default) behaves identically to the pre-PR-2 live-only resolver -- no
    crash, no local DB, straight to esearch."""
    httpx_mock.add_response(url=_esearch_url("faecalibacterium"), json={"esearchresult": {"idlist": ["853"]}})
    resolver = NcbiTaxonomyResolver(cache_path=None)
    assert resolver.db is None

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium", client=client)

    assert asyncio.run(run()) == 853


# --- scorer: predictions resolve via TaxonomyDB, no taxa.csv involved ------------------------


def _gold_signature(taxa: frozenset[int]) -> GoldSignature:
    return GoldSignature(
        signature_id="bsdb:1/1/1",
        experiment_id="1/Experiment 1",
        source="table 1",
        source_type=source_type("table 1"),
        direction="increased",
        taxa=taxa,
        curation_state="Complete",
    )


def _gold_study(signatures: tuple[GoldSignature, ...]) -> GoldStudy:
    exp = GoldExperiment(
        experiment_id="1/Experiment 1",
        study_id="1",
        experiment_name="Experiment 1",
        location_of_subjects=(),
        host_species="Homo sapiens",
        body_site=("Feces",),
        uberon_id=None,
        condition=("CRC",),
        efo_id=None,
        group_0_name="healthy",
        group_1_name="CRC",
        group_1_definition=None,
        group_0_sample_size=None,
        group_1_sample_size=None,
        sequencing_type="16S",
        statistical_test=(),
        mht_correction=None,
        signatures=signatures,
    )
    return GoldStudy(
        study_id="1",
        pmid="1",
        doi=None,
        title=None,
        journal=None,
        year=None,
        study_design=(),
        pmcid=None,
        has_pmc=True,
        experiments=(exp,),
    )


def test_scorer_resolves_prediction_names_via_taxonomy_db_no_taxa_csv(built_db_path: Path) -> None:
    """`TaxonomyResolver` has no `taxa_csv`/`seed` concept at all any more
    (PR-2) -- predicted taxon *names* resolve entirely through the local
    `TaxonomyDB`, while `score_study`'s gold taxid sets still come from
    (gold-derived, already-resolved) `GoldSignature.taxa` ints, never from a
    name lookup. See `test_scorer_load_with_no_taxa_csv_parameter` below for
    the constructor-signature half of that guarantee."""
    with TaxonomyDB(built_db_path) as db:
        resolver = TaxonomyResolver(db=db)
        gold = _gold_study((_gold_signature(frozenset({TAXID_FAECALIBACTERIUM, TAXID_BACTEROIDES_FRAGILIS})),))
        pred = {
            "experiments": [
                {
                    "body_site": ["Feces"],
                    "condition": ["CRC"],
                    "group_0_name": "healthy",
                    "group_1_name": "CRC",
                    "sequencing_type": "16S",
                    "signatures": [
                        {
                            "abundance_in_group_1": "increased",
                            # Predicted by NAME only (no ncbi_id) -- must
                            # resolve via the local DB, not gold.
                            "taxa": [{"taxon_name": "Faecalibacterium"}, {"taxon_name": "Bacteroides fragilis"}],
                        }
                    ],
                }
            ]
        }

        result = score_study(gold, pred, resolver)

    assert result.micro_taxa.precision == 1.0
    assert result.micro_taxa.recall == 1.0
    assert result.micro_taxa.f1 == 1.0


def test_scorer_canonicalizes_retired_gold_id_end_to_end(built_db_path: Path) -> None:
    """The full retired-gold-id story, end to end through the real scorer:
    gold curated a since-retired tax_id (999, merged into Bacteroides
    fragilis's current 817 -- see `taxonomy_test_support.MERGED`), the
    de-novo curator predicted the CURRENT id (817, since name resolution
    against `names.dmp` always lands on the current id) -- without
    canonicalization these would score as a full miss (FN + FP); with it,
    `score_study` must canonicalize both sides and score a perfect match."""
    with TaxonomyDB(built_db_path) as db:
        resolver = TaxonomyResolver(db=db)
        gold = _gold_study((_gold_signature(frozenset({TAXID_RETIRED_MERGED_INTO_FRAGILIS})),))
        pred = {
            "experiments": [
                {
                    "body_site": ["Feces"],
                    "condition": ["CRC"],
                    "group_0_name": "healthy",
                    "group_1_name": "CRC",
                    "sequencing_type": "16S",
                    "signatures": [
                        {"abundance_in_group_1": "increased", "taxa": [{"ncbi_id": TAXID_BACTEROIDES_FRAGILIS}]}
                    ],
                }
            ]
        }

        result = score_study(gold, pred, resolver)

    assert result.micro_taxa.f1 == 1.0
    assert result.micro_taxa.tp == 1
    assert result.micro_taxa.fp == 0
    assert result.micro_taxa.fn == 0


def test_scorer_load_with_no_taxa_csv_parameter() -> None:
    """`TaxonomyResolver.load()`'s signature carries no `taxa_csv` parameter
    at all (the seed map it used to build from gold is gone)."""
    import inspect

    params = set(inspect.signature(TaxonomyResolver.load).parameters)
    assert "taxa_csv" not in params
    assert "seed" not in params


def test_scorer_load_with_corrupt_db_warns_and_disables_local_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 1, eval side: same corrupt-`.duckdb` scenario as
    `test_curator_load_with_corrupt_db_warns_and_falls_back_to_live`, but
    for `TaxonomyResolver.load()` -- pre-fix this raised `duckdb.IOException`
    (uncaught by the old `except (FileNotFoundError, ValueError)`) and
    crashed `eval score` outright. There's no live-network fallback on this
    side (see module docstring), so "graceful" here means offline
    resolution degrades to always-miss (with a one-time warning) rather
    than the whole scoring run crashing."""
    bad_db = tmp_path / "corrupt.duckdb"
    bad_db.write_bytes(b"not a real duckdb file, just some garbage bytes\x00\x01\x02" * 10)
    monkeypatch.setenv("BUGSIGDB_TAXONOMY_DB", str(bad_db))

    with pytest.warns(RuntimeWarning, match="failed to open local taxonomy DB"):
        resolver = TaxonomyResolver.load(cache_path=None)

    assert resolver.db is None  # degraded, not crashed
    assert resolver.resolve_name("Faecalibacterium") is None


# --- close(): a resolver owns and can close its local TaxonomyDB (Fix 4) ---------------------


def test_curator_resolver_close_closes_the_local_db(built_db_path: Path) -> None:
    db = TaxonomyDB(built_db_path)
    resolver = NcbiTaxonomyResolver(cache_path=None, db=db)
    resolver.close()
    assert db._closed is True
    resolver.close()  # idempotent -- a second close() must not raise


def test_curator_resolver_close_is_a_noop_with_no_db() -> None:
    resolver = NcbiTaxonomyResolver(cache_path=None)
    assert resolver.db is None
    resolver.close()  # must not raise


def test_scorer_resolver_close_closes_the_local_db(built_db_path: Path) -> None:
    db = TaxonomyDB(built_db_path)
    resolver = TaxonomyResolver(db=db)
    resolver.close()
    assert db._closed is True
    resolver.close()  # idempotent -- a second close() must not raise


def test_scorer_resolver_close_is_a_noop_with_no_db() -> None:
    resolver = TaxonomyResolver()
    assert resolver.db is None
    resolver.close()  # must not raise
