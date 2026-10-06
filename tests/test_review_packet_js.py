"""Runs a built packet's JavaScript under Node (fake DOM, no browser) with a simulated review.

Checks the pure verdict-building functions and the DOM wiring end to end: the exported JSON must validate
against `schema/review_verdict.schema.json` and carry the right counts. Skipped when `node` is missing.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from review_support import PMID, load_annotations, load_draft, sample_evidence, sample_meta

from bugsigdb_curation.review.packet import build_packet
from bugsigdb_curation.review.verdicts import canonical_sha256, validate_verdicts

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

DRIVER = Path(__file__).parent / "review_packet_driver.js"


def run_scenario(
    tmp_path: Path, scenario: str, record: dict[str, Any] | None = None, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a packet for `record` (default: the fixture draft), run one driver scenario on it, parse its transcript."""
    record = record if record is not None else load_draft()
    page = build_packet(record, load_annotations(), sample_evidence("cc by"), sample_meta(record))
    html = tmp_path / "packet.html"
    html.write_text(page, encoding="utf-8")
    args = ["node", str(DRIVER), str(html), scenario, json.dumps(options or {})]
    done = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture(scope="module")
def transcript(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return run_scenario(tmp_path_factory.mktemp("js"), "review")


def _exported(transcript: dict[str, Any]) -> dict[str, Any]:
    (json_download,) = [d for d in transcript["downloads"] if d["filename"].endswith(".json")]
    return json.loads(json_download["text"])


def test_progress_counts_every_reviewable_item(transcript):
    # study + 2 experiments + 3 signature directions + 4 taxa
    assert len(transcript["reviewKeys"]) == 10
    assert transcript["initialProgress"] == "0 of 10 items reviewed"
    # reviewed: study, both experiments, 2 of 3 directions, all 4 taxa; only exp 1's direction is left
    assert transcript["progressAfterReview"] == "9 of 10 items reviewed"


def test_exported_json_validates_against_schema_and_has_right_counts(transcript):
    verdicts = _exported(transcript)
    assert validate_verdicts(verdicts) == []
    record = load_draft()
    assert verdicts["draft_sha256"] == canonical_sha256(record)
    assert verdicts["packet_id"] == sample_meta(record).packet_id
    assert verdicts["pmid"] == PMID
    assert verdicts["schema_version"] == 1
    assert len(verdicts["experiments"]) == 2
    assert [len(e["signatures"]) for e in verdicts["experiments"]] == [2, 1]
    assert [[len(s["taxa"]) for s in e["signatures"]] for e in verdicts["experiments"]] == [[2, 1], [1]]
    assert verdicts["exported_at"] >= verdicts["started_at"]


def test_exported_json_carries_the_simulated_review(transcript):
    v = _exported(transcript)
    assert v["reviewer"] == {"name": "Ada B. Reviewer", "email": "ada@example.org", "role": "curator"}
    assert v["study"] == {"verdict": "ok", "note": ""}
    e0, e1 = v["experiments"]
    assert (e0["verdict"], e0["note"], e0["missing_note"]) == (
        "needs_edit",
        "check group labels",
        "Prevotella copri increased",
    )
    assert e1["verdict"] == "ok"
    s00, s01 = e0["signatures"]
    assert (s00["direction_verdict"], s01["direction_verdict"]) == ("ok", "flipped")
    assert [(t["name"], t["verdict"]) for t in s00["taxa"]] == [
        ("Bacteroides fragilis", "correct"),
        ("Candidatus Fixtureia unresolvedus", "wrong_taxon"),
    ]
    assert s00["taxa"][1]["note"] == 'has "quotes", and, commas'
    assert s01["taxa"][0]["verdict"] == "not_in_source"
    assert e1["signatures"][0]["direction_verdict"] is None  # left unreviewed -> null, never invented
    assert e1["signatures"][0]["taxa"][0]["verdict"] == "unsure"
    assert v["missing_experiments_note"] == "Experiment on weight loss"
    assert v["overall"] == {"time_saved_rating": 4, "would_publish_after_edits": "yes", "comment": "Nice draft"}
    assert v["minutes_spent"] == 25


def test_export_file_names_and_confirmation_for_unreviewed_items(transcript):
    names = [d["filename"] for d in transcript["downloads"]]
    assert names == [f"verdicts_{PMID}_ada-b-reviewer.json", f"verdicts_{PMID}_ada-b-reviewer.csv"]
    assert len(transcript["confirms"]) == 2 and "1 item(s) have no verdict yet" in transcript["confirms"][0]


def test_export_refused_without_reviewer_name(transcript):
    assert transcript["noNameDownloads"] == 0
    assert "enter your name" in transcript["noNameError"]


def test_csv_export_is_parseable_and_complete(transcript):
    (csv_download,) = [d for d in transcript["downloads"] if d["filename"].endswith(".csv")]
    assert csv_download["type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(csv_download["text"])))
    taxa = [r for r in rows if r["level"] == "taxon"]
    assert len(taxa) == 4
    assert {r["pmid"] for r in rows} == {PMID}
    noted = [r for r in taxa if r["note"]]
    assert [r["note"] for r in noted] == ['has "quotes", and, commas']
    assert [r["verdict"] for r in rows if r["level"] == "signature_direction"] == ["ok", "flipped", ""]
    assert [r["level"] for r in rows if r["level"].startswith("experiment")].count("experiment") == 2


def test_progress_is_autosaved_and_restored_on_reload(transcript):
    assert any(k.startswith("bugsigdb-review:" + PMID) for k in transcript["storageKeys"])
    assert transcript["restoredProgress"] == transcript["progressAfterReview"]
    assert transcript["restored"] == {
        "note": 'has "quotes", and, commas',
        "verdict": "flipped",
        "name": "Ada B. Reviewer",
        "rating": "4",
    }


def test_reset_needs_confirmation_and_clears_everything(transcript):
    assert transcript["progressAfterDeclinedReset"] == transcript["progressAfterReview"]
    assert transcript["progressAfterReset"] == "0 of 10 items reviewed"
    assert transcript["valueAfterReset"] == ""
    assert transcript["storageAfterReset"] == []


def test_mark_all_taxa_correct_and_select_styling_hook(transcript):
    assert transcript["afterMarkAll"] == ["correct", "correct"]
    assert transcript["selectDataV"] == "needs_edit"


def test_image_is_injected_from_the_embedded_store_and_zooms(transcript):
    assert transcript["imageCount"] == 1
    assert transcript["imageSrcPrefix"] == "data:image/png;base64,"
    assert transcript["zoomed"] is True


def test_restore_state_ignores_garbage(transcript):
    assert transcript["restoreGarbage"] == {"exp.0.note": "kept"}
    assert transcript["restoreNull"] == {}


def test_csv_cells_that_look_like_formulas_are_neutralised(tmp_path):
    record = load_draft()
    record["experiments"][0]["signatures"][0]["taxa"][0]["taxon_name"] = '=HYPERLINK("http://x","y")'
    csv_text = run_scenario(tmp_path, "csv_injection", record)["csv"]
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    by_level = {(r["level"], r["taxon"]): r for r in rows}
    assert by_level[("taxon", "'=HYPERLINK(\"http://x\",\"y\")")]["note"] == "'+1"
    assert all(not r["taxon"].startswith("=") for r in rows)
    assert {r["note"] for r in rows if r["level"] == "taxon"} >= {"'+1", "'-2 fold"}
    assert next(r for r in rows if r["level"] == "experiment")["note"] == "'\tindented"
    assert rows[0]["reviewer"] == "'@reviewer"
    # ordinary text is left alone
    assert next(r for r in rows if r["level"] == "study")["note"] == "plain, with comma"


def test_mark_remaining_taxa_correct_keeps_verdicts_already_given(tmp_path):
    transcript = run_scenario(tmp_path, "mark_remaining")
    assert transcript["verdicts"] == ["wrong_taxon", "correct"]
    assert transcript["other_signature"] == ""


def test_mark_taxa_button_is_labelled_remaining():
    record = load_draft()
    page = build_packet(record, load_annotations(), sample_evidence("cc by"), sample_meta(record))
    assert "Mark remaining taxa correct" in page and "Mark all taxa correct" not in page


@pytest.mark.parametrize("storage", ["null", "denied", "setItem-throws"])
def test_export_still_works_when_browser_storage_is_unavailable(tmp_path, storage):
    transcript = run_scenario(tmp_path, "storage_failure", options={"storage": storage})
    assert [d["filename"] for d in transcript["downloads"]] == [
        f"verdicts_{PMID}_ada-b-reviewer.json",
        f"verdicts_{PMID}_ada-b-reviewer.csv",
    ]
    assert validate_verdicts(transcript["json"]) == []
    assert transcript["json"]["study"]["verdict"] == "ok"
    assert "Not auto-saved" in transcript["status"]
    assert transcript["progress"].endswith("items reviewed")


def test_status_line_after_init_describes_what_is_really_happening(tmp_path):
    transcript = run_scenario(tmp_path, "status")
    assert transcript["status"] == "Progress is saved in this browser as you go."


def test_failing_init_is_visible_in_the_status_area(tmp_path):
    transcript = run_scenario(tmp_path, "status", options={"brokenData": True})
    assert "could not start" in transcript["status"]
    assert transcript["className"] == "failed"


def test_reviewer_identity_is_shown_and_never_overwritten_with_an_empty_name(tmp_path):
    t = run_scenario(tmp_path, "reviewer_identity")
    assert t["lineOnLoad"] == "Reviewing as Ada Remembered — not you?"
    assert t["nameOnLoad"] == "Ada Remembered"
    assert "Enter your name" in t["lineWhenBlank"]
    assert json.loads(t["rememberedAfterBlank"])["name"] == "Ada Remembered"
    assert json.loads(t["rememberedAfterReset"])["name"] == "Ada Remembered"
    assert t["nameAfterChange"] == "" and t["rememberedAfterChange"] is None and t["nameFocused"] is True


def test_export_file_name_keeps_non_ascii_letters(tmp_path):
    names = run_scenario(tmp_path, "file_name")
    assert names == {"wang": "verdicts_1_王伟.json", "ada": "verdicts_1_ada-b-reviewer.json", "punct": "verdicts_1_anonymous.json"}


def test_csv_numbers_experiments_and_signatures_like_the_page_and_keeps_zero_based_indexes(transcript):
    (csv_download,) = [d for d in transcript["downloads"] if d["filename"].endswith(".csv")]
    rows = list(csv.DictReader(io.StringIO(csv_download["text"])))
    taxa = [r for r in rows if r["level"] == "taxon"]
    assert [(r["experiment_no"], r["signature_no"]) for r in taxa] == [("1", "1"), ("1", "1"), ("1", "2"), ("2", "1")]
    assert [(r["experiment_index"], r["signature_index"]) for r in taxa] == [("0", "0"), ("0", "0"), ("0", "1"), ("1", "0")]
    study = next(r for r in rows if r["level"] == "study")
    assert (study["experiment_no"], study["signature_no"], study["experiment_index"]) == ("", "", "")


def test_duplicate_taxon_names_in_one_signature_keep_separate_verdicts(tmp_path):
    record = load_draft()
    taxa = record["experiments"][0]["signatures"][0]["taxa"]
    taxa[1] = {**taxa[1], "taxon_name": taxa[0]["taxon_name"]}
    exported = run_scenario(tmp_path, "duplicate_taxa", record)
    assert [(t["name"], t["verdict"], t["note"]) for t in exported] == [
        (taxa[0]["taxon_name"], "correct", ""),
        (taxa[0]["taxon_name"], "wrong_taxon", "second mention"),
    ]
