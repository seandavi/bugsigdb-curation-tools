"""Tests for decision-model routing (`curator.routing`) and its S5a / pipeline / CLI wiring."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import test_curator_pipeline_e2e as e2e
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.curator.evidence import (
    EvidenceBundle,
    EvidenceFigure,
    EvidenceTable,
)
from bugsigdb_curation.curator.locate import LocatedArtifact, locate_artifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.pipeline import _rank_or_none, curate_async
from bugsigdb_curation.curator.routing import candidate_artifacts, rank_artifacts
from bugsigdb_curation.decision import DecisionModelError, MockDecisionModel, NoulAnswer
from bugsigdb_curation.retrieval import ArticleMetadata, SectionEntry


def _table(n: str, caption: str, rows=(("Taxon", "LDA"), ("Bacteroides", "4.1"))) -> EvidenceTable:
    return EvidenceTable(table_id=f"T{n}", number=n, label=f"Table {n}.", caption=caption, rows=tuple(tuple(r) for r in rows))


def _figure(n: str, legend: str) -> EvidenceFigure:
    return EvidenceFigure(
        figure_id=f"F{n}", number=n, label=f"Figure {n}.", legend=legend, graphic_filename=None, blob_url=None
    )


def _bundle(tables=(), figures=()) -> EvidenceBundle:
    return EvidenceBundle(
        pmid="1",
        pmcid="PMC1",
        metadata=ArticleMetadata(title="A gut study", journal="J", year=2026, authors=("A",), doi=None),
        sections=(SectionEntry(section_id="s", title="Methods", text="text"),),
        tables=tuple(tables),
        figures=tuple(figures),
    )


def _p_by_caption(p_by_text: dict[str, float]):
    """Canned stage answers: p(DA) chosen by a substring of the caption/legend (default 0.1)."""

    def answers(state, questions):
        text = state["caption_or_legend"]
        return {"is_da_artifact": NoulAnswer(next((v for k, v in p_by_text.items() if k in text), 0.1))}

    return {"s5a_locate": answers}


def test_candidates_are_tables_then_figures_in_document_order():
    bundle = _bundle([_table("1", "a"), _table("2", "b")], [_figure("1", "c")])
    assert [a.provenance for a in candidate_artifacts(bundle)] == ["Table 1", "Table 2", "Figure 1"]


def test_rank_orders_by_p_da_and_attaches_it():
    bundle = _bundle([_table("1", "demographics")], [_figure("1", "diversity"), _figure("2", "LEfSe taxa")])
    mock = MockDecisionModel(_p_by_caption({"LEfSe": 0.95, "demographics": 0.02}))
    ranked = asyncio.run(rank_artifacts(bundle, mock))
    assert [a.provenance for a in ranked] == ["Figure 2", "Figure 1", "Table 1"]  # 0.95, 0.1 (default), 0.02
    assert ranked[0].p_da == 0.95 and ranked[-1].p_da == 0.02


def test_rank_ties_keep_document_order_and_empty_bundle_is_empty():
    bundle = _bundle([_table("1", "x"), _table("2", "y")])
    ranked = asyncio.run(rank_artifacts(bundle, MockDecisionModel(_p_by_caption({}))))
    assert [a.provenance for a in ranked] == ["Table 1", "Table 2"]
    assert asyncio.run(rank_artifacts(_bundle(), MockDecisionModel())) == []


def test_state_sent_to_the_decision_model_is_text_only_and_bounded():
    big = "x" * 10_000
    bundle = _bundle([_table("1", big, rows=[("h",)] + [(f"r{i}",) for i in range(30)])])
    mock = MockDecisionModel(_p_by_caption({}))
    asyncio.run(rank_artifacts(bundle, mock))
    (call,) = mock.calls
    assert call["images"] == [] and call["state"]["paper"] == "A gut study"
    assert len(call["state"]["caption_or_legend"]) == 3000 and len(call["state"]["first_rows"]) == 8


def test_locate_prefers_ranked_top_over_regex():
    # The regex would pick Table 1 (caption mentions LEfSe); the ranker says Figure 1 is the DA artifact.
    bundle = _bundle([_table("1", "LEfSe summary of cohort")], [_figure("1", "taxa that differ")])
    mock = MockDecisionModel(_p_by_caption({"taxa that differ": 0.9, "LEfSe": 0.2}))
    ranked = asyncio.run(rank_artifacts(bundle, mock))
    chosen = locate_artifact(bundle, ranked)
    assert chosen is not None and chosen.provenance == "Figure 1" and chosen.p_da == 0.9
    assert locate_artifact(bundle).provenance == "Table 1"  # no ranking -> regex, unchanged
    assert locate_artifact(bundle, []).provenance == "Table 1"  # empty ranking -> regex


def test_regex_locate_leaves_p_da_unset():
    artifact = locate_artifact(_bundle([_table("1", "LEfSe")]))
    assert isinstance(artifact, LocatedArtifact) and artifact.p_da is None


def test_pipeline_helper_falls_back_when_decision_call_fails():
    class Boom:
        async def decide(self, **_):
            raise DecisionModelError("down")

    bundle = _bundle([_table("1", "x")])
    assert asyncio.run(_rank_or_none(bundle, Boom())) is None  # type: ignore[arg-type]
    assert asyncio.run(_rank_or_none(bundle, None)) is None


def test_curate_async_records_artifact_ranking_sidecar(httpx_mock, tmp_path):
    e2e._mock_idconv(httpx_mock)
    e2e._mock_fulltext(httpx_mock)
    e2e._mock_taxonomy(httpx_mock)
    decision = MockDecisionModel(
        {"s5a_locate": lambda state, qs: {"is_da_artifact": NoulAnswer(0.8 if state["kind"] == "table" else 0.3)}}
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                e2e.PMID,
                model=MockModel(),
                client=client,
                decision_model=decision,
                taxonomy_cache_path=tmp_path / "c.json",
            )

    result = asyncio.run(run())
    ranking = result.annotations["artifact_ranking"]
    assert ranking and ranking == sorted(ranking, key=lambda r: -r["p_da"])
    assert {"artifact", "kind", "p_da"} <= set(ranking[0])
    json.dumps(result.annotations)  # sidecar must be JSON-serialisable
    assert result.valid


def test_curate_async_without_decision_model_has_no_annotations(httpx_mock, tmp_path):
    e2e._mock_idconv(httpx_mock)
    e2e._mock_fulltext(httpx_mock)
    e2e._mock_taxonomy(httpx_mock)

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                e2e.PMID, model=MockModel(), client=client, taxonomy_cache_path=tmp_path / "c.json"
            )

    assert asyncio.run(run()).annotations == {}


@pytest.mark.parametrize("flag", ["clef", "clef-flash"])
def test_cli_decision_model_flag_is_ignored_with_mock(flag, tmp_path, monkeypatch):
    import bugsigdb_curation.cli as cli_module
    from bugsigdb_curation.curator.pipeline import CurationResult

    seen = {}

    async def fake_curate_async(pmid, **kwargs):
        seen.update(kwargs)
        return CurationResult(pmid=pmid, pmcid=None, has_pmc=False, record={}, valid=True, problems=())

    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)
    res = CliRunner().invoke(
        app, ["curate", "--pmid", "1", "--mock", "--decision-model", flag, "--out", str(tmp_path / "o.json")]
    )
    assert res.exit_code == 0, res.output
    assert "ignored with --mock" in res.output
    assert seen["decision_model"] is None


def test_cli_writes_annotations_sidecar_next_to_out(tmp_path, monkeypatch):
    import bugsigdb_curation.cli as cli_module
    from bugsigdb_curation.curator.pipeline import CurationResult

    async def fake_curate_async(pmid, **kwargs):
        return CurationResult(
            pmid=pmid, pmcid=None, has_pmc=False, record={}, valid=True, problems=(),
            annotations={"artifact_ranking": [{"artifact": "Table 1", "kind": "table", "p_da": 0.9}]},
        )

    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)
    out = tmp_path / "o.json"
    res = CliRunner().invoke(app, ["curate", "--pmid", "1", "--mock", "--out", str(out)])
    assert res.exit_code == 0, res.output
    sidecar = json.loads((tmp_path / "o.annotations.json").read_text())
    assert sidecar["artifact_ranking"][0]["p_da"] == 0.9


# --- reviewer follow-ups (PR #22): visible fallback, narrowed except, cancellation, CLI edges ----------


def test_fallback_on_transport_error_records_the_error_annotation():
    class Down:
        async def decide(self, **_):
            raise httpx.ConnectError("no route")

    annotations: dict = {}
    assert asyncio.run(_rank_or_none(_bundle([_table("1", "x")]), Down(), annotations)) is None  # type: ignore[arg-type]
    assert "ConnectError" in annotations["artifact_ranking_error"]


def test_programming_errors_are_not_swallowed_by_the_fallback():
    class Broken:
        async def decide(self, **_):
            return {}  # missing the answer -> KeyError in rank_artifacts: a bug, must surface

    with pytest.raises(KeyError):
        asyncio.run(_rank_or_none(_bundle([_table("1", "x")]), Broken()))  # type: ignore[arg-type]


def test_first_failure_cancels_the_sibling_calls():
    started: list[str] = []
    cancelled: list[str] = []

    class Mixed:
        async def decide(self, *, state, **_):
            started.append(state["artifact"])
            if state["artifact"] == "Table 1.":
                raise DecisionModelError("boom")
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(state["artifact"])
                raise
            return {"is_da_artifact": NoulAnswer(0.5)}

    bundle = _bundle([_table("1", "x"), _table("2", "y"), _table("3", "z")])
    with pytest.raises(DecisionModelError, match="boom"):
        asyncio.run(rank_artifacts(bundle, Mixed()))  # type: ignore[arg-type]
    assert sorted(cancelled) == ["Table 2.", "Table 3."]


def test_curate_async_falls_back_to_the_regex_choice_when_decisions_fail(httpx_mock, tmp_path):
    class Down:
        async def decide(self, **_):
            raise DecisionModelError("401")

    async def run(decision, tag):
        e2e._mock_idconv(httpx_mock)
        e2e._mock_fulltext(httpx_mock)
        e2e._mock_taxonomy(httpx_mock)
        async with httpx.AsyncClient() as client:
            return await curate_async(
                e2e.PMID, model=MockModel(), client=client, decision_model=decision, taxonomy_cache_path=tmp_path / f"{tag}.json"
            )

    baseline = asyncio.run(run(None, "base"))
    failed = asyncio.run(run(Down(), "failed"))
    assert failed.record == baseline.record  # same artifact chosen
    assert set(failed.annotations) == {"artifact_ranking_error"} and "401" in failed.annotations["artifact_ranking_error"]


def test_cli_fails_fast_without_credentials(monkeypatch, tmp_path):
    import bugsigdb_curation.decision as decision_module

    monkeypatch.setattr(decision_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr("bugsigdb_curation.cli._build_model", lambda mock, name: MockModel())
    res = CliRunner().invoke(app, ["curate", "--pmid", "1", "--decision-model", "clef", "--out", str(tmp_path / "o.json")])
    assert res.exit_code == 2 and "CLOUDFLARE_ACCOUNT_ID" in res.output
    smoke = CliRunner().invoke(app, ["curate", "--smoke", "--decision-model", "clef", "--out", str(tmp_path / "s")])
    assert smoke.exit_code == 2 and "CLOUDFLARE_ACCOUNT_ID" in smoke.output


def test_smoke_mode_sidecars_archive_default_and_fallback_count(monkeypatch, tmp_path):
    import contextlib

    import bugsigdb_curation.cli as cli_module
    from bugsigdb_curation.curator.pipeline import CurationResult

    opened: dict = {}

    @contextlib.asynccontextmanager
    async def fake_open(name, archive=None, **_):
        opened.update(name=name, archive=archive)
        yield None

    async def fake_curate_async(pmid, **kwargs):
        ann = {"artifact_ranking_error": "boom"} if pmid == "A" else {"artifact_ranking": [{"p_da": 0.9}]}
        return CurationResult(pmid=pmid, pmcid=None, has_pmc=False, record={"uid": pmid}, valid=True, problems=(), annotations=ann)

    monkeypatch.setattr(cli_module, "open_decision_model", fake_open)
    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)
    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: ["A", "B"])
    monkeypatch.setattr(cli_module, "require_credentials", lambda: None)
    out = tmp_path / "smoke"
    res = CliRunner().invoke(app, ["curate", "--smoke", "--decision-model", "clef-flash", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert opened == {"name": "clef-flash", "archive": out / "decision.jsonl"}
    assert sorted(p.name for p in (out / "_annotations").iterdir()) == ["A.json", "B.json"]
    assert "1 study(ies) fell back to the regex" in res.output
    # eval score reads *.json files directly in the pred dir: only the two records, no sidecars/archives
    assert sorted(p.name for p in out.glob("*.json")) == ["A.json", "B.json"]
