"""Per-design end-to-end tests for `curate_async` (workflow plan §6b).

`tests/test_curator_pipeline_e2e.py` already proves `fused-lean` (the
default, untouched by this PR) walks S0-S9 end-to-end; this file proves the
same walking skeleton produces a schema-valid, scoreable record for
`split-verify` and `split-panel` too -- entirely offline (`MockModel` +
`pytest_httpx`-mocked idconv/EuropePMC/PMC-HTML + a real built fixture
`TaxonomyDB`, no live network for taxonomy resolution).

Taxon names in the fixture table are deliberately drawn from the shared
synthetic taxdump (`tests/taxonomy_test_support.py`) -- "Bacteroides
fragilis" and "Cutibacterium acnes" -- rather than the `Faecalibacterium
prausnitzii`/`Escherichia coli` pair `test_curator_pipeline_e2e.py` uses, so
S6-reconcile resolves both purely from the local DB with no live-esearch
gap-fill mock needed.
"""

from __future__ import annotations

import asyncio

import httpx
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.design import Design
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.pipeline import curate_async
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver
from bugsigdb_curation.pmc_map import IDCONV_URL
from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL
from bugsigdb_curation.taxonomy.build import build_taxonomy_db
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from bugsigdb_curation.validate import default_schema_path, validate_instance
from taxonomy_test_support import TAXID_BACTEROIDES_FRAGILIS, TAXID_CUTIBACTERIUM_ACNES, write_synthetic_taxdump

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
          <tr><td>Bacteroides fragilis</td><td>decreased</td></tr>
          <tr><td>Cutibacterium acnes</td><td>increased</td></tr>
        </tbody>
      </table>
    </table-wrap>
  </body>
