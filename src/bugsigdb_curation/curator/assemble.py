"""S8 -- assemble: deterministic build of the nested-dict prediction record.

Produces exactly the `bugsigdb_curation.loader` nested-dict shape (`Study ->
experiments[] -> signatures[] -> taxa[]`) -- the shape `bugsigdb eval score`
consumes. Deliberately emits **only** slot names the LinkML schema defines
for each class: `bugsigdb_curation.validate`'s `JsonschemaValidationPlugin`
runs with `closed=True`, so an extra key (e.g. a `pmcid`/`has_pmc`
provenance field, which the eval package's `to_nested_dict` happily includes
since it's never schema-validated) would itself be a structural-validation
failure here. Any such provenance the caller wants to keep belongs on
`curator.pipeline.CurationResult`, not inside this dict -- see that module.

`uid` is set to the (string) PMID, matching how gold's `study_id` equals the
PMID for PMID-having studies (`bugsigdb_curation.eval.gold`), and how the
CLI's `_load_predictions` resolves a prediction file to its gold study (by
`study_id` or `uid`) -- so a curator prediction, written as-is, joins to its
gold study without any extra bookkeeping.
"""

from __future__ import annotations

from typing import Any

from bugsigdb_curation.curator.experiment import ExperimentFields
from bugsigdb_curation.curator.extract import StudyFields
from bugsigdb_curation.curator.resolve import ResolvedIds
from bugsigdb_curation.curator.signature import ExtractedSignature


def _set(d: dict[str, Any], key: str, value: Any) -> None:
    """Set `d[key] = value` only when non-None/non-empty (mirrors `loader._set`:
    never emit an invented blank -- an absent field is a cleaner signal than
    a null/empty one for both the schema gate and the scorer)."""
    if value is None:
        return
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return
    d[key] = value


def assemble_taxon(taxon_name: str, ncbi_id: int | None) -> dict[str, Any]:
    d: dict[str, Any] = {}
    _set(d, "taxon_name", taxon_name)
    _set(d, "ncbi_id", ncbi_id)
    return d


def assemble_signature(signature: ExtractedSignature, *, source: str | None) -> dict[str, Any]:
    d: dict[str, Any] = {}
    _set(d, "source", source)
    _set(d, "abundance_in_group_1", signature.direction)
    _set(d, "taxa", [assemble_taxon(t.taxon_name, t.ncbi_id) for t in signature.taxa])
    return d


def assemble_experiment(
    fields: ExperimentFields,
    signatures: list[ExtractedSignature],
    *,
    source: str | None,
) -> dict[str, Any]:
    d: dict[str, Any] = {}
    _set(d, "host_species", fields.host_species)
    _set(d, "body_site", list(fields.body_site))
    _set(d, "condition", list(fields.condition))
    _set(d, "group_0_name", fields.group_0_name)
    _set(d, "group_1_name", fields.group_1_name)
    _set(d, "sequencing_type", fields.sequencing_type)
    _set(d, "statistical_test", list(fields.statistical_test))
    _set(d, "mht_correction", fields.mht_correction)
    _set(d, "signatures", [assemble_signature(sig, source=source) for sig in signatures])
    return d


def assemble_record(
    resolved: ResolvedIds,
    study_fields: StudyFields,
    experiments: list[tuple[ExperimentFields, list[ExtractedSignature], str | None]],
) -> dict[str, Any]:
    """S8: deterministically build the nested Study prediction record.

    `experiments` is a list of `(fields, signatures, source)` triples, one
    per S3 experiment stub -- `source` is the S5a-located artifact's
    provenance string (e.g. `"Table 2"` / `"Figure 1"`), attached to every
    signature under that experiment (mirrors the schema's per-Signature
    `source` slot; a whole experiment shares one located artifact in this
    skeleton -- see `curator.locate`'s thin-heuristic note).
    """
    record: dict[str, Any] = {}
    _set(record, "uid", resolved.pmid)
    pmid_int = int(resolved.pmid) if resolved.pmid.isdigit() else None
    _set(record, "pmid", pmid_int)
    record["citation_mode"] = "Auto" if pmid_int is not None else "Manual"
    _set(record, "doi", study_fields.doi)
    _set(record, "title", study_fields.title)
    _set(record, "authors", list(study_fields.authors))
    _set(record, "journal", study_fields.journal)
    _set(record, "year", study_fields.year)
    _set(record, "study_design", list(study_fields.study_design))
    record["experiments"] = [
        assemble_experiment(fields, signatures, source=source) for fields, signatures, source in experiments
    ]
    return record
