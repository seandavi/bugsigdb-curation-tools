"""End-to-end tests for `bugsigdb review packet|ingest|report` (offline)."""

from __future__ import annotations

import json
import re
import shutil

import pytest
from review_support import DATA_DIR, PMID, load_draft, make_verdict, sample_evidence
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.review.packet import save_evidence
from bugsigdb_curation.review.verdicts import canonical_sha256

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@pytest.fixture(autouse=True)
def _plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")


def _plain(output: str) -> str:
    """CLI output as one line without ANSI codes, so assertions do not depend on terminal width or colour."""
    return " ".join(_ANSI.sub("", output).split())


def _pred(tmp_path, *, with_sidecar: bool = True):
    pred = tmp_path / "preds" / f"{PMID}.json"
    pred.parent.mkdir()
    shutil.copy(DATA_DIR / "draft.json", pred)
    if with_sidecar:
        shutil.copy(DATA_DIR / "draft.annotations.json", pred.with_suffix(".annotations.json"))
    return pred


def test_packet_offline_writes_html_and_manifest(tmp_path):
    pred = _pred(tmp_path)
    out = tmp_path / "packets"
    result = runner.invoke(
        app,
        [
            "review",
            "packet",
            "--pred",
            str(pred),
            "--out",
            str(out),
            "--offline",
            "--model-label",
            "gemini-x",
            "--design-label",
            "split-verify",
            "--pmcid",
            "PMC9000001",
        ],
    )
    assert result.exit_code == 0, result.output
    html = (out / f"{PMID}.html").read_text(encoding="utf-8")
    manifest = json.loads((out / f"{PMID}.manifest.json").read_text(encoding="utf-8"))
    assert "gemini-x" in html and "split-verify" in html
    assert "UBERON" in html  # sibling .annotations.json picked up automatically
    assert manifest["draft_sha256"] == canonical_sha256(load_draft())
    assert manifest["model_label"] == "gemini-x" and manifest["design_label"] == "split-verify"
    assert manifest["pmcid"] == "PMC9000001"
    assert manifest["packet_id"] in html


def test_packet_without_sidecar_and_explicit_annotations(tmp_path):
    pred = _pred(tmp_path, with_sidecar=False)
    out = tmp_path / "packets"
    result = runner.invoke(app, ["review", "packet", "--pred", str(pred), "--out", str(out), "--offline"])
    assert result.exit_code == 0, result.output
    assert "UBERON" not in (out / f"{PMID}.html").read_text(encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "review",
            "packet",
            "--pred",
            str(pred),
            "--out",
            str(out),
            "--offline",
            "--annotations",
            str(DATA_DIR / "draft.annotations.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "UBERON" in (out / f"{PMID}.html").read_text(encoding="utf-8")


def test_packet_uses_cached_evidence_dir_without_network(tmp_path, httpx_mock):
    pred = _pred(tmp_path)
    cache = tmp_path / "evidence"
    save_evidence(sample_evidence("cc by"), cache / PMID)
    out = tmp_path / "packets"
    result = runner.invoke(
        app, ["review", "packet", "--pred", str(pred), "--out", str(out), "--evidence-dir", str(cache)]
    )
    assert result.exit_code == 0, result.output  # httpx_mock would fail the test on any request
    html = (out / f"{PMID}.html").read_text(encoding="utf-8")
    assert "Evidence: Table 1" in html and 'data-image-ref="Figure 2"' in html


def test_packet_rejects_missing_or_multi_study_pred(tmp_path):
    out = tmp_path / "packets"
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps([load_draft(), load_draft()]))
    result = runner.invoke(app, ["review", "packet", "--pred", str(multi), "--out", str(out), "--offline"])
    assert result.exit_code == 1
    result = runner.invoke(
        app, ["review", "packet", "--pred", str(tmp_path / "nope.json"), "--out", str(out), "--offline"]
    )
    assert result.exit_code == 1


def test_ingest_then_report_round_trip(tmp_path):
    packets = tmp_path / "packets"
    packets.mkdir()
    (packets / f"{PMID}.manifest.json").write_text(json.dumps({"draft_sha256": canonical_sha256(load_draft())}))
    good = tmp_path / "in" / "good.json"
    good.parent.mkdir()
    good.write_text(json.dumps(make_verdict(name="Ada", taxa={(0, 0, 1): "wrong_taxon"})))
    stale = tmp_path / "in" / "stale.json"
    stale.write_text(json.dumps(make_verdict(name="Bo", sha="0" * 64)))
    reviews = tmp_path / "reviews"

    result = runner.invoke(
        app, ["review", "ingest", str(good), str(stale), "--dest", str(reviews), "--manifests", str(packets)]
    )
    assert result.exit_code == 1  # one refused
    assert "refused" in _plain(result.output) and "1 ingested, 1 not ingested" in _plain(result.output)
    assert [p.name for p in (reviews / PMID).iterdir()] == ["ada_20261006T123000Z.json"]

    result = runner.invoke(
        app, ["review", "ingest", str(stale), "--dest", str(reviews), "--manifests", str(packets), "--force"]
    )
    assert result.exit_code == 0, result.output
    assert len(list((reviews / PMID).iterdir())) == 2

    report = tmp_path / "out" / "report.md"
    result = runner.invoke(app, ["review", "report", "--reviews", str(reviews), "--out", str(report)])
    assert result.exit_code == 0, result.output
    md = report.read_text(encoding="utf-8")
    assert "# BugSigDB review report" in md and "## Legend" in md
    assert "| Reviewers | 2 |" in md
    assert "(7/8)" in md  # 4 taxa x 2 reviewers = 8 judged; one wrong_taxon


def test_report_missing_dir_errors(tmp_path):
    result = runner.invoke(app, ["review", "report", "--reviews", str(tmp_path / "none")])
    assert result.exit_code == 1


def test_packet_with_nan_in_the_draft_fails_cleanly(tmp_path):
    pred = tmp_path / "nan.json"
    pred.write_text(json.dumps(load_draft()).replace('"year": 2024', '"year": NaN'), encoding="utf-8")
    result = runner.invoke(app, ["review", "packet", "--pred", str(pred), "--out", str(tmp_path / "o"), "--offline"])
    assert result.exit_code == 1
    assert "holds NaN or Infinity" in _plain(result.output)
    assert not (tmp_path / "o").exists()
