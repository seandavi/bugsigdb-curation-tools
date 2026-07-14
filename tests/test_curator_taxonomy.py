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
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import (
    DEFAULT_MIN_INTERVAL_NO_KEY,
    DEFAULT_MIN_INTERVAL_WITH_KEY,
    NCBI_ESEARCH_URL,
    TOOL_NAME,
    NcbiTaxonomyResolver,
    _NCBI_KEY_ENV_NAMES,
    _RateLimiter,
    normalize_taxon_name,
    resolve_ncbi_api_key,
)

#: Every real esearch request carries these etiquette params regardless of
#: an API key -- baked into every mock URL below via `_esearch_url`.
_ETIQUETTE_PARAMS = {"tool": TOOL_NAME, "email": DEFAULT_EMAIL}


def _esearch_url(term: str, **extra: str) -> httpx.URL:
    params = {"db": "taxonomy", "term": term, "retmode": "json", **_ETIQUETTE_PARAMS, **extra}
    return httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(params)


def test_normalize_taxon_name_strips_rank_prefix_and_underscores():
    assert normalize_taxon_name("g__Faecalibacterium") == "faecalibacterium"
    assert normalize_taxon_name("s_Escherichia_coli") == "escherichia coli"
    assert normalize_taxon_name("  Bacteroides fragilis  ") == "bacteroides fragilis"


def test_resolve_name_hits_live_esearch_and_caches(httpx_mock: HTTPXMock):
    # The esearch query uses the *normalized* (lowercased) name, matching
    # the cache key -- see test_resolve_name_uses_normalized_query below.
    httpx_mock.add_response(
        url=_esearch_url("faecalibacterium prausnitzii"),
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


def test_resolve_name_uses_normalized_query_not_raw_rank_prefixed_name(httpx_mock: HTTPXMock):
    """Regression test: `resolve_name` must send the *normalized* name
    (rank-prefix stripped, underscores -> spaces, lowercased -- same as the
    cache key) to esearch, not the raw name. Pre-fix, a rank-prefixed input
    like "g__Faecalibacterium" (the exact form this module's docstring and
    MetaPhlAn/LEfSe cite) was sent to esearch verbatim, including the
    "g__" prefix, and so failed to resolve even when NCBI has the taxon."""
    httpx_mock.add_response(
        url=_esearch_url("faecalibacterium"),
        json={"esearchresult": {"idlist": ["853"]}},
    )
    resolver = NcbiTaxonomyResolver(cache_path=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("g__Faecalibacterium", client=client)

    result = asyncio.run(run())
    assert result == 853
    # The mocked response only matches a query term of "faecalibacterium"
    # (normalized) -- if the raw "g__Faecalibacterium" had been sent, no
    # registered mock would match and pytest_httpx would raise instead.


def test_resolve_name_returns_none_and_marks_unresolved_when_no_hit(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_esearch_url("nonexistentia madeuppii"), json={"esearchresult": {"idlist": []}})
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


def test_load_reads_existing_cache_file(tmp_path: Path, monkeypatch):
    # Hermetic against whatever real NCBI_API_KEY/.env this machine happens
    # to have -- irrelevant to this test's assertion (only `.cache`), but
    # neutralized anyway so it can't flake between environments.
    monkeypatch.setattr("bugsigdb_curation.curator.taxonomy.load_dotenv", lambda *a, **k: None)
    for name in _NCBI_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"faecalibacterium prausnitzii": 853, "madeuppia": None}))

    resolver = NcbiTaxonomyResolver.load(cache_path=cache_path)

    assert resolver.cache == {"faecalibacterium prausnitzii": 853, "madeuppia": None}


def test_load_missing_cache_file_yields_empty_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("bugsigdb_curation.curator.taxonomy.load_dotenv", lambda *a, **k: None)
    for name in _NCBI_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    resolver = NcbiTaxonomyResolver.load(cache_path=tmp_path / "does_not_exist.json")
    assert resolver.cache == {}


def test_load_resolves_api_key_from_environment_by_default(tmp_path: Path, monkeypatch):
    """`.load()` -- the real construction path `curate`/`curate_async` uses --
    auto-resolves `NCBI_API_KEY` from the environment; a bare
    `NcbiTaxonomyResolver(...)` constructor call does not (see its
    docstring) -- that's what every other test in this file relies on to
    stay hermetic without having to neutralize `.env` itself."""
    monkeypatch.setattr("bugsigdb_curation.curator.taxonomy.load_dotenv", lambda *a, **k: None)
    for name in _NCBI_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NCBI_API_KEY", "env-resolved-key")

    resolver = NcbiTaxonomyResolver.load(cache_path=tmp_path / "does_not_exist.json")

    assert resolver.api_key == "env-resolved-key"


