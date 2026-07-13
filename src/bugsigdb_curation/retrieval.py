"""Shared REST-retrieval + JATS/HTML parsing primitives for full-text article evidence.

This module holds the **pure parsers** (XML/HTML in, data out -- no I/O) plus
thin `httpx`-based fetch wrappers for the retrieval recipe verified by the
figure-extraction benchmark (see `benchmarks/figure-extraction/README.md` and
`docs/LEDGER.md` L010):

1. EuropePMC ``fullTextXML`` -> per-figure label/legend/graphic-filename
   (:func:`parse_fulltext_figures`, :func:`match_figure`), plus (new, for the
   de-novo curator's S1 evidence assembly) body ``<sec>`` sections
   (:func:`parse_fulltext_sections`), ``<table-wrap>`` tables
   (:func:`parse_fulltext_tables`), and ``<article-meta>`` bibliographic
   fields (:func:`parse_article_metadata`).
2. The PMC article HTML -> candidate CDN blob URLs
   (:func:`extract_blob_urls`, :func:`match_filename_to_blob`).
3. The matched blob URL -> raw image bytes (:func:`fetch_image_bytes`).
4. The NCBI OA service -> license string, used as an inclusion gate
   (:func:`parse_oa_response`).

`benchmarks/figure-extraction/retrieve.py` re-exports this module's names
verbatim (see that file's docstring) so the existing figure-extraction
benchmark and its tests are unaffected by this consolidation; the de-novo
curator (`bugsigdb_curation.curator.evidence`) imports directly from here.

All parsing functions are pure and covered by inline-XML/HTML-fixture unit
tests (no network access required). The `fetch_*` functions are thin
wrappers around `httpx` that do the actual HTTP calls; network-dependent
behavior is exercised only by `@pytest.mark.network`-marked tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

EUROPEPMC_FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
PMC_ARTICLE_URL = "https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
OA_SERVICE_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"

# PMC's Cloudflare front-end blocks non-browser-looking requests; a plain
# desktop-browser UA is enough to get a 200 (verified manually — see README).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_BLOB_URL_RE = re.compile(
    r"https://cdn\.ncbi\.nlm\.nih\.gov/pmc/blobs/[^\"'\s]+\.(?:jpg|jpeg|png|gif)",
    re.IGNORECASE,
)
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


@dataclass(frozen=True, slots=True)
class FigureEntry:
    """One `<fig>` extracted from a fullTextXML document."""

    label: str  # verbatim, e.g. "Figure 2."
    number: str | None  # normalized leading integer, e.g. "2"
    legend: str  # caption text, tags stripped, whitespace collapsed
    graphic_filename: str | None  # e.g. "TEMI_A_1783188_F0002_OC.jpg"


def _text_content(elem: ET.Element) -> str:
    """Flatten an element's text (incl. nested tags like <italic>) to a string."""
    return "".join(elem.itertext())


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_figure_number(label: str) -> str | None:
    match = re.search(r"(\d+)", label)
    return match.group(1) if match else None


def parse_fulltext_figures(xml_text: str) -> list[FigureEntry]:
    """Parse all `<fig>` elements out of a EuropePMC/JATS fullTextXML document.

    Each `<fig>` typically looks like::

        <fig id="F2" position="float">
          <label>Figure 2.</label>
          <caption><p>Relative abundance ...</p></caption>
          <graphic xlink:href="TEMI_A_1783188_F0002_OC.jpg"/>
        </fig>

    Figures with no `<label>`/`<graphic>` still produce an entry (with
    ``label=""`` / ``graphic_filename=None``) rather than being skipped, so
    callers can see the full figure count if needed.
    """
    root = ET.fromstring(xml_text)
    entries: list[FigureEntry] = []
    for fig in root.iter("fig"):
        label_el = fig.find("label")
        label = _normalize_ws(_text_content(label_el)) if label_el is not None else ""

        caption_el = fig.find("caption")
        legend = _normalize_ws(_text_content(caption_el)) if caption_el is not None else ""

        # `<graphic>` is sometimes a direct child of `<fig>` and sometimes
        # nested inside an `<alternatives>` wrapper (multiple format
        # variants) — search all descendants, not just direct children.
        graphic_el = fig.find(".//graphic")
        filename = graphic_el.get(_XLINK_HREF) if graphic_el is not None else None

        entries.append(
            FigureEntry(
                label=label,
                number=_extract_figure_number(label),
                legend=legend,
                graphic_filename=filename,
            )
        )
    return entries


def normalize_source_label(source: str) -> str | None:
    """Extract the figure number a BugSigDB `source` cell refers to.

    Handles panel suffixes: "Figure 2" -> "2", "figure 3b" -> "3",
    "Figure 2B" -> "2". Returns None for non-figure sources (e.g. "Table 1").
    """
    match = re.match(r"\s*figure\s*#?\s*(\d+)", source, re.IGNORECASE)
    return match.group(1) if match else None


