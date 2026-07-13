"""Parse BugSigDB's denormalized `full_dump.csv` into nested LinkML-shaped records.

`full_dump.csv` (see :mod:`bugsigdb_curation.export`) has one row per curated
*Signature*, with its parent Study and Experiment columns repeated across every
row that shares them. This module:

1. reads the file, skipping the leading `#` banner comment line (:func:`read_rows`),
2. coerces individual cells to the types/shapes the LinkML schema
   (`schema/bugsigdb.yaml`) expects (ints, floats, bools, enum values, and
   multivalued lists — see the `coerce_*` / `split_*` / `parse_*` helpers), and
3. folds the flat rows into nested `Study -> Experiment -> Signature` dicts
   keyed by PMID (falling back to the `Study` column when PMID is blank) via
   :func:`load_studies`.

This module has no CLI/IO-formatting concerns (those live in
:mod:`bugsigdb_curation.cli`) and no dependency on `linkml`/`pydantic` — the
output is plain dicts/lists whose keys match the schema's slot names, so they
can later be validated structurally (e.g. by a `bugsigdb validate` command).

Column -> slot mapping and delimiter conventions here were derived by
inspecting the real `full_dump.csv` (not just the wiki docs), since the two
occasionally disagree — see the module-level comments next to each mapping
for what was actually observed.
"""

from __future__ import annotations

import csv
import itertools
import re
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

# ---------------------------------------------------------------------------
# blank / coercion helpers
# ---------------------------------------------------------------------------

#: The dump renders every blank cell as the literal string "NA" (not empty).
_BLANK_VALUES = frozenset({"", "NA"})


def is_blank(value: str | None) -> bool:
    """True if `value` is None or a blank cell (empty string or the literal "NA")."""
    return value is None or value.strip() in _BLANK_VALUES


def coerce_int(value: str | None) -> int | None:
    """Parse an integer cell; returns None for blank or unparseable values (never invents 0)."""
    if is_blank(value):
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def coerce_float(value: str | None) -> float | None:
    """Parse a float cell; returns None for blank or unparseable values."""
    if is_blank(value):
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def coerce_bool(value: str | None) -> bool | None:
    """Parse a TRUE/FALSE cell (case-insensitive); returns None for blank/unrecognized values."""
    if is_blank(value):
        return None
    v = value.strip().lower()
    if v in ("true", "yes"):
        return True
    if v in ("false", "no"):
        return False
    return None


def normalize_enum(value: str | None, allowed: Iterable[str]) -> str | None:
    """Validate/normalize a cell against a closed set of permissible enum values.

    Blank cells and values outside `allowed` become None (never invented/guessed);
    a case-insensitive match is re-cased to the canonical spelling in `allowed`.
    """
    if is_blank(value):
        return None
    v = value.strip()
    allowed_set = allowed if isinstance(allowed, (set, frozenset)) else set(allowed)
    if v in allowed_set:
        return v
    lower_map = {a.lower(): a for a in allowed_set}
    return lower_map.get(v.lower())


# ---------------------------------------------------------------------------
# multivalue splitting
# ---------------------------------------------------------------------------
#
# The dump is NOT consistent about how it delimits multivalued cells:
#   * Keywords uses ", " (comma + space) between distinct keywords.
#   * Every other multivalued free-list column (Location of subjects, Body
#     site, Condition, Sequencing platform, Statistical test, Matched on,
#     Confounders controlled for, Study design, Revision editor, Curator)
#     uses a bare "," (NO following space) as the separator, but individual
#     values may themselves *contain* a comma followed by a space (e.g. the
#     country "Korea, Republic of", or the StudyDesignEnum value
#     "cross-sectional observational, not case-control"). So for those
#     columns we split only on a comma that is NOT followed by whitespace.

_STRICT_SPLIT_RE = re.compile(r",(?!\s)")


def split_comma_loose(value: str | None) -> list[str]:
    """Split a "Keywords"-style cell: separator is comma optionally followed by whitespace."""
    if is_blank(value):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def split_comma_strict(value: str | None) -> list[str]:
    """Split a cell whose separator is a bare comma (not followed by whitespace).

    Preserves values that themselves contain ", " (comma+space), e.g. "Korea, Republic of".
    """
    if is_blank(value):
        return []
    return [part.strip() for part in _STRICT_SPLIT_RE.split(value) if part.strip()]


#: An "Initials" token like "A.K.", "R.", or "R.R." — one or more capital
#: letters each followed by a period, with no other characters.
_INITIALS_RE = re.compile(r"^[A-Z]\.(?:\s?[A-Z]\.)*$")
_AND_RE = re.compile(r"\s+and\s+")


