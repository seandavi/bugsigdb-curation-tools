"""S1 -- evidence-bundle assembly: text + main tables + figures (no supplements).

Fetches EuropePMC ``fullTextXML`` for a PMCID and parses it into a normalized
`EvidenceBundle` of labeled body sections, parsed tables, and figure metadata
-- reusing the retrieval recipe verified by the figure-extraction benchmark
(`bugsigdb_curation.retrieval`, consolidated there in this same effort; see
`docs/LEDGER.md` L010). Per §6/§6e of the workflow plan:

* **Supplements are deliberately out of scope** (deferred; see the plan's §5
  open decision #1) -- this bundle never attempts to fetch them.
* Figure **image bytes are fetched lazily**, only for a figure S5a actually
  locates as the differential-abundance artifact for some experiment (via
  :func:`fetch_figure_image`) -- eagerly downloading every figure in every
  paper would be wasteful and unnecessary for the walking skeleton.
* This module fetches only from EuropePMC/PMC/NCBI REST endpoints the
  curator resolves for itself; it never reads any cached/gold file.
"""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from loguru import logger

from bugsigdb_curation.retrieval import (
    ArticleMetadata,
    FigureEntry,
    SectionEntry,
    TableEntry,
    extract_blob_urls,
    fetch_article_html,
    fetch_fulltext_xml,
    fetch_image_bytes,
    match_filename_to_blob,
    parse_article_metadata,
    parse_fulltext_figures,
    parse_fulltext_sections,
    parse_fulltext_tables,
)


# --- PMC article-HTML fetch: challenge-aware, throttled, cached ---------------------------------------
#
# PMC's article HTML is the only source of figure *image* URLs, and PMC intermittently answers a
# plain-looking client with a ~20 KB reCAPTCHA page (HTTP 200!) after a handful of quick requests.
# That page has no blob URLs, so every figure silently came back image-less and S5b fell back to
# legend-only extraction (empty or wrong signatures) -- a large, invisible source of run-to-run
# variance in a batch. So: detect the challenge, back off and retry, space requests out, and cache
# good pages on disk so each article is fetched once across runs.

PMC_HTML_MIN_INTERVAL = 3.0
PMC_HTML_ATTEMPTS = 4
#: Waits before retry 1, 2, 3 after a challenge (seconds; +-20% jitter applied).
PMC_HTML_BACKOFF = (5.0, 12.0, 25.0)
_CHALLENGE_MARKERS = ("recaptcha", "captcha", "challenge-platform")


def default_html_cache_dir() -> Path:
    """`data/curator/pmc_html` (override with `BUGSIGDB_PMC_HTML_CACHE`; resolved at call time)."""
    return Path(os.environ.get("BUGSIGDB_PMC_HTML_CACHE", "data/curator/pmc_html"))


def is_pmc_challenge(html_text: str) -> bool:
    """True for a bot-challenge page: no figure blob URLs *and* a captcha/challenge marker.

    A real article page that merely has no figures has no marker, so it is not retried.
    """
    if extract_blob_urls(html_text):
        return False
    lowered = html_text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


