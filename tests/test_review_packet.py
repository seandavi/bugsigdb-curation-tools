"""Tests for bugsigdb_curation.review.packet: the pure HTML builder, manifest, licence gate, evidence fetch."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import re
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock
from review_support import PMID, figure_png, load_annotations, load_draft, sample_evidence, sample_meta

from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL
from bugsigdb_curation.review.packet import (
    EUROPEPMC_CORE_SEARCH_URL,
    MAX_IMAGE_BYTES,
    PacketEvidence,
    build_manifest,
    build_packet,
    cited_artifact,
    fetch_packet_evidence,
    license_allows_embedding,
    load_evidence,
    make_meta,
    save_evidence,
)
from bugsigdb_curation.review.verdicts import canonical_sha256

PNG_B64 = base64.b64encode(figure_png()).decode("ascii")


def _build(*, annotations: Any = ..., evidence: Any = ..., record: dict[str, Any] | None = None) -> str:
    record = record if record is not None else load_draft()
    return build_packet(
        record,
        load_annotations() if annotations is ... else annotations,
        sample_evidence() if evidence is ... else evidence,
        sample_meta(record),
    )


class _Refs(HTMLParser):
    """Collects every src/href (with its tag), <link>/<iframe>/<form action> and <script src>."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []
        self.style_text = ""
        self._in_style = False
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "style":
            self._in_style = True
        for name, value in attrs:
            if name in {"src", "href", "action", "data", "poster", "srcset"} and value is not None:
                self.refs.append((tag, name, value))

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.style_text += data


def _json_block(page: str, element_id: str) -> Any:
    match = re.search(rf'<script type="application/json" id="{element_id}">(.*?)</script>', page, re.DOTALL)
    assert match, element_id
    return json.loads(match.group(1))


def _script(page: str) -> str:
    match = re.search(r"<script>\n(.*?)</script>", page, re.DOTALL)
    assert match
    return match.group(1)


# --- self-containment ----------------------------------------------------------------------


def test_packet_makes_no_external_requests():
    page = _build()
    parser = _Refs()
    parser.feed(page)
    for tag, name, value in parser.refs:
        if re.match(r"(https?:)?//", value):
            assert tag == "a" and name == "href", f"external {name} on <{tag}>: {value}"
    assert {"link", "iframe", "form", "video", "audio", "embed", "object"}.isdisjoint(parser.tags)
    assert "@import" not in parser.style_text
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", parser.style_text)
    assert "url(" not in parser.style_text  # no remote or local resources at all in the stylesheet
    script = _script(page)
    for primitive in (
        "fetch(",
        "XMLHttpRequest",
        "sendBeacon",
        "WebSocket",
        "EventSource",
        "importScripts",
        "new Image",
    ):
        assert primitive not in script


def test_only_allowed_links_are_to_paper_pmc_pubmed_doi():
    parser = _Refs()
    parser.feed(_build())
    external = {v for t, n, v in parser.refs if t == "a" and v.startswith("http")}
    assert external
    for url in external:
        assert re.match(r"https://(pubmed\.ncbi\.nlm\.nih\.gov/|pmc\.ncbi\.nlm\.nih\.gov/articles/|doi\.org/)", url), (
            url
        )


def test_packet_embeds_the_draft_and_meta_exactly():
    record = load_draft()
    payload = _json_block(_build(record=record), "packet-data")
    assert payload["record"] == record
    assert payload["meta"]["draft_sha256"] == canonical_sha256(record)
    assert payload["meta"]["packet_id"] == f"{PMID}-{canonical_sha256(record)[:12]}"


def test_draft_text_cannot_break_out_of_the_page():
    record = load_draft()
    record["title"] = "</script><img src=x onerror=alert(1)>"
    record["experiments"][0]["signatures"][0]["taxa"][0]["taxon_name"] = '"><b id=pwned>'
    page = _build(record=record)
    assert "<img src=x" not in page
    assert "<b id=pwned>" not in page
    assert _json_block(page, "packet-data")["record"] == record


