"""Logic for splitting a flat BugSigDB full dump CSV into relational CSV files."""

from __future__ import annotations

import csv
from pathlib import Path

STUDY_FIELDS = [
    "study_id",
    "pmid",
    "doi",
    "url",
    "authors",
    "title",
    "journal",
    "year",
    "keywords",
    "study_design",
    "curator",
    "curated_date",
    "revision_editor",
    "state",
    "reviewer",
]

EXPERIMENT_FIELDS = [
    "experiment_id",
    "study_id",
    "experiment_name",
    "location_of_subjects",
    "host_species",
    "body_site",
    "uberon_id",
    "condition",
    "efo_id",
    "group_0_name",
    "group_1_name",
    "group_1_definition",
    "group_0_sample_size",
    "group_1_sample_size",
    "antibiotics_exclusion",
    "sequencing_type",
    "variable_region",
    "sequencing_platform",
    "data_transformation",
    "statistical_test",
    "significance_threshold",
    "mht_correction",
    "lda_score_above",
    "matched_on",
    "confounders_controlled_for",
    "pielou",
    "shannon",
    "chao1",
    "simpson",
    "inverse_simpson",
    "richness",
]

SIGNATURE_FIELDS = [
    "signature_id",
    "experiment_id",
    "signature_name",
    "source",
    "description",
    "abundance_in_group_1",
    "curation_state",
    "curator",
    "curated_date",
    "revision_editor",
    "reviewer",
]

TAXON_FIELDS = [
    "ncbi_id",
    "taxon_name",
]

SIGNATURE_TAXA_FIELDS = [
    "signature_id",
    "ncbi_id",
]


def clean_val(val: str | None) -> str:
    """Normalize missing or 'NA' string values to empty string."""
    if val is None:
        return ""
    val_stripped = val.strip()
    if val_stripped.upper() in ("NA", "NULL", ""):
        return ""
    return val_stripped


def get_clean(row: dict[str, str], key: str) -> str:
    """Extract a cleaned value from a row dictionary."""
    return clean_val(row.get(key))


