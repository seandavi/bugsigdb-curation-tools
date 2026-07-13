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

from bugsigdb_curation.pmc_map import PmcMapError, convert_pmids

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


async def resolve(pmid: str, *, client: httpx.AsyncClient, email: str = DEFAULT_EMAIL) -> ResolvedIds:
    """Resolve a single PMID to its PMCID/DOI via the live NCBI idconv API.

    Raises `bugsigdb_curation.pmc_map.PmcMapError` on an idconv-reported
    error (e.g. malformed PMID); a PMID with no PMC record is not an error
    -- it comes back with `pmcid=None` (`has_pmc=False`).
    """
    records = await convert_pmids([pmid], email=email, client=client, concurrency=1)
    if not records:
        # idconv returned zero records for a well-formed single-PMID request
        # (rare -- e.g. a PMID it doesn't recognize at all); treat as "no PMC".
        return ResolvedIds(pmid=pmid, pmcid=None, doi=None)
    record = records[0]
    return ResolvedIds(pmid=pmid, pmcid=record.pmcid, doi=record.doi)


__all__ = ["DEFAULT_EMAIL", "PmcMapError", "ResolvedIds", "resolve"]