# --- structure: one control per item --------------------------------------------------------


def test_one_control_per_taxon_signature_and_experiment():
    page = _build()
    keys = re.findall(r'data-key="([^"]+)"', page)
    assert len(keys) == len(set(keys)), "every control has a unique key"
    taxon_verdicts = [k for k in keys if re.fullmatch(r"exp\.\d+\.sig\.\d+\.taxon\.\d+\.verdict", k)]
    assert sorted(taxon_verdicts) == sorted(
        [
            "exp.0.sig.0.taxon.0.verdict",
            "exp.0.sig.0.taxon.1.verdict",
            "exp.0.sig.1.taxon.0.verdict",
            "exp.1.sig.0.taxon.0.verdict",
        ]
    )
    assert sorted(k for k in keys if k.endswith(".direction")) == [
        "exp.0.sig.0.direction",
        "exp.0.sig.1.direction",
        "exp.1.sig.0.direction",
    ]
    assert [k for k in keys if re.fullmatch(r"exp\.\d+\.verdict", k)] == ["exp.0.verdict", "exp.1.verdict"]
    assert [k for k in keys if k.endswith(".missing_note")] == ["exp.0.missing_note", "exp.1.missing_note"]
    for required in (
        "study.verdict",
        "study.note",
        "missing_experiments_note",
        "overall.time_saved_rating",
        "overall.would_publish_after_edits",
        "overall.comment",
        "reviewer.name",
        "reviewer.email",
        "reviewer.role",
        "minutes_spent",
    ):
        assert required in keys
    assert page.count('data-action="mark-taxa-correct"') == 3
    assert 'data-action="reset"' in page and 'data-action="export-json"' in page and 'data-action="export-csv"' in page
    assert 'id="progress-text"' in page


def test_banner_header_and_fields():
    page = _build()
    assert "MACHINE-GENERATED DRAFT — UNREVIEWED. Not curated data." in page
    for text in ("gemini-test", "fused-lean", "2026-10-06T12:00:00Z", f"{PMID}-"):
        assert text in page
    assert "Gut microbiome shifts in a synthetic cohort of fixture patients" in page
    assert "Journal of Fixtures" in page and "2024" in page
    assert 'href="https://doi.org/10.1000/review.fixture.1"' in page
    assert f'href="https://pubmed.ncbi.nlm.nih.gov/{PMID}/"' in page
    assert 'href="https://pmc.ncbi.nlm.nih.gov/articles/PMC9000001/"' in page
    assert "case-control" in page
    for text in ("Healthy controls", "Fixture patients", "LEfSe", "Benjamini-Hochberg", "Homo sapiens", "Mus musculus"):
        assert text in page
    assert "INCREASED in Fixture patients (group 1) vs Healthy controls (group 0)" in page
    assert "DECREASED in Fixture patients (group 1) vs Healthy controls (group 0)" in page
    assert "not stated in the draft" in page  # experiment 2 has no MHT correction


def test_unresolved_taxon_shown_with_badge_never_dropped():
    page = _build()
    assert page.count("unresolved id") == 1
    assert "Candidatus Fixtureia unresolvedus" in page
    assert "NCBI:817" in page


# --- annotations ---------------------------------------------------------------------------


def test_ranking_and_body_site_terms_rendered():
    page = _build()
    assert "How the pipeline chose the source table/figure" in page
    assert "0.91" in page
    assert "feces (UBERON:0001988)" in page
    assert "caecum (UBERON:0001153)" in page
    assert page.count("suggestion, low confidence") == 1
    # the mapped suggestion is plain; only the low-confidence one carries the badge
    mapped = re.search(r"“Cecum”.*?</div>", page)
    assert mapped and "low confidence" not in mapped.group(0)


