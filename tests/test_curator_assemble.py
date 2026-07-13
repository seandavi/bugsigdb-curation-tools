"""Unit tests for `bugsigdb_curation.curator.assemble` (S8) -- deterministic
build of the nested prediction record, plus its S9 schema-validity gate."""

from __future__ import annotations

from bugsigdb_curation.curator.assemble import assemble_record
from bugsigdb_curation.curator.experiment import ExperimentFields
from bugsigdb_curation.curator.extract import StudyFields
from bugsigdb_curation.curator.resolve import ResolvedIds
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon
from bugsigdb_curation.validate import default_schema_path, validate_instance


def _study_fields(**overrides) -> StudyFields:
    base = dict(
        title="A CRC study",
        journal="Gut Microbes",
        year=2020,
        authors=("Jane Smith",),
        doi="10.1/x",
        study_design=("case-control",),
    )
    base.update(overrides)
    return StudyFields(**base)


def _experiment_fields(**overrides) -> ExperimentFields:
    base = dict(
        host_species="Homo sapiens",
        body_site=("Feces",),
        condition=("CRC",),
        group_0_name="Control",
        group_1_name="Case",
        sequencing_type="16S",
        statistical_test=("LEfSe",),
        mht_correction=False,
    )
    base.update(overrides)
    return ExperimentFields(**base)


def test_assemble_record_top_level_shape():
    resolved = ResolvedIds(pmid="21850056", pmcid="PMC1234567", doi="10.1/fallback")
    record = assemble_record(resolved, _study_fields(), [])

    assert record["uid"] == "21850056"
    assert record["pmid"] == 21850056
    assert record["citation_mode"] == "Auto"
    assert record["doi"] == "10.1/x"
    assert record["title"] == "A CRC study"
    assert record["authors"] == ["Jane Smith"]
    assert record["journal"] == "Gut Microbes"
    assert record["year"] == 2020
    assert record["study_design"] == ["case-control"]
    assert record["experiments"] == []


def test_assemble_record_omits_blank_optional_fields_never_emits_none_or_empty():
    resolved = ResolvedIds(pmid="1", pmcid="PMC1", doi=None)
    fields = _study_fields(title=None, journal=None, year=None, authors=(), doi=None, study_design=())
    record = assemble_record(resolved, fields, [])

    for key in ("doi", "title", "authors", "journal", "year", "study_design"):
        assert key not in record


def test_assemble_record_non_numeric_pmid_uses_manual_citation_mode():
    resolved = ResolvedIds(pmid="Study 7", pmcid=None, doi=None)
    record = assemble_record(resolved, _study_fields(), [])
    assert record["citation_mode"] == "Manual"
    assert "pmid" not in record
    assert record["uid"] == "Study 7"


def test_assemble_record_experiment_and_signature_and_taxon_shape():
    resolved = ResolvedIds(pmid="21850056", pmcid="PMC1234567", doi=None)
    signature = ExtractedSignature(
        direction="increased",
        taxa=(ExtractedTaxon(taxon_name="Bacteroides fragilis", direction="increased", ncbi_id=817),),
    )
    record = assemble_record(resolved, _study_fields(), [(_experiment_fields(), [signature], "Table 2")])

    exp = record["experiments"][0]
    assert exp["host_species"] == "Homo sapiens"
    assert exp["body_site"] == ["Feces"]
    assert exp["condition"] == ["CRC"]
    assert exp["group_0_name"] == "Control"
    assert exp["group_1_name"] == "Case"
    assert exp["sequencing_type"] == "16S"
    assert exp["statistical_test"] == ["LEfSe"]
    assert exp["mht_correction"] is False

    sig = exp["signatures"][0]
    assert sig["source"] == "Table 2"
    assert sig["abundance_in_group_1"] == "increased"
    assert sig["taxa"] == [{"taxon_name": "Bacteroides fragilis", "ncbi_id": 817}]


def test_assemble_record_taxon_without_verified_id_omits_ncbi_id():
    resolved = ResolvedIds(pmid="1", pmcid="PMC1", doi=None)
    signature = ExtractedSignature(
        direction="decreased",
        taxa=(ExtractedTaxon(taxon_name="Some Novel Taxon", direction="decreased", ncbi_id=None),),
    )
    record = assemble_record(resolved, _study_fields(), [(_experiment_fields(), [signature], None)])
    taxon = record["experiments"][0]["signatures"][0]["taxa"][0]
    assert taxon == {"taxon_name": "Some Novel Taxon"}
    assert "source" not in record["experiments"][0]["signatures"][0]


def test_assemble_record_experiment_with_no_signatures_omits_signatures_key():
    resolved = ResolvedIds(pmid="1", pmcid="PMC1", doi=None)
    record = assemble_record(resolved, _study_fields(), [(_experiment_fields(), [], None)])
    assert "signatures" not in record["experiments"][0]


# --- S9: the assembled record must pass structural validation (well-formed case) --------------


def test_fully_populated_record_passes_structural_validation():
    resolved = ResolvedIds(pmid="21850056", pmcid="PMC1234567", doi=None)
    signature = ExtractedSignature(
        direction="increased",
        taxa=(ExtractedTaxon(taxon_name="Bacteroides fragilis", direction="increased", ncbi_id=817),),
    )
    record = assemble_record(resolved, _study_fields(), [(_experiment_fields(), [signature], "Table 2")])

    problems = validate_instance(record, "Study", default_schema_path())
    assert problems == []


def test_record_missing_required_host_species_fails_structural_validation():
    resolved = ResolvedIds(pmid="21850056", pmcid="PMC1234567", doi=None)
    fields_missing_host = _experiment_fields(host_species=None)
    record = assemble_record(resolved, _study_fields(), [(fields_missing_host, [], None)])

    problems = validate_instance(record, "Study", default_schema_path())
    assert problems  # host_species is required on Experiment -> must fail
