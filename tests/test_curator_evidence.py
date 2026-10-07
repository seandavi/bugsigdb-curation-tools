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
    # persistent (the fetch retries a 500 a few times first, so the mock must outlast the retries)
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567"), status_code=500, is_reusable=True)

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


# --- PMC article HTML: challenge detection, retry, throttle, cache ----------------------------------------

import test_curator_pipeline_e2e as _e2e

from bugsigdb_curation.curator.evidence import (
    PmcRequestLimiter,
    fetch_pmc_html,
    is_pmc_challenge,
)

CHALLENGE = "<html><body>" + "x" * 500 + '<div class="g-recaptcha">please verify</div></body></html>'
GOOD = '<html><img src="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/a/1/b/fig-g001.webp"></html>'
URL = PMC_ARTICLE_URL.format(pmcid="PMC1")


def _no_sleep_recorder():
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    return sleeps, sleep


def test_is_pmc_challenge():
    assert is_pmc_challenge(CHALLENGE)
    assert not is_pmc_challenge(GOOD)
    assert not is_pmc_challenge("<html>a short real page with no figures</html>")
    # a real page that has figures AND mentions captcha in a script is not a challenge
    assert not is_pmc_challenge(GOOD + "<script>recaptcha</script>")


def test_challenge_then_success_retries_with_backoff_and_caches(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(url=URL, text=CHALLENGE)
    httpx_mock.add_response(url=URL, text=CHALLENGE)
    httpx_mock.add_response(url=URL, text=GOOD)
    sleeps, sleep = _no_sleep_recorder()

    async def run():
        async with httpx.AsyncClient() as client:
            first = await fetch_pmc_html(
                client, "PMC1", cache_dir=tmp_path, limiter=PmcRequestLimiter(0.0), sleep=sleep, backoff=(5.0, 12.0)
            )
            # second call: served from the cache with no HTTP at all (an extra request would fail the mock)
            second = await fetch_pmc_html(client, "PMC1", cache_dir=tmp_path, sleep=sleep)
            return first, second

    first, second = asyncio.run(run())
    assert first == second == GOOD
    assert len(sleeps) == 2 and 4.0 <= sleeps[0] <= 6.0 and 9.6 <= sleeps[1] <= 14.4  # 5 s and 12 s, +-20% jitter
    assert (tmp_path / "PMC1.html").read_text() == GOOD
    assert len(httpx_mock.get_requests()) == 3


def test_challenge_pages_are_never_cached(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(url=URL, text=CHALLENGE, is_reusable=True)
    sleeps, sleep = _no_sleep_recorder()

    async def run():
        async with httpx.AsyncClient() as client:
            return await fetch_pmc_html(
                client, "PMC1", cache_dir=tmp_path, limiter=PmcRequestLimiter(0.0), sleep=sleep, attempts=3, backoff=(1.0,)
            )

    assert asyncio.run(run()) is None  # exhausted: best-effort None, never raises
    assert len(httpx_mock.get_requests()) == 3 and len(sleeps) == 2
    assert not list(tmp_path.glob("*.html"))


def test_429_is_retried_but_404_and_500_give_up_immediately(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(url=URL, status_code=429)
    httpx_mock.add_response(url=URL, text=GOOD)
    _, sleep = _no_sleep_recorder()

    async def run(pmcid):
        async with httpx.AsyncClient() as client:
            return await fetch_pmc_html(client, pmcid, limiter=PmcRequestLimiter(0.0), sleep=sleep, backoff=(1.0,))

    assert asyncio.run(run("PMC1")) == GOOD
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC404"), status_code=404)
    assert asyncio.run(run("PMC404")) is None
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC500"), status_code=500)
    assert asyncio.run(run("PMC500")) is None
    assert len(httpx_mock.get_requests()) == 4  # 429 + retry, then one each for 404 and 500: no retries


def test_limiter_spaces_requests_by_min_interval():
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    async def run():
        limiter = PmcRequestLimiter(min_interval=3.0)
        for _ in range(3):
            await limiter.acquire(sleep=sleep, clock=lambda: now[0])

    asyncio.run(run())
    assert sleeps == [3.0, 3.0]  # the first request is free


def test_assemble_evidence_survives_a_challenge_and_resolves_figure_blobs(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567"), text=XML_FIXTURE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC1234567"), text=CHALLENGE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC1234567"), text=HTML_FIXTURE)

    async def run() -> EvidenceBundle:
        async with httpx.AsyncClient() as client:
            return await assemble_evidence("21850056", "PMC1234567", client=client)

    assert asyncio.run(run()).figures[0].blob_url is not None  # the old code returned None here, silently


def _ranked_figure_first():
    from bugsigdb_curation.decision import MockDecisionModel, NoulAnswer

    return MockDecisionModel(
        {"s5a_locate": lambda state, qs: {"is_da_artifact": NoulAnswer(0.9 if state["kind"] == "figure" else 0.1)}}
    )


FIG_BLOB = "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/a1/1/b2/fig1.jpg"
FIG_XML = _e2e.XML_FIXTURE.replace(
    "</body>",
    '<fig id="F1"><label>Figure 1.</label><caption><p>LEfSe taxa that differ between groups.</p></caption>'
    '<graphic xlink:href="fig1.jpg"/></fig></body>',
)


def _e2e_study(httpx_mock, tmp_path, *, image_status, n_experiments=2):
    from bugsigdb_curation.curator.model import MockModel
    from bugsigdb_curation.curator.pipeline import curate_async

    _e2e._mock_idconv(httpx_mock)
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=_e2e.PMCID), text=FIG_XML)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid=_e2e.PMCID), text=f'<html><img src="{FIG_BLOB}"></html>')
    _e2e._mock_taxonomy(httpx_mock)
    httpx_mock.add_response(url=FIG_BLOB, status_code=image_status, content=b"\x89PNG\r\n\x1a\n" + b"0" * 16, is_reusable=True)
    segment = {"experiments": [{"index": i, "description": f"comparison {i}"} for i in range(n_experiments)]}
    model = MockModel(responses={"segment": segment})

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                _e2e.PMID, model=model, client=client, decision_model=_ranked_figure_first(),
                taxonomy_cache_path=tmp_path / "t.json", ols_cache_path=tmp_path / "o.json", html_cache_dir=tmp_path / "h",
            )

    return asyncio.run(run()), FIG_BLOB


