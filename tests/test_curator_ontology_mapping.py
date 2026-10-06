"""Tests for S4 body-site -> UBERON mapping (`curator.routing.map_body_sites`) and its pipeline wiring."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import test_curator_pipeline_e2e as e2e
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.ols import OLS_SEARCH_URL, OlsClient
from bugsigdb_curation.curator.pipeline import _body_site_terms, curate_async
from bugsigdb_curation.curator.routing import (
    NONE_OF_THESE,
    ONTOLOGY_CONFIDENCE_THRESHOLD,
    OntologyMapping,
    map_body_sites,
)
from bugsigdb_curation.curator.taxonomy import _RateLimiter
from bugsigdb_curation.decision import Choice, ChoiceAnswer, DecisionModelError, MockDecisionModel, NoulAnswer

FECES = {"obo_id": "UBERON:0001988", "label": "feces", "description": ["Excreted waste."], "exact_synonyms": ["faeces"]}
GUT = {"obo_id": "UBERON:0001555", "label": "digestive tract"}
SKIN = {"obo_id": "UBERON:0002097", "label": "skin of body"}


def _ols_url(query: str) -> httpx.URL:
    return httpx.URL(OLS_SEARCH_URL).copy_merge_params(
        {"q": query, "ontology": "uberon", "rows": "10", "type": "class", "queryFields": "label,synonym,short_form,obo_id"}
    )


def _mock_ols(httpx_mock: HTTPXMock, query: str, docs: list[dict]) -> None:
    httpx_mock.add_response(url=_ols_url(query), json={"response": {"docs": docs}})


def _ols(tmp_path=None) -> OlsClient:
    async def no_sleep(_: float) -> None:
        return None

    return OlsClient(client=httpx.AsyncClient(), cache_path=tmp_path, rate_limiter=_RateLimiter(0.0, sleep=no_sleep))


def _choosing(choice: str, confidence: float) -> MockDecisionModel:
    def answers(state, questions):
        (question,) = questions.values()
        assert isinstance(question, Choice)
        options = list(question.criteria)
        picked = choice if choice in options else options[0]
        probs = {o: 0.0 for o in options} | {picked: confidence}
        return {"term": ChoiceAnswer(picked, probs, confidence)}

    return MockDecisionModel({"s4_ontology": answers})


def _map(labels, decision, ols, title="A gut study"):
    return asyncio.run(map_body_sites(labels, context_title=title, decision_model=decision, ols=ols))


def test_mapped_when_a_term_is_chosen_at_or_above_threshold(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES, GUT])
    (m,) = _map(["Feces"], _choosing("UBERON:0001988", 0.93), _ols())
    assert m == OntologyMapping(
        label="Feces", term_id="UBERON:0001988", term_label="feces", confidence=0.93,
        status="mapped", candidates=("UBERON:0001988", "UBERON:0001555"),
    )


def test_threshold_is_inclusive(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES])
    (m,) = _map(["Feces"], _choosing("UBERON:0001988", ONTOLOGY_CONFIDENCE_THRESHOLD), _ols())
    assert m.status == "mapped"


def test_low_confidence_keeps_the_guess_visible(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES, GUT])
    (m,) = _map(["Feces"], _choosing("UBERON:0001988", 0.55), _ols())
    assert (m.status, m.term_id, m.term_label, m.confidence) == ("low_confidence", "UBERON:0001988", "feces", 0.55)


def test_unmapped_when_the_model_picks_none_of_these(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES, GUT])
    (m,) = _map(["Feces"], _choosing(NONE_OF_THESE, 0.97), _ols())
    assert (m.status, m.term_id, m.term_label) == ("unmapped", None, None)
    assert m.confidence == 0.97 and m.candidates == ("UBERON:0001988", "UBERON:0001555")


def test_no_candidates_skips_the_model_call(httpx_mock):
    _mock_ols(httpx_mock, "Zzz", [])
    decision = MockDecisionModel()
    (m,) = _map(["Zzz"], decision, _ols())
    assert m == OntologyMapping("Zzz", None, None, None, "no_candidates", ())
    assert decision.calls == []


def test_a_single_candidate_still_gets_a_choice_with_none_of_these(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES])
    decision = _choosing("UBERON:0001988", 0.9)
    (m,) = _map(["Feces"], decision, _ols())
    assert m.status == "mapped" and list(decision.calls[0]["questions"]["term"].criteria) == ["UBERON:0001988", NONE_OF_THESE]


def test_question_options_are_the_ols_docs_plus_none_of_these(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES, GUT, {"label": "no id, ignored"}])
    decision = _choosing("UBERON:0001988", 0.9)
    _map(["Feces"], decision, _ols(), title="Study T")
    (call,) = decision.calls
    assert call["stage"] == "s4_ontology"
    assert call["state"] == {"field": "body_site", "label": "Feces", "study_title": "Study T"}
    question = call["questions"]["term"]
    assert isinstance(question, Choice)
    assert list(question.criteria) == ["UBERON:0001988", "UBERON:0001555", NONE_OF_THESE]
    assert question.criteria["UBERON:0001988"] == {"label": "feces", "definition": "Excreted waste.", "synonyms": ["faeces"]}
    assert question.criteria["UBERON:0001555"] == {"label": "digestive tract"}
    assert isinstance(question.criteria[NONE_OF_THESE], str)
    assert question.instructions["label"] == "Feces" and question.instructions["study_title"] == "Study T"


def test_repeated_and_blank_labels_cost_one_decision_per_distinct_label(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES, GUT])
    _mock_ols(httpx_mock, "Skin of body", [SKIN])
    decision = _choosing("UBERON:0001988", 0.9)
    mappings = _map(["Feces", "Skin of body", "Feces", "  "], decision, _ols())
    assert [m.label for m in mappings] == ["Feces", "Skin of body"]
    assert len(decision.calls) == 2 and len(httpx_mock.get_requests()) == 2


def test_no_labels_means_no_calls():
    assert _map([], MockDecisionModel(), _ols()) == []


def test_decision_failure_propagates(httpx_mock):
    _mock_ols(httpx_mock, "Feces", [FECES])

    class Down:
        async def decide(self, **_):
            raise DecisionModelError("down")

    with pytest.raises(DecisionModelError):
        _map(["Feces"], Down(), _ols())  # type: ignore[arg-type]


# --- pipeline wiring ----------------------------------------------------------------------------------


def _curate(httpx_mock, tmp_path, decision, *, tag, ols_docs=None, ols_status=200):
    """Run the e2e study (MockModel => body_site ['Feces']) with an OLS mock registered when needed."""
    e2e._mock_idconv(httpx_mock)
    e2e._mock_fulltext(httpx_mock)
    e2e._mock_taxonomy(httpx_mock)
    if decision is not None:
        if ols_status == 200:
            _mock_ols(httpx_mock, "Feces", ols_docs if ols_docs is not None else [FECES, GUT])
        else:
            for _ in range(3):  # OlsClient makes 3 attempts on a retryable status
                httpx_mock.add_response(url=_ols_url("Feces"), status_code=ols_status)

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                e2e.PMID,
                model=MockModel(),
                client=client,
                decision_model=decision,
                taxonomy_cache_path=tmp_path / f"tax-{tag}.json",
                ols_cache_path=tmp_path / f"ols-{tag}.json",
            )

    return asyncio.run(run())


def _s5a_answer(state, questions):
    return {"is_da_artifact": NoulAnswer(0.5)}


def _decision(choice="UBERON:0001988", confidence=0.9) -> MockDecisionModel:
    base = _choosing(choice, confidence)
    base.answers_by_stage["s5a_locate"] = _s5a_answer
    return base


def test_pipeline_records_body_site_terms_and_leaves_the_record_unchanged(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, None, tag="base")
    result = _curate(httpx_mock, tmp_path, _decision(), tag="dm")
    assert result.record == baseline.record
    (term,) = result.annotations["body_site_terms"]
    assert term["experiment_index"] == 0 and term["label"] == "Feces"
    assert term["term_id"] == "UBERON:0001988" and term["status"] == "mapped" and term["confidence"] == 0.9
    assert "body_site_terms_error" not in result.annotations
    json.dumps(result.annotations)
    assert json.loads((tmp_path / "ols-dm.json").read_text()).keys() == {"uberon|Feces|10"}  # cache saved in finally


def test_no_decision_model_means_no_ols_traffic_and_no_annotations(httpx_mock, tmp_path):
    result = _curate(httpx_mock, tmp_path, None, tag="none")
    assert result.annotations == {}
    assert not (tmp_path / "ols-none.json").exists() or json.loads((tmp_path / "ols-none.json").read_text()) == {}
    assert all("ols4" not in str(r.url) for r in httpx_mock.get_requests())


def test_ols_outage_is_recorded_and_the_study_still_succeeds(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, None, tag="base")
    result = _curate(httpx_mock, tmp_path, _decision(), tag="out", ols_status=503)
    assert result.valid and result.record == baseline.record
    assert "body_site_terms" not in result.annotations
    assert "503" in result.annotations["body_site_terms_error"]


def test_decision_failure_on_s4_ontology_is_recorded_too(httpx_mock, tmp_path):
    decision = MockDecisionModel({"s5a_locate": _s5a_answer})
    result = _curate(httpx_mock, tmp_path, decision, tag="nostage")  # no s4_ontology canned answers -> DecisionModelError
    assert result.valid and "s4_ontology" in result.annotations["body_site_terms_error"]


def test_programming_errors_are_not_swallowed(httpx_mock):
    class Broken:
        async def decide(self, **_):
            return {}  # missing the 'term' answer -> KeyError: a bug, must surface

    _mock_ols(httpx_mock, "Feces", [FECES])
    with pytest.raises(KeyError):
        asyncio.run(_body_site_terms(0, ("Feces",), "T", Broken(), _ols(), {}))  # type: ignore[arg-type]


def test_a_caller_supplied_ols_client_is_used_and_its_cache_left_to_the_caller(httpx_mock, tmp_path):
    e2e._mock_idconv(httpx_mock)
    e2e._mock_fulltext(httpx_mock)
    e2e._mock_taxonomy(httpx_mock)
    _mock_ols(httpx_mock, "Feces", [FECES, GUT])

    async def run():
        async with httpx.AsyncClient() as client:
            ols = OlsClient.load(client, cache_path=tmp_path / "shared.json")
            result = await curate_async(
                e2e.PMID, model=MockModel(), client=client, decision_model=_decision(), ols=ols,
                taxonomy_cache_path=tmp_path / "tax.json", ols_cache_path=tmp_path / "ignored.json",
            )
            return result, ols

    result, ols = asyncio.run(run())
    assert result.annotations["body_site_terms"][0]["status"] == "mapped"
    assert "uberon|Feces|10" in ols.cache
    assert not (tmp_path / "shared.json").exists() and not (tmp_path / "ignored.json").exists()  # caller saves


def test_smoke_counts_studies_with_body_site_term_failures(monkeypatch, tmp_path):
    import contextlib

    from typer.testing import CliRunner

    import bugsigdb_curation.cli as cli_module
    from bugsigdb_curation.cli import app
    from bugsigdb_curation.curator.pipeline import CurationResult

    seen_ols: list = []

    @contextlib.asynccontextmanager
    async def fake_open(name, archive=None, **_):
        yield object()  # a (never-called) decision model: only wiring is under test

    async def fake_curate_async(pmid, **kwargs):
        seen_ols.append(kwargs["ols"])
        ann = {"body_site_terms_error": "boom"} if pmid == "A" else {"body_site_terms": []}
        return CurationResult(pmid=pmid, pmcid=None, has_pmc=False, record={"uid": pmid}, valid=True, problems=(), annotations=ann)

    monkeypatch.setattr(cli_module, "open_decision_model", fake_open)
    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)
    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: ["A", "B"])
    monkeypatch.setattr(cli_module, "require_credentials", lambda: None)
    monkeypatch.chdir(tmp_path)  # OlsClient's default cache path is relative: keep it out of the repo
    res = CliRunner().invoke(app, ["curate", "--smoke", "--decision-model", "clef", "--out", str(tmp_path / "smoke")])
    assert res.exit_code == 0, res.output
    assert "1 study(ies) have no body-site ontology terms" in res.output
    assert "fell back to the regex" not in res.output
    assert len(seen_ols) == 2 and seen_ols[0] is seen_ols[1] and isinstance(seen_ols[0], OlsClient)  # one shared client
    assert (tmp_path / "data" / "curator" / "ols_cache.json").exists()  # saved once after the batch
