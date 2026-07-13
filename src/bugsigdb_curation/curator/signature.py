"""S5b -- fused extract: Design-1's defining stage.

**One** model call reads the artifact S5a located (a table's text, OR a
figure's image + legend) and emits, per taxon: a name string, a direction
(`abundance_in_group_1`), and a *proposed* `ncbi_id` -- extraction and ID
proposal fused into a single call, per Design-1 ("Fused-Lean") in the
workflow plan §6b.

The proposed id is never trusted on its own: every proposal is passed
through S6 (`curator.taxonomy.NcbiTaxonomyResolver.verify_id`), which
independently re-resolves the *name* against the live NCBI authority and
only keeps the id if that resolution agrees. A taxon whose id can't be
verified this way is still kept (by name), just with `ncbi_id=None` --
"never guess" (plan §3): S6 is verification-only, never generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import Model, build_image_content, build_text_content
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver

Direction = Literal["increased", "decreased"]

_DIRECTIONS: tuple[Direction, ...] = ("increased", "decreased")


@dataclass(frozen=True, slots=True)
class ExtractedTaxon:
    """One taxon proposed by S5b. `ncbi_id` is set iff S6 verified it."""

    taxon_name: str
    direction: Direction
    ncbi_id: int | None


@dataclass(frozen=True, slots=True)
class ExtractedSignature:
    """One direction-grouped signature (<=2 per experiment, per the schema)."""

    direction: Direction
    taxa: tuple[ExtractedTaxon, ...]


_PROMPT_TEMPLATE = (
    "You are extracting a differential-abundance microbial signature from a microbiome "
    "research paper's {artifact_kind}, for BugSigDB curation.\n\n"
    "For every taxon reported as significantly different between the two compared groups, "
    "report: its name (genus/species, as written), whether it is INCREASED or DECREASED in "
    "Group 1 relative to Group 0, and your best-guess NCBI Taxonomy id for that name. Your "
    "proposed id will be independently verified against the NCBI Taxonomy database and "
    "DISCARDED if it doesn't match -- so it is fine, and preferred, to omit proposed_ncbi_id "
    "(return null) rather than guess if you are not confident.\n\n"
    'Return ONLY a JSON object: {{"taxa": [{{"name": "<taxon name>", "direction": '
    '"increased"|"decreased", "proposed_ncbi_id": <int or null>}}, ...]}}\n\n'
    "{artifact_content}"
)


def build_signature_messages(artifact: LocatedArtifact, *, image_bytes: bytes | None = None) -> list[dict]:
    """Build S5b's fused-extract prompt: table text, or figure legend + image."""
    if artifact.kind == "table" and artifact.table is not None:
        artifact_kind = "table"
        artifact_content = f"Table ({artifact.table.provenance}):\n{artifact.table.as_text()}"
    elif artifact.kind == "figure" and artifact.figure is not None:
        artifact_kind = "figure"
        artifact_content = f"Figure legend ({artifact.figure.provenance}):\n{artifact.figure.legend}"
    else:
        raise ValueError(f"LocatedArtifact of kind {artifact.kind!r} is missing its payload")

    text = _PROMPT_TEMPLATE.format(artifact_kind=artifact_kind, artifact_content=artifact_content)
    content: list[dict] = [build_text_content(text)]
    if image_bytes is not None:
        content.append(build_image_content(image_bytes))
    return [{"role": "user", "content": content}]


async def extract_signatures(
    artifact: LocatedArtifact,
    *,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    client: httpx.AsyncClient,
    image_bytes: bytes | None = None,
) -> list[ExtractedSignature]:
    """S5b (fused extract) + S6 (verify): one model call, per-taxon id verification.

    Groups the model's flat taxon list by direction into <=2
    `ExtractedSignature`s (schema shape: one signature per direction).
    """
    messages = build_signature_messages(artifact, image_bytes=image_bytes)
    response = model.complete(stage="signature_extract", messages=messages)
    raw_taxa = response.get("taxa", []) or []

    by_direction: dict[Direction, list[ExtractedTaxon]] = {d: [] for d in _DIRECTIONS}
    for item in raw_taxa:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        raw_direction = item.get("direction")
        direction = str(raw_direction).strip().lower() if raw_direction is not None else None
        if not name or direction not in _DIRECTIONS:
            continue

        ncbi_id: int | None = None
        proposed = item.get("proposed_ncbi_id")
        if proposed is not None:
            verified = await resolver.verify_id(str(name), proposed, client=client)
            if verified:
                ncbi_id = int(proposed)

        by_direction[direction].append(
            ExtractedTaxon(taxon_name=str(name), direction=direction, ncbi_id=ncbi_id)
        )

    return [
        ExtractedSignature(direction=direction, taxa=tuple(taxa))
        for direction, taxa in by_direction.items()
        if taxa
    ]
