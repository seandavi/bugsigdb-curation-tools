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


def group_orientation_text(group_0: str | None, group_1: str | None, *, may_decline: bool = False) -> str:
    """The prompt block that pins down which group is which, or "" when either name is unknown.

    BugSigDB's convention (it holds for ~95% of curated experiments where one group is
    recognisably the control): Group 0 is the reference/control/baseline group and Group 1 the
    case/exposed/treated group, and every signature reports taxa INCREASED or DECREASED in Group 1
    relative to Group 0. Without the names the model has to guess which group "Group 1" is, which
    flips directions wholesale (L031).

    `may_decline` adds the escape hatch: return no taxa when the artifact does not report this
    comparison. Offer it only when the caller has somewhere to fall back to (another experiment or
    candidate artifact): otherwise a decline just empties the experiment, and S4's group names often
    differ from the artifact's own labels (HC vs "Healthy controls").
    """
    if not (group_0 and group_0.strip() and group_1 and group_1.strip()):
        return ""
    escape_hatch = (
        "If this table or figure does NOT report a comparison between these two groups, return "
        '{"taxa": []} -- do not fill in taxa from a different comparison.\n'
        if may_decline
        else ""
    )
    return (
        "The two compared groups are:\n"
        f"- Group 0 (the reference / control / baseline group): {group_0.strip()}\n"
        f"- Group 1 (the case / exposed / treated group): {group_1.strip()}\n"
        "Report each taxon's direction relative to these groups: INCREASED means more abundant in "
        "Group 1 than in Group 0; DECREASED means less abundant in Group 1 than in Group 0. In a "
        "figure, use the legend to decide which colour or side belongs to which group -- never "
        "assume the left/top/first-listed group is Group 1.\n"
        f"{escape_hatch}\n"
    )
