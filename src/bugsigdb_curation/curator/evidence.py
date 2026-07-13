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

from dataclasses import dataclass

import httpx

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


def build_bundle(pmid: str, pmcid: str, xml_text: str, html_text: str | None) -> EvidenceBundle:
    """Pure assembly: parse already-fetched fullTextXML (+ optional article HTML) into a bundle.

    Separated from the network fetch (`assemble_evidence`) so bundle
    construction is unit-testable on inline XML/HTML fixtures with no
    network access, mirroring the figure-extraction benchmark's pure-parser
    test style.
    """
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


async def assemble_evidence(pmid: str, pmcid: str, *, client: httpx.AsyncClient) -> EvidenceBundle:
    """S1: fetch EuropePMC fullTextXML + PMC article HTML for `pmcid` and build a bundle.

    The article HTML fetch is best-effort: if it fails (e.g. a transient PMC
    Cloudflare hiccup), figures still come back with `blob_url=None` rather
    than aborting the whole bundle -- text and tables are unaffected either
    way, and a figure without a resolved blob URL simply can't be fetched by
    S5b's vision path later (it degrades gracefully to "no image evidence
    for this figure", not a crash).
    """
    xml_text = await fetch_fulltext_xml(client, pmcid)
    try:
        html_text: str | None = await fetch_article_html(client, pmcid)
    except httpx.HTTPError:
        html_text = None
    return build_bundle(pmid, pmcid, xml_text, html_text)


async def fetch_figure_image(figure: EvidenceFigure, *, client: httpx.AsyncClient) -> bytes | None:
    """Lazily fetch one figure's raw image bytes (S1c), or None if unfetchable.

    Called only for the figure(s) S5a locates as the DA artifact for some
    experiment -- see module docstring. Returns None (rather than raising)
    when there's no resolved `blob_url` to fetch from.
    """
    if figure.blob_url is None:
        return None
    return await fetch_image_bytes(client, figure.blob_url)
