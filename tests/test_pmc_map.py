"""Unit tests for bugsigdb_curation.pmc_map — the pure/testable PMC-mapping logic."""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.pmc_map import (
    IDCONV_URL,
    ConversionRecord,
    CoverageStats,
    MappedStudy,
    PmcMapError,
    StudyPmid,
    build_request_params,
    chunk,
    compute_coverage,
    convert_pmids,
    distinct_pmids,
    fetch_batch,
    join_results,
    parse_idconv_response,
    read_study_pmids,
    write_mapping_csv,
)


def _write_studies_csv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "studies.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["study_id", "pmid", "doi", "title"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# --- read_study_pmids / distinct_pmids -------------------------------------------------


def test_read_study_pmids_skips_pmidless_row_and_preserves_dup_join(tmp_path):
    csv_path = _write_studies_csv(
        tmp_path,
        [
            {"study_id": "Study 1", "pmid": "19849869", "doi": "10.1/a", "title": "A"},
            {"study_id": "Study 2", "pmid": "", "doi": "10.1/b", "title": "B (no PMID)"},
            # Same PMID as Study 1 but a different study -> both must survive the join.
            {"study_id": "Study 3", "pmid": "19849869", "doi": "10.1/c", "title": "C (dup pmid)"},
            {"study_id": "Study 4", "pmid": "23209786", "doi": "10.1/d", "title": "D"},
        ],
    )

    rows = read_study_pmids(csv_path)

    assert rows == [
        StudyPmid(study_id="Study 1", pmid="19849869"),
        StudyPmid(study_id="Study 3", pmid="19849869"),
        StudyPmid(study_id="Study 4", pmid="23209786"),
    ]
    # De-duped for the purposes of querying the API...
    assert distinct_pmids(rows) == ["19849869", "23209786"]


def test_read_study_pmids_skips_non_numeric_pmid(tmp_path):
    csv_path = _write_studies_csv(
        tmp_path,
        [
            {"study_id": "Study 1", "pmid": "NA", "doi": "", "title": "A"},
            {"study_id": "Study 2", "pmid": "12345", "doi": "", "title": "B"},
        ],
    )

    rows = read_study_pmids(csv_path)

    assert rows == [StudyPmid(study_id="Study 2", pmid="12345")]


# --- chunk -------------------------------------------------------------------------------


def test_chunk_splits_into_200s():
    ids = [str(i) for i in range(250)]
    chunks = chunk(ids, size=200)

    assert len(chunks) == 2
    assert len(chunks[0]) == 200
    assert len(chunks[1]) == 50
    # every id is covered exactly once
    assert [i for c in chunks for i in c] == ids


def test_chunk_exact_multiple():
    ids = [str(i) for i in range(400)]
    chunks = chunk(ids, size=200)
    assert [len(c) for c in chunks] == [200, 200]


def test_chunk_default_size_is_200():
    ids = [str(i) for i in range(201)]
    chunks = chunk(ids)
    assert [len(c) for c in chunks] == [200, 1]


def test_chunk_empty():
    assert chunk([]) == []


# --- build_request_params ------------------------------------------------------------


def test_build_request_params():
    params = build_request_params(["1", "2", "3"], email="me@example.com")
    assert params == {
        "ids": "1,2,3",
        "idtype": "pmid",
        "format": "json",
        "tool": "bugsigdb-curation",
        "email": "me@example.com",
    }


# --- parse_idconv_response -------------------------------------------------------------


def test_parse_idconv_response_with_pmcid():
    response_json = {
        "status": "ok",
        "records": [{"pmid": "19849869", "pmcid": "PMC2809006", "doi": "10.1007/s00248-009-9491-y"}],
    }
    records = parse_idconv_response(response_json)
    assert records == [ConversionRecord(pmid="19849869", pmcid="PMC2809006", doi="10.1007/s00248-009-9491-y")]


def test_parse_idconv_response_without_pmcid():
    # Not every PubMed article is in PMC; idconv reports this via a record
    # with no pmcid (and often a status/errmsg/live field), not an error.
    response_json = {
        "status": "ok",
        "records": [{"pmid": "23209786", "status": "error", "errmsg": "live 0", "live": "false"}],
    }
    records = parse_idconv_response(response_json)
    assert records == [ConversionRecord(pmid="23209786", pmcid=None, doi=None)]


