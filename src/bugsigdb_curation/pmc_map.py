"""Map curated BugSigDB study PMIDs to PubMed Central IDs (PMCIDs).

Uses the NCBI PMC ID Converter API
(https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/) to look up which curated
studies have a corresponding PMC full-text article, as a gold/eval set for
de-novo curation workflows (which typically need full text, not just an
abstract).

This module is pure I/O + data transformation and has no CLI/UI concerns —
those live in :mod:`bugsigdb_curation.cli`. Mirrors the structure of
:mod:`bugsigdb_curation.export`: async `httpx.AsyncClient`, bounded
concurrency, a friendly error type.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# NCBI moved this endpoint (the old www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/
# URL now 301-redirects here) — use the current canonical location directly so
# a plain `client.get()` (no follow_redirects) works.
IDCONV_URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
TOOL_NAME = "bugsigdb-curation"

#: Max PMIDs per idconv request (NCBI-documented limit).
DEFAULT_CHUNK_SIZE = 200
#: Keep concurrency low and polite — no API key is used, so NCBI etiquette
#: (https://www.ncbi.nlm.nih.gov/books/NBK25497/) asks for a gentle rate.
DEFAULT_CONCURRENCY = 3


class PmcMapError(RuntimeError):
    """Raised for user-facing PMC ID conversion failures."""


@dataclass(frozen=True, slots=True)
class StudyPmid:
    """A curated study's id, joined to its (numeric, string-form) PMID."""

    study_id: str
    pmid: str


@dataclass(frozen=True, slots=True)
class ConversionRecord:
    """The idconv result for a single PMID.

    `pmcid` and `doi` are None when idconv has no PMC record for this PMID
    (common — most PubMed articles aren't in PMC — not an error condition).
    """

    pmid: str
    pmcid: str | None
    doi: str | None


@dataclass(frozen=True, slots=True)
class MappedStudy:
    """A study_id/pmid pair joined with its conversion result."""

    study_id: str
    pmid: str
    pmcid: str | None
    doi: str | None

    @property
    def has_pmc(self) -> bool:
        return self.pmcid is not None


@dataclass(frozen=True, slots=True)
class CoverageStats:
    """Summary of how many (distinct) PMIDs resolved to a PMCID."""

    total: int
    with_pmc: int

    @property
    def without_pmc(self) -> int:
        return self.total - self.with_pmc

    @property
    def coverage_pct(self) -> float:
        return (self.with_pmc / self.total * 100) if self.total else 0.0


