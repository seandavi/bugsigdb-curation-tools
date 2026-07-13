"""Unit tests for `benchmarks/figure-extraction/retrieve.py`'s pure parsers.

All tests here use tiny inline XML/HTML fixtures — no live network access.
The one test that does hit the network is marked `@pytest.mark.network` and
is deselected by default (see `pyproject.toml`'s `addopts`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import retrieve

FULLTEXT_XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <body>
    <fig id="F1" position="float">
      <label>Figure 1.</label>
      <caption>
        <title>Alpha diversity.</title>
        <p>Relative abundance of <italic>Bacteroides</italic> spp. in cases vs controls.</p>
      </caption>
      <graphic xlink:href="TEMI_A_0000001_F0001_OC.jpg"/>
    </fig>
    <fig id="F2" position="float">
      <label>Figure 2.</label>
      <caption><p>LEfSe LDA scores   for  differentially abundant taxa.</p></caption>
      <graphic xlink:href="TEMI_A_0000001_F0002_OC.jpg"/>
    </fig>
    <fig id="F3" position="float">
      <label>Figure 3.</label>
      <caption><p>Panel A and B show cladograms.</p></caption>
      <graphic xlink:href="TEMI_A_0000001_F0003_OC.png"/>
    </fig>
  </body>
</article>
"""

ARTICLE_HTML_FIXTURE = """
<html><body>
<a href="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ab12/cd34/TEMI_A_0000001_F0001_OC.jpg">
  <img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ab12/cd34/TEMI_A_0000001_F0001_OC.jpg" />
</a>
<a href="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ef56/gh78/TEMI_A_0000001_F0002_OC.jpg">fig2</a>
<a href='https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ij90/kl12/TEMI_A_0000001_F0003_OC.png'>fig3</a>
<a href="https://example.com/not-a-blob.jpg">unrelated</a>
</body></html>
"""

OA_XML_OPEN = """<?xml version="1.0"?>
<OA>
  <records>
    <record id="PMC1226180" citation="..." license="CC BY">
      <link format="tgz" href="ftp://example/foo.tar.gz"/>
    </record>
  </records>
</OA>
"""

OA_XML_ERROR = """<?xml version="1.0"?>
<OA>
  <error code="idIsNotOpenAccess">identifier is not Open Access</error>
</OA>
"""


# --- parse_fulltext_figures ---------------------------------------------------------


def test_parse_fulltext_figures_extracts_label_legend_and_filename():
    figures = retrieve.parse_fulltext_figures(FULLTEXT_XML_FIXTURE)

    assert len(figures) == 3
    fig1 = figures[0]
    assert fig1.label == "Figure 1."
    assert fig1.number == "1"
    assert fig1.graphic_filename == "TEMI_A_0000001_F0001_OC.jpg"
    # Nested tags (title, italic) are flattened and whitespace collapsed.
    assert fig1.legend == "Alpha diversity. Relative abundance of Bacteroides spp. in cases vs controls."


def test_parse_fulltext_figures_collapses_internal_whitespace():
    figures = retrieve.parse_fulltext_figures(FULLTEXT_XML_FIXTURE)
    fig2 = figures[1]
    assert fig2.legend == "LEfSe LDA scores for differentially abundant taxa."


# --- normalize_source_label / match_figure -------------------------------------------


@pytest.mark.parametrize(
    "source,expected",
    [
        ("Figure 2", "2"),
        ("figure 3b", "3"),
        ("Figure 2B", "2"),
        ("Figure  10", "10"),
        ("Table 1", None),
        ("Figure 2; Supplementary Table 1A", "2"),
    ],
)
def test_normalize_source_label(source, expected):
    assert retrieve.normalize_source_label(source) == expected


def test_match_figure_finds_by_number_ignoring_panel_letter():
    figures = retrieve.parse_fulltext_figures(FULLTEXT_XML_FIXTURE)

    matched = retrieve.match_figure("figure 3b", figures)

    assert matched is not None
    assert matched.label == "Figure 3."
    assert matched.graphic_filename == "TEMI_A_0000001_F0003_OC.png"


def test_match_figure_returns_none_for_non_figure_source():
    figures = retrieve.parse_fulltext_figures(FULLTEXT_XML_FIXTURE)
    assert retrieve.match_figure("Table 1", figures) is None


