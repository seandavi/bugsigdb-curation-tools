"""Gold join: relational CSV exports + the PMC-map -> nested gold study tree.

Joins `data/exports/relational/{studies,experiments,signatures,signatures_taxa}.csv`
(see `bugsigdb split`) with `data/eval/pmid_pmcid_map.csv` (see `bugsigdb pmc-map`)
into a `study_id -> GoldStudy` dict, mirroring the `Study -> experiments[] ->
signatures[] -> taxa[]` nesting of `bugsigdb_curation.loader.load_studies` so
gold and predictions share one shape (see `to_nested_dict`).

Join keys, verified against the real export (see module docstring of the
originating task brief for the full contract):

* `studies.study_id` is the primary key everywhere. It equals the PMID
  (numeric string) for studies that have one (2052/2068); for the 12 studies
  without a PMID it is the wiki page name (``"Study N"``, blank `pmid`).
* `experiments.experiment_id` is ``"<study_id>/Experiment N"``; joins to
  `experiments.study_id`.
* `signatures.experiment_id` joins `experiments.experiment_id` directly (the
  signature_id's own embedded wiki-page number is NOT the study_id -- always
  chain through experiment_id, never parse it out of signature_id).
* `signatures_taxa.signature_id` joins `signatures.signature_id`; one row per
  gold taxon (`ncbi_id`).
* `pmid_pmcid_map.study_id` is produced by `bugsigdb pmc-map` directly from
  `studies.study_id` (rows without a `pmid` are skipped entirely by pmc-map),
  so joining on `study_id` is exact and needs no PMID fallback.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bugsigdb_curation.loader import coerce_bool, coerce_int, is_blank, split_comma_strict

SourceType = Literal["main-table", "figure", "supplement", "other"]

# Priority order (most-specific first) matters for sources that mention more
# than one artifact kind, e.g. "Table S5" or "Supporting Info. Table S5 +
# Fig. S2": a supplement marker (`suppl`/`S<digit>`) wins over a bare
# "table"/"figure" mention, because a *supplementary* table/figure is not the
# main-text table/figure the "main-table"/"figure" buckets are meant to
# capture; "figure" in turn wins over a co-occurring bare "table" mention
# (e.g. "Table 2, Figure 6") since there is no principled way to prefer one,
# and figures are the harder/rarer-recall channel worth flagging distinctly.
_SUPPLEMENT_RE = re.compile(r"suppl|\bS\d", re.IGNORECASE)
_FIGURE_RE = re.compile(r"\bfig(?:ure)?\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"\btable\b", re.IGNORECASE)


def source_type(source: str | None) -> SourceType:
    """Classify a signature's free-text `source` cell for stratified scoring.

    See the module-level comment above `_SUPPLEMENT_RE` for the priority
    rationale when a source string mentions more than one artifact kind.
    """
    if source is None or not source.strip():
        return "other"
    s = source.strip()
    if _SUPPLEMENT_RE.search(s):
        return "supplement"
    if _FIGURE_RE.search(s):
        return "figure"
    if _TABLE_RE.search(s):
        return "main-table"
    return "other"


@dataclass(frozen=True, slots=True)
class GoldSignature:
    """One gold curated signature: a taxon set for one direction of one experiment."""

    signature_id: str
    experiment_id: str
    source: str | None
    source_type: SourceType
    direction: Literal["increased", "decreased"] | None
    taxa: frozenset[int]
    #: "Complete" / "Incomplete", or `None` if the `State` cell is blank --
    #: which is how "not complete" is actually represented in the real
    #: export (the literal string "Incomplete" never occurs; see
    #: `score.py`'s module docstring for the corpus counts). Used as the
    #: best-available proxy for the plan's "known-bad gold" discount (§4d):
    #: the relational export carries no per-taxon "missing ncbi_id" flag
    #: (rows without a resolvable ncbi_id simply have no
    #: `signatures_taxa.csv` row at all), so a blank/Incomplete signature is
    #: the closest available signal that its gold taxon set may itself be
    #: short -- see `score.py`'s `_is_known_bad_gold` / `discount_incomplete`.
    curation_state: str | None


@dataclass(frozen=True, slots=True)
class GoldExperiment:
    """One gold curated experiment (a single 2-group comparison)."""

    experiment_id: str
    study_id: str
    experiment_name: str | None
    location_of_subjects: tuple[str, ...]
    host_species: str | None
    body_site: tuple[str, ...]
    uberon_id: str | None
    condition: tuple[str, ...]
    efo_id: str | None
    group_0_name: str | None
    group_1_name: str | None
    group_1_definition: str | None
    group_0_sample_size: int | None
    group_1_sample_size: int | None
    sequencing_type: str | None
    statistical_test: tuple[str, ...]
    mht_correction: bool | None
    signatures: tuple[GoldSignature, ...]


@dataclass(frozen=True, slots=True)
class GoldStudy:
    """One gold curated study, with its experiments/signatures/taxa nested in."""

    study_id: str
    pmid: str | None
    doi: str | None
    title: str | None
    journal: str | None
    year: int | None
    study_design: tuple[str, ...]
    pmcid: str | None
    #: True/False when `study_id` is present in the pmc-map (it is skipped
    #: there for the ~12 PMID-less studies), else None ("unknown"/not queried).
    has_pmc: bool | None
    experiments: tuple[GoldExperiment, ...]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _clean(row: dict[str, str], key: str) -> str | None:
    value = row.get(key)
    return None if is_blank(value) else value.strip()  # type: ignore[union-attr]


def load_gold(relational_dir: Path, pmc_map_csv: Path) -> dict[str, GoldStudy]:
    """Join the relational gold CSVs into `study_id -> GoldStudy`.

    `relational_dir` must contain `studies.csv`, `experiments.csv`,
    `signatures.csv`, and `signatures_taxa.csv` (as written by `bugsigdb
    split`). `pmc_map_csv` is the `bugsigdb pmc-map` output
    (`study_id,pmid,pmcid,doi,has_pmc`); if it doesn't exist, every study's
    `pmcid`/`has_pmc` come back as `None` rather than raising (handy for
    scoring against a relational export before pmc-map has run).
    """
    relational_dir = Path(relational_dir)
    studies_rows = _read_csv(relational_dir / "studies.csv")
    experiments_rows = _read_csv(relational_dir / "experiments.csv")
    signatures_rows = _read_csv(relational_dir / "signatures.csv")
    taxa_rows = _read_csv(relational_dir / "signatures_taxa.csv")

    pmc_map_csv = Path(pmc_map_csv)
    pmc_by_study: dict[str, dict[str, str]] = {}
    if pmc_map_csv.exists():
        pmc_by_study = {row["study_id"]: row for row in _read_csv(pmc_map_csv)}

    taxa_by_sig: dict[str, set[int]] = {}
    for row in taxa_rows:
        ncbi_id = coerce_int(row.get("ncbi_id"))
        if ncbi_id is None:
            continue
        taxa_by_sig.setdefault(row["signature_id"], set()).add(ncbi_id)

    sigs_by_exp: dict[str, list[GoldSignature]] = {}
    for row in signatures_rows:
        sig_id = row["signature_id"]
        exp_id = row["experiment_id"]
        source = _clean(row, "source")
        direction_raw = _clean(row, "abundance_in_group_1")
        direction: Literal["increased", "decreased"] | None = (
            direction_raw if direction_raw in ("increased", "decreased") else None  # type: ignore[assignment]
        )
        signature = GoldSignature(
            signature_id=sig_id,
            experiment_id=exp_id,
            source=source,
            source_type=source_type(source),
            direction=direction,
            taxa=frozenset(taxa_by_sig.get(sig_id, ())),
            curation_state=_clean(row, "curation_state"),
        )
        sigs_by_exp.setdefault(exp_id, []).append(signature)

    exps_by_study: dict[str, list[GoldExperiment]] = {}
    for row in experiments_rows:
        study_id = row["study_id"]
        exp_id = row["experiment_id"]
        experiment = GoldExperiment(
            experiment_id=exp_id,
            study_id=study_id,
            experiment_name=_clean(row, "experiment_name"),
            location_of_subjects=tuple(split_comma_strict(row.get("location_of_subjects"))),
            host_species=_clean(row, "host_species"),
            body_site=tuple(split_comma_strict(row.get("body_site"))),
            uberon_id=_clean(row, "uberon_id"),
            condition=tuple(split_comma_strict(row.get("condition"))),
            efo_id=_clean(row, "efo_id"),
            group_0_name=_clean(row, "group_0_name"),
            group_1_name=_clean(row, "group_1_name"),
            group_1_definition=_clean(row, "group_1_definition"),
            group_0_sample_size=coerce_int(row.get("group_0_sample_size")),
            group_1_sample_size=coerce_int(row.get("group_1_sample_size")),
            sequencing_type=_clean(row, "sequencing_type"),
            statistical_test=tuple(split_comma_strict(row.get("statistical_test"))),
            mht_correction=coerce_bool(row.get("mht_correction")),
            signatures=tuple(sigs_by_exp.get(exp_id, ())),
        )
        exps_by_study.setdefault(study_id, []).append(experiment)

    studies: dict[str, GoldStudy] = {}
    for row in studies_rows:
        study_id = row["study_id"]
        pmc_row = pmc_by_study.get(study_id)
        pmcid = _clean(pmc_row, "pmcid") if pmc_row else None
        has_pmc = coerce_bool(pmc_row.get("has_pmc")) if pmc_row else None
        studies[study_id] = GoldStudy(
            study_id=study_id,
            pmid=_clean(row, "pmid"),
            doi=_clean(row, "doi"),
            title=_clean(row, "title"),
            journal=_clean(row, "journal"),
            year=coerce_int(row.get("year")),
            study_design=tuple(split_comma_strict(row.get("study_design"))),
            pmcid=pmcid,
            has_pmc=has_pmc,
            experiments=tuple(exps_by_study.get(study_id, ())),
        )
    return studies


# ---------------------------------------------------------------------------
# round-trip to the loader nested-dict shape (for `bugsigdb eval gold` and as
# the reference the prediction contract mirrors)
# ---------------------------------------------------------------------------


def to_nested_dict(study: GoldStudy) -> dict[str, Any]:
    """Convert a `GoldStudy` to the nested `Study -> experiments[] ->
    signatures[] -> taxa[]` dict shape predictions are expected to match
    (see `bugsigdb_curation.loader`). Used by `bugsigdb eval gold` to dump
    known-correct examples for authoring predictions against.
    """
    return {
        "study_id": study.study_id,
        "pmid": study.pmid,
        "doi": study.doi,
        "title": study.title,
        "journal": study.journal,
        "year": study.year,
        "study_design": list(study.study_design),
        "pmcid": study.pmcid,
        "has_pmc": study.has_pmc,
        "experiments": [_experiment_to_nested(e) for e in study.experiments],
    }


def _experiment_to_nested(experiment: GoldExperiment) -> dict[str, Any]:
    return {
        "experiment_id": experiment.experiment_id,
        "experiment_name": experiment.experiment_name,
        "location_of_subjects": list(experiment.location_of_subjects),
        "host_species": experiment.host_species,
        "body_site": list(experiment.body_site),
        "uberon_id": experiment.uberon_id,
        "condition": list(experiment.condition),
        "efo_id": experiment.efo_id,
        "group_0_name": experiment.group_0_name,
        "group_1_name": experiment.group_1_name,
        "group_1_definition": experiment.group_1_definition,
        "group_0_sample_size": experiment.group_0_sample_size,
        "group_1_sample_size": experiment.group_1_sample_size,
        "sequencing_type": experiment.sequencing_type,
        "statistical_test": list(experiment.statistical_test),
        "mht_correction": experiment.mht_correction,
        "signatures": [_signature_to_nested(s) for s in experiment.signatures],
    }


def _signature_to_nested(signature: GoldSignature) -> dict[str, Any]:
    return {
        "signature_id": signature.signature_id,
        "source": signature.source,
        "abundance_in_group_1": signature.direction,
        "taxa": [{"ncbi_id": ncbi_id} for ncbi_id in sorted(signature.taxa)],
    }
