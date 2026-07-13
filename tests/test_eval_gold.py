"""Unit tests for bugsigdb_curation.eval.gold -- the relational-CSV gold join."""

from __future__ import annotations

import csv
from pathlib import Path

from bugsigdb_curation.eval.gold import (
    GoldExperiment,
    GoldSignature,
    GoldStudy,
    load_gold,
    source_type,
    to_nested_dict,
)

STUDY_FIELDS = [
    "study_id", "pmid", "doi", "url", "authors", "title", "journal", "year", "keywords",
    "study_design", "curator", "curated_date", "revision_editor", "state", "reviewer",
]
EXPERIMENT_FIELDS = [
    "experiment_id", "study_id", "experiment_name", "location_of_subjects", "host_species",
    "body_site", "uberon_id", "condition", "efo_id", "group_0_name", "group_1_name",
    "group_1_definition", "group_0_sample_size", "group_1_sample_size", "antibiotics_exclusion",
    "sequencing_type", "variable_region", "sequencing_platform", "data_transformation",
    "statistical_test", "significance_threshold", "mht_correction", "lda_score_above",
    "matched_on", "confounders_controlled_for", "pielou", "shannon", "chao1", "simpson",
    "inverse_simpson", "richness",
]
SIGNATURE_FIELDS = [
    "signature_id", "experiment_id", "signature_name", "source", "description",
    "abundance_in_group_1", "curation_state", "curator", "curated_date", "revision_editor",
    "reviewer",
]
SIGNATURES_TAXA_FIELDS = ["signature_id", "ncbi_id"]
PMC_MAP_FIELDS = ["study_id", "pmid", "pmcid", "doi", "has_pmc"]


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def _write_relational_fixture(
    tmp_path: Path,
    *,
    studies: list[dict[str, str]],
    experiments: list[dict[str, str]] | None = None,
    signatures: list[dict[str, str]] | None = None,
    signatures_taxa: list[dict[str, str]] | None = None,
    pmc_map: list[dict[str, str]] | None = None,
) -> tuple[Path, Path]:
    relational_dir = tmp_path / "relational"
    relational_dir.mkdir()
    _write_csv(relational_dir / "studies.csv", STUDY_FIELDS, studies)
    _write_csv(relational_dir / "experiments.csv", EXPERIMENT_FIELDS, experiments or [])
    _write_csv(relational_dir / "signatures.csv", SIGNATURE_FIELDS, signatures or [])
    _write_csv(relational_dir / "signatures_taxa.csv", SIGNATURES_TAXA_FIELDS, signatures_taxa or [])

    pmc_map_path = tmp_path / "pmid_pmcid_map.csv"
    _write_csv(pmc_map_path, PMC_MAP_FIELDS, pmc_map or [])
    return relational_dir, pmc_map_path


# --- source_type classifier -------------------------------------------------------------


def test_source_type_main_table():
    assert source_type("table 2") == "main-table"
    assert source_type("Table 3") == "main-table"


def test_source_type_figure():
    assert source_type("Figure 4") == "figure"
    assert source_type("fig. 2a") == "figure"


def test_source_type_supplement_by_keyword():
    assert source_type("Supplementary Table 1") == "supplement"


def test_source_type_supplement_by_s_digit_pattern():
    assert source_type("Table S5") == "supplement"


def test_source_type_supplement_wins_over_figure_mention():
    assert source_type("Supporting Info. Table S5 + Fig. S2") == "supplement"


def test_source_type_other_for_blank_or_unrecognized():
    assert source_type("") == "other"
    assert source_type(None) == "other"
    assert source_type("see main text") == "other"


# --- load_gold: study_id shapes ---------------------------------------------------------


def test_load_gold_study_id_is_pmid_for_pmid_studies(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "19849869", "pmid": "19849869", "title": "A", "study_design": "case-control"}],
    )
    gold = load_gold(relational_dir, pmc_map)
    assert "19849869" in gold
    assert gold["19849869"].pmid == "19849869"
    assert gold["19849869"].study_design == ("case-control",)


def test_load_gold_study_id_is_wiki_page_name_when_pmid_blank(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "Study 11", "pmid": "", "study_design": "prospective cohort"}],
    )
    gold = load_gold(relational_dir, pmc_map)
    assert "Study 11" in gold
    assert gold["Study 11"].pmid is None


# --- load_gold: has_pmc / pmc-map join ---------------------------------------------------


def test_load_gold_has_pmc_true(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "21850056", "pmid": "21850056"}],
        pmc_map=[{"study_id": "21850056", "pmid": "21850056", "pmcid": "PMC3126210", "has_pmc": "true"}],
    )
    gold = load_gold(relational_dir, pmc_map)
    assert gold["21850056"].has_pmc is True
    assert gold["21850056"].pmcid == "PMC3126210"


def test_load_gold_has_pmc_false(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "19849869", "pmid": "19849869"}],
        pmc_map=[{"study_id": "19849869", "pmid": "19849869", "pmcid": "", "has_pmc": "false"}],
    )
    gold = load_gold(relational_dir, pmc_map)
    assert gold["19849869"].has_pmc is False
    assert gold["19849869"].pmcid is None


def test_load_gold_has_pmc_none_when_study_absent_from_pmc_map(tmp_path):
    # e.g. a "Study N" (no PMID) row: pmc-map skips PMID-less rows entirely.
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "Study 11", "pmid": ""}],
        pmc_map=[],
    )
    gold = load_gold(relational_dir, pmc_map)
    assert gold["Study 11"].has_pmc is None
    assert gold["Study 11"].pmcid is None


