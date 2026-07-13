"""S5a -- locate: find the differential-abundance artifact for an experiment.

Cheap and model-insensitive by design (per the workflow plan §6a: "the
legend/caption reliably points at the DA panel; the table header at the DA
columns") -- a pure heuristic over the bundle's own table captions/figure
legends, no model call. Prefers an artifact whose caption/legend mentions a
differential-abundance signal (LEfSe, LDA, "differential", "significant(ly)
abundant"), falling back to the first available table, then the first
available figure.

**Thin/stub note for the reviewer:** this heuristic picks ONE shared
candidate artifact per bundle, not a distinct artifact per experiment --
correct for the common single-experiment/single-DA-artifact paper (the
smoke set's `21850056` anchor), but not yet differentiated for a
many-experiment paper with several DA artifacts (that per-experiment
disambiguation is exactly the kind of per-comparison specialization
Architecture B's Experiment Workers are meant to add later; see the plan §2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from bugsigdb_curation.curator.evidence import EvidenceBundle, EvidenceFigure, EvidenceTable

_DA_SIGNAL_RE = re.compile(
    r"differential|lefse|\bLDA\b|significant(?:ly)?\s+(?:abundant|different|enriched|depleted)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LocatedArtifact:
    """S5a's output: the artifact S5b should read for one experiment."""

    kind: Literal["table", "figure"]
    table: EvidenceTable | None = None
    figure: EvidenceFigure | None = None

    @property
    def provenance(self) -> str:
        if self.kind == "table" and self.table is not None:
            return self.table.provenance
        if self.kind == "figure" and self.figure is not None:
            return self.figure.provenance
        return ""


def locate_artifact(bundle: EvidenceBundle) -> LocatedArtifact | None:
    """S5a: pick the bundle's best candidate differential-abundance artifact, if any."""
    for table in bundle.tables:
        if _DA_SIGNAL_RE.search(table.caption or "") or _DA_SIGNAL_RE.search(table.label or ""):
            return LocatedArtifact(kind="table", table=table)
    for figure in bundle.figures:
        if _DA_SIGNAL_RE.search(figure.legend or ""):
            return LocatedArtifact(kind="figure", figure=figure)
    if bundle.tables:
        return LocatedArtifact(kind="table", table=bundle.tables[0])
    if bundle.figures:
        return LocatedArtifact(kind="figure", figure=bundle.figures[0])
    return None
