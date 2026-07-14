"""End-to-end offline walking-skeleton test.

`curate_async(pmid, model=MockModel(), ...)` -> a nested prediction record
that is (a) structurally valid per S9's `validate_instance` gate and (b)
directly consumable by `bugsigdb_curation.eval.score.score_study` -- the
proof the Design-1 skeleton walks end-to-end (S0-S9), entirely offline
(MockModel + `pytest_httpx`-mocked idconv/EuropePMC/PMC-HTML/NCBI-esearch,
no live network).

Per the workflow plan §6e, the data firewall applies to the **curator
package**, not to tests: "you MAY import the eval scorer in a TEST to close
the loop." Importing `bugsigdb_curation.eval` here is safe precisely because
it happens *after* `curate_async` has already produced its prediction --
nothing from the eval/gold side ever flows back into the curator call above.
"""

from __future__ import annotations

import asyncio

import httpx
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.pipeline import curate_async
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.curator.taxonomy import NCBI_ESEARCH_URL
from bugsigdb_curation.pmc_map import IDCONV_URL
from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL
from bugsigdb_curation.validate import default_schema_path, validate_instance

PMID = "21850056"
PMCID = "PMC1234567"

XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <journal-meta><journal-title-group><journal-title>Gut Microbes</journal-title></journal-title-group></journal-meta>
    <article-meta>
      <title-group><article-title>Fecal microbiome in CRC</article-title></title-group>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Smith</surname><given-names>Jane</given-names></name></contrib>
      </contrib-group>
      <pub-date pub-type="epub"><year>2011</year></pub-date>
    </article-meta>
  </front>
  <body>
    <sec id="s1"><title>Methods</title>
      <p>We recruited 40 CRC cases and 40 controls; fecal 16S sequencing; LEfSe for differential abundance.</p>
    </sec>
    <sec id="s2"><title>Results</title>
      <p>Several taxa were differentially abundant between cases and controls.</p>
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


def _mock_idconv(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=httpx.URL(IDCONV_URL).copy_merge_params(
            {"ids": PMID, "idtype": "pmid", "format": "json", "tool": "bugsigdb-curation", "email": DEFAULT_EMAIL}
        ),
        json={"status": "ok", "records": [{"pmid": PMID, "pmcid": PMCID, "doi": "10.1/x"}]},
    )


def _mock_fulltext(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=PMCID), text=XML_FIXTURE)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid=PMCID), text=HTML_FIXTURE)


def _mock_taxonomy(httpx_mock: HTTPXMock) -> None:
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


def test_curate_end_to_end_offline_produces_valid_scoreable_record(httpx_mock: HTTPXMock, tmp_path):
    _mock_idconv(httpx_mock)
    _mock_fulltext(httpx_mock)
    _mock_taxonomy(httpx_mock)

    model = MockModel()

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                PMID, model=model, client=client, taxonomy_cache_path=tmp_path / "ncbi_cache.json"
            )

    result = asyncio.run(run())

    assert result.pmid == PMID
    assert result.pmcid == PMCID
    assert result.has_pmc is True

    # S9: structurally valid against schema/bugsigdb.yaml (closed=True gate).
    assert result.valid, result.problems
    assert validate_instance(result.record, "Study", default_schema_path()) == []

    # The loader nested-dict shape, populated end-to-end.
    assert result.record["uid"] == PMID
    assert result.record["pmid"] == int(PMID)
    assert len(result.record["experiments"]) == 1
    exp = result.record["experiments"][0]
    assert exp["host_species"] == "Homo sapiens"

    sigs = exp["signatures"]
    assert {s["abundance_in_group_1"] for s in sigs} == {"increased", "decreased"}
    taxon_names = {t["taxon_name"] for s in sigs for t in s["taxa"]}
    assert taxon_names == {"Faecalibacterium prausnitzii", "Escherichia coli"}
    taxon_ids = {t.get("ncbi_id") for s in sigs for t in s["taxa"]}
    assert taxon_ids == {853, 562}  # both S6-verified against the (mocked) live authority

    _assert_eval_score_can_consume_this_record(result.record)


