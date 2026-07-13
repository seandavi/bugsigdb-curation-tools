"""S2 -- study-metadata extraction: bundle -> Study fields.

Bibliographic fields (`title`/`journal`/`year`/`authors`/`doi`) are pulled
**deterministically** from the evidence bundle's already-parsed
`<article-meta>` (`EvidenceBundle.metadata`, see `curator/evidence.py`) --
no model call needed, matching the plan's "bibliographic from S0/metadata"
framing. Only `study_design` needs a judgment call, so it's the one field
here that goes through the Model seam, enum-constrained against
`bugsigdb_curation.loader.STUDY_DESIGN_VALUES` (never inventing a value
outside the schema's permissible set).
"""

from __future__ import annotations

from dataclasses import dataclass

from bugsigdb_curation.curator.evidence import EvidenceBundle
from bugsigdb_curation.curator.model import Model, build_text_content
from bugsigdb_curation.curator.resolve import ResolvedIds
from bugsigdb_curation.loader import STUDY_DESIGN_VALUES, normalize_enum

#: How much of the assembled full text to show the model for study-design
#: classification -- generous but bounded, since Methods/Results (where the
#: design signal lives) are usually well within the first few thousand chars
#: and this is a cheap classification call, not the fused extraction stage.
_STUDY_DESIGN_TEXT_CHARS = 8000


@dataclass(frozen=True, slots=True)
class StudyFields:
    """S2's output: the Study-level fields of the prediction record."""

    title: str | None
    journal: str | None
    year: int | None
    authors: tuple[str, ...]
    doi: str | None
    study_design: tuple[str, ...]


def build_study_design_messages(bundle: EvidenceBundle) -> list[dict]:
    allowed = sorted(STUDY_DESIGN_VALUES)
    prompt = (
        "You are extracting curation metadata from a microbiome research paper's full text "
        "for the BugSigDB database.\n\n"
        "Classify this study's design. Choose only from this exact list of permissible values "
        f"(you may return more than one if genuinely mixed-design): {allowed}\n\n"
        'Return ONLY a JSON object: {"study_design": ["<value from the list above>", ...]}\n\n'
        f"Article text:\n{bundle.full_text()[:_STUDY_DESIGN_TEXT_CHARS]}"
    )
    return [{"role": "user", "content": [build_text_content(prompt)]}]


def extract_study_design(bundle: EvidenceBundle, *, model: Model) -> tuple[str, ...]:
    """S2's one model call: classify `study_design` against the closed enum."""
    response = model.complete(stage="study_design", messages=build_study_design_messages(bundle))
    raw = response.get("study_design", [])
    if isinstance(raw, str):
        raw = [raw]
    normalized = [v for v in (normalize_enum(x, STUDY_DESIGN_VALUES) for x in raw) if v]
    return tuple(dict.fromkeys(normalized))  # de-dup, preserve order


def extract_study(bundle: EvidenceBundle, resolved: ResolvedIds, *, model: Model) -> StudyFields:
    """S2: assemble the Study-level fields from bundle metadata + one model call."""
    study_design = extract_study_design(bundle, model=model)
    return StudyFields(
        title=bundle.metadata.title,
        journal=bundle.metadata.journal,
        year=bundle.metadata.year,
        authors=bundle.metadata.authors,
        doi=bundle.metadata.doi or resolved.doi,
        study_design=study_design,
    )
