"""Retrieval helpers for the figure-extraction benchmark.

Implements the verified retrieval recipe (see ``README.md``) for turning a
BugSigDB `(pmcid, "Figure N")` reference into a legend + a downloaded image:

1. EuropePMC ``fullTextXML`` -> per-figure label/legend/graphic-filename
   (:func:`parse_fulltext_figures`, :func:`match_figure`).
2. The PMC article HTML -> candidate CDN blob URLs
   (:func:`extract_blob_urls`, :func:`match_filename_to_blob`).
3. The matched blob URL -> raw image bytes (:func:`fetch_image_bytes`).
4. The NCBI OA service -> license string, used as an inclusion gate
   (:func:`parse_oa_response`).

The parsing/matching logic above is pure (string/XML in, data out) and is
covered by ``tests/test_figbench_retrieve.py`` using tiny inline fixtures —
no network access required. The `fetch_*` functions are thin wrappers around
`httpx` that do the actual HTTP calls; they're exercised by a single
opt-in ``@pytest.mark.network`` test.
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