def test_curate_survives_fulltext_404_without_aborting_the_study(httpx_mock: HTTPXMock, tmp_path):
    """S0 (idconv) resolves a PMCID -- the study IS in PMC -- but EuropePMC
    has no `fullTextXML` record for it (404). Pre-fix, that 404 propagated
    out of `assemble_evidence` and aborted `curate_async` entirely; now it
    degrades to an empty evidence bundle and the pipeline still produces a
    (structurally valid, if signature-less) record instead of raising. No
    article-HTML or esearch mocks are registered -- if the pipeline tried
    either of those calls on an empty bundle, this test would fail on an
    unmocked request rather than a wrong assertion.
    """
    _mock_idconv(httpx_mock)
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=PMCID), status_code=404)

    model = MockModel()

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                PMID, model=model, client=client, taxonomy_cache_path=tmp_path / "ncbi_cache.json"
            )

    result = asyncio.run(run())

    assert result.pmid == PMID
    assert result.pmcid == PMCID
    assert result.has_pmc is True
    # No table/figure evidence at all -> S5a has nothing to locate -> no
    # signatures for the (still-segmented, per MockModel's canned response)
    # experiment -- degraded, not crashed. An empty `signatures` list is
    # never emitted (`assemble._set` omits empty/None fields, mirroring the
    # loader), so "absent" is the correctly-degraded shape here.
    assert "signatures" not in result.record["experiments"][0]
    assert result.valid, result.problems


def _assert_eval_score_can_consume_this_record(record: dict) -> None:
    """Close the loop: `bugsigdb eval score`'s own scoring machinery can score
    this exact record against a small hand-built gold study. See module
    docstring for why this import is fine in a test but never in curator/*.
    """
    from bugsigdb_curation.eval.gold import GoldExperiment, GoldSignature, GoldStudy
    from bugsigdb_curation.eval.score import score_study
    from bugsigdb_curation.eval.taxonomy import TaxonomyResolver

    gold = GoldStudy(
        study_id=PMID,
        pmid=PMID,
        doi=None,
        title=None,
        journal=None,
        year=None,
        study_design=(),
        pmcid=PMCID,
        has_pmc=True,
        experiments=(
            GoldExperiment(
                experiment_id=f"{PMID}/Experiment 1",
                study_id=PMID,
                experiment_name="Experiment 1",
                location_of_subjects=(),
                host_species="Homo sapiens",
                body_site=("Feces",),
                uberon_id=None,
                condition=("Disease",),
                efo_id=None,
                group_0_name="Control",
                group_1_name="Case",
                group_1_definition=None,
                group_0_sample_size=None,
                group_1_sample_size=None,
                sequencing_type="16S",
                statistical_test=("LEfSe",),
                mht_correction=False,
                signatures=(
                    GoldSignature(
                        signature_id=f"{PMID}/1/1",
                        experiment_id=f"{PMID}/Experiment 1",
                        source="Table 2",
                        source_type="main-table",
                        direction="decreased",
                        taxa=frozenset({853}),
                        curation_state="Complete",
                    ),
                    GoldSignature(
                        signature_id=f"{PMID}/1/2",
                        experiment_id=f"{PMID}/Experiment 1",
                        source="Table 2",
                        source_type="main-table",
                        direction="increased",
                        taxa=frozenset({562}),
                        curation_state="Complete",
                    ),
                ),
            ),
        ),
    )

    resolver = TaxonomyResolver()  # ids are already integers in the prediction; no name resolution needed
    score = score_study(gold, record, resolver)

    assert score.experiment_alignment.matched == [(0, 0)]
    assert score.micro_taxa.tp == 2
    assert score.micro_taxa.fp == 0
    assert score.micro_taxa.fn == 0
    assert score.direction_correct == 2
    assert score.direction_total == 2
