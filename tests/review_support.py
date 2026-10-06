"""Shared fixtures for the review-packet tests: the synthetic draft, sample evidence, hand-made verdicts."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from bugsigdb_curation.review.verdicts import canonical_sha256

DATA_DIR = Path(__file__).parent / "data" / "review"
PMID = "99000001"


def load_draft() -> dict[str, Any]:
    return json.loads((DATA_DIR / "draft.json").read_text(encoding="utf-8"))


def load_annotations() -> dict[str, Any]:
    return json.loads((DATA_DIR / "draft.annotations.json").read_text(encoding="utf-8"))


def figure_png() -> bytes:
    return (DATA_DIR / "figure2.png").read_bytes()


def sample_evidence(license_: str | None = "cc by", *, image: bytes | None = None) -> Any:
    from bugsigdb_curation.curator.evidence import EvidenceFigure, EvidenceTable
    from bugsigdb_curation.review.packet import PacketEvidence

    return PacketEvidence(
        pmcid="PMC9000001",
        license=license_,
        figures=(
            EvidenceFigure(
                figure_id="F2",
                number="2",
                label="Figure 2.",
                legend="LEfSe analysis of fixture patients versus healthy controls.",
                graphic_filename="fig2.png",
                blob_url="https://cdn.ncbi.nlm.nih.gov/pmc/blobs/x/fig2.png",
            ),
        ),
        tables=(
            EvidenceTable(
                table_id="T1",
                number="1",
                label="Table 1.",
                caption="Differentially abundant taxa in treated mice.",
                rows=(("Taxon", "log2FC", "padj"), ("Akkermansia muciniphila", "2.1", "0.01")),
            ),
        ),
        images={"Figure 2": image if image is not None else figure_png()},
    )


def sample_meta(record: dict[str, Any] | None = None) -> Any:
    from bugsigdb_curation.review.packet import PacketMeta

    sha = canonical_sha256(record if record is not None else load_draft())
    return PacketMeta(
        packet_id=f"{PMID}-{sha[:12]}",
        pmid=PMID,
        draft_sha256=sha,
        built_at="2026-10-06T12:00:00Z",
        builder_commit="abc1234",
        model_label="gemini-test",
        design_label="fused-lean",
        pmcid="PMC9000001",
    )


def make_verdict(
    *,
    name: str = "Ada Reviewer",
    taxa: dict[tuple[int, int, int], str | None] | None = None,
    directions: dict[tuple[int, int], str | None] | None = None,
    experiments: dict[int, str | None] | None = None,
    study: str | None = "ok",
    rating: int | None = 4,
    exported_at: str = "2026-10-06T12:30:00.000Z",
    record: dict[str, Any] | None = None,
    sha: str | None = None,
    missing_note: str = "",
) -> dict[str, Any]:
    """A schema-valid verdict file for the fixture draft; unspecified taxa/directions are 'correct'/'ok'."""
    record = copy.deepcopy(record if record is not None else load_draft())
    sha = sha or canonical_sha256(record)
    taxa = taxa or {}
    directions = directions or {}
    experiments = experiments or {}
    exps = []
    for e, exp in enumerate(record["experiments"]):
        sigs = []
        for s, sig in enumerate(exp["signatures"]):
            sigs.append(
                {
                    "index": s,
                    "direction_verdict": directions.get((e, s), "ok"),
                    "taxa": [
                        {"name": t["taxon_name"], "verdict": taxa.get((e, s, k), "correct"), "note": ""}
                        for k, t in enumerate(sig["taxa"])
                    ],
                }
            )
        exps.append(
            {
                "index": e,
                "verdict": experiments.get(e, "ok"),
                "note": "",
                "missing_note": missing_note if e == 0 else "",
                "signatures": sigs,
            }
        )
    return {
        "schema_version": 1,
        "packet_id": f"{record['pmid']}-{sha[:12]}",
        "pmid": str(record["pmid"]),
        "draft_sha256": sha,
        "reviewer": {"name": name, "email": "", "role": ""},
        "started_at": "2026-10-06T12:00:00.000Z",
        "exported_at": exported_at,
        "minutes_spent": 20,
        "study": {"verdict": study, "note": ""},
        "experiments": exps,
        "missing_experiments_note": "",
        "overall": {"time_saved_rating": rating, "would_publish_after_edits": "yes", "comment": ""},
    }
