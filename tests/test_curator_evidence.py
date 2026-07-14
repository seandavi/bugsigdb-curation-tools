"""Unit tests for `bugsigdb_curation.curator.evidence` (S1 evidence assembly).

`build_bundle` is pure (inline XML/HTML fixtures, no network). The
`assemble_evidence` / `fetch_figure_image` network paths are covered with
`pytest_httpx` mocks -- no live requests.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.evidence import (
    EvidenceBundle,
    assemble_evidence,
    build_bundle,
    fetch_figure_image,
)
from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL

XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <journal-meta><journal-title-group><journal-title>Gut Microbes</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <title-group><article-title>A CRC microbiome study</article-title></title-group>
      <pub-date pub-type="epub"><year>2020</year></pub-date>
    </article-meta>
  </front>
  <body>
    <sec id="s1"><title>Methods</title><p>We recruited 40 subjects.</p></sec>
    <sec id="s2"><title>Results</title><p>Bacteroides was increased in cases.</p></sec>
    <table-wrap id="T2">
      <label>Table 2.</label>
      <caption><p>Differentially abundant taxa.</p></caption>
      <table>
        <tbody><tr><td>Bacteroides</td><td>3.2</td></tr></tbody>
      </table>
    </table-wrap>
    <fig id="F1">
      <label>Figure 1.</label>
      <caption><p>LEfSe cladogram.</p></caption>
      <graphic xlink:href="IMG_F0001.jpg"/>
    </fig>
  </body>
</article>
"""

HTML_FIXTURE = """
<html><body>
<a href="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ab12/cd34/IMG_F0001.jpg">fig1</a>
</body></html>
"""


def test_build_bundle_assembles_sections_tables_figures_and_metadata():
    bundle = build_bundle("21850056", "PMC1234567", XML_FIXTURE, HTML_FIXTURE)

    assert bundle.pmid == "21850056"
    assert bundle.pmcid == "PMC1234567"
    assert bundle.metadata.title == "A CRC microbiome study"
    assert bundle.metadata.journal == "Gut Microbes"
    assert bundle.metadata.year == 2020

    assert [s.title for s in bundle.sections] == ["Methods", "Results"]
    assert "Methods" in bundle.full_text()
    assert "Bacteroides was increased in cases." in bundle.full_text()

    assert len(bundle.tables) == 1
    table = bundle.tables[0]
    assert table.provenance == "Table 2"
    assert "Bacteroides" in table.as_text()

    assert len(bundle.figures) == 1
    fig = bundle.figures[0]
    assert fig.provenance == "Figure 1"
    assert fig.blob_url == "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ab12/cd34/IMG_F0001.jpg"


def test_build_bundle_handles_missing_html_gracefully():
    bundle = build_bundle("21850056", "PMC1234567", XML_FIXTURE, None)
    assert len(bundle.figures) == 1
    assert bundle.figures[0].blob_url is None


def test_build_bundle_figure_id_falls_back_to_index_when_no_number():
    xml = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
      <fig><caption><p>no label</p></caption><graphic xlink:href="x.jpg"/></fig>
    </body></article>"""
    bundle = build_bundle("1", "PMC1", xml, None)
    assert bundle.figures[0].figure_id == "fig-0"
    assert bundle.figures[0].provenance == "fig-0"


# --- assemble_evidence (network, mocked) ------------------------------------------------


def test_assemble_evidence_fetches_and_builds_bundle(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567"), text=XML_FIXTURE
    )
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC1234567"), text=HTML_FIXTURE)

    async def run() -> EvidenceBundle:
        async with httpx.AsyncClient() as client:
            return await assemble_evidence("21850056", "PMC1234567", client=client)

    bundle = asyncio.run(run())
    assert bundle.metadata.title == "A CRC microbiome study"
    assert bundle.figures[0].blob_url is not None


def test_assemble_evidence_degrades_gracefully_when_html_fetch_fails(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567"), text=XML_FIXTURE
    )
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC1234567"), status_code=500)

    async def run() -> EvidenceBundle:
        async with httpx.AsyncClient() as client:
            return await assemble_evidence("21850056", "PMC1234567", client=client)

    bundle = asyncio.run(run())
    assert len(bundle.sections) == 2  # text/tables unaffected
    assert bundle.figures[0].blob_url is None  # figure just has no resolvable image


def test_assemble_evidence_degrades_gracefully_when_fulltext_404s(httpx_mock: HTTPXMock):
    """EuropePMC returns 404 for a PMCID it has no `fullTextXML` record for
    (in PMC per idconv, but not mirrored into EuropePMC full text) -- that's
    a normal "no full-text channel" outcome, not a failure: the bundle
    still comes back (empty sections/tables/figures/metadata) instead of
    the fetch raising out of the study. No article-HTML mock is registered
    here at all -- proof that a 404'd fulltext skips that fetch entirely
    (nothing to match figure blob URLs against), not just that it degrades."""
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567"), status_code=404)

    async def run() -> EvidenceBundle:
        async with httpx.AsyncClient() as client:
            return await assemble_evidence("21850056", "PMC1234567", client=client)

    bundle = asyncio.run(run())
    assert bundle.pmid == "21850056"
    assert bundle.pmcid == "PMC1234567"
    assert bundle.sections == ()
    assert bundle.tables == ()
    assert bundle.figures == ()
    assert bundle.metadata.title is None
    assert bundle.full_text() == ""


def test_assemble_evidence_propagates_non_404_fulltext_error(httpx_mock: HTTPXMock):
    """A genuine unexpected error (e.g. a 500) fetching fullTextXML must
    still surface -- only "not found" degrades gracefully."""
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567"), status_code=500)

    async def run() -> EvidenceBundle:
        async with httpx.AsyncClient() as client:
            return await assemble_evidence("21850056", "PMC1234567", client=client)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())


def test_build_bundle_handles_none_xml_text():
    """`build_bundle(..., xml_text=None, ...)` is the pure-function half of
    the 404 case above -- exercised directly on inline fixtures, no
    network."""
    bundle = build_bundle("21850056", "PMC1234567", None, HTML_FIXTURE)
    assert bundle.sections == ()
    assert bundle.tables == ()
    assert bundle.figures == ()
    assert bundle.metadata.title is None


# --- fetch_figure_image ------------------------------------------------------------------


def test_fetch_figure_image_downloads_bytes_when_blob_url_present(httpx_mock: HTTPXMock):
    bundle = build_bundle("21850056", "PMC1234567", XML_FIXTURE, HTML_FIXTURE)
    figure = bundle.figures[0]
    httpx_mock.add_response(url=figure.blob_url, content=b"fake-jpeg-bytes")

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_figure_image(figure, client=client)

    assert asyncio.run(run()) == b"fake-jpeg-bytes"


def test_fetch_figure_image_returns_none_without_blob_url():
    bundle = build_bundle("21850056", "PMC1234567", XML_FIXTURE, None)
    figure = bundle.figures[0]
    assert figure.blob_url is None

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_figure_image(figure, client=client)

    assert asyncio.run(run()) is None