def split_full_dump(input_file: Path, output_dir: Path) -> dict[str, int]:
    """Split the flat bugsigdb full_dump.csv into relational CSV files.

    Returns a dictionary of written table names and their row counts.
    """
    if not input_file.exists():
        raise FileNotFoundError(f"Input file not found: {input_file}")

    # Read lines, filtering out leading comments
    with open(input_file, mode="r", encoding="utf-8") as f:
        first_line = f.readline()
        while first_line.startswith("#"):
            first_line = f.readline()
        
        # Reset and parse using a generator that filters comment lines
        f.seek(0)
        lines = (line for line in f if not line.startswith("#"))
        reader = csv.DictReader(lines)
        rows = list(reader)

    studies: dict[str, dict[str, str]] = {}
    experiments: dict[str, dict[str, str]] = {}
    signatures: dict[str, dict[str, str]] = {}
    taxa: dict[str, dict[str, str]] = {}
    signatures_taxa: list[dict[str, str]] = []

    for row in rows:
        study_id = get_clean(row, "Study")
        if not study_id:
            continue

        # 1. Study-level data
        if study_id not in studies:
            studies[study_id] = {
                "study_id": study_id,
                "pmid": get_clean(row, "PMID"),
                "doi": get_clean(row, "DOI"),
                "url": get_clean(row, "URL"),
                "authors": get_clean(row, "Authors list"),
                "title": get_clean(row, "Title"),
                "journal": get_clean(row, "Journal"),
                "year": get_clean(row, "Year"),
                "keywords": get_clean(row, "Keywords"),
                "study_design": get_clean(row, "Study design"),
                "curator": get_clean(row, "Curator"),
                "curated_date": get_clean(row, "Curated date"),
                "revision_editor": get_clean(row, "Revision editor"),
                "state": get_clean(row, "State"),
                "reviewer": get_clean(row, "Reviewer"),
            }

        # 2. Experiment-level data
        experiment_name = get_clean(row, "Experiment")
        if experiment_name:
            experiment_id = f"{study_id}/{experiment_name}"
            if experiment_id not in experiments:
                experiments[experiment_id] = {
                    "experiment_id": experiment_id,
                    "study_id": study_id,
                    "experiment_name": experiment_name,
                    "location_of_subjects": get_clean(row, "Location of subjects"),
                    "host_species": get_clean(row, "Host species"),
                    "body_site": get_clean(row, "Body site"),
                    "uberon_id": get_clean(row, "UBERON ID"),
                    "condition": get_clean(row, "Condition"),
                    "efo_id": get_clean(row, "EFO ID"),
                    "group_0_name": get_clean(row, "Group 0 name"),
                    "group_1_name": get_clean(row, "Group 1 name"),
                    "group_1_definition": get_clean(row, "Group 1 definition"),
                    "group_0_sample_size": get_clean(row, "Group 0 sample size"),
                    "group_1_sample_size": get_clean(row, "Group 1 sample size"),
                    "antibiotics_exclusion": get_clean(row, "Antibiotics exclusion"),
                    "sequencing_type": get_clean(row, "Sequencing type"),
                    "variable_region": get_clean(row, "16S variable region"),
                    "sequencing_platform": get_clean(row, "Sequencing platform"),
                    "data_transformation": get_clean(row, "Data transformation"),
                    "statistical_test": get_clean(row, "Statistical test"),
                    "significance_threshold": get_clean(row, "Significance threshold"),
                    "mht_correction": get_clean(row, "MHT correction"),
                    "lda_score_above": get_clean(row, "LDA Score above"),
                    "matched_on": get_clean(row, "Matched on"),
                    "confounders_controlled_for": get_clean(row, "Confounders controlled for"),
                    "pielou": get_clean(row, "Pielou"),
                    "shannon": get_clean(row, "Shannon"),
                    "chao1": get_clean(row, "Chao1"),
                    "simpson": get_clean(row, "Simpson"),
                    "inverse_simpson": get_clean(row, "Inverse Simpson"),
                    "richness": get_clean(row, "Richness"),
                }

        # 3. Signature-level data
        signature_id = get_clean(row, "BSDB ID")
        if signature_id and experiment_name:
            experiment_id = f"{study_id}/{experiment_name}"
            if signature_id not in signatures:
                signatures[signature_id] = {
                    "signature_id": signature_id,
                    "experiment_id": experiment_id,
                    "signature_name": get_clean(row, "Signature page name"),
                    "source": get_clean(row, "Source"),
                    "description": get_clean(row, "Description"),
                    "abundance_in_group_1": get_clean(row, "Abundance in Group 1"),
                    "curation_state": get_clean(row, "State"),
                    "curator": get_clean(row, "Curator"),
                    "curated_date": get_clean(row, "Curated date"),
                    "revision_editor": get_clean(row, "Revision editor"),
                    "reviewer": get_clean(row, "Reviewer"),
                }

            # 4. Taxa and signature-taxon relationships
            metaphlan_str = get_clean(row, "MetaPhlAn taxon names")
            ncbi_str = get_clean(row, "NCBI Taxonomy IDs")

            if metaphlan_str or ncbi_str:
                names_list = [name.strip() for name in metaphlan_str.split(",") if name.strip()] if metaphlan_str else []
                ids_list = [nid.strip() for nid in ncbi_str.split(";") if nid.strip()] if ncbi_str else []

                max_len = max(len(names_list), len(ids_list))
                for i in range(max_len):
                    name_item = names_list[i] if i < len(names_list) else ""
                    id_item = ids_list[i] if i < len(ids_list) else ""

                    if "|" in name_item or "|" in id_item:
                        name_parts = [p.strip() for p in name_item.split("|") if p.strip()]
                        id_parts = [p.strip() for p in id_item.split("|") if p.strip()]

                        max_parts = max(len(name_parts), len(id_parts))
                        leaf_id = ""
                        for j in range(max_parts):
                            part_name = name_parts[j] if j < len(name_parts) else ""
                            part_id = id_parts[j] if j < len(id_parts) else ""

                            if part_id:
                                taxa[part_id] = {
                                    "ncbi_id": part_id,
                                    "taxon_name": part_name,
                                }
                                leaf_id = part_id

                        if leaf_id:
                            signatures_taxa.append({
                                "signature_id": signature_id,
                                "ncbi_id": leaf_id,
                            })
                    else:
                        if id_item:
                            taxa[id_item] = {
                                "ncbi_id": id_item,
                                "taxon_name": name_item,
                            }
                            signatures_taxa.append({
                                "signature_id": signature_id,
                                "ncbi_id": id_item,
                            })

    output_dir.mkdir(parents=True, exist_ok=True)

    def write_table(filename: str, fields: list[str], data: list[dict[str, str]]) -> int:
        filepath = output_dir / filename
        with open(filepath, mode="w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in data:
                filtered_r = {k: r.get(k, "") for k in fields}
                writer.writerow(filtered_r)
        return len(data)

    counts = {
        "studies.csv": write_table("studies.csv", STUDY_FIELDS, list(studies.values())),
        "experiments.csv": write_table("experiments.csv", EXPERIMENT_FIELDS, list(experiments.values())),
        "signatures.csv": write_table("signatures.csv", SIGNATURE_FIELDS, list(signatures.values())),
        "taxa.csv": write_table("taxa.csv", TAXON_FIELDS, list(taxa.values())),
        "signatures_taxa.csv": write_table("signatures_taxa.csv", SIGNATURE_TAXA_FIELDS, signatures_taxa),
    }

    return counts
