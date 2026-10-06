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
    parse_timestamp,
    render_report,
    review_schema_path,
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


# --- timestamps and identifiers the schema regex alone lets through ---------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "2026-02-30T12:00:00.000Z",  # no 30 February
        "2026-10-06T25:00:00.000Z",  # no hour 25
        "\u0662\u0660\u0662\u0666-10-06T12:00:00.000Z",  # Arabic-Indic digits: Python \d matches them
        "2026-10-06T12:00:00.000Z\n",  # trailing newline: `$` accepts it
    ],
)
def test_timestamps_that_are_not_real_instants_are_invalid(bad):
    for field in ("exported_at", "started_at"):
        v = make_verdict()
        v[field] = bad
        errors = validate_verdicts(v)
        assert errors and any(field in e for e in errors)


@pytest.mark.parametrize(("field", "bad"), [("pmid", "123\n"), ("draft_sha256", "a" * 64 + "\n")])
def test_trailing_newline_is_not_allowed_in_pmid_or_hash(field, bad):
    v = make_verdict()
    v[field] = bad
    assert any(field in e for e in validate_verdicts(v))


def test_schema_patterns_are_ascii_only():
    schema = json.loads(review_schema_path().read_text(encoding="utf-8"))
    assert "\\d" not in schema["$defs"]["timestamp"]["pattern"]


def test_one_bad_date_does_not_abort_the_batch(tmp_path):
    good1 = _write(tmp_path / "in" / "a.json", make_verdict(name="Ada", exported_at="2026-10-06T10:00:00.000Z"))
    bad = _write(tmp_path / "in" / "b.json", make_verdict(name="Bo", exported_at="2026-02-30T10:00:00.000Z"))
    good2 = _write(tmp_path / "in" / "c.json", make_verdict(name="Cy", exported_at="2026-10-06T11:00:00.000Z"))
    results = ingest_verdict_files([good1, bad, good2], dest=tmp_path / "reviews")
    assert [r.status for r in results] == ["ingested", "invalid", "ingested"]
    assert "exported_at" in results[1].message
    assert sorted(p.name for p in (tmp_path / "reviews" / PMID).iterdir()) == [
        "ada_20261006T100000Z.json",
        "cy_20261006T110000Z.json",
    ]


