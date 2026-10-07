"""S0 -- resolve & ingest: PMID -> {pmid, pmcid, doi, has_pmc}.

Reuses `bugsigdb_curation.pmc_map`'s idconv client verbatim -- per the
workflow plan's data firewall (§6e), "`pmc-map` is usable only for S0
resolve (PMID->PMCID is public NCBI data, not curation), never as a hint
source for anything downstream." This module calls the live NCBI idconv API
directly; it never reads the repo's cached `data/eval/pmid_pmcid_map.csv`
(that file is gold-derived -- see the module docstring of
`bugsigdb_curation.pmc_map` and `docs/plans/de-novo-curation-workflow-plan.md`
§6e).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from loguru import logger

from bugsigdb_curation.pmc_map import PmcMapError, PmcMapTransientError, convert_pmids

#: Europe PMC's search API: an independent public source of PMID -> PMCID / DOI, used only when idconv is
#: transiently unavailable (it rate-limits by IP).
EUROPEPMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

#: NCBI idconv etiquette contact email for unauthenticated use (matches the
#: CLI's default for `bugsigdb pmc-map`).
DEFAULT_EMAIL = "seandavi@gmail.com"


@dataclass(frozen=True, slots=True)
class ResolvedIds:
    """S0's output: a PMID resolved to its (optional) PMCID/DOI."""

    pmid: str
    pmcid: str | None
    doi: str | None

    @property
    def has_pmc(self) -> bool:
        return self.pmcid is not None


async def _resolve_via_europepmc(pmid: str, *, client: httpx.AsyncClient) -> ResolvedIds:
    """PMID -> PMCID/DOI from Europe PMC's search API; raises `PmcMapError` if it cannot answer either."""
    try:
        response = await client.get(
            EUROPEPMC_SEARCH_URL,
            params={"query": f"EXT_ID:{pmid} AND SRC:MED", "format": "json", "resultType": "lite"},
        )
        response.raise_for_status()
        results = response.json()["resultList"]["result"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise PmcMapError(f"idconv was unavailable and Europe PMC could not resolve PMID {pmid}: {exc!r}") from exc
    record = next((r for r in results if isinstance(r, dict) and str(r.get("pmid") or r.get("id")) == pmid), None)
    if record is None:
        return ResolvedIds(pmid=pmid, pmcid=None, doi=None)
    pmcid, doi = record.get("pmcid"), record.get("doi")
    return ResolvedIds(pmid=pmid, pmcid=str(pmcid) if pmcid else None, doi=str(doi) if doi else None)


async def resolve(pmid: str, *, client: httpx.AsyncClient, email: str = DEFAULT_EMAIL) -> ResolvedIds:
    """Resolve a single PMID to its PMCID/DOI via the live NCBI idconv API.

    Raises `bugsigdb_curation.pmc_map.PmcMapError` on an idconv-reported
    error (e.g. malformed PMID); a PMID with no PMC record is not an error
    -- it comes back with `pmcid=None` (`has_pmc=False`).
    """
    try:
        records = await convert_pmids([pmid], email=email, client=client, concurrency=1)
    except (PmcMapTransientError, httpx.TransportError) as exc:
        # idconv is throttled/down, not telling us anything about the PMID: ask Europe PMC instead. A real idconv
        # error (e.g. a malformed PMID) is a plain PmcMapError and is NOT retried against another source.
        logger.bind(stage="S0").warning("idconv unavailable; resolving via Europe PMC", error=repr(exc))
        resolved = await _resolve_via_europepmc(pmid, client=client)
        logger.bind(stage="S0").info(
            "resolved",
            pmcid=resolved.pmcid,
            has_pmc=resolved.has_pmc,
            has_doi=resolved.doi is not None,
            via="europepmc",
        )
        return resolved
    if not records:
        # idconv returned zero records for a well-formed single-PMID request
        # (rare -- e.g. a PMID it doesn't recognize at all); treat as "no PMC".
        resolved = ResolvedIds(pmid=pmid, pmcid=None, doi=None)
    else:
        record = records[0]
        resolved = ResolvedIds(pmid=pmid, pmcid=record.pmcid, doi=record.doi)

    logger.bind(stage="S0").info(
        "resolved", pmcid=resolved.pmcid, has_pmc=resolved.has_pmc, has_doi=resolved.doi is not None
    )
    return resolved


__all__ = ["DEFAULT_EMAIL", "PmcMapError", "ResolvedIds", "resolve"]