def split_authors(value: str | None) -> list[str]:
    """Split an "Authors list" cell into individual author names.

    The dump uses (at least) two conventions, distinguished by a heuristic:

    * PubMed/citation style, comma-separating "Surname" from "Initials" pairs
      and joining the last pair with " and ": e.g.
      "Feehan, A.K., Rose, R. and Lamers, S.L." -> 3 authors. Detected when,
      after normalizing " and " to ", ", every odd-indexed token is an
      initials-only token (see `_INITIALS_RE`); such tokens are then paired
      with the preceding surname token.
    * A simple comma-separated list of already-complete names, e.g.
      "Smith J, Doe A" -> 2 authors (returned as-is; pairing would wrongly
      merge these two authors into one).
    """
    if is_blank(value):
        return []
    normalized = _AND_RE.sub(", ", value.strip())
    tokens = [t.strip() for t in normalized.split(",") if t.strip()]
    if (
        len(tokens) >= 2
        and len(tokens) % 2 == 0
        and all(_INITIALS_RE.match(tokens[i]) for i in range(1, len(tokens), 2))
    ):
        return [f"{tokens[i]} {tokens[i + 1]}" for i in range(0, len(tokens), 2)]
    return tokens


# ---------------------------------------------------------------------------
# "16S variable region" coercion
# ---------------------------------------------------------------------------
#
# The dump stores this as a single column of concatenated region digits, e.g.
# "34" for V3-V4, "123456789" for V1-V9, "4" for a lone V4. The schema splits
# this into two single-digit slots (variable_region_lower_bound/upper_bound),
# so we take the first and last digit of the cell.


def parse_variable_region(value: str | None) -> tuple[int | None, int | None]:
    """Split a "16S variable region" cell into (lower_bound, upper_bound)."""
    if is_blank(value):
        return None, None
    digits = value.strip()
    if not digits.isdigit():
        return None, None
    lower = int(digits[0])
    upper = int(digits[-1]) if len(digits) > 1 else None
    return lower, upper


# ---------------------------------------------------------------------------
# taxa construction
# ---------------------------------------------------------------------------
#
# "MetaPhlAn taxon names" and "NCBI Taxonomy IDs" each encode one or more
# taxa reported for the signature. Within one taxon, the ordered lineage
# (root -> leaf, e.g. kingdom|phylum|...|species) is "|"-delimited in BOTH
# columns with matching segment counts. Between DIFFERENT taxa in the same
# cell, the two columns use DIFFERENT separators: "," in the names column,
# ";" in the ids column. (Confirmed against the real dump: of ~13.5k rows
# with taxa, only 1 had a names/ids segment-count mismatch.)

_RANK_BY_PREFIX = {
    "k": "kingdom",
    "p": "phylum",
    "c": "class",
    "o": "order",
    "f": "family",
    "g": "genus",
    "s": "species",
}
_RANK_PREFIX_RE = re.compile(r"^([a-zA-Z])__(.+)$")


def _parse_one_taxon(name_group: str, id_group: str) -> dict[str, Any] | None:
    """Build one Taxon dict from one taxon's "|"-joined name and id lineage segments."""
    id_segs = [s for s in id_group.split("|") if s]
    if not id_segs:
        return None
    name_segs = [s for s in name_group.split("|") if s] if name_group else []

    taxon: dict[str, Any] = {}
    ncbi_id = coerce_int(id_segs[-1])
    if ncbi_id is not None:
        taxon["ncbi_id"] = ncbi_id

    if name_segs:
        last = name_segs[-1]
        m = _RANK_PREFIX_RE.match(last)
        if m:
            prefix, bare_name = m.group(1).lower(), m.group(2).strip()
            if bare_name:
                taxon["taxon_name"] = bare_name
            rank = _RANK_BY_PREFIX.get(prefix)
            if rank:
                taxon["taxonomic_rank"] = rank
        elif last.strip():
            taxon["taxon_name"] = last.strip()

        if len(name_segs) > 1:
            # Root-down-to-leaf lineage, stripped of rank prefixes.
            lineage = []
            for seg in name_segs:
                m2 = _RANK_PREFIX_RE.match(seg)
                lineage.append(m2.group(2).strip() if m2 else seg.strip())
            taxon["lineage"] = lineage

    return taxon or None


def parse_taxa(names_raw: str | None, ids_raw: str | None) -> list[dict[str, Any]]:
    """Pair "MetaPhlAn taxon names" with "NCBI Taxonomy IDs" into a list of Taxon dicts."""
    if is_blank(ids_raw):
        return []
    id_groups = ids_raw.split(";")
    name_groups = names_raw.split(",") if not is_blank(names_raw) else []
    if len(name_groups) != len(id_groups):
        # Rare real-world mismatch (see module docstring above): fall back to
        # ids only rather than mis-pairing names to the wrong taxon.
        name_groups = [""] * len(id_groups)

    taxa = []
    for name_group, id_group in zip(name_groups, id_groups):
        taxon = _parse_one_taxon(name_group, id_group)
        if taxon:
            taxa.append(taxon)
    return taxa