def match_figure(source: str, figures: list[FigureEntry]) -> FigureEntry | None:
    """Find the `FigureEntry` a BugSigDB `source` cell (e.g. "figure 3b") refers to.

    Matches by normalized figure number only — panel letters in `source`
    don't need a corresponding sub-figure entry in the XML (BugSigDB figures
    are frequently panels of a single combined `<fig>`).
    """
    number = normalize_source_label(source)
    if number is None:
        return None
    for fig in figures:
        if fig.number == number:
            return fig
    return None


def extract_blob_urls(html_text: str) -> list[str]:
    """Extract candidate `cdn.ncbi.nlm.nih.gov/pmc/blobs/...` image URLs from article HTML.

    Order is preserved and duplicates removed (first occurrence wins) —
    the same blob URL can appear multiple times (e.g. thumbnail + full-size
    links to the same figure).
    """
    return list(dict.fromkeys(_BLOB_URL_RE.findall(html_text)))


def match_filename_to_blob(filename: str, blob_urls: list[str]) -> str | None:
    """Match a fullTextXML graphic filename to its blob URL from the article HTML.

    Blob URLs end in `.../<hash-dirs>/<filename>`, so a suffix match on the
    filename is sufficient and robust to the hash-directory prefix. Falls
    back to a stem-only match (drops the extension) since the two sources
    occasionally disagree on file extension (e.g. `.jpg` vs `.jpeg`).
    """
    for url in blob_urls:
        if url.endswith("/" + filename):
            return url
    stem = Path(filename).stem
    for url in blob_urls:
        if Path(url).stem == stem:
            return url
    return None


def parse_oa_response(xml_text: str) -> str | None:
    """Parse the NCBI OA service response, returning the license string if OA.

    Returns None for `<error code="idIsNotOpenAccess">` (or any other
    error/missing-record shape) — callers should treat None as "exclude".
    """
    root = ET.fromstring(xml_text)
    if root.find(".//error") is not None:
        return None
    record = root.find(".//record")
    if record is None:
        return None
    return record.get("license")


def build_image_path(images_dir: Path, pmcid: str, figure_number: str, filename: str) -> Path:
    """Build the local save path for a fetched figure image.

    e.g. ``build_image_path(Path("data/figbench/images"), "PMC1234567", "2",
    "TEMI_A_1783188_F0002_OC.jpg")`` -> ``data/figbench/images/PMC1234567_F2.jpg``.
    """
    ext = Path(filename).suffix or ".jpg"
    return images_dir / f"{pmcid}_F{figure_number}{ext}"


# ---------------------------------------------------------------------------
# sections + tables + article metadata (new: for the de-novo curator's S1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionEntry:
    """One `<sec>` extracted from the JATS `<body>` (a labeled body section)."""

    section_id: str  # `<sec id="...">`, or a synthesized "sec-N" fallback
    title: str  # `<title>` text, "" if absent
    text: str  # flattened text of this section's own <p> children only
    #: `<p>` text belonging to NESTED `<sec>` elements is deliberately
    #: excluded from `text` (each nested `<sec>` gets its own `SectionEntry`,
    #: from the same `root.iter("sec")` walk) — including it here would
    #: duplicate every subsection's text into its parent's entry too.


def parse_fulltext_sections(xml_text: str) -> list[SectionEntry]:
    """Parse all `<sec>` elements out of the `<body>` of a fullTextXML document.

    Typical JATS shape::

        <body>
          <sec id="s1"><title>Methods</title>
            <p>...</p>
            <sec id="s1a"><title>Statistical analysis</title><p>...</p></sec>
          </sec>
        </body>

    Both the outer "Methods" section and the nested "Statistical analysis"
    subsection are returned as separate entries; the outer entry's `text`
    holds only its own direct `<p>` children (not the nested subsection's),
    so nothing is duplicated across entries.
    """
    root = ET.fromstring(xml_text)
    body = root.find(".//body")
    if body is None:
        return []

    entries: list[SectionEntry] = []
    for index, sec in enumerate(body.iter("sec")):
        title_el = sec.find("title")
        title = _normalize_ws(_text_content(title_el)) if title_el is not None else ""

        own_paragraphs = [_text_content(p) for p in sec.findall("p")]
        text = _normalize_ws(" ".join(own_paragraphs))

        section_id = sec.get("id") or f"sec-{index}"
        entries.append(SectionEntry(section_id=section_id, title=title, text=text))
    return entries


@dataclass(frozen=True, slots=True)
class TableEntry:
    """One `<table-wrap>` extracted from a fullTextXML document."""

    table_id: str  # `<table-wrap id="...">`, or a synthesized "table-N" fallback
    label: str  # e.g. "Table 2."
    number: str | None  # normalized leading integer, e.g. "2"
    caption: str  # caption text, tags stripped, whitespace collapsed
    rows: tuple[tuple[str, ...], ...]  # cell text grid, header rows included first


def _row_cells(tr: ET.Element) -> tuple[str, ...]:
    return tuple(_normalize_ws(_text_content(cell)) for cell in tr if cell.tag in ("td", "th"))


