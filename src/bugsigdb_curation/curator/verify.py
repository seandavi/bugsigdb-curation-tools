"""S10 -- adversarial verifier (`split-verify`'s A2 stage, workflow plan §6b).

Runs after S5b-NER + S6-reconcile (`curator.ner` / `curator.reconcile`),
targeting the two known failure modes the figure-extraction benchmark
surfaced: invented/missed taxon labels, and a whole-figure direction flip.
Each check is a **fresh model context** -- it sees only the proposed claim +
the cited source artifact, never the extractor's reasoning:

* **taxon-in-source**: one batched call confirms which proposed taxon names
  actually appear in the cited source; a name the verifier can't confirm is
  dropped (the taxon, not just its id -- an invented name has no id to keep
  either).
* **direction**: for each surviving taxon, independently re-derive
  `abundance_in_group_1` from the source and reconcile it against the
  extractor's claim via `curator.repair`'s bounded loop; on exhaustion the
  taxon is dropped and flagged rather than shipped with a guessed direction.
"""

from __future__ import annotations

from typing import Literal

from loguru import logger

from bugsigdb_curation.curator.artifact_text import artifact_kind_and_text
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import Model, build_image_content, build_text_content
from bugsigdb_curation.curator.repair import DEFAULT_MAX_REPAIR_ROUNDS, resolve_direction_with_repair
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon, dedup_taxa

Direction = Literal["increased", "decreased"]

_DIRECTIONS: tuple[Direction, ...] = ("increased", "decreased")

#: `stage=` for the batched grounding check, and for the per-taxon direction
#: re-derivation call.
IN_SOURCE_STAGE = "verify_taxon_in_source"
DIRECTION_STAGE = "verify_direction"


def build_in_source_messages(
    taxon_names: list[str], source_text: str, *, image_bytes: bytes | None = None
) -> list[dict]:
    """One batched grounding-check prompt: does each candidate name actually
    appear in the source (never "is this a real organism")."""
    names_list = ", ".join(repr(n) for n in taxon_names)
    prompt = (
        "You are verifying a list of candidate microbial taxon names against the SOURCE "
        "below (figure legend text and, when provided, the figure image). For each name, "
        "confirm whether it is literally present in this source -- a taxon visible in the "
        "figure image counts as in-source. Do not use outside biological knowledge; a real "
        "organism that is NOT present in this source must still be marked not-in-source.\n\n"
        f"Candidate names: [{names_list}]\n\n"
        f"Source:\n{source_text}\n\n"
        'Return ONLY a JSON object: {"results": [{"name": "<name>", "in_source": true|false}, ...]}'
    )
    content: list[dict] = [build_text_content(prompt)]
    if image_bytes is not None:
        content.append(build_image_content(image_bytes))
    return [{"role": "user", "content": content}]


def check_taxa_in_source(
    taxon_names: list[str],
    source_text: str,
    *,
    model: Model,
    stage: str = IN_SOURCE_STAGE,
    image_bytes: bytes | None = None,
) -> set[str]:
    """Return the subset of `taxon_names` (exact original casing) the
    verifier confirms are literally present in `source_text` (and, for a
    figure artifact, `image_bytes` -- see module docstring)."""
    if not taxon_names:
        return set()
    response = model.complete(
        stage=stage, messages=build_in_source_messages(taxon_names, source_text, image_bytes=image_bytes)
    )
    raw = response.get("results", []) or []
    grounded_norm: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name and item.get("in_source") is True:
            grounded_norm.add(str(name).strip().lower())
    return {name for name in taxon_names if name.strip().lower() in grounded_norm}


def verify_signatures(
    signatures: list[ExtractedSignature],
    *,
    artifact: LocatedArtifact,
    model: Model,
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
    image_bytes: bytes | None = None,
) -> tuple[list[ExtractedSignature], tuple[str, ...]]:
    """S10 (split-verify): taxon-in-source grounding + direction re-derivation.

    `image_bytes` (a figure artifact's decoded image, or None for a table)
    is forwarded to both the grounding check and the direction re-derivation
    repair loop -- see module docstring: figure-sourced taxa are extracted
    from the image via vision, so grounding/direction checks that see only
    the legend text can never confirm most of them.

    Returns `(verified_signatures, flags)` -- `flags` are provenance-only
    strings (never written into the schema record itself, see
    `curator.pipeline.CurationResult.flags`) describing anything dropped or
    left unresolved.
    """
    all_taxa: list[tuple[ExtractedTaxon, Direction]] = [
        (taxon, sig.direction) for sig in signatures for taxon in sig.taxa
    ]
    if not all_taxa:
        return [], ()

    _, source_text = artifact_kind_and_text(artifact)
    grounded_names = check_taxa_in_source(
        [taxon.taxon_name for taxon, _ in all_taxa], source_text, model=model, image_bytes=image_bytes
    )

    flags: list[str] = []
    n_dropped_not_in_source = 0
    n_direction_repaired = 0
    n_direction_unresolved = 0
    by_direction: dict[Direction, list[ExtractedTaxon]] = {d: [] for d in _DIRECTIONS}

    for taxon, claimed_direction in all_taxa:
        if taxon.taxon_name not in grounded_names:
            n_dropped_not_in_source += 1
            flags.append(f"verifier dropped {taxon.taxon_name!r}: not confirmed in source")
            continue

        final_direction, changed = resolve_direction_with_repair(
            taxon.taxon_name,
            claimed_direction,
            source_text,
            model=model,
            stage=DIRECTION_STAGE,
            max_rounds=max_repair_rounds,
            image_bytes=image_bytes,
        )
        if final_direction is None:
            n_direction_unresolved += 1
            flags.append(
                f"verifier: direction unresolved for {taxon.taxon_name!r} after {max_repair_rounds} repair round(s)"
            )
            continue
        if changed:
            n_direction_repaired += 1
        by_direction[final_direction].append(
            ExtractedTaxon(taxon_name=taxon.taxon_name, direction=final_direction, ncbi_id=taxon.ncbi_id)
        )

    signatures_out = [
        ExtractedSignature(direction=direction, taxa=tuple(dedup_taxa(taxa)))
        for direction, taxa in by_direction.items()
        if taxa
    ]
    logger.bind(stage="S10-verify").info(
        "verified",
        n_dropped_not_in_source=n_dropped_not_in_source,
        n_direction_repaired=n_direction_repaired,
        n_direction_unresolved=n_direction_unresolved,
    )
    return signatures_out, tuple(flags)
