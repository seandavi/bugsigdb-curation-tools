"""Per-experiment artifact search: S5a hands S5b several ranked candidates, S5b may decline one.

Observed failure (PMID 42404767): one top-ranked artifact was extracted for EVERY experiment stub, so a
paper with several comparisons got the same taxa copied into each experiment.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
import test_curator_pipeline_e2e as e2e
import test_curator_routing as routing_helpers
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.artifact_text import group_orientation_text
from bugsigdb_curation.curator.design import Design
from bugsigdb_curation.curator.evidence import EvidenceFigure
from bugsigdb_curation.curator.experiment import ExperimentFields
from bugsigdb_curation.curator.locate import LocatedArtifact, locate_artifact, locate_artifacts
from bugsigdb_curation.curator.model import DEFAULT_MOCK_RESPONSES, MockModel, ModelCallError
from bugsigdb_curation.curator.ner import build_ner_messages
from bugsigdb_curation.curator.pipeline import _drop_duplicate_signatures, _figure_image_once, curate_async
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon, build_signature_messages
from bugsigdb_curation.decision import MockDecisionModel, NoulAnswer
from bugsigdb_curation.retrieval import EUROPEPMC_FULLTEXT_URL, PMC_ARTICLE_URL

_table = routing_helpers._table
_figure = routing_helpers._figure
_bundle = routing_helpers._bundle

_ESCAPE_HATCH = "does NOT report a comparison between these two groups"


def _ranked(*p_das: float) -> list[LocatedArtifact]:
    """Figures 1..n with the given p_da, already best-first."""
    return [LocatedArtifact(kind="figure", figure=_figure(str(i), "x"), p_da=p) for i, p in enumerate(p_das, 1)]


# --- locate_artifacts --------------------------------------------------------------------------


def test_locate_artifacts_ranked_keeps_those_at_or_above_min_p_best_first():
    ranked = _ranked(0.98, 0.97, 0.5, 0.49)
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked, max_n=5)] == ["Figure 1", "Figure 2", "Figure 3"]


def test_locate_artifacts_ranked_caps_at_max_n():
    ranked = _ranked(0.99, 0.98, 0.97, 0.96)
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked)] == ["Figure 1", "Figure 2", "Figure 3"]
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked, max_n=2)] == ["Figure 1", "Figure 2"]


def test_locate_artifacts_ranked_always_returns_the_top_one_even_below_min_p():
    ranked = _ranked(0.2, 0.1)
    assert [a.provenance for a in locate_artifacts(_bundle(), ranked)] == ["Figure 1"]
    assert locate_artifacts(_bundle(), ranked)[0] == locate_artifact(_bundle(), ranked)


def test_locate_artifacts_min_p_is_a_parameter():
    assert len(locate_artifacts(_bundle(), _ranked(0.9, 0.6, 0.3), min_p=0.25)) == 3


def test_locate_artifacts_unranked_is_the_regex_choice_alone():
    bundle = _bundle([_table("1", "LEfSe summary"), _table("2", "differential taxa")], [_figure("1", "LEfSe")])
    assert locate_artifacts(bundle) == [locate_artifact(bundle)]
    assert locate_artifacts(bundle, []) == [locate_artifact(bundle)]


def test_locate_artifacts_with_nothing_to_locate_is_empty():
    assert locate_artifacts(_bundle()) == []


# --- the escape hatch in the S5b / NER prompts -------------------------------------------------


def _text(messages) -> str:
    return messages[0]["content"][0]["text"]


_ARTIFACT = LocatedArtifact(kind="table", table=_table("1", "LEfSe taxa"))


def test_orientation_text_has_the_escape_hatch_only_when_may_decline():
    text = group_orientation_text("HC", "ATB", may_decline=True)
    assert _ESCAPE_HATCH in text
    assert '{"taxa": []}' in text
    assert "do not fill in taxa from a different comparison" in text
    assert _ESCAPE_HATCH not in group_orientation_text("HC", "ATB")


def test_orientation_text_without_the_escape_hatch_is_the_pre_escape_hatch_prompt():
    assert group_orientation_text("HC", "ATB") == (
        "The two compared groups are:\n"
        "- Group 0 (the reference / control / baseline group): HC\n"
        "- Group 1 (the case / exposed / treated group): ATB\n"
        "Report each taxon's direction relative to these groups: INCREASED means more abundant in "
        "Group 1 than in Group 0; DECREASED means less abundant in Group 1 than in Group 0. In a "
        "figure, use the legend to decide which colour or side belongs to which group -- never "
        "assume the left/top/first-listed group is Group 1.\n\n"
    )


def test_orientation_text_has_no_escape_hatch_without_names():
    assert group_orientation_text(None, "ATB", may_decline=True) == ""


def test_signature_and_ner_prompts_carry_the_escape_hatch_iff_names_are_known_and_may_decline():
    groups = ("Healthy controls", "Active TB")
    for build in (build_signature_messages, build_ner_messages):
        assert _ESCAPE_HATCH in _text(build(_ARTIFACT, groups=groups, may_decline=True))
        assert _ESCAPE_HATCH not in _text(build(_ARTIFACT, groups=groups))
        assert _ESCAPE_HATCH not in _text(build(_ARTIFACT, may_decline=True))
        assert _ESCAPE_HATCH not in _text(build(_ARTIFACT, groups=(None, "x"), may_decline=True))


# --- pipeline: per-experiment artifact search --------------------------------------------------

BLOB_3 = "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/a1/1/b2/fig3.jpg"
BLOB_7 = "https://cdn.ncbi.nlm.nih.gov/pmc/blobs/a1/1/b2/fig7.jpg"
TWO_FIG_XML = e2e.XML_FIXTURE.replace(
    "</body>",
    '<fig id="F3"><label>Figure 3.</label><caption><p>Taxa differing in the antibiotic comparison.</p></caption>'
    '<graphic xlink:href="fig3.jpg"/></fig>'
    '<fig id="F7"><label>Figure 7.</label><caption><p>Taxa differing in the diet comparison.</p></caption>'
    '<graphic xlink:href="fig7.jpg"/></fig></body>',
)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def _taxa(*names: str, direction: str = "increased") -> dict:
    return {"taxa": [{"name": n, "direction": direction, "proposed_ncbi_id": None} for n in names]}


TAXA_A = _taxa("Alistipes onderdonkii", "Bilophila wadsworthia", "Dorea longicatena")
TAXA_B = _taxa("Roseburia hominis", "Blautia obeum", "Ruminococcus bromii")


def _ranker():
    """Figure 7 narrowly outranks Figure 3 (as in PMID 42404767); the table is not a DA artifact."""
    p_by_legend = {"diet comparison": 0.981, "antibiotic comparison": 0.98}

    def answers(state, questions):
        text = state["caption_or_legend"]
        return {"is_da_artifact": NoulAnswer(next((p for k, p in p_by_legend.items() if k in text), 0.1))}

    return MockDecisionModel({"s5a_locate": answers})


def _which_figure(messages) -> str:
    text = messages[0]["content"][0]["text"]
    return "Figure 7" if "Figure legend (Figure 7)" in text else "Figure 3" if "Figure legend (Figure 3)" in text else "other"


def _study(
    httpx_mock: HTTPXMock,
    tmp_path,
    signature_extract,
    *,
    n_experiments=2,
    decision_model=_ranker,
    design=Design.fused_lean,
    stages: dict | None = None,
    esearch_ids: tuple[str, ...] = (),
    **curate_kwargs,
):
    """Run curate_async over the two-figure paper with `n_experiments` stubs; returns (result, model).

    The split designs take their extractor responses from `stages` (`signature_ner`, `review_signature`, ...);
    taxon names resolve through a mocked esearch that returns `esearch_ids` (none by default, so they stay
    unresolved); it is only registered for the split designs and `ground_unresolved=True`.
    """
    e2e._mock_idconv(httpx_mock)
    httpx_mock.add_response(url=EUROPEPMC_FULLTEXT_URL.format(pmcid=e2e.PMCID), text=TWO_FIG_XML)
    httpx_mock.add_response(
        url=PMC_ARTICLE_URL.format(pmcid=e2e.PMCID), text=f'<html><img src="{BLOB_3}"><img src="{BLOB_7}"></html>'
    )
    for blob in (BLOB_3, BLOB_7):
        httpx_mock.add_response(url=blob, content=PNG, is_reusable=True, is_optional=True)
    httpx_mock.add_response(
        url=re.compile(r"https://www\.ebi\.ac\.uk/ols4/api/search.*"),
        json={"response": {"docs": []}},
        is_optional=True,
        is_reusable=True,
    )
    if design is not Design.fused_lean or curate_kwargs.get("ground_unresolved"):
        httpx_mock.add_response(
            url=re.compile(r"https://eutils\.ncbi\.nlm\.nih\.gov/entrez/eutils/esearch.*"),
            json={"esearchresult": {"idlist": list(esearch_ids)}},
            is_optional=True,
            is_reusable=True,
        )
    segment = {"experiments": [{"index": i, "description": f"comparison {i}"} for i in range(n_experiments)]}
    model = MockModel(responses={"segment": segment, "signature_extract": signature_extract, **(stages or {})})

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                e2e.PMID,
                model=model,
                design=design,
                client=client,
                decision_model=decision_model() if decision_model else None,
                taxonomy_cache_path=tmp_path / "t.json",
                ols_cache_path=tmp_path / "o.json",
                html_cache_dir=tmp_path / "h",
                **curate_kwargs,
            )

    return asyncio.run(run()), model


def _sources(result, experiment: int) -> set[str]:
    return {s["source"] for s in result.record["experiments"][experiment].get("signatures", [])}


def _taxon_names(result, experiment: int) -> set[str]:
    return {t["taxon_name"] for s in result.record["experiments"][experiment].get("signatures", []) for t in s["taxa"]}


def test_a_declined_artifact_falls_through_to_the_next_candidate_per_experiment(httpx_mock, tmp_path):
    """Experiment 0's comparison is in Figure 3: Figure 7 (ranked first) declines it, Figure 3 supplies it."""
    seen_f7: list[int] = []

    def signature_extract(messages):
        figure = _which_figure(messages)
        if figure == "Figure 7":
            seen_f7.append(1)
            return {"taxa": []} if len(seen_f7) == 1 else TAXA_B  # declines experiment 0, reports experiment 1
        return TAXA_A

    result, model = _study(httpx_mock, tmp_path, signature_extract)

    assert result.annotations["experiment_artifacts"] == [
        {"experiment_index": 0, "artifact_tried": ["Figure 7", "Figure 3"], "artifact_used": "Figure 3"},
        {"experiment_index": 1, "artifact_tried": ["Figure 7"], "artifact_used": "Figure 7"},
    ]
    assert _sources(result, 0) == {"Figure 3"} and _taxon_names(result, 0) == {t["name"] for t in TAXA_A["taxa"]}
    assert _sources(result, 1) == {"Figure 7"} and _taxon_names(result, 1) == {t["name"] for t in TAXA_B["taxa"]}
    assert "duplicate_signatures_dropped" not in result.annotations
    # The prompts told the model it may decline (the groups are known from S4).
    prompts = [c["messages"][0]["content"][0]["text"] for c in model.calls if c["stage"] == "signature_extract"]
    assert len(prompts) == 3 and all(_ESCAPE_HATCH in p for p in prompts)


