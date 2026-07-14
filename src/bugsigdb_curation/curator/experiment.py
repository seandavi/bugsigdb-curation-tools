"""S4 -- experiment-metadata extraction: per experiment stub -> §1b fields.

One model call per `ExperimentStub` (S3's output), enum-constrained against
the loader's vocabularies wherever the schema defines a closed set
(`sequencing_type`, `statistical_test`); `body_site`/`condition` are emitted
as free-text strings (S7 ontology CURIE mapping is explicitly out of scope
for this skeleton, per the workflow plan §6).
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from bugsigdb_curation.curator.evidence import EvidenceBundle
from bugsigdb_curation.curator.model import Model, build_text_content
from bugsigdb_curation.curator.segment import ExperimentStub
from bugsigdb_curation.loader import SEQUENCING_TYPE_VALUES, STATISTICAL_TEST_VALUES, normalize_enum

_EXPERIMENT_TEXT_CHARS = 8000


@dataclass(frozen=True, slots=True)
class ExperimentFields:
    """S4's output: the §1b Experiment-level fields (minus signatures, S5's job)."""

    host_species: str | None
    body_site: tuple[str, ...]
    condition: tuple[str, ...]
    group_0_name: str | None
    group_1_name: str | None
    sequencing_type: str | None
    statistical_test: tuple[str, ...]
    mht_correction: bool | None


def build_experiment_messages(bundle: EvidenceBundle, stub: ExperimentStub) -> list[dict]:
    allowed_seq = sorted(SEQUENCING_TYPE_VALUES)
    allowed_stats = sorted(STATISTICAL_TEST_VALUES)
    prompt = (
        "You are extracting comparison-level metadata for one specific 2-group comparison "
        "from a microbiome research paper, for BugSigDB curation.\n\n"
        f"The comparison to describe: {stub.description!r}\n\n"
        "Extract these fields from the article text:\n"
        "- host_species: the organism studied (e.g. \"Homo sapiens\", \"Mus musculus\")\n"
        "- body_site: list of anatomical site(s) sampled (free text, e.g. [\"Feces\"])\n"
        "- condition: list of disease/condition label(s) (free text, e.g. [\"Colorectal cancer\"])\n"
        "- group_0_name / group_1_name: short names for the two compared groups\n"
        f"- sequencing_type: EXACTLY one of {allowed_seq}, or null if not stated\n"
        f"- statistical_test: list, each EXACTLY one of {allowed_stats}\n"
        "- mht_correction: true if a multiple-hypothesis-testing correction was applied, "
        "false if explicitly not, or null if not stated\n\n"
        'Return ONLY a JSON object with exactly these keys: {"host_species": ..., "body_site": '
        '[...], "condition": [...], "group_0_name": ..., "group_1_name": ..., "sequencing_type": '
        '..., "statistical_test": [...], "mht_correction": ...}\n\n'
        f"Article text:\n{bundle.full_text()[:_EXPERIMENT_TEXT_CHARS]}"
    )
    return [{"role": "user", "content": [build_text_content(prompt)]}]


def _as_str_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(v) for v in value if v is not None and str(v).strip())


def _as_optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_list(value: object) -> list:
    """Coerce a model-response field meant to be a list into one.

    Mirrors `extract.py`'s `extract_study_design` guard: a model that returns
    a single bare string (e.g. `"LEfSe"`) for a multivalued field must not be
    iterated character-by-character -- wrap it as a one-element list instead.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def extract_experiment(bundle: EvidenceBundle, stub: ExperimentStub, *, model: Model) -> ExperimentFields:
    """S4: one model call filling in one experiment stub's §1b fields."""
    response = model.complete(stage="experiment_metadata", messages=build_experiment_messages(bundle, stub))

    sequencing_type = normalize_enum(response.get("sequencing_type"), SEQUENCING_TYPE_VALUES)
    statistical_test = tuple(
        v for v in (normalize_enum(x, STATISTICAL_TEST_VALUES) for x in _as_list(response.get("statistical_test"))) if v
    )
    host_species = response.get("host_species")
    host_species = str(host_species).strip() if host_species else None

    fields = ExperimentFields(
        host_species=host_species,
        body_site=_as_str_list(response.get("body_site")),
        condition=_as_str_list(response.get("condition")),
        group_0_name=response.get("group_0_name") or None,
        group_1_name=response.get("group_1_name") or None,
        sequencing_type=sequencing_type,
        statistical_test=statistical_test,
        mht_correction=_as_optional_bool(response.get("mht_correction")),
    )
    logger.bind(stage="S4").debug(
        "experiment metadata extracted",
        experiment_index=stub.index,
        host_species=fields.host_species,
        sequencing_type=fields.sequencing_type,
    )
    return fields