def test_parse_idconv_response_mixed_batch():
    response_json = {
        "status": "ok",
        "records": [
            {"pmid": "1", "pmcid": "PMC1", "doi": "10.1/a"},
            {"pmid": "2", "status": "error", "errmsg": "pmid not found"},
            {"pmid": "3", "pmcid": "PMC3"},
        ],
    }
    records = parse_idconv_response(response_json)
    assert records == [
        ConversionRecord(pmid="1", pmcid="PMC1", doi="10.1/a"),
        ConversionRecord(pmid="2", pmcid=None, doi=None),
        ConversionRecord(pmid="3", pmcid="PMC3", doi=None),
    ]


def test_parse_idconv_response_top_level_error_raises():
    # Real idconv error bodies nest the message inside an `errors` list, e.g.
    # {"status": "error", "errors": [{"message": "...", "code": "..."}]}.
    response_json = {
        "status": "error",
        "errors": [{"message": "Your maximum number of identifiers to convert is 200", "code": "too-many-ids"}],
    }
    with pytest.raises(PmcMapError, match="Your maximum number of identifiers to convert is 200"):
        parse_idconv_response(response_json)


def test_parse_idconv_response_top_level_error_falls_back_to_message_key():
    # Defensive fallback for any error body that doesn't use the `errors` shape.
    response_json = {"status": "error", "message": "ID list empty or invalid"}
    with pytest.raises(PmcMapError, match="ID list empty or invalid"):
        parse_idconv_response(response_json)


# --- fetch_batch / convert_pmids (mocked HTTP) ------------------------------------------


def test_fetch_batch_parses_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "19849869",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        json={"status": "ok", "records": [{"pmid": "19849869", "pmcid": "PMC2809006"}]},
    )

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient() as client:
            return await fetch_batch(client, ["19849869"], email="me@example.com")

    records = asyncio.run(run())
    assert records == [ConversionRecord(pmid="19849869", pmcid="PMC2809006", doi=None)]


def test_convert_pmids_success_across_multiple_batches(httpx_mock: HTTPXMock):
    batch1 = [str(i) for i in range(200)]
    batch2 = [str(i) for i in range(200, 250)]

    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": ",".join(batch1),
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        json={"status": "ok", "records": [{"pmid": pmid, "pmcid": f"PMC{pmid}"} for pmid in batch1]},
    )
    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": ",".join(batch2),
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        json={"status": "ok", "records": [{"pmid": pmid} for pmid in batch2]},
    )

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient() as client:
            return await convert_pmids(batch1 + batch2, email="me@example.com", client=client)

    records = asyncio.run(run())
    assert len(records) == 250
    by_pmid = {r.pmid: r for r in records}
    assert by_pmid["0"].pmcid == "PMC0"
    assert by_pmid["249"].pmcid is None


def test_convert_pmids_mixed_batch_some_with_some_without_pmc(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "1,2,3",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        json={
            "status": "ok",
            "records": [
                {"pmid": "1", "pmcid": "PMC1"},
                {"pmid": "2", "status": "error", "errmsg": "live 0"},
                {"pmid": "3", "pmcid": "PMC3"},
            ],
        },
    )

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient() as client:
            return await convert_pmids(["1", "2", "3"], email="me@example.com", client=client)

    records = asyncio.run(run())
    assert [r.pmcid for r in records] == ["PMC1", None, "PMC3"]


def test_convert_pmids_raises_friendly_error_on_real_api_error_shape(httpx_mock: HTTPXMock):
    # Mirrors the REAL idconv API: HTTP 400, with the message nested inside
    # an `errors` list, not a top-level `message` field on a 200 response.
    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "1",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        status_code=400,
        json={
            "status": "error",
            "errors": [{"message": "Your maximum number of identifiers to convert is 200", "code": "too-many-ids"}],
        },
    )

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient() as client:
            return await convert_pmids(["1"], email="me@example.com", client=client)

    with pytest.raises(PmcMapError, match="Your maximum number of identifiers to convert is 200"):
        asyncio.run(run())


def test_fetch_batch_raises_friendly_error_on_http_400(httpx_mock: HTTPXMock):
    # Same real-API error shape as above, exercised directly against
    # fetch_batch (mirrors export.py/test_export.py's 404 convention).
    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "1",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        status_code=400,
        json={"status": "error", "errors": [{"message": "Identifiers must be numeric", "code": "bad-id"}]},
    )

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient() as client:
            return await fetch_batch(client, ["1"], email="me@example.com")

    with pytest.raises(PmcMapError, match="Identifiers must be numeric"):
        asyncio.run(run())