def test_each_candidate_figure_image_is_fetched_once_per_study(httpx_mock, tmp_path):
    def signature_extract(messages):
        return {"taxa": []} if _which_figure(messages) == "Figure 7" else TAXA_A

    _study(httpx_mock, tmp_path, signature_extract)  # Figure 7 is tried for BOTH experiments, Figure 3 for both
    urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert urls.count(BLOB_7) == 1 and urls.count(BLOB_3) == 1


def test_an_experiment_no_candidate_reports_gets_no_signatures_but_is_kept(httpx_mock, tmp_path):
    result, _ = _study(httpx_mock, tmp_path, {"taxa": []})
    assert len(result.record["experiments"]) == 2
    assert all("signatures" not in e for e in result.record["experiments"])
    assert result.valid, result.problems  # an experiment left without signatures is still a valid record
    assert [e["artifact_used"] for e in result.annotations["experiment_artifacts"]] == [None, None]
    assert all(e["artifact_tried"] == ["Figure 7", "Figure 3"] for e in result.annotations["experiment_artifacts"])


def _ranker_figure_7_only():
    """Only Figure 7 is a candidate (Figure 3 scores below the cut-off), so there is nothing to fall back to."""

    def answers(state, questions):
        return {"is_da_artifact": NoulAnswer(0.98 if "diet comparison" in state["caption_or_legend"] else 0.1)}

    return MockDecisionModel({"s5a_locate": answers})