def parse_fulltext_tables(xml_text: str) -> list[TableEntry]:
    """Parse all `<table-wrap>` elements out of a fullTextXML document.

    Typical JATS shape::

        <table-wrap id="T2">
          <label>Table 2.</label>
          <caption><p>Differentially abundant taxa.</p></caption>
          <table>
            <thead><tr><th>Taxon</th><th>LDA score</th></tr></thead>
            <tbody><tr><td>Bacteroides</td><td>3.2</td></tr></tbody>
          </table>
        </table-wrap>

    `rows` flattens `<thead>` and `<tbody>` (and any bare `<tr>` outside
    either) into one ordered sequence of cell-text tuples, header row(s)
    first — good enough for an LLM to read as evidence; it is not a
    round-trippable table model (no colspan/rowspan handling).
    """
    root = ET.fromstring(xml_text)
    entries: list[TableEntry] = []
    for index, wrap in enumerate(root.iter("table-wrap")):
        label_el = wrap.find("label")
        label = _normalize_ws(_text_content(label_el)) if label_el is not None else ""

        caption_el = wrap.find("caption")
        caption = _normalize_ws(_text_content(caption_el)) if caption_el is not None else ""

        rows: list[tuple[str, ...]] = []
        table_el = wrap.find(".//table")
        if table_el is not None:
            for tr in table_el.iter("tr"):
                cells = _row_cells(tr)
                if cells:
                    rows.append(cells)

        table_id = wrap.get("id") or f"table-{index}"
        entries.append(
            TableEntry(
                table_id=table_id,
                label=label,
                number=_extract_figure_number(label) if label else None,
                caption=caption,
                rows=tuple(rows),
            )
        )
    return entries


@dataclass(frozen=True, slots=True)
class ArticleMetadata:
    """Bibliographic fields pulled from a fullTextXML document's `<front>`.

    Deterministic/structural extraction (no LLM involved) — this is the
    "bibliographic from S0/metadata" half of S2 the workflow plan calls for;
    only `study_design` needs a model call.
    """

    title: str | None
    journal: str | None
    year: int | None
    authors: tuple[str, ...]
    doi: str | None


def parse_article_metadata(xml_text: str) -> ArticleMetadata:
    """Parse `<article-meta>` bibliographic fields out of a fullTextXML document."""
    root = ET.fromstring(xml_text)
    meta = root.find(".//article-meta")
    if meta is None:
        return ArticleMetadata(title=None, journal=None, year=None, authors=(), doi=None)

    title_el = meta.find(".//title-group/article-title")
    title = _normalize_ws(_text_content(title_el)) if title_el is not None else None

    journal_el = root.find(".//journal-meta//journal-title")
    journal = _normalize_ws(_text_content(journal_el)) if journal_el is not None else None

    year: int | None = None
    for pub_date in meta.findall(".//pub-date"):
        year_el = pub_date.find("year")
        if year_el is not None and year_el.text and year_el.text.strip().isdigit():
            year = int(year_el.text.strip())
            break

    authors: list[str] = []
    for contrib in meta.findall('.//contrib-group/contrib[@contrib-type="author"]'):
        name_el = contrib.find("name")
        if name_el is None:
            continue
        surname_el = name_el.find("surname")
        given_el = name_el.find("given-names")
        surname = _normalize_ws(_text_content(surname_el)) if surname_el is not None else ""
        given = _normalize_ws(_text_content(given_el)) if given_el is not None else ""
        full = " ".join(part for part in (given, surname) if part)
        if full:
            authors.append(full)

    doi: str | None = None
    for article_id in meta.findall('.//article-id[@pub-id-type="doi"]'):
        if article_id.text:
            doi = article_id.text.strip()
            break

    return ArticleMetadata(title=title, journal=journal, year=year, authors=tuple(authors), doi=doi)


# --- thin network I/O (not covered by pure-parser tests) --------------------------------


async def fetch_fulltext_xml(client: httpx.AsyncClient, pmcid: str) -> str:
    """GET the EuropePMC fullTextXML for `pmcid` (e.g. "PMC1226180")."""
    response = await client.get(EUROPEPMC_FULLTEXT_URL.format(pmcid=pmcid))
    response.raise_for_status()
    return response.text


async def fetch_article_html(client: httpx.AsyncClient, pmcid: str) -> str:
    """GET the PMC article HTML page for `pmcid` (needs a browser User-Agent)."""
    response = await client.get(
        PMC_ARTICLE_URL.format(pmcid=pmcid),
        headers={"User-Agent": BROWSER_USER_AGENT},
    )
    response.raise_for_status()
    return response.text


async def fetch_oa_status(client: httpx.AsyncClient, pmcid: str) -> str | None:
    """GET the NCBI OA service result for `pmcid`; returns the license or None."""
    response = await client.get(OA_SERVICE_URL, params={"id": pmcid})
    response.raise_for_status()
    return parse_oa_response(response.text)


async def fetch_image_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    """GET the raw bytes of a matched blob URL."""
    response = await client.get(url, headers={"User-Agent": BROWSER_USER_AGENT})
    response.raise_for_status()
    return response.content
