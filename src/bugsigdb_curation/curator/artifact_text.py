"""Shared helper: render a S5a-located artifact as plain-text prompt content.

Every S5b-family stage across all three designs (fused-lean's fused extract,
split A1's NER, split-verify's verifier, split-panel's reviewer) needs to
show a model "the source" for the same `LocatedArtifact` -- this is the one
place that decides how a table's rows vs. a figure's legend become prompt
text, so the new split-design stages render it identically to how
`curator.signature` already does for fused-lean (that module keeps its own
inline copy rather than importing this, so fused-lean's prompt bytes stay
untouched by this addition -- see its module docstring / the workflow plan's
"fused-lean output must not change").
"""

from __future__ import annotations

from bugsigdb_curation.curator.locate import LocatedArtifact


def artifact_kind_and_text(artifact: LocatedArtifact) -> tuple[str, str]:
    """Return `(artifact_kind, artifact_content)` for `artifact`.

    `artifact_kind` is `"table"` or `"figure"` (for a prompt's "this
    {artifact_kind}" phrasing); `artifact_content` is the table's rendered
    text (label + caption + rows) or the figure's legend, labeled with its
    provenance string. Raises `ValueError` if `artifact.kind` doesn't match
    its own payload (a `locate_artifact` contract violation, not a normal
    runtime case).
    """
    if artifact.kind == "table" and artifact.table is not None:
        return "table", f"Table ({artifact.table.provenance}):\n{artifact.table.as_text()}"
    if artifact.kind == "figure" and artifact.figure is not None:
        return "figure", f"Figure legend ({artifact.figure.provenance}):\n{artifact.figure.legend}"
    raise ValueError(f"LocatedArtifact of kind {artifact.kind!r} is missing its payload")