@pytest.mark.parametrize("annotations", [None, {}, {"artifact_ranking": []}])
def test_packet_builds_without_annotations(annotations):
    page = _build(annotations=annotations)
    assert "UBERON" not in page
    assert "How the pipeline chose" not in page
    assert "decision-model step failed" not in page
    assert "MACHINE-GENERATED DRAFT" in page


def test_body_site_terms_absent_but_ranking_present():
    page = _build(annotations={"artifact_ranking": load_annotations()["artifact_ranking"]})
    assert "UBERON" not in page and "How the pipeline chose" in page


def test_unmapped_body_site_term_says_no_suggestion():
    ann = {"body_site_terms": [{"experiment_index": 0, "label": "Feces", "status": "no_candidates"}]}
    page = _build(annotations=ann)
    assert "no ontology term suggested" in page


@pytest.mark.parametrize("key", ["artifact_ranking_error", "body_site_terms_error"])
def test_error_keys_produce_fallback_notice(key):
    page = _build(annotations={key: "DecisionModelError('secret detail')"})
    assert "A decision-model step failed" in page
    assert key in page
    assert "secret detail" not in page


# --- licence gate and evidence -------------------------------------------------------------


@pytest.mark.parametrize(
    ("license_", "allowed"),
    [
        ("cc by", True),
        ("CC BY", True),
        ("cc0", True),
        ("cc by 4.0", True),
        ("cc by-nc", False),
        ("cc by-sa", False),
        ("cc by-nc-nd", False),
        ("cc by-nc-sa", False),
        ("", False),
        (None, False),
        ("publisher license", False),
    ],
)
def test_license_allows_embedding(license_, allowed):
    assert license_allows_embedding(license_) is allowed


@pytest.mark.parametrize("license_", ["cc by", "cc0"])
def test_image_embedded_for_cc_by_and_cc0(license_):
    page = _build(evidence=sample_evidence(license_))
    images = _json_block(page, "packet-images")
    assert images == {"Figure 2": {"type": "image/png", "data": PNG_B64}}
    assert 'data-image-ref="Figure 2"' in page
    assert "Figures shown in this packet are reproduced from" in page
    assert f"License: {license_}" in page
    assert "LEfSe analysis of fixture patients versus healthy controls." in page


@pytest.mark.parametrize("license_", ["cc by-nc", "cc by-sa", "cc by-nc-nd", None])
def test_image_not_embedded_for_other_licenses_but_legend_and_link_remain(license_):
    page = _build(evidence=sample_evidence(license_))
    assert _json_block(page, "packet-images") == {}
    assert PNG_B64 not in page
    assert "<img" not in page
    assert "is not CC BY or CC0" in page
    assert "LEfSe analysis of fixture patients versus healthy controls." in page
    assert 'href="https://pmc.ncbi.nlm.nih.gov/articles/PMC9000001/"' in page
    assert "Figures shown in this packet are reproduced from" not in page


def test_oversized_image_is_linked_not_embedded():
    big = figure_png() + b"\x00" * (MAX_IMAGE_BYTES + 1)
    page = _build(evidence=sample_evidence("cc by", image=big))
    assert _json_block(page, "packet-images") == {}
    assert "too large to embed" in page
    assert "Figures shown in this packet are reproduced from" not in page


def test_image_that_could_not_be_fetched_says_so():
    evidence = sample_evidence("cc by")
    evidence = PacketEvidence(pmcid=evidence.pmcid, license="cc by", figures=evidence.figures, tables=evidence.tables)
    page = _build(evidence=evidence)
    assert "could not be retrieved" in page


def test_table_evidence_rows_and_caption_shown():
    page = _build()
    assert "Evidence: Table 1" in page
    assert "Differentially abundant taxa in treated mice." in page
    assert "<td>Akkermansia muciniphila</td><td>2.1</td><td>0.01</td>" in page