def test_cli_batch_with_a_bad_date_ends_with_a_summary(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bugsigdb_curation.cli import app

    for name, value in (("COLUMNS", "200"), ("NO_COLOR", "1"), ("TERM", "dumb")):
        monkeypatch.setenv(name, value)
    files = [
        _write(tmp_path / "in" / "a.json", make_verdict(name="Ada")),
        _write(tmp_path / "in" / "b.json", make_verdict(name="Bo", exported_at="2026-10-06T25:00:00.000Z")),
        _write(tmp_path / "in" / "c.json", make_verdict(name="Cy")),
    ]
    result = CliRunner().invoke(app, ["review", "ingest", *map(str, files), "--dest", str(tmp_path / "r")])
    assert result.exit_code == 1
    assert "2 ingested, 1 not ingested" in " ".join(result.output.split())


def test_parse_timestamp_is_utc_and_ordered_across_forms():
    a = parse_timestamp("2026-10-06T12:00:00Z")
    b = parse_timestamp("2026-10-06T12:00:00.123Z")
    c = parse_timestamp("2026-10-06T13:00:00+02:00")  # 11:00 UTC
    assert c < a < b
    with pytest.raises(ValueError, match="timestamp"):
        parse_timestamp("2026-13-01T00:00:00Z")


# --- reviewer identity ----------------------------------------------------------------------


def test_reviewer_slug_keeps_non_ascii_names_distinct_and_path_safe():
    wang = reviewer_slug({"name": "王伟", "email": ""})
    li = reviewer_slug({"name": "李娜", "email": ""})
    assert wang == "王伟" and li == "李娜" and wang != li
    for name in ("../../etc/passwd", "a/b\\c", "..", ".", "con", "x\x00y", "  ", "!!!", "😀", "a" * 300):
        slug = reviewer_slug({"name": name, "email": ""})
        assert slug and not set(slug) & set("/\\\x00.:") and len(slug) <= 64
    # nothing sluggable: a stable hash of name+email keeps two such reviewers apart
    emoji = reviewer_slug({"name": "😀", "email": ""})
    assert emoji.startswith("r-") and emoji != reviewer_slug({"name": "😎", "email": ""})
    assert emoji == reviewer_slug({"name": "😀", "email": ""})


def test_non_ascii_reviewers_stay_distinct_on_disk_and_in_the_report(tmp_path):
    one = _write(tmp_path / "in" / "1.json", make_verdict(name="王伟", study="ok"))
    two = _write(tmp_path / "in" / "2.json", make_verdict(name="李娜", study="wrong"))
    results = ingest_verdict_files([one, two], dest=tmp_path / "reviews")
    assert [r.status for r in results] == ["ingested", "ingested"]
    assert len(list((tmp_path / "reviews" / PMID).iterdir())) == 2
    verdicts, problems = load_reviews(tmp_path / "reviews")
    assert problems == []
    (tally,) = tally_verdicts(verdicts)
    assert tally.reviewers == {"王伟", "李娜"}
    assert tally.study == {"ok": 1, "wrong": 1}


def test_ingest_refuses_to_overwrite_a_different_file_unless_forced(tmp_path):
    first = _write(tmp_path / "in" / "1.json", make_verdict(name="Ada", study="ok"))
    second = _write(tmp_path / "in" / "2.json", make_verdict(name="Ada", study="wrong"))  # same name, same second
    dest = tmp_path / "reviews"
    assert ingest_verdict_files([first], dest=dest)[0].status == "ingested"
    conflict = ingest_verdict_files([second], dest=dest)[0]
    assert conflict.status == "conflict"
    assert "--force" in conflict.message
    (target,) = (dest / PMID).iterdir()
    assert json.loads(target.read_text())["study"]["verdict"] == "ok"
    assert ingest_verdict_files([second], dest=dest, force=True)[0].status == "ingested"
    assert json.loads(target.read_text())["study"]["verdict"] == "wrong"


def test_ingesting_identical_content_again_is_a_duplicate_no_op(tmp_path):
    f = _write(tmp_path / "in" / "1.json", make_verdict(name="Ada"))
    dest = tmp_path / "reviews"
    assert ingest_verdict_files([f], dest=dest)[0].status == "ingested"
    (target,) = (dest / PMID).iterdir()
    mtime = target.stat().st_mtime_ns
    again = ingest_verdict_files([f], dest=dest)[0]
    assert again.status == "duplicate" and again.dest == target
    assert target.stat().st_mtime_ns == mtime


# --- latest wins / numbering ----------------------------------------------------------------


def test_latest_export_is_decided_by_instant_not_by_string():
    # as strings "...:00.123Z" < "...:00Z" ('.' < 'Z'), but it is the later instant
    earlier = make_verdict(name="Ada", study="wrong", exported_at="2026-10-06T12:00:00Z")
    later = make_verdict(name="Ada", study="ok", exported_at="2026-10-06T12:00:00.123Z")
    assert tally_verdicts([later, earlier])[0].study == {"ok": 1}
    assert tally_verdicts([earlier, later])[0].study == {"ok": 1}
    # an offset form: 13:00+02:00 is 11:00 UTC, i.e. earlier than 12:00Z although "13" > "12"
    offset = make_verdict(name="Ada", study="needs_edit", exported_at="2026-10-06T13:00:00+02:00")
    assert tally_verdicts([offset, earlier])[0].study == {"wrong": 1}


def test_report_numbers_experiments_and_signatures_from_one_like_the_packet():
    v = make_verdict(directions={(0, 1): "flipped"}, missing_note="Missed Prevotella")
    md = render_report(tally_verdicts([v]))
    assert f"| {PMID} | 1 | 2 | 0 | 1 | 0 | 100.0% |" in md
    assert f"| {PMID} | 0 |" not in md
    assert "(experiment 1 missing from draft)" in md and "experiment 0" not in md
