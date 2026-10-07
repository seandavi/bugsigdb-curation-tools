"""Unit tests for `bugsigdb_curation.curator.resolve` (S0).

Mocks the live idconv HTTP call via `pytest_httpx` -- never reads
`data/eval/pmid_pmcid_map.csv` (the firewall guard test separately confirms
no curator module even references that path).
"""

from __future__ import annotations

import asyncio

import httpx
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.resolve import ResolvedIds, resolve
from bugsigdb_curation.pmc_map import IDCONV_URL


def test_resolve_returns_pmcid_and_doi_on_hit(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {"ids": "21850056", "idtype": "pmid", "format": "json", "tool": "bugsigdb-curation", "email": "a@b.com"}
        ),
        json={
            "status": "ok",
            "records": [{"pmid": "21850056", "pmcid": "PMC3123456", "doi": "10.1/x"}],
        },
    )

    async def run() -> ResolvedIds:
        async with httpx.AsyncClient() as client:
            return await resolve("21850056", client=client, email="a@b.com")

    result = asyncio.run(run())
    assert result == ResolvedIds(pmid="21850056", pmcid="PMC3123456", doi="10.1/x")
    assert result.has_pmc is True


def test_resolve_returns_no_pmcid_when_idconv_has_no_pmc_record(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        json={"status": "ok", "records": [{"pmid": "19849869", "live": "false"}]},
    )

    async def run() -> ResolvedIds:
        async with httpx.AsyncClient() as client:
            return await resolve("19849869", client=client)

    result = asyncio.run(run())
    assert result.pmcid is None
    assert result.doi is None
    assert result.has_pmc is False


def test_resolve_handles_empty_records_list(httpx_mock: HTTPXMock):
    httpx_mock.add_response(json={"status": "ok", "records": []})

    async def run() -> ResolvedIds:
        async with httpx.AsyncClient() as client:
            return await resolve("00000000", client=client)

    result = asyncio.run(run())
    assert result == ResolvedIds(pmid="00000000", pmcid=None, doi=None)


# --- idconv transiently unavailable -> Europe PMC fallback (real errors are NOT sent to another source) ----------

import re  # noqa: E402

import pytest  # noqa: E402

from bugsigdb_curation.curator.resolve import EUROPEPMC_SEARCH_URL  # noqa: E402
from bugsigdb_curation.pmc_map import PmcMapError  # noqa: E402

_IDCONV = re.compile(re.escape(IDCONV_URL) + ".*")
_EPMC = re.compile(re.escape(EUROPEPMC_SEARCH_URL) + ".*")


def _run_resolve(pmid="34620922"):
    async def run():
        async with httpx.AsyncClient() as client:
            return await resolve(pmid, client=client)

    return asyncio.run(run())


def test_persistent_idconv_429_falls_back_to_europepmc(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_IDCONV, status_code=429, is_reusable=True)
    httpx_mock.add_response(
        url=_EPMC,
        json={
            "resultList": {
                "result": [{"id": "34620922", "pmid": "34620922", "pmcid": "PMC8497572", "doi": "10.1038/x"}]
            }
        },
    )
    assert _run_resolve() == ResolvedIds(pmid="34620922", pmcid="PMC8497572", doi="10.1038/x")


def test_idconv_5xx_also_falls_back(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_IDCONV, status_code=503, is_reusable=True)
    httpx_mock.add_response(url=_EPMC, json={"resultList": {"result": [{"id": "1", "pmid": "1", "pmcid": "PMC9"}]}})
    assert _run_resolve("1").pmcid == "PMC9"


def test_idconv_transport_errors_also_fall_back(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), url=_IDCONV, is_reusable=True)
    httpx_mock.add_response(url=_EPMC, json={"resultList": {"result": [{"id": "1", "pmid": "1", "pmcid": "PMC9"}]}})
    assert _run_resolve("1").pmcid == "PMC9"


def test_fallback_reports_no_pmc_when_europepmc_has_a_record_without_a_pmcid(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_IDCONV, status_code=429, is_reusable=True)
    httpx_mock.add_response(
        url=_EPMC, json={"resultList": {"result": [{"id": "19849869", "pmid": "19849869", "doi": "10.1017/x"}]}}
    )
    resolved = _run_resolve("19849869")
    assert resolved.pmcid is None and not resolved.has_pmc and resolved.doi == "10.1017/x"


def test_fallback_that_cannot_answer_raises_a_clear_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_IDCONV, status_code=429, is_reusable=True)
    httpx_mock.add_response(url=_EPMC, status_code=500)
    with pytest.raises(PmcMapError, match="Europe PMC could not resolve"):
        _run_resolve()


def test_a_real_idconv_error_is_not_sent_to_europepmc(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=_IDCONV,
        status_code=400,
        json={"status": "error", "errors": [{"message": "Identifiers must be numeric", "code": "bad-id"}]},
    )
    with pytest.raises(PmcMapError, match="Identifiers must be numeric"):
        _run_resolve("abc")
    assert len(httpx_mock.get_requests()) == 1  # no Europe PMC request: nothing was added for it to answer
