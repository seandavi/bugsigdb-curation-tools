"""Tests for bugsigdb_curation.review.verdicts: schema validation, ingest, and report math."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from review_support import PMID, load_draft, make_verdict

from bugsigdb_curation.review.verdicts import (
    canonical_sha256,
    flip_rate,
    ingest_verdict_files,
    load_reviews,
    ok_rate,
    render_report,
    reviewer_slug,
    tally_verdicts,
    taxa_precision,
    validate_verdicts,
)


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _manifest(directory: Path, sha: str) -> Path:
    return _write(directory / f"{PMID}.manifest.json", {"pmid": PMID, "draft_sha256": sha})


# --- canonical hash / schema ---------------------------------------------------------------


def test_canonical_sha256_is_key_order_independent_and_keeps_unicode():
    assert canonical_sha256({"a": 1, "b": "é"}) == canonical_sha256({"b": "é", "a": 1})
    assert canonical_sha256({"a": "é"}) != canonical_sha256({"a": "e"})


def test_valid_verdict_passes_schema():
    assert validate_verdicts(make_verdict()) == []


def test_unreviewed_items_may_be_null():
    v = make_verdict(study=None, rating=None, taxa={(0, 0, 0): None}, directions={(0, 0): None}, experiments={0: None})
    assert validate_verdicts(v) == []


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda v: v.update(schema_version=2), "schema_version"),
        (lambda v: v.pop("draft_sha256"), "draft_sha256"),
        (lambda v: v.update(draft_sha256="abc"), "draft_sha256"),
        (lambda v: v.update(pmid="../etc"), "pmid"),
        (lambda v: v["experiments"][0]["signatures"][0]["taxa"][0].update(verdict="maybe"), "verdict"),
        (lambda v: v["experiments"][0].update(verdict="flipped"), "verdict"),
        (lambda v: v["experiments"][0]["signatures"][0].update(direction_verdict="wrong"), "direction_verdict"),
        (lambda v: v["overall"].update(time_saved_rating=6), "time_saved_rating"),
        (lambda v: v["reviewer"].update(name=""), "name"),
        (lambda v: v.update(exported_at="yesterday"), "exported_at"),
        (lambda v: v.update(surprise=1), "surprise"),
    ],
)
def test_schema_rejects_violations(mutate, fragment):
    v = make_verdict()
    mutate(v)
    errors = validate_verdicts(v)
    assert errors
    assert any(fragment in e for e in errors)


# --- ingest --------------------------------------------------------------------------------


def test_ingest_valid_file_lands_in_pmid_dir_with_reviewer_and_timestamp(tmp_path):
    src = _write(tmp_path / "in" / "verdicts.json", make_verdict(name="Ada B. Reviewer"))
    results = ingest_verdict_files([src], dest=tmp_path / "reviews")
    assert [r.status for r in results] == ["ingested"]
    expected = tmp_path / "reviews" / PMID / "ada-b-reviewer_20261006T123000Z.json"
    assert results[0].dest == expected
    assert json.loads(expected.read_text()) == json.loads(src.read_text())


def test_ingest_checks_manifest_sha(tmp_path):
    sha = canonical_sha256(load_draft())
    manifests = tmp_path / "packets"
    _manifest(manifests, sha)
    good = _write(tmp_path / "good.json", make_verdict())
    assert ingest_verdict_files([good], dest=tmp_path / "r", manifests_dir=manifests)[0].status == "ingested"

    _manifest(manifests, "f" * 64)
    refused = ingest_verdict_files([good], dest=tmp_path / "r2", manifests_dir=manifests)[0]
    assert refused.status == "refused"
    assert "--force" in refused.message
    assert not (tmp_path / "r2").exists()


def test_ingest_force_overrides_sha_mismatch_with_warning(tmp_path):
    manifests = tmp_path / "packets"
    _manifest(manifests, "f" * 64)
    f = _write(tmp_path / "v.json", make_verdict())
    result = ingest_verdict_files([f], dest=tmp_path / "r", manifests_dir=manifests, force=True)[0]
    assert result.status == "ingested"
    assert result.dest is not None and result.dest.is_file()
    assert any("does not match" in w for w in result.warnings)


def test_ingest_without_manifest_warns_but_ingests(tmp_path):
    f = _write(tmp_path / "v.json", make_verdict())
    result = ingest_verdict_files([f], dest=tmp_path / "r")[0]
    assert result.status == "ingested"
    assert any("no manifest" in w for w in result.warnings)


def test_ingest_looks_beside_verdict_file_when_no_manifests_dir(tmp_path):
    _manifest(tmp_path, "f" * 64)
    f = _write(tmp_path / "v.json", make_verdict())
    assert ingest_verdict_files([f], dest=tmp_path / "r")[0].status == "refused"


def test_ingest_rejects_schema_violations_and_bad_json(tmp_path):
    bad = make_verdict()
    bad["experiments"][0]["verdict"] = "great"
    f_bad = _write(tmp_path / "bad.json", bad)
    f_junk = tmp_path / "junk.json"
    f_junk.write_text("{not json")
    results = ingest_verdict_files([f_bad, f_junk, tmp_path / "missing.json"], dest=tmp_path / "r")
    assert [r.status for r in results] == ["invalid", "invalid", "invalid"]
    assert not (tmp_path / "r").exists()


# --- report math ---------------------------------------------------------------------------
# Fixture draft taxa: (0,0,0) B. fragilis, (0,0,1) unresolved, (0,1,0) F. prausnitzii, (1,0,0) Akkermansia.


def test_report_helpers():
    from collections import Counter

    assert taxa_precision(Counter(correct=3, wrong_taxon=1, not_in_source=1, unsure=9)) == 0.6
    assert taxa_precision(Counter(unsure=2)) is None
    assert flip_rate(Counter(ok=3, flipped=1, unsure=5)) == 0.25
    assert ok_rate(Counter(ok=1, needs_edit=1, wrong=2, unsure=7)) == 0.25


def test_report_aggregates_exact_numbers():
    a = make_verdict(
        name="Ada",
        taxa={(0, 0, 1): "wrong_taxon", (0, 1, 0): "not_in_source", (1, 0, 0): "unsure"},
        directions={(0, 1): "flipped"},
        experiments={0: "needs_edit", 1: "ok"},
        study="needs_edit",
        rating=5,
        missing_note="Missed Prevotella copri increase",
    )
    b = make_verdict(
        name="Ben",
        taxa={(0, 0, 0): "correct", (0, 0, 1): "unsure", (0, 1, 0): "correct"},
        directions={(0, 0): "unsure"},
        experiments={0: "wrong", 1: "unsure"},
        study="ok",
        rating=2,
    )
    tallies = tally_verdicts([a, b])
    assert len(tallies) == 1
    t = tallies[0]
    assert t.reviewers == {"ada", "ben"}
    # Ada: B.fragilis correct, unresolved wrong_taxon, F.prausnitzii not_in_source, Akkermansia unsure
    # Ben:  B.fragilis correct, unresolved unsure,      F.prausnitzii correct,      Akkermansia correct
    assert (t.taxa["correct"], t.taxa["wrong_taxon"], t.taxa["not_in_source"], t.taxa["unsure"]) == (4, 1, 1, 2)
    assert taxa_precision(t.taxa) == 4 / 6
    # Directions: Ada ok, flipped, ok; Ben unsure, ok, ok  -> flipped 1 of 5 judged
    assert (t.directions["ok"], t.directions["flipped"], t.directions["unsure"]) == (4, 1, 1)
    assert flip_rate(t.directions) == 0.2
    assert t.signature_directions[(0, 1)]["flipped"] == 1
    assert (t.experiments["ok"], t.experiments["needs_edit"], t.experiments["wrong"], t.experiments["unsure"]) == (
        1,
        1,
        1,
        1,
    )
    assert ok_rate(t.study) == 0.5
    assert t.ratings == [5, 2]

    md = render_report(tallies)
    assert "| Taxa precision | 66.7% (4/6) |" in md
    assert "| Taxa unsure / unreviewed | 2 / 0 |" in md
    assert "| Direction flip rate | 20.0% (1/5) |" in md
    assert "| Experiments ok / needs edit / wrong | 1 / 1 / 1 |" in md
    assert "| Study ok rate | 50.0% (1/2) |" in md
    assert "| Mean time-saved rating (1-5) | 3.50 (n=2) |" in md
    assert "| Reviewers | 2 |" in md
    assert "Missed Prevotella copri increase" in md
    assert "## Legend" in md


def test_unsure_and_unreviewed_never_count_as_negatives():
    v = make_verdict(taxa={(0, 0, 0): "unsure", (0, 0, 1): None, (0, 1, 0): "correct", (1, 0, 0): "correct"})
    t = tally_verdicts([v])[0]
    assert t.taxa["unsure"] == 1 and t.taxa["unreviewed"] == 1
    assert taxa_precision(t.taxa) == 1.0


def test_same_reviewer_re_export_counts_once_latest_wins():
    first = make_verdict(name="Ada", study="wrong", exported_at="2026-10-06T10:00:00.000Z")
    second = make_verdict(name="Ada", study="ok", exported_at="2026-10-06T11:00:00.000Z")
    t = tally_verdicts([second, first])[0]
    assert t.reviewers == {"ada"}
    assert t.study == {"ok": 1}


def test_different_draft_versions_reported_as_separate_rows():
    other = copy.deepcopy(load_draft())
    other["experiments"][0]["host_species"] = "Pan troglodytes"
    tallies = tally_verdicts([make_verdict(), make_verdict(record=other)])
    assert len(tallies) == 2
    md = render_report(tallies)
    assert md.count(f"{PMID} (draft ") >= 2


def test_load_reviews_skips_invalid_and_reports_them(tmp_path):
    _write(tmp_path / PMID / "ada_1.json", make_verdict())
    _write(tmp_path / PMID / "broken_1.json", {"schema_version": 1})
    verdicts, problems = load_reviews(tmp_path)
    assert len(verdicts) == 1
    assert len(problems) == 1 and "broken_1.json" in problems[0]
    assert "Skipped 1" in render_report(tally_verdicts(verdicts), problems=problems)


def test_report_with_no_verdicts():
    assert "No verdicts found." in render_report([])


def test_reviewer_slug_fallbacks():
    assert reviewer_slug({"name": "Dr. A. Reviewer", "email": "x@y.org"}) == "dr-a-reviewer"
    assert reviewer_slug({"name": "", "email": "x@y.org"}) == "x-y-org"
    assert reviewer_slug({"name": "", "email": ""}) == "anonymous"