def test_fetch_batch_coerces_int_pmid_to_str(httpx_mock: HTTPXMock):
    # The real API returns "pmid" as a JSON *number*, not a string
    # (e.g. `"pmid": 19849869`). Dropping the `str()` coercion in
    # parse_idconv_response should fail this test.
    httpx_mock.add_response(
        url=IDCONV_URL,
        match_params={
            "ids": "19849869",
            "idtype": "pmid",
            "format": "json",
            "tool": "bugsigdb-curation",
            "email": "me@example.com",
        },
        json={"status": "ok", "records": [{"pmid": 19849869, "pmcid": "PMC2809006"}]},
    )

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient() as client:
            return await fetch_batch(client, ["19849869"], email="me@example.com")

    records = asyncio.run(run())
    assert records == [ConversionRecord(pmid="19849869", pmcid="PMC2809006", doi=None)]
    assert isinstance(records[0].pmid, str)


def test_idconv_url_is_the_canonical_non_redirecting_endpoint():
    # Regression guard: the old www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/
    # URL 301-redirects here; a plain client.get() (no follow_redirects) only
    # works against the canonical URL directly.
    assert IDCONV_URL == "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"


# --- join_results / compute_coverage ----------------------------------------------------


def test_join_results_joins_by_pmid_and_preserves_dup_studies():
    rows = [
        StudyPmid(study_id="Study 1", pmid="1"),
        StudyPmid(study_id="Study 3", pmid="1"),
        StudyPmid(study_id="Study 4", pmid="2"),
    ]
    records = [
        ConversionRecord(pmid="1", pmcid="PMC1", doi="10.1/a"),
        ConversionRecord(pmid="2", pmcid=None, doi=None),
    ]

    mapped = join_results(rows, records)

    assert mapped == [
        MappedStudy(study_id="Study 1", pmid="1", pmcid="PMC1", doi="10.1/a"),
        MappedStudy(study_id="Study 3", pmid="1", pmcid="PMC1", doi="10.1/a"),
        MappedStudy(study_id="Study 4", pmid="2", pmcid=None, doi=None),
    ]
    assert mapped[0].has_pmc is True
    assert mapped[2].has_pmc is False


def test_join_results_drops_rows_with_no_matching_record():
    # e.g. when --limit trimmed the queried pmid set
    rows = [StudyPmid(study_id="Study 1", pmid="1"), StudyPmid(study_id="Study 2", pmid="99")]
    records = [ConversionRecord(pmid="1", pmcid="PMC1", doi=None)]

    mapped = join_results(rows, records)

    assert mapped == [MappedStudy(study_id="Study 1", pmid="1", pmcid="PMC1", doi=None)]


def test_compute_coverage():
    records = [
        ConversionRecord(pmid="1", pmcid="PMC1", doi=None),
        ConversionRecord(pmid="2", pmcid=None, doi=None),
        ConversionRecord(pmid="3", pmcid="PMC3", doi=None),
    ]
    stats = compute_coverage(records)
    assert stats == CoverageStats(total=3, with_pmc=2)
    assert stats.without_pmc == 1
    assert stats.coverage_pct == pytest.approx(66.6666, rel=1e-3)


def test_compute_coverage_empty():
    stats = compute_coverage([])
    assert stats.total == 0
    assert stats.coverage_pct == 0.0


# --- write_mapping_csv -------------------------------------------------------------------


def test_write_mapping_csv(tmp_path):
    mapped = [
        MappedStudy(study_id="Study 1", pmid="1", pmcid="PMC1", doi="10.1/a"),
        MappedStudy(study_id="Study 2", pmid="2", pmcid=None, doi=None),
    ]
    output_path = tmp_path / "nested" / "out.csv"

    write_mapping_csv(mapped, output_path)

    with open(output_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert rows == [
        {"study_id": "Study 1", "pmid": "1", "pmcid": "PMC1", "doi": "10.1/a", "has_pmc": "true"},
        {"study_id": "Study 2", "pmid": "2", "pmcid": "", "doi": "", "has_pmc": "false"},
    ]


# --- opt-in real-network test ------------------------------------------------------------


@pytest.mark.network
def test_convert_pmids_real_network():
    """Convert a couple of real PMIDs and sanity-check the JSON shape/parser."""

    async def run() -> list[ConversionRecord]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await convert_pmids(
                ["19849869", "23209786"], email="seandavi@gmail.com", client=client
            )

    records = asyncio.run(run())
    assert len(records) == 2
    assert any(r.pmcid is not None for r in records)
    for r in records:
        assert r.pmid in {"19849869", "23209786"}