</article>
"""

HTML_FIXTURE = "<html><body>no figures in this fixture</body></html>"

_NER_RESPONSE = {
    "taxa": [
        {"name": "Bacteroides fragilis", "direction": "decreased"},
        {"name": "Cutibacterium acnes", "direction": "increased"},
    ]
}


def _mock_idconv_and_fulltext(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {"ids": PMID, "idtype": "pmid", "format": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"status": "ok", "records": [{"pmid": PMID, "pmcid": PMCID, "doi": "10.1/x"}]},
    )
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=PMCID), text=XML_FIXTURE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid=PMCID), text=HTML_FIXTURE)


def _build_fixture_resolver(tmp_path) -> NcbiTaxonomyResolver:
    taxdump_dir = write_synthetic_taxdump(tmp_path / "taxdump")
    db_path = tmp_path / "taxonomy.duckdb"
    build_taxonomy_db(taxdump_dir, db_path, release="test", source="fixture", build_timestamp="2026-07-14T00:00:00+00:00")
    db = TaxonomyDB(db_path)
    return NcbiTaxonomyResolver(cache={}, cache_path=None, db=db)


def _run_curate(pmid, *, model, design, resolver, httpx_mock: HTTPXMock, tmp_path):
    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                pmid,
                model=model,
                design=design,
                client=client,
                resolver=resolver,
                taxonomy_cache_path=tmp_path / "unused_cache.json",
            )

    return asyncio.run(run())


def test_curate_async_defaults_to_fused_lean_with_no_flags(tmp_path, httpx_mock: HTTPXMock):
    """No `design=` kwarg -> `Design.fused_lean`, unchanged output, and an
    empty `flags` tuple (no semantic A2 stage exists to flag anything)."""
    _mock_idconv_and_fulltext(httpx_mock)
    httpx_mock.add_response(
        url=httpx.URL("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").copy_merge_params(
            {"db": "taxonomy", "term": "bacteroides fragilis", "retmode": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"esearchresult": {"idlist": [str(TAXID_BACTEROIDES_FRAGILIS)]}},
    )
    httpx_mock.add_response(
        url=httpx.URL("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").copy_merge_params(
            {"db": "taxonomy", "term": "cutibacterium acnes", "retmode": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"esearchresult": {"idlist": [str(TAXID_CUTIBACTERIUM_ACNES)]}},
    )
    model = MockModel(
        responses={
            "signature_extract": {
                "taxa": [
                    {"name": "Bacteroides fragilis", "direction": "decreased", "proposed_ncbi_id": TAXID_BACTEROIDES_FRAGILIS},
                    {"name": "Cutibacterium acnes", "direction": "increased", "proposed_ncbi_id": TAXID_CUTIBACTERIUM_ACNES},
                ]
            }
        }
    )
    resolver = NcbiTaxonomyResolver(cache={}, cache_path=None, db=None)

    result = _run_curate(PMID, model=model, design=Design.fused_lean, resolver=resolver, httpx_mock=httpx_mock, tmp_path=tmp_path)

    assert result.valid, result.problems
    assert result.design is Design.fused_lean
    assert result.flags == ()


def test_curate_async_split_verify_produces_valid_record(tmp_path, httpx_mock: HTTPXMock):
    _mock_idconv_and_fulltext(httpx_mock)
    model = MockModel(
        responses={
            "signature_ner": _NER_RESPONSE,
            "verify_taxon_in_source": {
                "results": [
                    {"name": "Bacteroides fragilis", "in_source": True},
                    {"name": "Cutibacterium acnes", "in_source": True},
                ]
            },
            "verify_direction": lambda messages: {
                "direction": "decreased" if "Bacteroides fragilis" in messages[0]["content"][0]["text"] else "increased"
            },
        }
    )
    resolver = _build_fixture_resolver(tmp_path)

    result = _run_curate(PMID, model=model, design=Design.split_verify, resolver=resolver, httpx_mock=httpx_mock, tmp_path=tmp_path)
    resolver.close()

    assert result.valid, result.problems
    assert validate_instance(result.record, "Study", default_schema_path()) == []
    assert result.design is Design.split_verify

    taxa = {t["taxon_name"]: t.get("ncbi_id") for exp in result.record["experiments"] for sig in exp.get("signatures", []) for t in sig["taxa"]}
    assert taxa == {"Bacteroides fragilis": TAXID_BACTEROIDES_FRAGILIS, "Cutibacterium acnes": TAXID_CUTIBACTERIUM_ACNES}


def test_curate_async_dispatches_correctly_when_design_is_a_plain_string(tmp_path, httpx_mock: HTTPXMock):
    """`Design` is a `str` subclass whose docstring promises a plain string
    interoperates with the enum member everywhere -- but `pipeline.py`'s
    dispatch uses `is Design.X`, which a bare string is never `is`, even
    though it compares equal. Passing `design="split-verify"`/`"split-panel"`
    (plain strings, not `Design` members) must still run the correct
    branch (not fall through to the final `assert design is Design.
    split_panel` and crash) and must come back with `.design` set to the
    real enum member (`cli.py`'s `result.design.value` would otherwise
    `AttributeError` on a bare string)."""
    _mock_idconv_and_fulltext(httpx_mock)
    verify_model = MockModel(
        responses={
            "signature_ner": _NER_RESPONSE,
            "verify_taxon_in_source": {
                "results": [
                    {"name": "Bacteroides fragilis", "in_source": True},
                    {"name": "Cutibacterium acnes", "in_source": True},
                ]
            },
            "verify_direction": lambda messages: {
                "direction": "decreased" if "Bacteroides fragilis" in messages[0]["content"][0]["text"] else "increased"
            },
        }
    )
    resolver = _build_fixture_resolver(tmp_path)

    result = _run_curate(
        PMID, model=verify_model, design="split-verify", resolver=resolver, httpx_mock=httpx_mock, tmp_path=tmp_path
    )
    resolver.close()

    assert result.valid, result.problems
    assert result.design is Design.split_verify
    assert result.design.value == "split-verify"  # exercises the .value access cli.py:_report_result relies on


def test_curate_async_dispatches_correctly_when_design_is_a_plain_string_split_panel(tmp_path, httpx_mock: HTTPXMock):
    _mock_idconv_and_fulltext(httpx_mock)
    panel_model = MockModel(
        responses={
            "signature_ner": _NER_RESPONSE,
            "review_signature": _NER_RESPONSE,  # reviewer agrees with the extractor on both taxa
        }
    )
    resolver = _build_fixture_resolver(tmp_path)

    result = _run_curate(
        PMID, model=panel_model, design="split-panel", resolver=resolver, httpx_mock=httpx_mock, tmp_path=tmp_path
    )
    resolver.close()

    assert result.valid, result.problems
    assert result.design is Design.split_panel
    assert result.design.value == "split-panel"
    assert result.flags == ()


def test_curate_async_split_panel_produces_valid_record(tmp_path, httpx_mock: HTTPXMock):
    _mock_idconv_and_fulltext(httpx_mock)
    model = MockModel(
        responses={
            "signature_ner": _NER_RESPONSE,
            "review_signature": _NER_RESPONSE,  # reviewer agrees with the extractor on both taxa
        }
    )
    resolver = _build_fixture_resolver(tmp_path)

    result = _run_curate(PMID, model=model, design=Design.split_panel, resolver=resolver, httpx_mock=httpx_mock, tmp_path=tmp_path)
    resolver.close()

    assert result.valid, result.problems
    assert validate_instance(result.record, "Study", default_schema_path()) == []
    assert result.design is Design.split_panel
    assert result.flags == ()

    taxa = {t["taxon_name"]: t.get("ncbi_id") for exp in result.record["experiments"] for sig in exp.get("signatures", []) for t in sig["taxa"]}
    assert taxa == {"Bacteroides fragilis": TAXID_BACTEROIDES_FRAGILIS, "Cutibacterium acnes": TAXID_CUTIBACTERIUM_ACNES}