def test_match_figure_returns_none_when_number_not_present():
    figures = retrieve.parse_fulltext_figures(FULLTEXT_XML_FIXTURE)
    assert retrieve.match_figure("Figure 99", figures) is None


# --- extract_blob_urls / match_filename_to_blob --------------------------------------


def test_extract_blob_urls_finds_all_cdn_links_deduped_and_ignores_unrelated():
    urls = retrieve.extract_blob_urls(ARTICLE_HTML_FIXTURE)

    assert len(urls) == 3
    assert all("cdn.ncbi.nlm.nih.gov/pmc/blobs" in u for u in urls)
    assert not any("example.com" in u for u in urls)


def test_extract_blob_urls_dedupes_repeated_occurrences():
    html = ARTICLE_HTML_FIXTURE + ARTICLE_HTML_FIXTURE  # duplicate the whole fixture
    urls = retrieve.extract_blob_urls(html)
    assert len(urls) == 3


def test_match_filename_to_blob_matches_by_suffix():
    urls = retrieve.extract_blob_urls(ARTICLE_HTML_FIXTURE)

    matched = retrieve.match_filename_to_blob("TEMI_A_0000001_F0002_OC.jpg", urls)

    assert matched == "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ef56/gh78/TEMI_A_0000001_F0002_OC.jpg"


def test_match_filename_to_blob_falls_back_to_stem_on_extension_mismatch():
    urls = ["https://cdn.ncbi.nlm.nih.gov/pmc/blobs/xx/yy/TEMI_A_0000001_F0003_OC.jpeg"]

    matched = retrieve.match_filename_to_blob("TEMI_A_0000001_F0003_OC.png", urls)

    assert matched == urls[0]


def test_match_filename_to_blob_returns_none_when_no_match():
    urls = retrieve.extract_blob_urls(ARTICLE_HTML_FIXTURE)
    assert retrieve.match_filename_to_blob("does_not_exist.jpg", urls) is None


# --- parse_oa_response ----------------------------------------------------------------


def test_parse_oa_response_returns_license_for_open_access_record():
    assert retrieve.parse_oa_response(OA_XML_OPEN) == "CC BY"


def test_parse_oa_response_returns_none_for_error():
    assert retrieve.parse_oa_response(OA_XML_ERROR) is None


# --- build_image_path ------------------------------------------------------------------


def test_build_image_path_uses_pmcid_and_figure_number_with_source_extension():
    path = retrieve.build_image_path(
        Path("data/figbench/images"), "PMC1226180", "2", "TEMI_A_0000001_F0002_OC.jpg"
    )
    assert path == Path("data/figbench/images/PMC1226180_F2.jpg")


def test_build_image_path_defaults_extension_when_filename_has_none():
    path = retrieve.build_image_path(Path("out"), "PMC1", "1", "no_extension")
    assert path == Path("out/PMC1_F1.jpg")


# --- end-to-end network test (opt-in) --------------------------------------------------


@pytest.mark.network
def test_fetch_and_match_one_known_figure_end_to_end():
    """Exercises the full recipe against the real network for one known figure.

    PMC1226180 (PMID 15987522) is one of the benchmark's included studies,
    with its curated signature sourced from "Figure 1" — a known-good,
    stable target for this smoke test.
    """
    pmcid = "PMC1226180"

    async def run() -> None:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            oa_license = await retrieve.fetch_oa_status(client, pmcid)
            assert oa_license  # must be Open Access to be usable at all

            xml_text = await retrieve.fetch_fulltext_xml(client, pmcid)
            figures = retrieve.parse_fulltext_figures(xml_text)
            fig = retrieve.match_figure("Figure 1", figures)
            assert fig is not None
            assert fig.graphic_filename

            html_text = await retrieve.fetch_article_html(client, pmcid)
            blob_urls = retrieve.extract_blob_urls(html_text)
            blob_url = retrieve.match_filename_to_blob(fig.graphic_filename, blob_urls)
            assert blob_url is not None

            image_bytes = await retrieve.fetch_image_bytes(client, blob_url)
            assert len(image_bytes) > 1000

    asyncio.run(run())
