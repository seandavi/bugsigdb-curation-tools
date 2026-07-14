"""S6-reconcile -- deterministic name -> taxid resolution for split A1.

`curator.ner`'s S5b-NER emits **names only** (no id proposal); this module
resolves each name to an NCBI taxid through the already-wired
`NcbiTaxonomyResolver` (cache -> local `TaxonomyDB` -> live E-utilities
gap-fill, same authority `curator.taxonomy`/`curator.signature`'s S6
verification gate uses) -- but here the id comes ONLY from the authority,
never from a model proposal, which is the whole point of the A1-split axis
(workflow plan §6a/§6b): "an NER agent emits name strings + direction; a
**deterministic cached NCBI reconcile** maps name->taxid".

A name whose local `TaxonomyDB` resolution is **ambiguous** (a true
homonym -- several distinct tax_ids match, see `TaxonomyDB.resolve`'s
ambiguity policy) is never silently picked either way: `_disambiguate` runs
one fresh model call constrained to choose among the candidate tax_ids using
the source context (lineage/body-site/host cues the pipeline assembles from
S4's experiment metadata + the located artifact), or return null. A chosen
id outside the candidate set is rejected -- "never guess" applies to
disambiguation exactly as it does to every other id decision in this
package. A name with no hit anywhere (local DB miss AND live gap-fill miss)
is kept, unresolved (`ncbi_id=None`) -- never dropped.
"""

from __future__ import annotations

from typing import Literal

import httpx
from loguru import logger

from bugsigdb_curation.curator.model import Model, build_text_content
from bugsigdb_curation.curator.ner import NamedTaxon
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon, dedup_taxa
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver
from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name

Direction = Literal["increased", "decreased"]

_DIRECTIONS: tuple[Direction, ...] = ("increased", "decreased")

#: `stage=` for the disambiguation model call.
DISAMBIGUATION_STAGE = "taxon_disambiguate"


def build_disambiguation_messages(
    name: str, candidates: tuple[int, ...], *, resolver: NcbiTaxonomyResolver, source_context: str
) -> list[dict]:
    """Build the disambiguation prompt: the ambiguous name + each candidate's
    scientific name/lineage (from the local `TaxonomyDB`, when available) +
    the pipeline's source context, asking the model to pick one candidate
    tax_id or null."""
    lines = []
    for candidate_id in candidates:
        sci_name = candidate_id
        lineage_str = ""
        if resolver.db is not None:
            sci_name = resolver.db.scientific_name(candidate_id) or candidate_id
            lineage = resolver.db.lineage(candidate_id)
            lineage_str = " > ".join(n for _, _, n in lineage if n)
        lines.append(f"- tax_id {candidate_id}: {sci_name}" + (f" (lineage: {lineage_str})" if lineage_str else ""))

    prompt = (
        f"The taxon name {name!r} is AMBIGUOUS in the NCBI Taxonomy database -- it matches "
        "more than one distinct taxon (a homonym). The candidates are:\n" + "\n".join(lines) + "\n\n"
        f"Source context from the paper (lineage/body-site/host cues):\n{source_context}\n\n"
        "Pick the tax_id that best matches this source context. If you cannot confidently "
        "choose between the candidates, return null rather than guessing.\n\n"
        'Return ONLY a JSON object: {"chosen_tax_id": <int, must be one of the candidates '
        "above, or null>}"
    )
    return [{"role": "user", "content": [build_text_content(prompt)]}]


def _disambiguate(
    name: str, candidates: tuple[int, ...], source_context: str, *, model: Model, resolver: NcbiTaxonomyResolver
) -> int | None:
    """One model call, constrained to `candidates` or `None` -- never a guess outside the set."""
    response = model.complete(
        stage=DISAMBIGUATION_STAGE,
        messages=build_disambiguation_messages(name, candidates, resolver=resolver, source_context=source_context),
    )
    chosen = response.get("chosen_tax_id")
    if chosen is None:
        return None
    try:
        chosen_id = int(chosen)
    except (TypeError, ValueError):
        return None
    if chosen_id not in candidates:
        return None
    return chosen_id


