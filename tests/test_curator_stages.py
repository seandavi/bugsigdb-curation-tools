"""Unit tests for curator S2-S5a stages (extract, segment, experiment, locate)
using `MockModel` and small fixture `EvidenceBundle`s -- fully offline.

S5b (fused extract+verify) is tested separately in
`tests/test_curator_signature.py` since it's async and needs a taxonomy
resolver + httpx client fixture.
"""

from __future__ import annotations

from bugsigdb_curation.curator.evidence import EvidenceBundle, EvidenceFigure, EvidenceTable
from bugsigdb_curation.curator.experiment import extract_experiment
from bugsigdb_curation.curator.extract import extract_study, extract_study_design
from bugsigdb_curation.curator.locate import locate_artifact
from bugsigdb_curation.curator.model import MockModel
from bugsigdb_curation.curator.resolve import ResolvedIds
from bugsigdb_curation.curator.segment import ExperimentStub, segment_experiments
from bugsigdb_curation.retrieval import ArticleMetadata, SectionEntry


def _bundle(*, tables=(), figures=(), sections=None, metadata=None) -> EvidenceBundle:
    return EvidenceBundle(
        pmid="21850056",
        pmcid="PMC1234567",
        metadata=metadata
        or ArticleMetadata(title="A CRC study", journal="Gut Microbes", year=2020, authors=("Jane Smith",), doi="10.1/x"),
        sections=sections
        or (SectionEntry(section_id="s1", title="Methods", text="We compared cases and controls."),),
        tables=tables,
        figures=figures,
    )


# --- S2 extract_study ---------------------------------------------------------------------


def test_extract_study_uses_bundle_metadata_for_bibliographic_fields():
    bundle = _bundle()
    resolved = ResolvedIds(pmid="21850056", pmcid="PMC1234567", doi="10.1/fallback")
    model = MockModel()

    fields = extract_study(bundle, resolved, model=model)

    assert fields.title == "A CRC study"
    assert fields.journal == "Gut Microbes"
    assert fields.year == 2020
    assert fields.authors == ("Jane Smith",)
    assert fields.doi == "10.1/x"  # bundle metadata wins over resolved.doi fallback


def test_extract_study_falls_back_to_resolved_doi_when_bundle_has_none():
    bundle = _bundle(metadata=ArticleMetadata(title=None, journal=None, year=None, authors=(), doi=None))
    resolved = ResolvedIds(pmid="1", pmcid="PMC1", doi="10.1/fallback")
    fields = extract_study(bundle, resolved, model=MockModel())
    assert fields.doi == "10.1/fallback"


def test_extract_study_design_uses_model_and_normalizes_enum():
    bundle = _bundle()
    model = MockModel(responses={"study_design": {"study_design": ["Case-Control"]}})  # wrong case
    resolved = ResolvedIds(pmid="1", pmcid="PMC1", doi=None)

    fields = extract_study(bundle, resolved, model=model)

    assert fields.study_design == ("case-control",)  # re-cased to canonical spelling


def test_extract_study_design_drops_values_outside_the_enum():
    bundle = _bundle()
    model = MockModel(responses={"study_design": {"study_design": ["not-a-real-design", "meta-analysis"]}})
    resolved = ResolvedIds(pmid="1", pmcid="PMC1", doi=None)

    fields = extract_study(bundle, resolved, model=model)

    assert fields.study_design == ("meta-analysis",)


def test_extract_study_design_tolerates_null_study_design():
    """A model reply of {"study_design": null} must not crash -- `.get(...,
    []) or []` guards against `None`, matching segment.py/signature.py."""
    bundle = _bundle()
    model = MockModel(responses={"study_design": {"study_design": None}})

    assert extract_study_design(bundle, model=model) == ()


# --- S3 segment_experiments ------------------------------------------------------------------


def test_segment_experiments_parses_default_mock_response():
    bundle = _bundle()
    stubs = segment_experiments(bundle, model=MockModel())
    assert len(stubs) == 1
    assert stubs[0].index == 0
    assert "Cases vs. controls" in stubs[0].description


