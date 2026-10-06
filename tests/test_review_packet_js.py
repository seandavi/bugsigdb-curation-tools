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


@pytest.fixture(scope="module")
def transcript(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    record = load_draft()
    page = build_packet(record, load_annotations(), sample_evidence("cc by"), sample_meta(record))
    html = tmp_path_factory.mktemp("js") / "packet.html"
    html.write_text(page, encoding="utf-8")
    done = subprocess.run(["node", str(DRIVER), str(html)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


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