async def resolve_one_name(
    name: str,
    *,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    client: httpx.AsyncClient,
    source_context: str,
) -> tuple[int | None, bool]:
    """Resolve one bare name to a taxid: cache -> local `TaxonomyDB` (with
    disambiguation on an ambiguous hit) -> live gap-fill.

    Returns `(tax_id_or_None, was_disambiguated)`. Exposed (not `_`-private)
    because `curator.panel` reuses it for a reviewer-only recall taxon (a
    name the extractor's NER never saw, so it never went through
    `reconcile_names`'s loop at all).

    Only a genuinely context-*independent* resolution -- an unambiguous
    local-DB hit, a local-DB miss that falls through to live gap-fill, or no
    DB at all -- ever gets written into the shared `resolver.cache`/
    `.unresolved`. An ambiguous hit's disambiguation depends on
    `source_context` (a per-call argument, not a property of the name
    itself), so that outcome is intentionally local to this call and never
    persisted onto the shared resolver -- see the comment at the ambiguous
    branch below.
    """
    norm = normalize_taxon_name(name)
    if norm in resolver.cache:
        return resolver.cache[norm], False

    if resolver.db is not None:
        resolution = resolver.db.resolve(name)
        if resolution is None:
            # Local DB has no hit -- fall through to live gap-fill, exactly
            # like `NcbiTaxonomyResolver.resolve_name` does for fused-lean.
            tax_id = await resolver.resolve_name(name, client=client)
            return tax_id, False
        if resolution.ambiguous:
            # CURATOR: This disambiguation is decided from `source_context`
            # (per-experiment host/body-site/lineage cues), so it is NOT the
            # same decision for every caller of this name -- a different
            # experiment/study sharing this resolver may have different
            # context and legitimately get a different chosen tax_id (or
            # decline). Never write the outcome into `resolver.cache`/
            # `.unresolved`: those are shared/persisted across the whole
            # batch run, and caching a context-dependent choice there would
            # let the FIRST caller's context silently decide this homonym
            # for every later caller (and leak onto disk via `save_cache`).
            # A declined choice (`chosen is None`) is likewise not a genuine
            # "no hit anywhere" -- it's "undecidable in this context" -- so
            # it must not land in `.unresolved` either (see that field's
            # docstring in `curator.taxonomy`).
            chosen = _disambiguate(name, resolution.candidates, source_context, model=model, resolver=resolver)
            return chosen, True
        resolver.cache[norm] = resolution.tax_id
        resolver.unresolved.discard(norm)
        return resolution.tax_id, False

    tax_id = await resolver.resolve_name(name, client=client)
    return tax_id, False


async def reconcile_names(
    names: list[NamedTaxon],
    *,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    client: httpx.AsyncClient,
    source_context: str,
) -> list[ExtractedSignature]:
    """S6-reconcile: resolve every S5b-NER name to a taxid, group into `<=2`
    direction-keyed `ExtractedSignature`s -- the same output shape
    fused-lean's `extract_signatures` produces, so both A1 branches feed the
    same S8 assemble step."""
    by_direction: dict[Direction, list[ExtractedTaxon]] = {d: [] for d in _DIRECTIONS}
    n_disambiguated = 0

    for entry in names:
        tax_id, disambiguated = await resolve_one_name(
            entry.name, model=model, resolver=resolver, client=client, source_context=source_context
        )
        if disambiguated:
            n_disambiguated += 1
        by_direction[entry.direction].append(
            ExtractedTaxon(taxon_name=entry.name, direction=entry.direction, ncbi_id=tax_id)
        )

    signatures = [
        ExtractedSignature(direction=direction, taxa=tuple(dedup_taxa(taxa)))
        for direction, taxa in by_direction.items()
        if taxa
    ]
    n_taxa = sum(len(sig.taxa) for sig in signatures)
    n_resolved = sum(1 for sig in signatures for taxon in sig.taxa if taxon.ncbi_id is not None)
    logger.bind(stage="S6-reconcile").info(
        "names reconciled",
        n_taxa=n_taxa,
        n_resolved=n_resolved,
        n_unresolved=n_taxa - n_resolved,
        n_disambiguated=n_disambiguated,
    )
    return signatures