def test_a_duplicate_from_a_single_candidate_is_dropped_afterwards_and_not_reported_as_used(httpx_mock, tmp_path):
    result, _ = _study(httpx_mock, tmp_path, TAXA_A, decision_model=_ranker_figure_7_only)  # same taxa for both

    assert _taxon_names(result, 0) == {t["name"] for t in TAXA_A["taxa"]}
    assert len(result.record["experiments"]) == 2
    assert "signatures" not in result.record["experiments"][1]
    assert result.annotations["duplicate_signatures_dropped"] == [
        {"experiment_index": 1, "source": "Figure 7", "direction": "increased", "same_as_experiment_index": 0}
    ]
    assert result.annotations["experiment_artifacts"] == [
        {"experiment_index": 0, "artifact_tried": ["Figure 7"], "artifact_used": "Figure 7"},
        {
            "experiment_index": 1,
            "artifact_tried": ["Figure 7"],
            "artifact_used": None,
            "artifact_duplicate": ["Figure 7"],
        },
    ]


def test_a_candidate_that_copies_an_earlier_experiment_is_a_decline_and_the_next_candidate_is_tried(httpx_mock, tmp_path):
    """The model ignores the escape hatch and returns experiment 0's Figure 7 taxa again for experiment 1."""

    def signature_extract(messages):
        return TAXA_A if _which_figure(messages) == "Figure 7" else TAXA_B

    result, _ = _study(httpx_mock, tmp_path, signature_extract)

    assert result.annotations["experiment_artifacts"] == [
        {"experiment_index": 0, "artifact_tried": ["Figure 7"], "artifact_used": "Figure 7"},
        {
            "experiment_index": 1,
            "artifact_tried": ["Figure 7", "Figure 3"],
            "artifact_used": "Figure 3",
            "artifact_duplicate": ["Figure 7"],
        },
    ]
    assert _sources(result, 1) == {"Figure 3"} and _taxon_names(result, 1) == {t["name"] for t in TAXA_B["taxa"]}
    assert "duplicate_signatures_dropped" not in result.annotations


