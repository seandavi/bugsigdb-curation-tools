"""End-to-end tests for the `bugsigdb curate` CLI command.

Uses `--mock` (MockModel, no API key) with `pytest_httpx`-mocked
idconv/EuropePMC/PMC-HTML/NCBI-esearch calls -- no live network, no
API key required to run this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import NCBI_ESEARCH_URL
from bugsigdb_curation.pmc_map import IDCONV_URL
from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL

runner = CliRunner()

PMID = "30854760"
PMCID = "PMC6750128"

XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <article-meta><title-group><article-title>A vaginal microbiome study</article-title></title-group></article-meta>
  </front>
  <body>
    <sec id="s1"><title>Methods</title><p>Cerclage cohort; LEfSe for differential abundance.</p></sec>
    <table-wrap id="T1">
      <label>Table 1.</label>
      <caption><p>Differentially abundant taxa.</p></caption>
      <table><tbody>
        <tr><td>Faecalibacterium prausnitzii</td><td>decreased</td></tr>
        <tr><td>Escherichia coli</td><td>increased</td></tr>
      </tbody></table>
    </table-wrap>
  </body>
</article>
"""


def _mock_all(httpx_mock: HTTPXMock, *, pmid: str = PMID, pmcid: str = PMCID) -> None:
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {"ids": pmid, "idtype": "pmid", "format": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"status": "ok", "records": [{"pmid": pmid, "pmcid": pmcid, "doi": "10.1/x"}]},
    )
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=pmcid), text=XML_FIXTURE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid=pmcid), text="<html><body></body></html>")
    # esearch is queried with the *normalized* (lowercased) name -- see
    # taxonomy.py's resolve_name / normalize_taxon_name -- plus the
    # etiquette params (tool/email) every E-utilities call now carries.
    httpx_mock.add_response(
        url=httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(
            {
                "db": "taxonomy",
                "term": "faecalibacterium prausnitzii",
                "retmode": "json",
                "tool": "bugsigdb-curation",
                "email": DEFAULT_EMAIL,
            }
        ),
        json={"esearchresult": {"idlist": ["853"]}},
    )
    httpx_mock.add_response(
        url=httpx.URL(NCBI_ESEARCH_URL).copy_merge_params(
            {
                "db": "taxonomy",
                "term": "escherichia coli",
                "retmode": "json",
                "tool": "bugsigdb-curation",
                "email": DEFAULT_EMAIL,
            }
        ),
        json={"esearchresult": {"idlist": ["562"]}},
    )


def test_curate_single_pmid_writes_valid_json_record(httpx_mock: HTTPXMock, tmp_path: Path):
    _mock_all(httpx_mock)
    out_path = tmp_path / "prediction.json"
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(
        app,
        ["curate", "--pmid", PMID, "--mock", "--out", str(out_path), "--taxonomy-cache", str(cache_path)],
    )

    assert result.exit_code == 0, result.output
    record = json.loads(out_path.read_text())
    assert record["uid"] == PMID
    assert len(record["experiments"]) == 1
    # `_report_result` now logs the outcome (structured, via loguru) rather
    # than printing a rich "PMID ...: valid" line -- the default console sink
    # still lands on stderr, so the message/kv fields are still visible there.
    assert "curate result" in result.stderr
    assert "valid=True" in result.stderr


def test_curate_single_pmid_logs_structured_json_result(httpx_mock: HTTPXMock, tmp_path: Path):
    """Same run as above, but with `--log-format json`: every stderr line is a
    parseable JSON object, and one of them is the `curate_result` event
    carrying the structured fields `_report_result` binds (pmid/valid/
    has_pmc/n_experiments/n_problems) -- proof the CLI's own summary line is
    genuinely structured, not just human-readable prose that happens to
    contain the word "valid"."""
    _mock_all(httpx_mock)
    out_path = tmp_path / "prediction.json"
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(
        app,
        [
            "curate",
            "--pmid",
            PMID,
            "--mock",
            "--out",
            str(out_path),
            "--taxonomy-cache",
            str(cache_path),
            "--log-format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    curate_results = [r for r in records if r["record"]["extra"].get("event") == "curate_result"]
    assert len(curate_results) == 1
    extra = curate_results[0]["record"]["extra"]
    assert extra["pmid"] == PMID
    assert extra["valid"] is True
    assert extra["has_pmc"] is True
    assert extra["n_experiments"] == 1
    assert extra["n_problems"] == 0


def test_curate_defaults_to_stdout_when_no_out_given(httpx_mock: HTTPXMock, tmp_path: Path):
    _mock_all(httpx_mock)
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(app, ["curate", "--pmid", PMID, "--mock", "--taxonomy-cache", str(cache_path)])

    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)
    assert record["uid"] == PMID


def test_curate_requires_exactly_one_of_pmid_or_smoke():
    result = runner.invoke(app, ["curate", "--mock"])
    assert result.exit_code == 2

    result = runner.invoke(app, ["curate", "--pmid", PMID, "--smoke", "--mock"])
    assert result.exit_code == 2


def test_curate_smoke_requires_out(tmp_path: Path):
    result = runner.invoke(app, ["curate", "--smoke", "--mock"])
    assert result.exit_code == 2


def test_curate_smoke_writes_one_file_per_study(httpx_mock: HTTPXMock, tmp_path: Path, monkeypatch):
    # Restrict the smoke set to just one id for a fast, fully-mocked test.
    import bugsigdb_curation.cli as cli_module

    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: [PMID])
    _mock_all(httpx_mock)
    out_dir = tmp_path / "smoke_out"
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(
        app, ["curate", "--smoke", "--mock", "--out", str(out_dir), "--taxonomy-cache", str(cache_path)]
    )

    assert result.exit_code == 0, result.output
    written = list(out_dir.glob("*.json"))
    assert len(written) == 1
    assert written[0].name == f"{PMID}.json"