# ---------------------------------------------------------------------------
# row reading
# ---------------------------------------------------------------------------


def _read_rows(fh: TextIO) -> Iterator[dict[str, str]]:
    first_line = fh.readline()
    if first_line.startswith("#"):
        reader: csv.DictReader = csv.DictReader(fh)
    else:
        # Defensive: no banner present, so the first line IS the header.
        reader = csv.DictReader(itertools.chain([first_line], fh))
    yield from reader


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    """Yield raw string-valued dict rows from `full_dump.csv`, skipping the `#` banner line."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        yield from _read_rows(fh)


# ---------------------------------------------------------------------------
# enum vocabularies used for validation (kept in sync with schema/bugsigdb.yaml)
# ---------------------------------------------------------------------------

ABUNDANCE_DIRECTION_VALUES = frozenset({"increased", "decreased"})
ALPHA_DIVERSITY_VALUES = frozenset({"increased", "decreased", "unchanged"})
STUDY_DESIGN_VALUES = frozenset(
    {
        "case-control",
        "cross-sectional observational, not case-control",
        "time series / longitudinal observational",
        "laboratory experiment",
        "randomized controlled trial",
        "prospective cohort",
        "meta-analysis",
    }
)

_ALPHA_DIVERSITY_COLUMNS = ("Pielou", "Shannon", "Chao1", "Simpson", "Inverse Simpson", "Richness")
_ALPHA_DIVERSITY_SLOTS = ("pielou", "shannon", "chao1", "simpson", "inverse_simpson", "richness")


# ---------------------------------------------------------------------------
# per-level field extraction
# ---------------------------------------------------------------------------


def _set(d: dict[str, Any], key: str, value: Any) -> None:
    """Set `d[key] = value` only when value is non-None/non-empty (never invent blanks)."""
    if value is None:
        return
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return
    d[key] = value


def _study_key(row: dict[str, str]) -> str:
    """Grouping key for a Study: PMID when present, else the raw `Study` column."""
    pmid = row.get("PMID")
    if not is_blank(pmid):
        return pmid.strip()
    return (row.get("Study") or "").strip()


def _extract_study(row: dict[str, str]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    _set(d, "pmid", coerce_int(row.get("PMID")))
    _set(d, "doi", None if is_blank(row.get("DOI")) else row["DOI"].strip())
    _set(d, "uri", None if is_blank(row.get("URL")) else row["URL"].strip())
    _set(d, "authors", split_authors(row.get("Authors list")))
    _set(d, "title", None if is_blank(row.get("Title")) else row["Title"].strip())
    _set(d, "journal", None if is_blank(row.get("Journal")) else row["Journal"].strip())
    _set(d, "year", coerce_int(row.get("Year")))
    _set(d, "keywords", split_comma_loose(row.get("Keywords")))
    designs = [
        v
        for v in (normalize_enum(x, STUDY_DESIGN_VALUES) for x in split_comma_strict(row.get("Study design")))
        if v
    ]
    _set(d, "study_design", designs)
    return d


def _extract_experiment(row: dict[str, str]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    _set(d, "location_of_subjects", split_comma_strict(row.get("Location of subjects")))
    _set(d, "host_species", None if is_blank(row.get("Host species")) else row["Host species"].strip())
    _set(d, "body_site", split_comma_strict(row.get("Body site")))
    _set(d, "condition", split_comma_strict(row.get("Condition")))
    _set(d, "group_0_name", None if is_blank(row.get("Group 0 name")) else row["Group 0 name"].strip())
    _set(d, "group_1_name", None if is_blank(row.get("Group 1 name")) else row["Group 1 name"].strip())
    _set(
        d,
        "group_1_definition",
        None if is_blank(row.get("Group 1 definition")) else row["Group 1 definition"].strip(),
    )
    _set(d, "group_0_sample_size", coerce_int(row.get("Group 0 sample size")))
    _set(d, "group_1_sample_size", coerce_int(row.get("Group 1 sample size")))
    _set(
        d,
        "antibiotics_exclusion",
        None if is_blank(row.get("Antibiotics exclusion")) else row["Antibiotics exclusion"].strip(),
    )
    _set(
        d,
        "sequencing_type",
        None if is_blank(row.get("Sequencing type")) else row["Sequencing type"].strip(),
    )
    lower, upper = parse_variable_region(row.get("16S variable region"))
    _set(d, "variable_region_lower_bound", lower)
    _set(d, "variable_region_upper_bound", upper)
    _set(d, "sequencing_platform", split_comma_strict(row.get("Sequencing platform")))
    _set(
        d,
        "data_transformation",
        None if is_blank(row.get("Data transformation")) else row["Data transformation"].strip(),
    )
    _set(d, "statistical_test", split_comma_strict(row.get("Statistical test")))
    _set(d, "significance_threshold", coerce_float(row.get("Significance threshold")))
    _set(d, "mht_correction", coerce_bool(row.get("MHT correction")))
    _set(d, "lda_score_above", coerce_float(row.get("LDA Score above")))
    _set(d, "matched_on", split_comma_strict(row.get("Matched on")))
    _set(d, "confounders_controlled_for", split_comma_strict(row.get("Confounders controlled for")))
    for column, slot in zip(_ALPHA_DIVERSITY_COLUMNS, _ALPHA_DIVERSITY_SLOTS):
        _set(d, slot, normalize_enum(row.get(column), ALPHA_DIVERSITY_VALUES))
    return d


def _extract_signature(row: dict[str, str]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    _set(d, "source", None if is_blank(row.get("Source")) else row["Source"].strip())
    _set(d, "description", None if is_blank(row.get("Description")) else row["Description"].strip())
    _set(d, "abundance_in_group_1", normalize_enum(row.get("Abundance in Group 1"), ABUNDANCE_DIRECTION_VALUES))
    _set(d, "taxa", parse_taxa(row.get("MetaPhlAn taxon names"), row.get("NCBI Taxonomy IDs")))
    # Curation provenance (CurationProvenance mixin), scoped to the Signature
    # page per this row-block (see header ordering: Curated date/Curator/
    # Revision editor/... /State all sit adjacent to Description/Abundance/taxa).
    _set(d, "curated_date", None if is_blank(row.get("Curated date")) else row["Curated date"].strip())
    _set(d, "curator", split_comma_strict(row.get("Curator")))
    _set(d, "revision_editor", split_comma_strict(row.get("Revision editor")))
    _set(d, "curation_state", normalize_enum(row.get("State"), {"Complete", "Incomplete"}))
    return d


# ---------------------------------------------------------------------------
# grouping
# ---------------------------------------------------------------------------


def load_studies(source: Path | Iterable[dict[str, str]], *, limit: int | None = None) -> list[dict[str, Any]]:
    """Group flat `full_dump.csv` rows into nested Study -> Experiment -> Signature dicts.

    `source` is either a path to the CSV file, or (mainly for testing) an
    iterable of raw row dicts as `read_rows` would yield.

    Studies are keyed by PMID, falling back to the `Study` column when PMID is
    blank. Experiments are keyed within a study by the `Experiment` column.
    Each row contributes exactly one Signature (the dump is one row per
    signature; see module docstring).

    `limit`, if given, stops reading once that many *distinct* studies have
    been started — handy for sampling a prefix of the real 30 MB file without
    parsing all of it. This assumes rows for a given study are not
    interleaved with rows of studies seen after it, which holds for the real
    export (rows are grouped by study).
    """
    rows: Iterable[dict[str, str]] = read_rows(source) if isinstance(source, (str, Path)) else source

    # Keep experiments as an inner dict (keyed by the `Experiment` column)
    # while building, then flatten to a list per the schema's `inlined_as_list`.
    studies: dict[str, dict[str, Any]] = {}

    for row in rows:
        skey = _study_key(row)
        if skey not in studies:
            if limit is not None and len(studies) >= limit:
                break
            study = _extract_study(row)
            study["_experiments"] = {}
            studies[skey] = study
        study = studies[skey]

        ekey = (row.get("Experiment") or "").strip()
        experiments = study["_experiments"]
        if ekey not in experiments:
            experiment = _extract_experiment(row)
            experiment["signatures"] = []
            experiments[ekey] = experiment
        experiment = experiments[ekey]

        experiment["signatures"].append(_extract_signature(row))

    result: list[dict[str, Any]] = []
    for study in studies.values():
        study = dict(study)
        study["experiments"] = list(study.pop("_experiments").values())
        result.append(study)
    return result


def summarize(studies: list[dict[str, Any]]) -> tuple[int, int, int]:
    """Return (n_studies, n_experiments, n_signatures) for a list of loaded studies."""
    n_studies = len(studies)
    experiments = [e for s in studies for e in s.get("experiments", [])]
    n_experiments = len(experiments)
    n_signatures = sum(len(e.get("signatures", [])) for e in experiments)
    return n_studies, n_experiments, n_signatures