def test_figure_image_is_fetched_once_per_study_not_once_per_experiment(httpx_mock: HTTPXMock, tmp_path):
    _mock_ols_none(httpx_mock)
    result, blob = _e2e_study(httpx_mock, tmp_path, image_status=200)
    assert sum(1 for r in httpx_mock.get_requests() if str(r.url) == blob) == 1
    assert len(result.record["experiments"]) == 2
    assert "figure_image_unavailable" not in result.annotations


def test_a_failed_image_download_degrades_instead_of_aborting_and_is_flagged(httpx_mock: HTTPXMock, tmp_path):
    _mock_ols_none(httpx_mock)
    result, _ = _e2e_study(httpx_mock, tmp_path, image_status=500)
    assert result.annotations["figure_image_unavailable"] == ["Figure 1"]  # a list, flagged once per figure
    assert result.record["experiments"]  # the study still produced a record


def _mock_ols_none(httpx_mock: HTTPXMock) -> None:
    """The decision-model path also maps body_site via OLS4; answer it with no candidates (optional mock)."""
    import re

    httpx_mock.add_response(url=re.compile(r"https://www\.ebi\.ac\.uk/ols4/api/search.*"), json={"response": {"docs": []}}, is_optional=True, is_reusable=True)


# --- Europe PMC fullTextXML: transient failures are retried ---------------------------------------------------

from bugsigdb_curation.curator.evidence import fetch_fulltext_xml_with_retry  # noqa: E402

XML_URL = EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC1234567")


def _fetch_xml(**kw):
    sleeps, sleep = _no_sleep_recorder()

    async def run():
        async with httpx.AsyncClient() as client:
            return await fetch_fulltext_xml_with_retry(client, "PMC1234567", sleep=sleep, **kw)

    return run, sleeps


def test_fulltext_502_then_success_is_retried_with_backoff(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=XML_URL, status_code=502)
    httpx_mock.add_response(url=XML_URL, status_code=503)
    httpx_mock.add_response(url=XML_URL, text="<article/>")
    run, sleeps = _fetch_xml(backoff=(2.0, 6.0))
    assert asyncio.run(run()) == "<article/>"
    assert len(sleeps) == 2 and 1.6 <= sleeps[0] <= 2.4 and 4.8 <= sleeps[1] <= 7.2
    assert len(httpx_mock.get_requests()) == 3


def test_fulltext_transport_error_is_retried(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), url=XML_URL)
    httpx_mock.add_response(url=XML_URL, text="<article/>")
    run, _ = _fetch_xml()
    assert asyncio.run(run()) == "<article/>"


def test_fulltext_404_and_other_4xx_are_not_retried(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=XML_URL, status_code=404)
    run, sleeps = _fetch_xml()
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert sleeps == [] and len(httpx_mock.get_requests()) == 1


def test_fulltext_persistent_failure_raises_after_the_attempts(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=XML_URL, status_code=500, is_reusable=True)
    run, sleeps = _fetch_xml(attempts=3, backoff=(1.0,))
    with pytest.raises(httpx.HTTPStatusError) as err:
        asyncio.run(run())
    assert err.value.response.status_code == 500 and len(httpx_mock.get_requests()) == 3 and len(sleeps) == 2


def test_assemble_evidence_survives_a_transient_fulltext_502(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=XML_URL, status_code=502)
    httpx_mock.add_response(url=XML_URL, text=XML_FIXTURE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC1234567"), text=HTML_FIXTURE)

    async def run() -> EvidenceBundle:
        async with httpx.AsyncClient() as client:
            return await assemble_evidence("21850056", "PMC1234567", client=client)

    assert asyncio.run(run()).metadata.title == "A CRC microbiome study"  # the blip no longer costs the study
