"""Structured-logging coverage for `curate_async`'s S0-S9 stage events.

`tests/test_curator_pipeline_e2e.py` already proves the walking skeleton
produces a valid, scoreable record end-to-end; this file proves the *log
stream* that same run produces is genuinely structured -- each stage emits a
record carrying `stage`/expected fields, and every record is contextualized
with `study_id`/`pmid`/`run_id` -- fully offline (`MockModel` +
`pytest_httpx`-mocked idconv/EuropePMC/PMC-HTML/NCBI-esearch).

Records are captured via `logger.add(records.append, ...)` -- a plain Python
sink, not `configure_logging`'s console/JSON sink -- which loguru always
calls with the (dict-like) record itself, no string parsing needed.
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.pipeline import curate_async
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import NCBI_ESEARCH_URL
from bugsigdb_curation.pmc_map import IDCONV_URL
from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL

PMID = "21850056"
PMCID = "PMC1234567"

XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <journal-meta><journal-title-group><journal-title>Gut Microbes</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <title-group><article-title>Fecal microbiome in CRC</article-title></title-group>
      <pub-date pub-type="epub"><year>2011</year></pub-date>
    </article-meta>
  </front>
  <body>
    <sec id="s1"><title>Methods</title>
      <p>We recruited 40 CRC cases and 40 controls; fecal 16S sequencing; LEfSe for differential abundance.</p>
    </sec>
    <table-wrap id="T2">
      <label>Table 2.</label>
      <caption><p>Differentially abundant taxa (LEfSe).</p></caption>
      <table>
        <thead><tr><th>Taxon</th><th>Direction</th></tr></thead>
        <tbody>
          <tr><td>Faecalibacterium prausnitzii</td><td>decreased</td></tr>
          <tr><td>Escherichia coli</td><td>increased</td></tr>
        </tbody>
      </table>
    </table-wrap>
  </body>
</article>
"""

HTML_FIXTURE = "<html><body>no figures in this fixture</body></html>"


def _mock_everything(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {"ids": PMID, "idtype": "pmid", "format": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"status": "ok", "records": [{"pmid": PMID, "pmcid": PMCID, "doi": "10.1/x"}]},
    )
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=PMCID), text=XML_FIXTURE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid=PMCID), text=HTML_FIXTURE)
    for term, taxid in (("faecalibacterium prausnitzii", "853"), ("escherichia coli", "562")):
        httpx_mock.add_response(
            url=httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(
                {
                    "db": "taxonomy",
                    "term": term,
                    "retmode": "json",
                    "tool": "bugsigdb-curation",
                    "email": DEFAULT_EMAIL,
                }
            ),
            json={"esearchresult": {"idlist": [taxid]}},
        )


def _curate_capturing_records(tmp_path, httpx_mock: HTTPXMock) -> list[dict]:
    _mock_everything(httpx_mock)
    records: list[dict] = []
    logger.remove()
    logger.configure(extra={"stage": "-"})
    logger.add(lambda message: records.append(message.record), level="DEBUG")

    model = MockModel()

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                PMID,
                model=model,
                client=client,
                taxonomy_cache_path=tmp_path / "ncbi_cache.json",
                run_id="test-run-id",
            )

    result = asyncio.run(run())
    assert result.valid, result.problems
    return records


def _events_for_stage(records: list[dict], stage: str) -> list[dict]:
    return [r for r in records if r["extra"].get("stage") == stage]


def test_every_record_is_contextualized_with_study_id_pmid_and_run_id(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    assert records  # sanity: the run actually logged something
    for record in records:
        assert record["extra"]["study_id"] == PMID
        assert record["extra"]["pmid"] == PMID
        assert record["extra"]["run_id"] == "test-run-id"


def test_s0_resolved_event_carries_pmcid_and_has_pmc(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    s0 = _events_for_stage(records, "S0")
    assert len(s0) == 1
    assert s0[0]["extra"]["pmcid"] == PMCID
    assert s0[0]["extra"]["has_pmc"] is True


def test_s1_evidence_event_carries_section_table_figure_counts(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    s1 = _events_for_stage(records, "S1")
    assert len(s1) == 1
    assert s1[0]["extra"]["n_sections"] == 1
    assert s1[0]["extra"]["n_tables"] == 1
    assert s1[0]["extra"]["n_figures"] == 0


def test_s3_segmented_event_carries_n_experiments(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    s3 = _events_for_stage(records, "S3")
    assert len(s3) == 1
    assert s3[0]["extra"]["n_experiments"] == 1


def test_s5b_signatures_event_carries_taxa_resolution_counts(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    s5b = _events_for_stage(records, "S5b")
    assert len(s5b) == 1
    assert s5b[0]["extra"]["n_taxa"] == 2
    assert s5b[0]["extra"]["n_resolved"] == 2
    assert s5b[0]["extra"]["n_unresolved"] == 0


def test_s6_resolution_events_report_gapfill_source_and_http_status(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    s6 = _events_for_stage(records, "S6")
    gapfill = [r for r in s6 if r["extra"].get("source") == "gapfill"]
    assert len(gapfill) == 2
    resolved_names = {r["extra"]["taxon_name"] for r in gapfill}
    assert resolved_names == {"Faecalibacterium prausnitzii", "Escherichia coli"}
    for r in gapfill:
        assert r["extra"]["ncbi_id"] in (853, 562)


def test_s9_validated_event_and_study_done_event(tmp_path, httpx_mock: HTTPXMock):
    records = _curate_capturing_records(tmp_path, httpx_mock)
    s9 = _events_for_stage(records, "S9")
    assert len(s9) == 1
    assert s9[0]["extra"]["valid"] is True
    assert s9[0]["extra"]["n_problems"] == 0

    study_done = [r for r in records if r["extra"].get("event") == "study_done"]
    assert len(study_done) == 1
    extra = study_done[0]["extra"]
    assert extra["valid"] is True
    assert extra["n_experiments"] == 1
    assert extra["n_signatures"] == 2
    assert isinstance(extra["latency_ms"], int)
    assert extra["latency_ms"] >= 0
