"""S10 -- independent reviewer panel (`split-panel`'s A2 stage, workflow plan §6b).

An independent reviewer re-derives the WHOLE signature (taxa + direction,
names only -- ids stay authority-only, same as every split-A1 taxon) from
the source in fresh context, via `curator.ner.extract_names` with
`stage="review_signature"` -- today the same model as the extractor (the
plan's mixed-model axis is a later model-sweep concern, §6c Phase B), but
architecturally this is the seam where a stronger/different reviewer model
plugs in.

**Arbitration** against the already-reconciled extractor signatures:

* a taxon both sides agree on (same name, same direction) is accepted as-is;
* a taxon both sides name but disagree on direction goes through
  `curator.repair`'s bounded reconciliation loop (same mechanism as
  split-verify's direction check, different `stage=`); unresolved -> flagged
  and dropped;
* an **extractor-only** taxon (the reviewer didn't mention it) is kept only
  if it re-grounds against the source (reuses `curator.verify`'s
  taxon-in-source check) -- otherwise dropped and flagged;
* a **reviewer-only** taxon (the recall path -- the extractor missed it) is
  kept if it grounds, with its id resolved via `curator.reconcile`'s
  authority lookup (it never went through S6-reconcile's main loop, since
  S5b-NER never proposed it).

Every repair/grounding check is capped at `max_repair_rounds`, so a
persistent disagreement can never loop forever.
"""

from __future__ import annotations

from typing import Literal

import httpx
from loguru import logger

from bugsigdb_curation.curator.artifact_text import artifact_kind_and_text
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.model import Model
from bugsigdb_curation.curator.ner import extract_names
from bugsigdb_curation.curator.reconcile import resolve_one_name
from bugsigdb_curation.curator.repair import DEFAULT_MAX_REPAIR_ROUNDS, resolve_direction_with_repair
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon, dedup_taxa
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver
from bugsigdb_curation.curator.verify import check_taxa_in_source
from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name

Direction = Literal["increased", "decreased"]

_DIRECTIONS: tuple[Direction, ...] = ("increased", "decreased")

#: `stage=` for the reviewer's fresh-context re-derivation (reuses
#: `curator.ner`'s prompt shape), the reconciliation-repair call, and the
#: batched grounding check for taxa only one side named.
REVIEW_SIGNATURE_STAGE = "review_signature"
RECONCILE_DIRECTION_STAGE = "review_reconcile_direction"
GROUND_CHECK_STAGE = "review_ground_check"