def test_one_evidence_panel_per_source_group_with_shared_figure():
    page = _build()
    # experiment 0's two signatures both cite Figure 2: one sticky panel, not two
    assert page.count("Evidence: Figure 2") == 1
    assert page.count("Evidence: Table 1") == 1
    assert page.count('data-image-ref="Figure 2"') == 1


def test_offline_packet_says_so_and_links_the_paper():
    page = _build(evidence=None)
    assert "built offline" in page
    assert "No evidence could be shown here" in page
    assert 'href="https://pmc.ncbi.nlm.nih.gov/articles/PMC9000001/"' in page
    assert _json_block(page, "packet-images") == {}


def test_missing_artifact_and_unsupported_sources_are_stated_plainly():
    record = load_draft()
    record["experiments"][0]["signatures"][0]["source"] = "Figure 9"
    record["experiments"][0]["signatures"][1]["source"] = "Supplementary Table 2"
    record["experiments"][1]["signatures"][0].pop("source")
    page = _build(record=record)
    assert "“Figure 9” was not found in the article full text" in page
    assert "“Supplementary Table 2” is not a main-text table or figure" in page
    assert "states no source" in page
    assert "none stated" in page


def test_without_pmcid_falls_back_to_pubmed_link():
    record = load_draft()
    meta = make_meta(record, built_at="2026-10-06T12:00:00Z")
    page = build_packet(record, None, None, meta)
    assert "pmc.ncbi.nlm.nih.gov" not in page
    assert "Open the paper on PubMed" in page


def test_draft_without_experiments_still_builds():
    record = load_draft()
    record["experiments"] = []
    assert "The draft contains no experiments." in _build(record=record)


def test_signature_without_taxa_and_unknown_direction():
    record = load_draft()
    record["experiments"][0]["signatures"][0]["taxa"] = []
    record["experiments"][0]["signatures"][0].pop("abundance_in_group_1")
    page = _build(record=record)
    assert "This signature lists no taxa." in page
    assert "DIRECTION NOT STATED" in page


# --- manifest / meta -----------------------------------------------------------------------


def test_manifest_sha_matches_canonical_sha_of_record():
    record = load_draft()
    meta = make_meta(record, model_label="m", design_label="d", pmcid="PMC1", builder_commit="deadbee")
    manifest = build_manifest(record, {"artifact_ranking_error": "x"}, sample_evidence("cc by"), meta)
    assert manifest["draft_sha256"] == canonical_sha256(record)
    assert manifest["packet_id"] == f"{PMID}-{canonical_sha256(record)[:12]}"
    assert manifest["pmid"] == PMID
    assert (manifest["model_label"], manifest["design_label"], manifest["builder_commit"]) == ("m", "d", "deadbee")
    assert (manifest["n_experiments"], manifest["n_signatures"], manifest["n_taxa"]) == (2, 3, 4)
    assert manifest["annotation_errors"] == ["artifact_ranking_error"]
    assert manifest["license"] == "cc by"
    assert manifest["built_at"]


def test_sha_changes_when_draft_changes_but_not_key_order():
    record = load_draft()
    reordered = {k: record[k] for k in reversed(list(record))}
    assert canonical_sha256(record) == canonical_sha256(reordered)
    changed = copy.deepcopy(record)
    changed["experiments"][0]["signatures"][0]["abundance_in_group_1"] = "decreased"
    assert canonical_sha256(record) != canonical_sha256(changed)
    assert make_meta(record).packet_id != make_meta(changed).packet_id


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Figure 4", ("figure", "4")),
        ("figure 3b", ("figure", "3")),
        ("Table 3", ("table", "3")),
        ("table  12", ("table", "12")),
        ("Supplementary Table 2", None),
        ("Table S1", None),
        ("", None),
        (None, None),
    ],
)
def test_cited_artifact(source, expected):
    assert cited_artifact(source) == expected


# --- evidence cache + fetch ----------------------------------------------------------------