def test_a_candidate_with_a_new_signature_is_not_skipped_even_if_another_signature_is_a_copy(httpx_mock, tmp_path):
    novel = {"taxa": TAXA_A["taxa"] + _taxa("Roseburia hominis", "Blautia obeum", "Ruminococcus bromii", direction="decreased")["taxa"]}
    responses = iter([TAXA_A, novel])

    result, _ = _study(httpx_mock, tmp_path, lambda messages: next(responses))

    assert result.annotations["experiment_artifacts"][1] == {
        "experiment_index": 1,
        "artifact_tried": ["Figure 7"],
        "artifact_used": "Figure 7",
    }
    assert [d["direction"] for d in result.annotations["duplicate_signatures_dropped"]] == ["increased"]
    assert [s["abundance_in_group_1"] for s in result.record["experiments"][1]["signatures"]] == ["decreased"]


def test_the_same_taxa_from_different_artifacts_are_not_deduplicated(httpx_mock, tmp_path):
    calls_f7: list[int] = []

    def signature_extract(messages):
        if _which_figure(messages) == "Figure 3":
            return TAXA_A
        calls_f7.append(1)
        return {"taxa": []} if len(calls_f7) == 1 else TAXA_A  # experiment 0: Figure 7 declines; experiment 1 reports

    result, _ = _study(httpx_mock, tmp_path, signature_extract)
    assert _sources(result, 0) == {"Figure 3"} and _sources(result, 1) == {"Figure 7"}
    assert "duplicate_signatures_dropped" not in result.annotations


