"""Unit tests for bugsigdb_curation.loader — CSV parsing, coercion, and grouping.

Written before the implementation (TDD): these describe the intended behavior of
`bugsigdb_curation.loader` against a small synthetic fixture that mimics the shape
of the real `full_dump.csv` (see tests/data/full_dump_sample.csv).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bugsigdb_curation.loader import (
    coerce_bool,
    coerce_int,
    is_blank,
    load_studies,
    normalize_enum,
    parse_curated_date,
    parse_taxa,
    parse_variable_region,
    read_rows,
    split_authors,
    split_comma_loose,
    split_comma_strict,
    summarize,
)

FIXTURE = Path(__file__).parent / "data" / "full_dump_sample.csv"


# --- row reading ---------------------------------------------------------


def test_read_rows_skips_banner_and_parses_header():
    rows = list(read_rows(FIXTURE))
    assert len(rows) == 7
    assert rows[0]["BSDB ID"] == "bsdb:100001/1/1"
    assert set(rows[0].keys()) >= {"Study", "PMID", "Experiment", "Signature page name"}


def test_read_rows_handles_missing_banner(tmp_path):
    # A file with no leading '#' banner line still parses correctly (defensive).
    no_banner = tmp_path / "no_banner.csv"
    no_banner.write_text("A,B\n1,2\n", encoding="utf-8")
    rows = list(read_rows(no_banner))
    assert rows == [{"A": "1", "B": "2"}]


# --- blank / coercion helpers ---------------------------------------------


@pytest.mark.parametrize("value,expected", [("", True), ("NA", True), (None, True), ("0", False), ("x", False)])
def test_is_blank(value, expected):
    assert is_blank(value) is expected


@pytest.mark.parametrize("value,expected", [("NA", None), ("", None), ("45", 45), ("  7 ", 7)])
def test_coerce_int(value, expected):
    assert coerce_int(value) == expected


def test_coerce_int_unparseable_returns_none():
    assert coerce_int("not-a-number") is None


@pytest.mark.parametrize(
    "value,expected",
    [("TRUE", True), ("true", True), ("FALSE", False), ("false", False), ("NA", None), ("", None)],
)
def test_coerce_bool(value, expected):
    assert coerce_bool(value) is expected


def test_parse_curated_date_converts_to_iso():
    assert parse_curated_date("10 January 2021") == "2021-01-10"


def test_parse_curated_date_blank_returns_none():
    assert parse_curated_date("NA") is None
    assert parse_curated_date("") is None


def test_parse_curated_date_unparseable_keeps_raw_string():
    assert parse_curated_date("not a date") == "not a date"


def test_normalize_enum_passes_known_value():
    assert normalize_enum("increased", {"increased", "decreased"}) == "increased"


def test_normalize_enum_case_insensitive():
    assert normalize_enum("Increased", {"increased", "decreased"}) == "increased"


def test_normalize_enum_blank_returns_none():
    assert normalize_enum("NA", {"increased", "decreased"}) is None


def test_normalize_enum_unknown_value_returns_none():
    assert normalize_enum("sideways", {"increased", "decreased"}) is None


# --- multivalue splitting ---------------------------------------------------


def test_split_comma_loose_keywords_style():
    assert split_comma_loose("gut, diabetes, microbiome") == ["gut", "diabetes", "microbiome"]


def test_split_comma_loose_blank():
    assert split_comma_loose("NA") == []


def test_split_comma_strict_splits_on_bare_comma_only():
    assert split_comma_strict("Italy,Luxembourg,United States of America") == [
        "Italy",
        "Luxembourg",
        "United States of America",
    ]


def test_split_comma_strict_preserves_embedded_comma_space():
    # "Korea, Republic of" contains a comma+space itself; only the bare comma
    # (no following space) before "Japan" is a real separator.
    assert split_comma_strict("Korea, Republic of,Japan") == ["Korea, Republic of", "Japan"]


def test_split_comma_strict_blank():
    assert split_comma_strict("NA") == []


def test_split_authors_pairs_surname_initials_citation_style():
    raw = "Feehan, A.K., Rose, R. and Lamers, S.L."
    assert split_authors(raw) == ["Feehan A.K.", "Rose R.", "Lamers S.L."]


def test_split_authors_simple_comma_list_not_paired():
    # Exactly two "Name Initial" authors: naive pairing would wrongly merge
    # these into a single author. The initials-detection heuristic must not
    # trigger here since neither odd-indexed token is initials-only.
    assert split_authors("Smith J, Doe A") == ["Smith J", "Doe A"]


def test_split_authors_blank():
    assert split_authors("NA") == []


def test_parse_variable_region_range():
    # Returned as strings: SixteenSRegionEnum's permissible values are "1".."9",
    # not integers.
    assert parse_variable_region("34") == ("3", "4")


def test_parse_variable_region_single():
    assert parse_variable_region("4") == ("4", None)


def test_parse_variable_region_blank():
    assert parse_variable_region("NA") == (None, None)


# --- taxa construction -------------------------------------------------------


def test_parse_taxa_pairs_names_and_ids_with_lineage():
    names = "g__Bacteroides,f__Ruminococcaceae|g__Faecalibacterium|s__Faecalibacterium prausnitzii"
    ids = "820;186803|186807|853"
    taxa = parse_taxa(names, ids)
    assert len(taxa) == 2
    first, second = taxa
    assert first["ncbi_id"] == 820
    assert first["taxon_name"] == "Bacteroides"
    assert first["taxonomic_rank"] == "genus"
    assert "lineage" not in first  # single-level: lineage would be trivial

    assert second["ncbi_id"] == 853
    assert second["taxon_name"] == "Faecalibacterium prausnitzii"
    assert second["taxonomic_rank"] == "species"
    assert second["lineage"] == ["Ruminococcaceae", "Faecalibacterium", "Faecalibacterium prausnitzii"]


def test_parse_taxa_single_taxon_no_lineage():
    taxa = parse_taxa("s__Escherichia coli", "562")
    assert taxa == [{"ncbi_id": 562, "taxon_name": "Escherichia coli", "taxonomic_rank": "species"}]


def test_parse_taxa_blank_returns_empty_list():
    assert parse_taxa("NA", "NA") == []


# --- grouping: load_studies --------------------------------------------------


@pytest.fixture()
def studies():
    return load_studies(FIXTURE)


def test_load_studies_counts(studies):
    n_studies, n_experiments, n_signatures = summarize(studies)
    assert n_studies == 2
    assert n_experiments == 3
    assert n_signatures == 6


def test_load_studies_citation_mode_inferred_from_pmid(studies):
    # citation_mode is `required: true` on the schema with no direct column in
    # full_dump.csv; infer Auto/Manual from whether a PMID applies.
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    assert study_a["citation_mode"] == "Auto"

    study_b = next(s for s in studies if s.get("title") is None and s.get("doi") is None)
    assert study_b["citation_mode"] == "Manual"


def test_load_studies_pmid_keyed_study(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    assert study_a["uid"] == "100001"
    assert study_a["doi"] == "10.1000/xyz1"
    assert study_a["title"] == "Test Study One"
    assert study_a["year"] == 2020
    assert study_a["keywords"] == ["gut", "diabetes", "microbiome"]
    assert study_a["study_design"] == ["case-control", "time series / longitudinal observational"]
    assert study_a["authors"] == ["Feehan A.K.", "Rose R.", "Lamers S.L."]
    assert len(study_a["experiments"]) == 2


def test_load_studies_uid_is_first_key(studies):
    # Cosmetic but required: `uid` is the Study identifier and should be the
    # first key emitted for each study dict (insertion order == dict order).
    for study in studies:
        assert next(iter(study)) == "uid"


def test_load_studies_pmid_less_study_keyed_by_study_column(studies):
    assert not any(s.get("pmid") == 77 for s in studies)
    study_b = next(s for s in studies if s.get("title") is None and s.get("doi") is None)
    assert "pmid" not in study_b
    # This is the whole point of the change: a PMID-less study still gets a
    # stable `uid` (the wiki page name / `Study` column), so it validates.
    assert study_b["uid"] == "Study 77"
    assert len(study_b["experiments"]) == 1
    assert len(study_b["experiments"][0]["signatures"]) == 3


def test_load_studies_experiment_with_two_signatures_increased_and_decreased(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp1 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Controls")
    directions = {sig["abundance_in_group_1"] for sig in exp1["signatures"]}
    assert directions == {"increased", "decreased"}
    assert len(exp1["signatures"]) == 2


def test_load_studies_multivalue_taxa_and_location(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp1 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Controls")
    assert exp1["location_of_subjects"] == ["Korea, Republic of", "Japan"]
    assert exp1["body_site"] == ["Feces", "Oral cavity"]
    assert exp1["variable_region_lower_bound"] == "3"
    assert exp1["variable_region_upper_bound"] == "4"

    sig1 = next(s for s in exp1["signatures"] if s["abundance_in_group_1"] == "increased")
    assert len(sig1["taxa"]) == 2
    assert {t["ncbi_id"] for t in sig1["taxa"]} == {820, 853}


def test_load_studies_blank_optional_fields_are_absent_not_zero_or_empty(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp2 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Baseline")
    # Location of subjects, body_site, condition were NA for Experiment 2 -> absent, not [].
    assert "location_of_subjects" not in exp2
    assert "body_site" not in exp2
    assert "condition" not in exp2
    assert "group_0_sample_size" not in exp2
    assert "group_1_sample_size" not in exp2
    assert "significance_threshold" not in exp2
    assert "mht_correction" not in exp2

    study_b = next(s for s in studies if s.get("title") is None and s.get("doi") is None)
    sig3 = study_b["experiments"][0]["signatures"][2]
    assert "abundance_in_group_1" not in sig3
    assert "taxa" not in sig3 or sig3["taxa"] == []
    assert "source" not in sig3
    assert "description" not in sig3


def test_load_studies_respects_limit():
    studies = load_studies(FIXTURE, limit=1)
    assert len(studies) == 1


def test_load_studies_alpha_diversity_enum_preserved(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp1 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Controls")
    assert exp1["shannon"] == "decreased"

    study_b = next(s for s in studies if s.get("title") is None and s.get("doi") is None)
    exp_b = study_b["experiments"][0]
    assert exp_b["pielou"] == "unchanged"
    assert exp_b["shannon"] == "increased"


def test_load_studies_no_signature_yet_row_skipped(studies):
    # ~2.9% of real rows are "Experiment has no Signature yet" (Signature page
    # name == "NA"). The fixture's third row for Experiment 1 of study 100001
    # (bsdb:100001/1/NA) is exactly such a row: it must still count toward the
    # Experiment (already true via the other two rows) but must NOT produce a
    # phantom empty Signature dict, and the experiment's signature count must
    # reflect only the two real signatures.
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp1 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Controls")
    assert len(exp1["signatures"]) == 2
    assert {} not in exp1["signatures"]
    assert all(sig for sig in exp1["signatures"])


def test_load_studies_curated_date_is_iso(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp1 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Controls")
    for sig in exp1["signatures"]:
        assert sig["curated_date"] == "2021-01-10"


def test_load_studies_experiment_enum_fields_normalized(studies):
    study_a = next(s for s in studies if s.get("pmid") == 100001)
    exp1 = next(e for e in study_a["experiments"] if e["group_0_name"] == "Controls")
    assert exp1["sequencing_type"] == "16S"
    assert exp1["data_transformation"] == "relative abundances"
    assert exp1["sequencing_platform"] == ["Illumina"]
    assert exp1["statistical_test"] == ["LEfSe", "Mann-Whitney (Wilcoxon)"]

    study_b = next(s for s in studies if s.get("title") is None and s.get("doi") is None)
    exp_b = study_b["experiments"][0]
    assert exp_b["sequencing_type"] == "WMS"
    assert exp_b["data_transformation"] == "raw counts"
    assert exp_b["sequencing_platform"] == ["Illumina"]
    assert exp_b["statistical_test"] == ["DESeq2"]