def test_segment_experiments_handles_multiple_stubs():
    model = MockModel(
        responses={
            "segment": {
                "experiments": [
                    {"index": 0, "description": "Feces, 16S"},
                    {"index": 1, "description": "Saliva, WMS"},
                ]
            }
        }
    )
    stubs = segment_experiments(_bundle(), model=model)
    assert [s.description for s in stubs] == ["Feces, 16S", "Saliva, WMS"]


def test_segment_experiments_handles_empty_response():
    model = MockModel(responses={"segment": {"experiments": []}})
    assert segment_experiments(_bundle(), model=model) == []


def test_segment_experiments_tolerates_bare_string_items():
    model = MockModel(responses={"segment": {"experiments": ["a bare description"]}})
    stubs = segment_experiments(_bundle(), model=model)
    assert stubs[0].description == "a bare description"
    assert stubs[0].index == 0


# --- S4 extract_experiment --------------------------------------------------------------------


def test_extract_experiment_uses_default_mock_response():
    bundle = _bundle()
    stub = ExperimentStub(index=0, description="Cases vs. controls")
    fields = extract_experiment(bundle, stub, model=MockModel())

    assert fields.host_species == "Homo sapiens"
    assert fields.body_site == ("Feces",)
    assert fields.condition == ("Disease",)
    assert fields.group_0_name == "Control"
    assert fields.group_1_name == "Case"
    assert fields.sequencing_type == "16S"
    assert fields.statistical_test == ("LEfSe",)
    assert fields.mht_correction is False


def test_extract_experiment_normalizes_sequencing_type_and_drops_bad_statistical_test():
    model = MockModel(
        responses={
            "experiment_metadata": {
                "host_species": "Mus musculus",
                "body_site": ["Cecum"],
                "condition": [],
                "group_0_name": "WT",
                "group_1_name": "KO",
                "sequencing_type": "16s",  # wrong case
                "statistical_test": ["LEfSe", "not-a-real-test"],
                "mht_correction": None,
            }
        }
    )
    stub = ExperimentStub(index=0, description="WT vs KO mice")
    fields = extract_experiment(_bundle(), stub, model=model)

    assert fields.sequencing_type == "16S"
    assert fields.statistical_test == ("LEfSe",)
    assert fields.condition == ()
    assert fields.mht_correction is None


def test_extract_experiment_mht_correction_only_true_or_false_or_none():
    model = MockModel(responses={"experiment_metadata": {"mht_correction": "yes"}})  # not a real bool
    fields = extract_experiment(_bundle(), ExperimentStub(index=0, description=""), model=model)
    assert fields.mht_correction is None


# --- S5a locate_artifact -----------------------------------------------------------------------


def test_locate_prefers_table_with_da_signal_caption():
    da_table = EvidenceTable(table_id="T1", number="1", label="Table 1.", caption="LEfSe results.", rows=())
    plain_table = EvidenceTable(table_id="T2", number="2", label="Table 2.", caption="Cohort demographics.", rows=())
    bundle = _bundle(tables=(plain_table, da_table))

    artifact = locate_artifact(bundle)

    assert artifact is not None
    assert artifact.kind == "table"
    assert artifact.table is da_table
    assert artifact.provenance == "Table 1"


def test_locate_falls_back_to_figure_with_da_signal_when_no_da_table():
    plain_table = EvidenceTable(table_id="T1", number="1", label="Table 1.", caption="Demographics.", rows=())
    da_figure = EvidenceFigure(
        figure_id="F1", number="1", label="Figure 1.", legend="Differentially abundant taxa.",
        graphic_filename="f1.jpg", blob_url="https://cdn/f1.jpg",
    )
    bundle = _bundle(tables=(plain_table,), figures=(da_figure,))

    artifact = locate_artifact(bundle)

    assert artifact is not None
    assert artifact.kind == "figure"
    assert artifact.figure is da_figure


def test_locate_falls_back_to_first_table_when_no_da_signal_anywhere():
    only_table = EvidenceTable(table_id="T1", number="1", label="Table 1.", caption="Demographics.", rows=())
    bundle = _bundle(tables=(only_table,))

    artifact = locate_artifact(bundle)

    assert artifact is not None
    assert artifact.kind == "table"
    assert artifact.table is only_table


def test_locate_returns_none_when_bundle_has_no_tables_or_figures():
    assert locate_artifact(_bundle()) is None