def test_without_a_decision_model_one_candidate_is_tried_and_nothing_is_recorded(httpx_mock, tmp_path):
    e2e._mock_taxonomy(httpx_mock)
    result, model = _study(httpx_mock, tmp_path, DEFAULT_MOCK_RESPONSES["signature_extract"], decision_model=None)

    # The two default taxa (< 3, so never a duplicate) reach both experiments, from the one regex-chosen table.
    assert [_sources(result, i) for i in (0, 1)] == [{"Table 2"}, {"Table 2"}]
    assert sum(c["stage"] == "signature_extract" for c in model.calls) == 2
    assert result.annotations == {}


def test_a_single_experiment_paper_uses_the_top_artifact_and_records_it(httpx_mock, tmp_path):
    result, model = _study(httpx_mock, tmp_path, TAXA_A, n_experiments=1)

    assert sum(c["stage"] == "signature_extract" for c in model.calls) == 1
    assert _sources(result, 0) == {"Figure 7"}
    assert result.annotations["experiment_artifacts"] == [
        {"experiment_index": 0, "artifact_tried": ["Figure 7"], "artifact_used": "Figure 7"}
    ]
    assert "duplicate_signatures_dropped" not in result.annotations


def _extract_prompts(model) -> list[str]:
    return [c["messages"][0]["content"][0]["text"] for c in model.calls if c["stage"] == "signature_extract"]


def test_the_escape_hatch_is_offered_when_there_are_several_candidates_even_for_one_experiment(httpx_mock, tmp_path):
    _, model = _study(httpx_mock, tmp_path, TAXA_A, n_experiments=1)  # Figure 7 and Figure 3 are both candidates
    assert all(_ESCAPE_HATCH in p for p in _extract_prompts(model))


def test_the_escape_hatch_is_offered_when_there_are_several_experiments_even_for_one_candidate(httpx_mock, tmp_path):
    e2e._mock_taxonomy(httpx_mock)
    _, model = _study(httpx_mock, tmp_path, DEFAULT_MOCK_RESPONSES["signature_extract"], decision_model=None)
    assert len(_extract_prompts(model)) == 2 and all(_ESCAPE_HATCH in p for p in _extract_prompts(model))


def test_the_escape_hatch_is_withheld_for_one_experiment_and_one_candidate(httpx_mock, tmp_path):
    e2e._mock_taxonomy(httpx_mock)
    _, model = _study(
        httpx_mock, tmp_path, DEFAULT_MOCK_RESPONSES["signature_extract"], n_experiments=1, decision_model=None
    )
    prompts = _extract_prompts(model)
    assert len(prompts) == 1 and _ESCAPE_HATCH not in prompts[0] and "Group 0" in prompts[0]


def _split_decline_stages(*, ner_calls_f7: list[int]):
    """Split-design stages for the 'Figure 7 declines experiment 0' scenario; the reviewer/verifier never declines.

    The reviewer (and verifier) answer from the figure they are shown regardless of the prompt, as a model that
    ignores the escape hatch would -- so the only thing that can make experiment 0 fall through to Figure 3 is the
    pipeline not calling them for the declined candidate.
    """
    names = [t["name"] for t in TAXA_A["taxa"] + TAXA_B["taxa"]]

    def ner(messages):
        if _which_figure(messages) == "Figure 3":
            return TAXA_A
        ner_calls_f7.append(1)
        return {"taxa": []} if len(ner_calls_f7) == 1 else TAXA_B

    def reviewer(messages):
        return TAXA_B if _which_figure(messages) == "Figure 7" else TAXA_A

    in_source = {"results": [{"name": n, "in_source": True} for n in names]}
    return {
        "signature_ner": ner,
        "review_signature": reviewer,
        "review_ground_check": in_source,
        "verify_taxon_in_source": in_source,
        "verify_direction": {"direction": "increased"},
    }