def test_load_explicit_api_key_overrides_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("bugsigdb_curation.curator.taxonomy.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("NCBI_API_KEY", "env-key")

    resolver = NcbiTaxonomyResolver.load(cache_path=tmp_path / "does_not_exist.json", api_key="explicit-key")

    assert resolver.api_key == "explicit-key"


def test_save_cache_round_trips(tmp_path: Path):
    cache_path = tmp_path / "sub" / "cache.json"
    resolver = NcbiTaxonomyResolver(cache={"faecalibacterium prausnitzii": 853}, cache_path=cache_path)

    resolver.save_cache()

    reloaded = NcbiTaxonomyResolver.load(cache_path=cache_path)
    assert reloaded.cache == {"faecalibacterium prausnitzii": 853}


def test_save_cache_is_noop_without_a_path():
    resolver = NcbiTaxonomyResolver(cache={"x": 1}, cache_path=None)
    resolver.save_cache()  # must not raise


# --- etiquette params (tool/email/api_key) on live esearch calls ------------------------------


def test_esearch_includes_api_key_when_present(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=_esearch_url("faecalibacterium prausnitzii", api_key="test-ncbi-key"),
        json={"esearchresult": {"idlist": ["853"]}},
    )
    resolver = NcbiTaxonomyResolver(cache_path=None, api_key="test-ncbi-key")

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    assert asyncio.run(run()) == 853
    # If `api_key` had NOT been sent, this mock (which requires it) would
    # not match and pytest_httpx would raise instead of returning 853.


def test_esearch_omits_api_key_when_absent(httpx_mock: HTTPXMock):
    # `_esearch_url` here has no `api_key` -- a request carrying one
    # wouldn't match, and pytest_httpx would raise.
    httpx_mock.add_response(
        url=_esearch_url("faecalibacterium prausnitzii"),
        json={"esearchresult": {"idlist": ["853"]}},
    )
    resolver = NcbiTaxonomyResolver(cache_path=None, api_key=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    assert asyncio.run(run()) == 853


# --- rate limiter -------------------------------------------------------------------------------


def test_rate_limiter_enforces_min_interval_spacing():
    """A run of `acquire()` calls must never be spaced less than
    `min_interval` apart. Uses a fake clock/sleep (no real waiting) so the
    test asserts on requested wait durations directly."""
    fake_now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return fake_now

    async def sleep(seconds: float) -> None:
        nonlocal fake_now
        sleeps.append(seconds)
        fake_now += seconds  # simulate time passing while "asleep"

    limiter = _RateLimiter(min_interval=0.35, clock=clock, sleep=sleep)

    async def run() -> None:
        nonlocal fake_now
        await limiter.acquire()  # first call: nothing elapsed yet, no wait
        fake_now += 0.05  # only 0.05s passed before the next call
        await limiter.acquire()  # must wait out the remaining 0.30s
        fake_now += 0.35  # a full interval passed -- no wait needed
        await limiter.acquire()

    asyncio.run(run())

    assert sleeps == [pytest.approx(0.30)]


def test_rate_limiter_default_interval_no_key_vs_with_key():
    assert DEFAULT_MIN_INTERVAL_NO_KEY > DEFAULT_MIN_INTERVAL_WITH_KEY
    # Safety margin under NCBI's documented ~3 req/s / ~10 req/s caps.
    assert DEFAULT_MIN_INTERVAL_NO_KEY >= 1 / 3
    assert DEFAULT_MIN_INTERVAL_WITH_KEY >= 1 / 10


def test_resolver_uses_tighter_interval_when_api_key_present():
    no_key = NcbiTaxonomyResolver(cache_path=None)
    with_key = NcbiTaxonomyResolver(cache_path=None, api_key="a-key")

    assert no_key.rate_limiter is not None
    assert with_key.rate_limiter is not None
    assert no_key.rate_limiter.min_interval == DEFAULT_MIN_INTERVAL_NO_KEY
    assert with_key.rate_limiter.min_interval == DEFAULT_MIN_INTERVAL_WITH_KEY
    assert with_key.rate_limiter.min_interval < no_key.rate_limiter.min_interval


def test_resolve_name_serializes_concurrent_calls_through_the_shared_limiter(httpx_mock: HTTPXMock):
    """Two concurrent `resolve_name` calls on the same resolver must still
    only ever issue esearch requests `min_interval` apart -- proving the
    limiter is actually shared/serializing, not per-call."""
    httpx_mock.add_response(url=_esearch_url("taxon a"), json={"esearchresult": {"idlist": ["1"]}})
    httpx_mock.add_response(url=_esearch_url("taxon b"), json={"esearchresult": {"idlist": ["2"]}})

    fake_now = 0.0
    acquire_times: list[float] = []

    def clock() -> float:
        return fake_now

    async def sleep(seconds: float) -> None:
        nonlocal fake_now
        fake_now += seconds

    limiter = _RateLimiter(min_interval=0.35, clock=clock, sleep=sleep)
    resolver = NcbiTaxonomyResolver(cache_path=None, rate_limiter=limiter)

    real_acquire = limiter.acquire

    async def tracking_acquire() -> None:
        await real_acquire()
        acquire_times.append(fake_now)

    limiter.acquire = tracking_acquire  # type: ignore[method-assign]

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                resolver.resolve_name("taxon a", client=client),
                resolver.resolve_name("taxon b", client=client),
            )

    asyncio.run(run())

    assert len(acquire_times) == 2
    assert acquire_times[1] - acquire_times[0] >= 0.35 - 1e-9


# --- 429/5xx backoff + retry ---------------------------------------------------------------------


def test_resolve_name_retries_429_then_succeeds(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_esearch_url("faecalibacterium prausnitzii"), status_code=429)
    httpx_mock.add_response(
        url=_esearch_url("faecalibacterium prausnitzii"),
        json={"esearchresult": {"idlist": ["853"]}},
    )

    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    limiter = _RateLimiter(min_interval=0.0, sleep=fast_sleep)
    resolver = NcbiTaxonomyResolver(cache_path=None, rate_limiter=limiter, retry_base_delay=0.01)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    result = asyncio.run(run())

    assert result == 853
    assert resolver.cache["faecalibacterium prausnitzii"] == 853
    # One backoff sleep happened between the 429 and the successful retry.
    assert 0.01 in sleeps


def test_resolve_name_persistent_429_ends_unresolved_without_crashing(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(url=_esearch_url("faecalibacterium prausnitzii"), status_code=429)

    limiter = _RateLimiter(min_interval=0.0, sleep=lambda s: asyncio.sleep(0))
    resolver = NcbiTaxonomyResolver(
        cache_path=None, rate_limiter=limiter, max_attempts=3, retry_base_delay=0.001
    )

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    result = asyncio.run(run())

    assert result is None
    # Never guesses AND never poisons the cache with a false negative --
    # a transient rate-limit exhaustion is not NCBI confirming "no hit",
    # so a later call should still be free to retry the network.
    assert "faecalibacterium prausnitzii" not in resolver.cache
    assert "faecalibacterium prausnitzii" not in resolver.unresolved


def test_resolve_name_retries_5xx_like_429(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_esearch_url("faecalibacterium prausnitzii"), status_code=503)
    httpx_mock.add_response(
        url=_esearch_url("faecalibacterium prausnitzii"),
        json={"esearchresult": {"idlist": ["853"]}},
    )
    limiter = _RateLimiter(min_interval=0.0, sleep=lambda s: asyncio.sleep(0))
    resolver = NcbiTaxonomyResolver(cache_path=None, rate_limiter=limiter, retry_base_delay=0.001)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    assert asyncio.run(run()) == 853


def test_resolve_name_propagates_non_retryable_http_error(httpx_mock: HTTPXMock):
    """A genuine unexpected error (not 429/5xx) must still surface -- the
    retry/backoff machinery only absorbs the rate-limiting condition."""
    httpx_mock.add_response(url=_esearch_url("faecalibacterium prausnitzii"), status_code=400)
    resolver = NcbiTaxonomyResolver(cache_path=None)

    async def run() -> int | None:
        async with httpx.AsyncClient() as client:
            return await resolver.resolve_name("Faecalibacterium prausnitzii", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


# --- resolve_ncbi_api_key ------------------------------------------------------------------------


def test_resolve_ncbi_api_key_checks_names_in_priority_order(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # Mirrors `test_resolve_google_api_key_checks_names_in_priority_order`
    # (test_curator_model.py): neutralize `load_dotenv` so this test is
    # hermetic against whatever real `.env` a given machine/CI has checked
    # out, rather than depending on none existing.
    monkeypatch.setattr("bugsigdb_curation.curator.taxonomy.load_dotenv", lambda *a, **k: None)
    for name in _NCBI_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    assert resolve_ncbi_api_key() is None

    monkeypatch.setenv("NCBI_EUTILS_API_KEY", "fallback-key")
    assert resolve_ncbi_api_key() == "fallback-key"

    monkeypatch.setenv("NCBI_API_KEY", "canonical-key")
    assert resolve_ncbi_api_key() == "canonical-key"