async def review_signatures(
    signatures: list[ExtractedSignature],
    *,
    artifact: LocatedArtifact,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    client: httpx.AsyncClient,
    source_context: str,
    max_repair_rounds: int = DEFAULT_MAX_REPAIR_ROUNDS,
) -> tuple[list[ExtractedSignature], tuple[str, ...]]:
    """S10 (split-panel): independent reviewer + arbitration + recall path.

    Returns `(reviewed_signatures, flags)` -- see `curator.verify.
    verify_signatures` for the same `flags` contract (provenance-only, never
    written into the schema record).
    """
    _, source_text = artifact_kind_and_text(artifact)
    reviewer_names = extract_names(artifact, model=model, stage=REVIEW_SIGNATURE_STAGE)

    # CURATOR: keyed by normalized name -> a *list* of (entry, direction)
    # pairs, not a single entry. A taxon can legitimately appear in BOTH
    # direction-signatures (schema-legal -- `signature.dedup_taxa` only
    # dedupes within one direction, never across `increased`/`decreased`),
    # so a plain `dict[name] = (entry, direction)` would silently overwrite
    # the first direction's entry with the second's before arbitration ever
    # ran. Grouping by name (not `(name, direction)`) is still needed so the
    # agree/disagree logic below can match an extractor mention against a
    # reviewer mention of the same taxon regardless of which direction each
    # side used.
    extractor_by_norm: dict[str, list[tuple[ExtractedTaxon, Direction]]] = {}
    for sig in signatures:
        for taxon in sig.taxa:
            extractor_by_norm.setdefault(normalize_taxon_name(taxon.taxon_name), []).append((taxon, sig.direction))

    reviewer_by_norm: dict[str, list[tuple[str, Direction]]] = {}
    for named in reviewer_names:
        reviewer_by_norm.setdefault(normalize_taxon_name(named.name), []).append((named.name, named.direction))

    extractor_only = set(extractor_by_norm) - set(reviewer_by_norm)
    both = set(extractor_by_norm) & set(reviewer_by_norm)
    reviewer_only = set(reviewer_by_norm) - set(extractor_by_norm)

    # One batched grounding check covers every taxon only one side named --
    # an extractor-only taxon the reviewer silently omitted, and a
    # reviewer-only taxon (the recall path) that needs confirming before
    # it's trusted at all. One representative name per normalized taxon is
    # enough here (grounding is a name-in-source question, not a
    # per-direction one), even when that taxon has more than one direction
    # entry below.
    one_sided_names = [extractor_by_norm[n][0][0].taxon_name for n in extractor_only] + [
        reviewer_by_norm[n][0][0] for n in reviewer_only
    ]
    grounded = check_taxa_in_source(one_sided_names, source_text, model=model, stage=GROUND_CHECK_STAGE)

    flags: list[str] = []
    n_reconciled = 0
    n_reviewer_added = 0
    n_dropped = 0
    by_direction: dict[Direction, list[ExtractedTaxon]] = {d: [] for d in _DIRECTIONS}

    for norm_name in extractor_only:
        for taxon, direction in extractor_by_norm[norm_name]:
            if taxon.taxon_name in grounded:
                by_direction[direction].append(taxon)
            else:
                n_dropped += 1
                flags.append(f"panel dropped {taxon.taxon_name!r}: reviewer omitted it and it did not re-ground")

    for norm_name in both:
        # A name both sides mentioned may still carry more than one
        # extractor-side direction entry (the both-directions case this fix
        # addresses); each is arbitrated independently against the set of
        # directions the reviewer used for this same name.
        reviewer_directions = {direction for _, direction in reviewer_by_norm[norm_name]}
        for taxon, extractor_direction in extractor_by_norm[norm_name]:
            if extractor_direction in reviewer_directions:
                by_direction[extractor_direction].append(taxon)
                continue

            final_direction, changed = resolve_direction_with_repair(
                taxon.taxon_name,
                extractor_direction,
                source_text,
                model=model,
                stage=RECONCILE_DIRECTION_STAGE,
                max_rounds=max_repair_rounds,
            )
            if final_direction is None:
                n_dropped += 1
                flags.append(
                    f"panel: direction unresolved for {taxon.taxon_name!r} after {max_repair_rounds} repair round(s)"
                )
                continue
            if changed:
                n_reconciled += 1
            by_direction[final_direction].append(
                ExtractedTaxon(taxon_name=taxon.taxon_name, direction=final_direction, ncbi_id=taxon.ncbi_id)
            )

    for norm_name in reviewer_only:
        for name, direction in reviewer_by_norm[norm_name]:
            if name not in grounded:
                continue
            tax_id, _ = await resolve_one_name(
                name, model=model, resolver=resolver, client=client, source_context=source_context
            )
            by_direction[direction].append(ExtractedTaxon(taxon_name=name, direction=direction, ncbi_id=tax_id))
            n_reviewer_added += 1

    signatures_out = [
        ExtractedSignature(direction=direction, taxa=tuple(dedup_taxa(taxa)))
        for direction, taxa in by_direction.items()
        if taxa
    ]
    logger.bind(stage="S10-panel").info(
        "reviewed", n_reconciled=n_reconciled, n_reviewer_added=n_reviewer_added, n_dropped=n_dropped
    )
    return signatures_out, tuple(flags)