@dataclass(slots=True)
class PmcRequestLimiter:
    """Minimum spacing between PMC HTML requests, shared by every fetch in the process."""

    min_interval: float = PMC_HTML_MIN_INTERVAL
    _lock: asyncio.Lock | None = field(default=None, repr=False)
    _last: float = field(default=float("-inf"), repr=False)

    async def acquire(
        self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            wait = self._last + self.min_interval - clock()
            if wait > 0:
                await sleep(wait)
            self._last = clock()


PMC_LIMITER = PmcRequestLimiter()


async def fetch_pmc_html(
    client: httpx.AsyncClient,
    pmcid: str,
    *,
    cache_dir: Path | None = None,
    limiter: PmcRequestLimiter | None = None,
    attempts: int | None = None,
    backoff: tuple[float, ...] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """The article HTML for `pmcid`, or None if it could not be obtained (best-effort, never raises).

    A cached good page is returned without any request. Otherwise requests go through `limiter`; a
    challenge page (see :func:`is_pmc_challenge`) or a 429 is retried with backoff; any other HTTP or
    transport error gives up immediately (matching the old best-effort contract). Only genuine pages
    are cached. Exhausting the retries is logged at WARNING -- the caller's figures then have no images.
    """
    log = logger.bind(stage="S1", pmcid=pmcid)
    # Resolved at call time (not def time) so tests can shrink them via the module constants.
    attempts = PMC_HTML_ATTEMPTS if attempts is None else attempts
    backoff = PMC_HTML_BACKOFF if backoff is None else backoff
    cache_file = cache_dir / f"{pmcid}.html" if cache_dir is not None else None
    if cache_file is not None and cache_file.exists():
        cached = cache_file.read_text(encoding="utf-8")
        if cached and not is_pmc_challenge(cached):
            return cached
    limiter = limiter or PMC_LIMITER
    for attempt in range(1, attempts + 1):
        await limiter.acquire(sleep=sleep, clock=clock)
        try:
            html_text = await fetch_article_html(client, pmcid)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429:
                log.warning("PMC article HTML fetch failed", status=exc.response.status_code)
                return None
            reason = "HTTP 429"
        except httpx.HTTPError as exc:
            log.warning("PMC article HTML fetch failed", error=repr(exc))
            return None
        else:
            if not is_pmc_challenge(html_text):
                if cache_file is not None:
                    _write_text_atomic(cache_file, html_text)
                return html_text
            reason = "challenge page"
        if attempt < attempts:
            wait = backoff[min(attempt - 1, len(backoff) - 1)] * random.uniform(0.8, 1.2)
            log.info("PMC article HTML throttled; retrying", reason=reason, attempt=attempt, wait_s=round(wait, 1))
            await sleep(wait)
    log.warning("PMC article HTML unavailable after retries; figures will have no images", attempts=attempts)
    return None


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class EvidenceFigure:
    """One figure's metadata (S1c) -- image bytes are NOT included here.

    `blob_url`, if resolved, is the CDN URL `fetch_figure_image` needs;
    it's None when the article HTML didn't yield a matching blob (e.g. the
    figure has no `graphic_filename`, or the HTML page couldn't be matched).
    """

    figure_id: str  # e.g. "F2" (fullTextXML's <fig id="...">, index fallback)
    number: str | None  # normalized leading integer, e.g. "2"
    label: str  # e.g. "Figure 2."
    legend: str
    graphic_filename: str | None
    blob_url: str | None

    @property
    def provenance(self) -> str:
        return f"Figure {self.number}" if self.number else self.label or self.figure_id


@dataclass(frozen=True, slots=True)
class EvidenceTable:
    """One main-text table (S1's "main tables" channel)."""

    table_id: str
    number: str | None
    label: str
    caption: str
    rows: tuple[tuple[str, ...], ...]

    @property
    def provenance(self) -> str:
        return f"Table {self.number}" if self.number else self.label or self.table_id

    def as_text(self) -> str:
        """Render the table as a plain-text grid for an LLM's text context."""
        lines = [self.label, self.caption] if (self.label or self.caption) else []
        for row in self.rows:
            lines.append(" | ".join(row))
        return "\n".join(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """The normalized S1 evidence bundle for one article: text + main tables + figures.

    Every item (`sections`, `tables`, `figures`) carries its own provenance
    handle (section id/title, table/figure label) so later stages can cite a
    `source` back to something concrete, per the plan's "provenance travels
    with every value" design rule (§1).
    """

    pmid: str
    pmcid: str
    metadata: ArticleMetadata
    sections: tuple[SectionEntry, ...]
    tables: tuple[EvidenceTable, ...]
    figures: tuple[EvidenceFigure, ...]

    def full_text(self) -> str:
        """All section text concatenated with title headers -- a simple text context slice."""
        parts = []
        for sec in self.sections:
            header = f"## {sec.title}" if sec.title else "## (untitled section)"
            parts.append(f"{header}\n{sec.text}")
        return "\n\n".join(p for p in parts if p.strip())


def _build_figures(figure_entries: list[FigureEntry], blob_urls: list[str]) -> tuple[EvidenceFigure, ...]:
    figures = []
    for index, fig in enumerate(figure_entries):
        blob_url = match_filename_to_blob(fig.graphic_filename, blob_urls) if fig.graphic_filename else None
        figure_id = f"F{fig.number}" if fig.number else f"fig-{index}"
        figures.append(
            EvidenceFigure(
                figure_id=figure_id,
                number=fig.number,
                label=fig.label,
                legend=fig.legend,
                graphic_filename=fig.graphic_filename,
                blob_url=blob_url,
            )
        )
    return tuple(figures)


def _build_tables(table_entries: list[TableEntry]) -> tuple[EvidenceTable, ...]:
    return tuple(
        EvidenceTable(
            table_id=t.table_id,
            number=t.number,
            label=t.label,
            caption=t.caption,
            rows=t.rows,
        )
        for t in table_entries
    )


def build_bundle(pmid: str, pmcid: str, xml_text: str | None, html_text: str | None) -> EvidenceBundle:
    """Pure assembly: parse already-fetched fullTextXML (+ optional article HTML) into a bundle.

    Separated from the network fetch (`assemble_evidence`) so bundle
    construction is unit-testable on inline XML/HTML fixtures with no
    network access, mirroring the figure-extraction benchmark's pure-parser
    test style.

    `xml_text=None` means "no full text available at all" (EuropePMC has no
    `fullTextXML` for this PMCID -- see `assemble_evidence`'s 404 handling);
    the bundle still comes back, just with no sections/tables/figures and
    empty metadata, rather than this function raising on `None`.
    """
    if xml_text is None:
        return EvidenceBundle(
            pmid=pmid,
            pmcid=pmcid,
            metadata=ArticleMetadata(title=None, journal=None, year=None, authors=(), doi=None),
            sections=(),
            tables=(),
            figures=(),
        )
    metadata = parse_article_metadata(xml_text)
    sections = tuple(parse_fulltext_sections(xml_text))
    tables = _build_tables(parse_fulltext_tables(xml_text))
    blob_urls = extract_blob_urls(html_text) if html_text else []
    figures = _build_figures(parse_fulltext_figures(xml_text), blob_urls)
    return EvidenceBundle(
        pmid=pmid,
        pmcid=pmcid,
        metadata=metadata,
        sections=sections,
        tables=tables,
        figures=figures,
    )


async def assemble_evidence(
    pmid: str, pmcid: str, *, client: httpx.AsyncClient, html_cache_dir: Path | None = None
) -> EvidenceBundle:
    """S1: fetch EuropePMC fullTextXML + PMC article HTML for `pmcid` and build a bundle.

    The fullTextXML fetch is best-effort against a **404 specifically**:
    EuropePMC returns 404 for a PMCID it has no full-text record for (the
    article is in PMC per S0's idconv resolution, but not mirrored into
    EuropePMC's full-text service) -- that's a normal "no full-text
    channel" outcome, not a failure, so it degrades to an empty bundle
    (`build_bundle(..., xml_text=None, ...)`) rather than aborting the
    whole study. Any other HTTP error status (a genuine unexpected failure,
    not "not found") still propagates.

    The article HTML fetch is separately best-effort (`fetch_pmc_html`: throttled, retried through
    PMC's intermittent captcha page, cached under `html_cache_dir` -- default
    `default_html_cache_dir()`): if it still fails, figures come back with
    `blob_url=None` rather than aborting the whole bundle -- text and
    tables are unaffected either way, and a figure without a resolved blob
    URL simply can't be fetched by S5b's vision path later (it degrades
    gracefully to "no image evidence for this figure", not a crash).
    """
    try:
        xml_text: str | None = await fetch_fulltext_xml(client, pmcid)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            xml_text = None
        else:
            raise

    html_text: str | None = None
    if xml_text is not None:
        # No point fetching the article HTML (used only to resolve figure
        # blob URLs) when there's no full text to have parsed figures from.
        html_text = await fetch_pmc_html(
            client, pmcid, cache_dir=html_cache_dir if html_cache_dir is not None else default_html_cache_dir()
        )
    bundle = build_bundle(pmid, pmcid, xml_text, html_text)
    logger.bind(stage="S1").info(
        "evidence assembled",
        n_sections=len(bundle.sections),
        n_tables=len(bundle.tables),
        n_figures=len(bundle.figures),
    )
    return bundle


async def fetch_figure_image(figure: EvidenceFigure, *, client: httpx.AsyncClient) -> bytes | None:
    """Lazily fetch one figure's raw image bytes (S1c), or None if unfetchable.

    Called only for the figure(s) S5a locates as the DA artifact for some
    experiment -- see module docstring. Returns None (rather than raising)
    when there's no resolved `blob_url` to fetch from.
    """
    if figure.blob_url is None:
        return None
    return await fetch_image_bytes(client, figure.blob_url)