def test_load_gold_missing_pmc_map_file_yields_none_has_pmc(tmp_path):
    relational_dir, _ = _write_relational_fixture(tmp_path, studies=[{"study_id": "1", "pmid": "1"}])
    gold = load_gold(relational_dir, tmp_path / "does_not_exist.csv")
    assert gold["1"].has_pmc is None


# --- load_gold: experiment/signature/taxa chaining ----------------------------------------


def test_load_gold_chains_experiment_signature_taxa(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "21850056", "pmid": "21850056"}],
        experiments=[
            {
                "experiment_id": "21850056/Experiment 1",
                "study_id": "21850056",
                "experiment_name": "Experiment 1",
                "body_site": "Feces",
                "condition": "Colorectal cancer",
                "sequencing_type": "16S",
                "mht_correction": "FALSE",
            }
        ],
        signatures=[
            {
                "signature_id": "bsdb:21850056/1/1",
                "experiment_id": "21850056/Experiment 1",
                "source": "table 2",
                "abundance_in_group_1": "increased",
                "curation_state": "Complete",
            },
            {
                "signature_id": "bsdb:21850056/1/2",
                "experiment_id": "21850056/Experiment 1",
                "source": "table 2",
                "abundance_in_group_1": "decreased",
                "curation_state": "Complete",
            },
        ],
        signatures_taxa=[
            {"signature_id": "bsdb:21850056/1/1", "ncbi_id": "561"},
            {"signature_id": "bsdb:21850056/1/1", "ncbi_id": "620"},
            {"signature_id": "bsdb:21850056/1/2", "ncbi_id": "816"},
        ],
    )
    gold = load_gold(relational_dir, pmc_map)
    study = gold["21850056"]
    assert len(study.experiments) == 1
    experiment = study.experiments[0]
    assert experiment.body_site == ("Feces",)
    assert experiment.condition == ("Colorectal cancer",)
    assert experiment.mht_correction is False
    assert len(experiment.signatures) == 2

    increased = next(s for s in experiment.signatures if s.direction == "increased")
    decreased = next(s for s in experiment.signatures if s.direction == "decreased")
    assert increased.taxa == frozenset({561, 620})
    assert decreased.taxa == frozenset({816})
    assert increased.source_type == "main-table"


def test_load_gold_signature_with_no_taxa_rows_gets_empty_frozenset(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "1", "pmid": "1"}],
        experiments=[{"experiment_id": "1/Experiment 1", "study_id": "1"}],
        signatures=[{"signature_id": "bsdb:1/1/NA", "experiment_id": "1/Experiment 1"}],
        signatures_taxa=[],
    )
    gold = load_gold(relational_dir, pmc_map)
    sig = gold["1"].experiments[0].signatures[0]
    assert sig.taxa == frozenset()
    assert sig.direction is None


def test_load_gold_blank_direction_becomes_none(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(
        tmp_path,
        studies=[{"study_id": "1", "pmid": "1"}],
        experiments=[{"experiment_id": "1/Experiment 1", "study_id": "1"}],
        signatures=[
            {"signature_id": "s1", "experiment_id": "1/Experiment 1", "abundance_in_group_1": ""}
        ],
    )
    gold = load_gold(relational_dir, pmc_map)
    assert gold["1"].experiments[0].signatures[0].direction is None


def test_load_gold_study_with_no_experiments_gets_empty_tuple(tmp_path):
    relational_dir, pmc_map = _write_relational_fixture(tmp_path, studies=[{"study_id": "1", "pmid": "1"}])
    gold = load_gold(relational_dir, pmc_map)
    assert gold["1"].experiments == ()


# --- to_nested_dict round-trip ------------------------------------------------------------


def test_to_nested_dict_shape():
    study = GoldStudy(
        study_id="21850056",
        pmid="21850056",
        doi=None,
        title="T",
        journal=None,
        year=2012,
        study_design=("case-control",),
        pmcid="PMC1",
        has_pmc=True,
        experiments=(
            GoldExperiment(
                experiment_id="21850056/Experiment 1",
                study_id="21850056",
                experiment_name="Experiment 1",
                location_of_subjects=(),
                host_species="Homo sapiens",
                body_site=("Feces",),
                uberon_id="UBERON:0001988",
                condition=("Colorectal cancer",),
                efo_id=None,
                group_0_name="healthy",
                group_1_name="CRC",
                group_1_definition=None,
                group_0_sample_size=56,
                group_1_sample_size=46,
                sequencing_type="16S",
                statistical_test=("Mann-Whitney (Wilcoxon)",),
                mht_correction=False,
                signatures=(
                    GoldSignature(
                        signature_id="bsdb:21850056/1/1",
                        experiment_id="21850056/Experiment 1",
                        source="table 2",
                        source_type="main-table",
                        direction="increased",
                        taxa=frozenset({561, 620}),
                        curation_state="Complete",
                    ),
                ),
            ),
        ),
    )

    nested = to_nested_dict(study)
    assert nested["study_id"] == "21850056"
    assert len(nested["experiments"]) == 1
    exp = nested["experiments"][0]
    assert exp["body_site"] == ["Feces"]
    assert len(exp["signatures"]) == 1
    sig = exp["signatures"][0]
    assert sig["abundance_in_group_1"] == "increased"
    assert sorted(t["ncbi_id"] for t in sig["taxa"]) == [561, 620]
