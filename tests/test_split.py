"""Unit and CLI integration tests for the split command."""

from __future__ import annotations

import csv
from pathlib import Path
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.split import split_full_dump

runner = CliRunner()

SAMPLE_CSV_CONTENT = """# BugSigDB 2026-07-13_00:00_UTC, License: Creative Commons Attribution 4.0 International, URL: https://bugsigdb.org
BSDB ID,Study,Study design,PMID,DOI,URL,Authors list,Title,Journal,Year,Keywords,Experiment,Location of subjects,Host species,Body site,UBERON ID,Condition,EFO ID,Group 0 name,Group 1 name,Group 1 definition,Group 0 sample size,Group 1 sample size,Antibiotics exclusion,Sequencing type,16S variable region,Sequencing platform,Data transformation,Statistical test,Significance threshold,MHT correction,LDA Score above,Matched on,Confounders controlled for,Pielou,Shannon,Chao1,Simpson,Inverse Simpson,Richness,Signature page name,Source,Curated date,Curator,Revision editor,Description,Abundance in Group 1,MetaPhlAn taxon names,NCBI Taxonomy IDs,State,Reviewer
bsdb:100001/1/1,100001,case-control,100001,10.1000/xyz1,https://example.org/study1,"Feehan, A.K.",Test Study,J Test,2020,test,Experiment 1,Korea,Homo sapiens,Feces,NA,Type 2 diabetes,NA,Controls,Cases,Diagnosed,20,25,NA,16S,34,Illumina,relative abundances,LEfSe,0.05,TRUE,2,"age","age",NA,decreased,NA,NA,NA,NA,Signature 1,Table 2,10 Jan 2021,Jane Curator,Jane Curator,Taxa increased,increased,"g__Bacteroides,f__Ruminococcaceae|g__Faecalibacterium|s__Faecalibacterium prausnitzii",820;186803|186807|853,Complete,NA
bsdb:100001/1/2,100001,case-control,100001,10.1000/xyz1,https://example.org/study1,"Feehan, A.K.",Test Study,J Test,2020,test,Experiment 1,Korea,Homo sapiens,Feces,NA,Type 2 diabetes,NA,Controls,Cases,Diagnosed,20,25,NA,16S,34,Illumina,relative abundances,LEfSe,0.05,TRUE,2,"age","age",NA,decreased,NA,NA,NA,NA,Signature 2,Figure 3,10 Jan 2021,Jane Curator,Jane Curator,Taxa decreased,decreased,s__Escherichia coli,562,Complete,NA
"""


def test_split_full_dump(tmp_path: Path):
    input_file = tmp_path / "full_dump.csv"
    input_file.write_text(SAMPLE_CSV_CONTENT, encoding="utf-8")
    output_dir = tmp_path / "relational"

    counts = split_full_dump(input_file, output_dir)

    assert counts["studies.csv"] == 1
    assert counts["experiments.csv"] == 1
    assert counts["signatures.csv"] == 2
    # g__Bacteroides (820), f__Ruminococcaceae (186803), g__Faecalibacterium (186807), s__Faecalibacterium prausnitzii (853), s__Escherichia coli (562)
    assert counts["taxa.csv"] == 5
    assert counts["signatures_taxa.csv"] == 3  # signature 1 has 820 and 853; signature 2 has 562

    # Verify studies.csv
    studies_file = output_dir / "studies.csv"
    assert studies_file.exists()
    with open(studies_file, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["study_id"] == "100001"
        assert rows[0]["pmid"] == "100001"
        assert rows[0]["year"] == "2020"

    # Verify experiments.csv
    experiments_file = output_dir / "experiments.csv"
    assert experiments_file.exists()
    with open(experiments_file, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["experiment_id"] == "100001/Experiment 1"
        assert rows[0]["study_id"] == "100001"
        assert rows[0]["experiment_name"] == "Experiment 1"
        assert rows[0]["host_species"] == "Homo sapiens"

    # Verify signatures.csv
    signatures_file = output_dir / "signatures.csv"
    assert signatures_file.exists()
    with open(signatures_file, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["signature_id"] == "bsdb:100001/1/1"
        assert rows[0]["experiment_id"] == "100001/Experiment 1"
        assert rows[0]["abundance_in_group_1"] == "increased"
        assert rows[1]["signature_id"] == "bsdb:100001/1/2"
        assert rows[1]["abundance_in_group_1"] == "decreased"

    # Verify taxa.csv
    taxa_file = output_dir / "taxa.csv"
    assert taxa_file.exists()
    with open(taxa_file, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert len(rows) == 5
        ncbi_ids = {r["ncbi_id"] for r in rows}
        assert "820" in ncbi_ids
        assert "186803" in ncbi_ids
        assert "186807" in ncbi_ids
        assert "853" in ncbi_ids
        assert "562" in ncbi_ids

    # Verify signatures_taxa.csv
    sig_taxa_file = output_dir / "signatures_taxa.csv"
    assert sig_taxa_file.exists()
    with open(sig_taxa_file, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert len(rows) == 3
        # First row of signature 1 should map to 820 or 853
        assert rows[0]["signature_id"] == "bsdb:100001/1/1"
        assert rows[0]["ncbi_id"] in ("820", "853")


def test_cli_split_command(tmp_path: Path):
    input_file = tmp_path / "full_dump.csv"
    input_file.write_text(SAMPLE_CSV_CONTENT, encoding="utf-8")
    output_dir = tmp_path / "relational"

    result = runner.invoke(app, ["split", "-i", str(input_file), "-o", str(output_dir)])

    assert result.exit_code == 0
    assert "Successfully split" in result.output
    assert "studies.csv" in result.output
    assert (output_dir / "studies.csv").exists()