def _calls(model, stage: str) -> list[str]:
    return [_which_figure(c["messages"]) for c in model.calls if c["stage"] == stage]


def test_split_panel_a_declined_artifact_falls_through_even_if_the_reviewer_ignores_the_escape_hatch(
    httpx_mock, tmp_path
):
    result, model = _study(
        httpx_mock, tmp_path, None, design=Design.split_panel, stages=_split_decline_stages(ner_calls_f7=[])
    )

    assert [e["artifact_used"] for e in result.annotations["experiment_artifacts"]] == ["Figure 3", "Figure 7"]
    assert _sources(result, 0) == {"Figure 3"} and _taxon_names(result, 0) == {t["name"] for t in TAXA_A["taxa"]}
    assert _sources(result, 1) == {"Figure 7"} and _taxon_names(result, 1) == {t["name"] for t in TAXA_B["taxa"]}
    # The reviewer was not asked about the declined candidate, and saw the groups where it was asked.
    assert _calls(model, "review_signature") == ["Figure 3", "Figure 7"]
    reviewer_prompts = [c["messages"][0]["content"][0]["text"] for c in model.calls if c["stage"] == "review_signature"]
    assert all("Group 0" in p and _ESCAPE_HATCH in p for p in reviewer_prompts)


def test_split_verify_a_declined_artifact_falls_through_to_the_next_candidate(httpx_mock, tmp_path):
    result, model = _study(
        httpx_mock, tmp_path, None, design=Design.split_verify, stages=_split_decline_stages(ner_calls_f7=[])
    )

    assert [e["artifact_used"] for e in result.annotations["experiment_artifacts"]] == ["Figure 3", "Figure 7"]
    assert _sources(result, 0) == {"Figure 3"} and _sources(result, 1) == {"Figure 7"}
    assert _calls(model, "verify_taxon_in_source") == ["Figure 3", "Figure 7"]


def test_split_panel_the_last_candidate_is_still_reviewed_when_the_extractor_found_nothing(httpx_mock, tmp_path):
    """Nothing to fall back to: the reviewer's recall path stays available on the final candidate."""
    stages = _split_decline_stages(ner_calls_f7=[])
    stages["signature_ner"] = {"taxa": []}
    result, model = _study(httpx_mock, tmp_path, None, design=Design.split_panel, stages=stages, n_experiments=1)

    assert _calls(model, "review_signature") == ["Figure 3"]  # Figure 7 skipped, last candidate Figure 3 reviewed
    assert _sources(result, 0) == {"Figure 3"}


def _fails_on_figure_3(error: Exception):
    """Figure 7 declines experiment 0, then reports TAXA_B; Figure 3 raises `error`."""
    f7_calls: list[int] = []

    def signature_extract(messages):
        if _which_figure(messages) == "Figure 3":
            raise error
        f7_calls.append(1)
        return {"taxa": []} if len(f7_calls) == 1 else TAXA_B

    return signature_extract


def test_a_failing_later_candidate_is_recorded_and_does_not_abort_the_study(httpx_mock, tmp_path):
    error = ModelCallError("rate limited")
    result, _ = _study(httpx_mock, tmp_path, _fails_on_figure_3(error))

    assert "signatures" not in result.record["experiments"][0]
    assert _sources(result, 1) == {"Figure 7"}
    assert result.annotations["experiment_artifacts"] == [
        {
            "experiment_index": 0,
            "artifact_tried": ["Figure 7", "Figure 3"],
            "artifact_used": None,
            "errors": [{"artifact": "Figure 3", "error": repr(error)}],
        },
        {"experiment_index": 1, "artifact_tried": ["Figure 7"], "artifact_used": "Figure 7"},
    ]


def test_an_http_error_on_a_later_candidate_is_absorbed_too(httpx_mock, tmp_path):
    result, _ = _study(httpx_mock, tmp_path, _fails_on_figure_3(httpx.ConnectError("boom")))
    assert result.annotations["experiment_artifacts"][0]["errors"][0]["artifact"] == "Figure 3"