def test_curate_smoke_reuses_one_client_across_studies(tmp_path: Path, monkeypatch):
    """The --smoke loop must create ONE httpx.AsyncClient for the whole run
    and pass it into every curate_async call, rather than letting each call
    open (and curate_async tear down) its own client -- connection churn /
    weaker keep-alive / more NCBI-PMC rate-limit exposure otherwise.
    Pre-fix, curate_async was called with no `client` kwarg at all, so each
    call opened its own short-lived client."""
    import bugsigdb_curation.cli as cli_module
    from bugsigdb_curation.curator.pipeline import CurationResult

    study_ids = ["111", "222", "333"]
    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: study_ids)

    seen_clients: list[object] = []

    async def fake_curate_async(
        pmid, *, model, config, design=None, client=None, email, taxonomy_cache_path, resolver=None, run_id=None
    ):
        seen_clients.append(client)
        return CurationResult(pmid=pmid, pmcid=None, has_pmc=False, record={}, valid=True, problems=())

    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)

    out_dir = tmp_path / "smoke_out"
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(
        app, ["curate", "--smoke", "--mock", "--out", str(out_dir), "--taxonomy-cache", str(cache_path)]
    )

    assert result.exit_code == 0, result.output
    assert len(seen_clients) == len(study_ids)
    assert all(c is not None for c in seen_clients)
    assert len(set(id(c) for c in seen_clients)) == 1  # same client object every call


def test_curate_smoke_reuses_one_resolver_across_studies(tmp_path: Path, monkeypatch):
    """The --smoke loop must build ONE NcbiTaxonomyResolver for the whole run
    and pass it into every curate_async call, rather than letting each
    curate_async call build its own (fresh _RateLimiter + empty cache) --
    that only throttles calls *within* one study and never lets a taxon
    resolved for one study warm the cache for the next, which is the actual
    cause of the 429 storm a real --smoke run hit. Pre-fix, curate_async was
    called with no `resolver` kwarg at all, so each call built its own."""
    import bugsigdb_curation.cli as cli_module
    from bugsigdb_curation.curator.pipeline import CurationResult

    study_ids = ["111", "222", "333"]
    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: study_ids)

    seen_resolvers: list[object] = []

    async def fake_curate_async(
        pmid, *, model, config, design=None, client=None, email, taxonomy_cache_path, resolver=None, run_id=None
    ):
        seen_resolvers.append(resolver)
        return CurationResult(pmid=pmid, pmcid=None, has_pmc=False, record={}, valid=True, problems=())

    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)

    out_dir = tmp_path / "smoke_out"
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(
        app, ["curate", "--smoke", "--mock", "--out", str(out_dir), "--taxonomy-cache", str(cache_path)]
    )

    assert result.exit_code == 0, result.output
    assert len(seen_resolvers) == len(study_ids)
    assert all(r is not None for r in seen_resolvers)
    assert len(set(id(r) for r in seen_resolvers)) == 1  # same resolver object every call


def test_curate_network_failure_exits_nonzero_with_clean_error(httpx_mock: HTTPXMock, tmp_path: Path):
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {"ids": PMID, "idtype": "pmid", "format": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        status_code=500,
    )
    cache_path = tmp_path / "cache.json"

    result = runner.invoke(app, ["curate", "--pmid", PMID, "--mock", "--taxonomy-cache", str(cache_path)])

    assert result.exit_code == 1
    assert "Error curating PMID" in result.stderr