def test_evidence_cache_round_trip(tmp_path):
    evidence = sample_evidence("cc by")
    save_evidence(evidence, tmp_path / PMID)
    assert load_evidence(tmp_path / "nope") is None
    assert load_evidence(tmp_path / PMID) == evidence


XML = """<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
<title-group><article-title>T</article-title></title-group></article-meta></front><body>
<table-wrap id="T1"><label>Table 1.</label><caption><p>Cap one.</p></caption>
<table><tbody><tr><td>Akkermansia</td><td>2.1</td></tr></tbody></table></table-wrap>
<table-wrap id="T2"><label>Table 2.</label><caption><p>Not cited.</p></caption>
<table><tbody><tr><td>x</td></tr></tbody></table></table-wrap>
<fig id="F2"><label>Figure 2.</label><caption><p>Cap two.</p></caption><graphic xlink:href="fig2.png"/></fig>
<fig id="F3"><label>Figure 3.</label><caption><p>Not cited.</p></caption><graphic xlink:href="fig3.png"/></fig>
</body></article>"""
BLOB = "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/ab/cd/fig2.png"
HTML = f'<html><a href="{BLOB}">f</a></html>'


def _mock_article(httpx_mock: HTTPXMock, license_: str | None) -> None:
    result: dict[str, Any] = {"pmcid": "PMC9000001"}
    if license_:
        result["license"] = license_
    httpx_mock.add_response(
        url=httpx.URL(
            EUROPEPMC_CORE_SEARCH_URL, params={"query": "PMCID:PMC9000001", "resultType": "core", "format": "json"}
        ),
        json={"resultList": {"result": [result]}},
    )
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC9000001"), text=XML)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC9000001"), text=HTML)


def _fetch(pmcid: str | None = "PMC9000001") -> PacketEvidence:
    async def run() -> PacketEvidence:
        async with httpx.AsyncClient() as client:
            return await fetch_packet_evidence(load_draft(), client=client, pmcid=pmcid)

    return asyncio.run(run())


def test_fetch_keeps_only_cited_artifacts_and_embeds_image_for_cc_by(httpx_mock: HTTPXMock):
    _mock_article(httpx_mock, "cc by")
    httpx_mock.add_response(url=BLOB, content=figure_png())
    evidence = _fetch()
    assert evidence.license == "cc by"
    assert [f.provenance for f in evidence.figures] == ["Figure 2"]
    assert [t.provenance for t in evidence.tables] == ["Table 1"]
    assert evidence.images == {"Figure 2": figure_png()}
    page = build_packet(load_draft(), None, evidence, sample_meta())
    assert PNG_B64 in page


def test_fetch_does_not_download_images_for_non_cc_by_license(httpx_mock: HTTPXMock):
    _mock_article(httpx_mock, "cc by-nc")  # the blob URL is deliberately NOT mocked: a request would fail the test
    evidence = _fetch()
    assert evidence.license == "cc by-nc"
    assert evidence.images == {}
    assert [f.provenance for f in evidence.figures] == ["Figure 2"]


def test_fetch_unknown_license_means_no_images(httpx_mock: HTTPXMock):
    _mock_article(httpx_mock, None)
    assert _fetch().images == {}


def test_fetch_survives_license_lookup_failure(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=re.compile(re.escape(EUROPEPMC_CORE_SEARCH_URL) + ".*"), status_code=500)
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid="PMC9000001"), text=XML)
    httpx_mock.add_response(url=PMC_ARTICLE_URL.format(pmcid="PMC9000001"), text=HTML)
    evidence = _fetch()
    assert evidence.license is None and evidence.images == {} and evidence.figures


def test_fetch_survives_image_download_failure(httpx_mock: HTTPXMock):
    _mock_article(httpx_mock, "cc by")
    httpx_mock.add_response(url=BLOB, status_code=503)
    evidence = _fetch()
    assert evidence.images == {} and evidence.license == "cc by"