def test_a_failure_on_the_first_candidate_still_aborts_the_study(httpx_mock, tmp_path):
    def signature_extract(messages):
        raise ModelCallError("rate limited")

    with pytest.raises(ModelCallError):
        _study(httpx_mock, tmp_path, signature_extract)


def test_a_programming_error_on_a_later_candidate_still_surfaces(httpx_mock, tmp_path):
    with pytest.raises(ValueError, match="bug"):
        _study(httpx_mock, tmp_path, _fails_on_figure_3(ValueError("bug")))


def test_a_fused_lean_fallback_candidate_has_its_unresolved_taxa_grounded(httpx_mock, tmp_path):
    """ground_unresolved applies to the candidate that is actually used, after an earlier one declined."""
    f7_calls: list[int] = []

    def signature_extract(messages):
        if _which_figure(messages) == "Figure 3":
            return TAXA_A
        f7_calls.append(1)
        return {"taxa": []} if len(f7_calls) == 1 else TAXA_B

    result, _ = _study(httpx_mock, tmp_path, signature_extract, ground_unresolved=True, esearch_ids=("853",))

    assert result.annotations["experiment_artifacts"][0] == {
        "experiment_index": 0,
        "artifact_tried": ["Figure 7", "Figure 3"],
        "artifact_used": "Figure 3",
    }
    taxa = result.record["experiments"][0]["signatures"][0]["taxa"]
    assert {t["ncbi_id"] for t in taxa} == {853} and len(taxa) == 3
    assert result.valid, result.problems


def test_split_design_flags_name_the_experiment_and_artifact_they_came_from(httpx_mock, tmp_path):
    """The reviewer drops every extractor taxon (none re-grounds): once per candidate, each flag attributed."""
    stages = {
        "signature_ner": TAXA_A,
        "review_signature": {"taxa": []},
        "review_ground_check": {"results": []},
    }
    result, _ = _study(httpx_mock, tmp_path, None, design=Design.split_panel, stages=stages, n_experiments=1)

    assert len(result.flags) == 6
    assert sum(f.startswith("exp 0 / Figure 7: panel dropped ") for f in result.flags) == 3
    assert sum(f.startswith("exp 0 / Figure 3: panel dropped ") for f in result.flags) == 3


def _located_figure(number: str, label: str, blob_url: str | None) -> LocatedArtifact:
    figure = EvidenceFigure(
        figure_id=f"F{label}", number=number, label=label, legend="x", graphic_filename=None, blob_url=blob_url
    )
    return LocatedArtifact(kind="figure", figure=figure)


def test_figures_sharing_a_provenance_string_each_get_their_own_cached_image(httpx_mock):
    """'Figure 2A' and 'Figure 2' both read 'Figure 2' (provenance keeps the first integer): keyed by blob URL."""
    blob_a, blob_b = "https://cdn.example/a.jpg", "https://cdn.example/b.jpg"
    httpx_mock.add_response(url=blob_a, content=PNG + b"A")
    httpx_mock.add_response(url=blob_b, content=PNG + b"B")
    first, second = _located_figure("2", "Figure 2.", blob_a), _located_figure("2", "Figure 2A.", blob_b)
    assert first.provenance == second.provenance

    async def run():
        cache: dict = {}
        annotations: dict = {}
        async with httpx.AsyncClient() as client:
            images = [
                await _figure_image_once(a, client=client, cache=cache, annotations=annotations)
                for a in (first, second, first)
            ]
        return images, annotations

    images, annotations = asyncio.run(run())
    assert images == [PNG + b"A", PNG + b"B", PNG + b"A"]
    assert "figure_image_unavailable" not in annotations
    assert [str(r.url) for r in httpx_mock.get_requests()] == [blob_a, blob_b]  # `first` cached on its repeat


def test_figures_without_a_blob_url_are_cached_per_figure_not_per_provenance():
    first, second = _located_figure("2", "Figure 2.", None), _located_figure("2", "Figure 2A.", None)

    async def run():
        cache: dict = {}
        annotations: dict = {}
        async with httpx.AsyncClient() as client:
            for artifact in (first, second):
                await _figure_image_once(artifact, client=client, cache=cache, annotations=annotations)
        return cache, annotations

    cache, annotations = asyncio.run(run())
    assert len(cache) == 2
    assert annotations["figure_image_unavailable"] == ["Figure 2", "Figure 2"]