def read_study_pmids(csv_path: Path) -> list[StudyPmid]:
    """Read (study_id, pmid) pairs from a studies CSV, skipping PMID-less rows.

    Rows whose `pmid` cell is missing/blank or not purely numeric are
    skipped (16 of the 2068 current BugSigDB studies have no PMID at all).
    Duplicate PMIDs across different studies are all kept here (the
    study_id join is preserved) — de-duping for the actual API query
    happens separately, in :func:`distinct_pmids`.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows: list[StudyPmid] = []
        for row in reader:
            study_id = (row.get("study_id") or "").strip()
            pmid = (row.get("pmid") or "").strip()
            if not study_id or not pmid.isdigit():
                continue
            rows.append(StudyPmid(study_id=study_id, pmid=pmid))
    return rows


def distinct_pmids(rows: list[StudyPmid]) -> list[str]:
    """Distinct PMIDs from `rows`, in first-seen order."""
    seen: dict[str, None] = {}
    for row in rows:
        seen.setdefault(row.pmid, None)
    return list(seen)


def chunk(ids: list[str], size: int = DEFAULT_CHUNK_SIZE) -> list[list[str]]:
    """Split `ids` into chunks of at most `size` (idconv's per-request limit)."""
    if size <= 0:
        raise ValueError("size must be positive")
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def build_request_params(pmids: list[str], *, email: str) -> dict[str, str]:
    """Build the idconv query params for a batch of PMIDs."""
    return {
        "ids": ",".join(pmids),
        "idtype": "pmid",
        "format": "json",
        "tool": TOOL_NAME,
        "email": email,
    }


def parse_idconv_response(response_json: dict[str, Any]) -> list[ConversionRecord]:
    """Parse an idconv JSON response into per-PMID conversion records.

    Raises :class:`PmcMapError` on a top-level `status: "error"` response
    (e.g. malformed request). Individual records with no PMC match (no
    `pmcid`, possibly a `status`/`errmsg`/`live` field) are expected and
    common, and are returned with `pmcid=None` rather than treated as
    errors.
    """
    if response_json.get("status") == "error":
        message = response_json.get("message") or response_json.get("errmsg") or "unknown error"
        raise PmcMapError(f"NCBI idconv reported an error: {message}")

    records: list[ConversionRecord] = []
    for rec in response_json.get("records", []):
        pmid = rec.get("pmid")
        if not pmid:
            continue
        records.append(ConversionRecord(pmid=str(pmid), pmcid=rec.get("pmcid"), doi=rec.get("doi")))
    return records


async def fetch_batch(client: httpx.AsyncClient, pmids: list[str], *, email: str) -> list[ConversionRecord]:
    """Fetch and parse idconv results for a single batch (<= 200 PMIDs)."""
    params = build_request_params(pmids, email=email)
    response = await client.get(IDCONV_URL, params=params)
    response.raise_for_status()
    try:
        response_json = response.json()
    except ValueError as exc:
        raise PmcMapError(f"Unexpected (non-JSON) response from the idconv API: {exc}") from exc
    return parse_idconv_response(response_json)


async def convert_pmids(
    pmids: list[str],
    *,
    email: str,
    client: httpx.AsyncClient,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> list[ConversionRecord]:
    """Convert PMIDs to PMCIDs via the NCBI idconv API, chunked and rate-limited.

    Requests are chunked to `DEFAULT_CHUNK_SIZE` PMIDs each and issued with
    bounded concurrency (default 3 — no API key is used, so keep this
    gentle per NCBI etiquette). Raises :class:`PmcMapError` on API-level
    failures; network errors (`httpx.HTTPError`) propagate as-is.
    """
    chunks = chunk(pmids)
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(batch: list[str]) -> list[ConversionRecord]:
        async with semaphore:
            return await fetch_batch(client, batch, email=email)

    results = await asyncio.gather(*(_one(batch) for batch in chunks))
    return [record for batch_records in results for record in batch_records]


def join_results(rows: list[StudyPmid], records: list[ConversionRecord]) -> list[MappedStudy]:
    """Join study/pmid rows with conversion records (by pmid).

    Rows whose PMID has no corresponding record (e.g. excluded by
    `--limit`) are silently dropped — this is not an error, just means that
    PMID wasn't queried.
    """
    record_by_pmid = {r.pmid: r for r in records}
    mapped: list[MappedStudy] = []
    for row in rows:
        record = record_by_pmid.get(row.pmid)
        if record is None:
            continue
        mapped.append(MappedStudy(study_id=row.study_id, pmid=row.pmid, pmcid=record.pmcid, doi=record.doi))
    return mapped


def compute_coverage(records: list[ConversionRecord]) -> CoverageStats:
    """Compute PMCID coverage stats over a list of (distinct) conversion records."""
    with_pmc = sum(1 for r in records if r.pmcid)
    return CoverageStats(total=len(records), with_pmc=with_pmc)


def write_mapping_csv(mapped: list[MappedStudy], output_path: Path) -> None:
    """Write the study_id/pmid/pmcid/doi/has_pmc mapping to `output_path`."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["study_id", "pmid", "pmcid", "doi", "has_pmc"])
        writer.writeheader()
        for m in mapped:
            writer.writerow(
                {
                    "study_id": m.study_id,
                    "pmid": m.pmid,
                    "pmcid": m.pmcid or "",
                    "doi": m.doi or "",
                    "has_pmc": str(m.has_pmc).lower(),
                }
            )
