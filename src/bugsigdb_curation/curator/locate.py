"""S5a -- locate: find the differential-abundance artifact for an experiment.

Cheap and model-insensitive by design (per the workflow plan §6a: "the
legend/caption reliably points at the DA panel; the table header at the DA
columns") -- a pure heuristic over the bundle's own table captions/figure
legends, no model call. Prefers an artifact whose caption/legend mentions a
differential-abundance signal (LEfSe, LDA, "differential", "significant(ly)
abundant"), falling back to the first available table, then the first
available figure.

`locate_artifact` picks ONE shared best artifact per bundle -- correct for the common
single-experiment/single-DA-artifact paper (the smoke set's `21850056` anchor).
`locate_artifacts` returns the several top-ranked candidates, so the pipeline can try them in rank
order per experiment (a many-comparison paper reports its comparisons in different artifacts).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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
    #: Decision-model probability that this is a per-taxon DA artifact
    #: (`curator.routing.rank_artifacts`); None when the regex locate chose it.
    p_da: float | None = None

    @property
    def provenance(self) -> str:
        if self.kind == "table" and self.table is not None:
            return self.table.provenance
        if self.kind == "figure" and self.figure is not None:
            return self.figure.provenance
        return ""


def locate_artifact(bundle: EvidenceBundle, ranked: Sequence[LocatedArtifact] | None = None) -> LocatedArtifact | None:
    """S5a: pick the bundle's best candidate differential-abundance artifact, if any.

    `ranked` is the decision-model ranking (`curator.routing.rank_artifacts`, best first); when it is
    non-empty its top artifact wins. Otherwise (no decision model, or its call failed) the DA-keyword
    regex heuristic below decides, exactly as before.
    """
    if ranked:
        return ranked[0]
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


#: Candidates handed to the per-experiment search, and the p(DA) a ranked artifact needs to be one.
DEFAULT_MAX_CANDIDATES = 3
DEFAULT_MIN_P_DA = 0.5


def locate_artifacts(
    bundle: EvidenceBundle,
    ranked: Sequence[LocatedArtifact] | None = None,
    *,
    max_n: int = DEFAULT_MAX_CANDIDATES,
    min_p: float = DEFAULT_MIN_P_DA,
) -> list[LocatedArtifact]:
    """S5a candidates for the per-experiment search, best first.

    With a non-empty decision-model `ranked`: its artifacts with `p_da >= min_p`, at most `max_n`, and
    ALWAYS at least the top one (even below `min_p`, as `locate_artifact` picks it today). Without one,
    just the regex choice (`[]` when the bundle has no artifact at all).
    """
    if ranked:
        confident = [a for a in ranked if (a.p_da or 0.0) >= min_p]
        return (confident or list(ranked[:1]))[:max_n]
    artifact = locate_artifact(bundle)
    return [artifact] if artifact is not None else []