# --- the duplicate guard on its own ------------------------------------------------------------


def _sig(direction: str, *taxa: tuple[str, int | None]) -> ExtractedSignature:
    return ExtractedSignature(
        direction=direction, taxa=tuple(ExtractedTaxon(taxon_name=n, direction=direction, ncbi_id=i) for n, i in taxa)
    )


_FIELDS = ExperimentFields(
    host_species="Homo sapiens", body_site=(), condition=(), group_0_name="a", group_1_name="b",
    sequencing_type=None, statistical_test=(), mht_correction=None,
)
_THREE = (("A a", None), ("B b", None), ("C c", None))


def _guard(*experiments: tuple[list[ExtractedSignature], str | None]):
    return _drop_duplicate_signatures([(_FIELDS, sigs, source) for sigs, source in experiments])


def test_guard_drops_a_later_exact_copy_from_the_same_source_and_keeps_both_experiments():
    kept, dropped = _guard(([_sig("increased", *_THREE)], "Figure 7"), ([_sig("increased", *_THREE)], "Figure 7"))
    assert [len(sigs) for _, sigs, _ in kept] == [1, 0]
    assert dropped == [
        {"experiment_index": 1, "source": "Figure 7", "direction": "increased", "same_as_experiment_index": 0}
    ]


def test_guard_treats_a_near_copy_as_a_duplicate_at_the_supplement_jaccard():
    five = tuple((f"Taxon {c}", None) for c in "ABCDE")
    copy_plus_one = _sig("increased", *five, ("Taxon F", None))  # Jaccard 5/6 = 0.83 >= 0.8
    _, dropped = _guard(([_sig("increased", *five)], "Figure 7"), ([copy_plus_one], "Figure 7"))
    assert [d["same_as_experiment_index"] for d in dropped] == [0]

    _, dropped = _guard(([_sig("increased", *_THREE)], "Figure 7"), ([_sig("increased", *_THREE, ("D d", None))], "Figure 7"))
    assert dropped == []  # Jaccard 3/4 = 0.75 < 0.8


def test_guard_matches_on_ncbi_id_when_resolved_else_normalised_name():
    first = _sig("increased", ("Escherichia coli", 562), ("Bacteroides", 816), ("clostridium  butyricum", None))
    second = _sig("increased", ("E. coli", 562), ("Bacteroides fragilis group", 816), ("Clostridium butyricum", None))
    _, dropped = _guard(([first], "Table 1"), ([second], "Table 1"))
    assert len(dropped) == 1


def test_guard_leaves_different_taxa_other_directions_other_sources_and_small_sets_alone():
    base = _sig("increased", *_THREE)
    different = _sig("increased", ("A a", None), ("B b", None), ("D d", None))
    kept, dropped = _guard(
        ([base], "Figure 7"),
        ([different], "Figure 7"),  # one taxon differs
        ([_sig("decreased", *_THREE)], "Figure 7"),  # other direction
        ([base], "Figure 3"),  # other source
        ([_sig("increased", *_THREE[:2])], "Figure 7"),  # too small to call a duplicate...
        ([_sig("increased", *_THREE[:2])], "Figure 7"),  # ...even when repeated
    )
    assert dropped == [] and [len(sigs) for _, sigs, _ in kept] == [1] * 6


def test_guard_compares_each_signature_and_ignores_experiments_with_no_source():
    both = [_sig("increased", *_THREE), _sig("decreased", ("X x", None), ("Y y", None), ("Z z", None))]
    kept, dropped = _guard(
        (both, "Figure 7"),
        ([both[0], _sig("decreased", ("X x", None), ("Y y", None), ("W w", None))], "Figure 7"),
        ([both[0]], None),
    )
    assert [d["direction"] for d in dropped] == ["increased"] and dropped[0]["experiment_index"] == 1
    assert [s.direction for s in kept[1][1]] == ["decreased"]
    assert len(kept[2][1]) == 1
