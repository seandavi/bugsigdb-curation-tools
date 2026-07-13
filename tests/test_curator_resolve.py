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
