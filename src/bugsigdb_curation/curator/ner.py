"""S5b-NER -- split A1's name-only extraction (`split-verify`/`split-panel`).

One model call reads the artifact S5a located and emits, per taxon, ONLY a
name string and a direction (`abundance_in_group_1`) -- **no id proposal**,
unlike fused-lean's `curator.signature.extract_signatures`, which fuses
extraction and id-proposal into one call. Ids for a split-design taxon come
only from `curator.reconcile`'s deterministic `TaxonomyDB` authority lookup
-- the whole point of the A1-split axis (workflow plan §6a/§6b): a stronger
separation between "what taxon is this" (model judgment) and "what id does
it have" (authority lookup, never a model guess).

Also used, with a different `stage`, by `curator.panel`'s independent
reviewer (`stage="review_signature"`): the reviewer re-derives the same
name+direction shape in fresh context, from the same artifact, so this one
function serves both S5b-NER and split-panel's S10 reviewer call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from loguru import logger

from bugsigdb_curation.curator.artifact_text import artifact_kind_and_text
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import Model, build_image_content, build_text_content

Direction = Literal["increased", "decreased"]

_DIRECTIONS: tuple[Direction, ...] = ("increased", "decreased")

#: The default `stage=` this module's `extract_names` uses -- split A1's
#: NER call. `curator.panel` passes `stage="review_signature"` for the same
#: prompt shape, framed as an independent re-derivation.
DEFAULT_NER_STAGE = "signature_ner"


@dataclass(frozen=True, slots=True)
class NamedTaxon:
    """One taxon name+direction this module extracted -- not yet resolved to an id."""

    name: str
    direction: Direction


_PROMPT_TEMPLATE = (
    "You are extracting a differential-abundance microbial signature from a microbiome "
    "research paper's {artifact_kind}, for BugSigDB curation.\n\n"
    "For every taxon reported as significantly different between the two compared groups, "
    "report ONLY its name (genus/species, as written) and whether it is INCREASED or "
    "DECREASED in Group 1 relative to Group 0. Do NOT propose an NCBI Taxonomy id here -- "
    "identifiers are resolved separately against the authoritative NCBI Taxonomy database, "
    "never guessed by you.\n\n"
    'Return ONLY a JSON object: {{"taxa": [{{"name": "<taxon name>", "direction": '
    '"increased"|"decreased"}}, ...]}}\n\n'
    "{artifact_content}"
)


def build_ner_messages(artifact: LocatedArtifact, *, image_bytes: bytes | None = None) -> list[dict]:
    """Build S5b-NER's names-only prompt: table text, or figure legend + image."""
    artifact_kind, artifact_content = artifact_kind_and_text(artifact)
    text = _PROMPT_TEMPLATE.format(artifact_kind=artifact_kind, artifact_content=artifact_content)
    content: list[dict] = [build_text_content(text)]
    if image_bytes is not None:
        content.append(build_image_content(image_bytes))
    return [{"role": "user", "content": content}]


def extract_names(
    artifact: LocatedArtifact,
    *,
    model: Model,
    image_bytes: bytes | None = None,
    stage: str = DEFAULT_NER_STAGE,
) -> list[NamedTaxon]:
    """S5b-NER: one model call, names + direction only (no id).

    `stage` defaults to the split A1 NER call's own stage name; `curator.
    panel` overrides it (`"review_signature"`) to reuse this exact prompt
    shape for the independent reviewer's fresh-context re-derivation.
    """
    messages = build_ner_messages(artifact, image_bytes=image_bytes)
    response = model.complete(stage=stage, messages=messages)
    raw_taxa = response.get("taxa", []) or []

    names: list[NamedTaxon] = []
    for item in raw_taxa:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        raw_direction = item.get("direction")
        direction = str(raw_direction).strip().lower() if raw_direction is not None else None
        if not name or direction not in _DIRECTIONS:
            continue
        names.append(NamedTaxon(name=str(name), direction=direction))

    logger.bind(stage="S5b-NER").info("names extracted", model_stage=stage, n_names=len(names))
    return names
