"""Shared synthetic-taxdump fixture for `test_taxonomy_*.py`.

A tiny, hand-built `names.dmp`/`nodes.dmp` pair (real NCBI `.dmp` line
format: fields joined by `"\\t|\\t"`, each row terminated by `"\\t|\\n"")
covering every case the taxonomy subpackage's tests need:

- a scientific name (`Bacteroides`, tax_id 816) and its full genus->species
  lineage down to a synthetic root (tax_id 1, self-parented);
- a synonym (`Bacteroidus`) pointing at the *same* tax_id as the scientific
  name (816) -- the crux case for the resolver's ambiguity policy;
- a rank-prefixed label (`g__Bacteroides`) exercised via the *query* side
  (names.dmp itself never contains MetaPhlAn/LEfSe-style prefixes -- only a
  predicted taxon label would);
- a homonym: two distinct tax_ids (500, 600) both carrying the scientific
  name `Morganella`, to exercise "ambiguous -> deterministic pick +
  candidates exposed" for the SAME-class (`scientific name`/`scientific
  name`) collision;
- a cross-class homonym: tax_id 850 (`Alcaligenes`, scientific name) also
  carries a *synonym* `Providencia`, which collides with tax_id 860's
  *scientific name* `Providencia`. 850 < 860, so a naive
  smallest-tax_id-wins tie-break would (wrongly) pick 850's synonym row;
  the scientific-name-preferred policy must still pick 860. This is the
  case a same-class-only homonym fixture can't catch.
- PR-2's real-world regression set: `Firmicutes` (phylum), `Faecalibacterium`
  (genus -- also exercised query-side as `g__Faecalibacterium`, a
  MetaPhlAn-style rank-prefixed label), and `Cutibacterium acnes` (species)
  carrying the synonym `Propionibacterium acnes` -- the exact taxa a real
  `curate --smoke` run 429'd on before the local `TaxonomyDB` was wired in
  (see `tests/test_taxonomy_wiring.py`), plus the reclassification-synonym
  case the curator's/scorer's caches alone can't unify without this DB.

Not a test module itself (no `test_` prefix) -- pytest won't collect it.
"""

from __future__ import annotations

from pathlib import Path

# -- nodes: (tax_id, parent_tax_id, rank) ------------------------------------
NODES: list[tuple[int, int, str]] = [
    (1, 1, "no rank"),  # root, self-parented
    (2, 1, "superkingdom"),  # Bacteria
    (200, 2, "phylum"),  # Bacteroidota
    (816, 200, "genus"),  # Bacteroides
    (817, 816, "species"),  # Bacteroides fragilis
    (500, 2, "genus"),  # Morganella (homonym A)
    (600, 2, "genus"),  # Morganella (homonym B -- contrived duplicate name)
    (850, 2, "genus"),  # Alcaligenes (cross-class homonym A: has a synonym "Providencia")
    (860, 2, "genus"),  # Providencia (cross-class homonym B: scientific name "Providencia")
    (1239, 2, "phylum"),  # Firmicutes
    (216851, 1239, "genus"),  # Faecalibacterium
    (1912216, 2, "genus"),  # Cutibacterium
    (1747, 1912216, "species"),  # Cutibacterium acnes (has synonym "Propionibacterium acnes")
]

# -- names: (tax_id, name_txt, unique_name, name_class) ----------------------
NAMES: list[tuple[int, str, str, str]] = [
    (1, "root", "", "scientific name"),
    (2, "Bacteria", "", "scientific name"),
    (200, "Bacteroidota", "", "scientific name"),
    (816, "Bacteroides", "", "scientific name"),
    (816, "Bacteroidus", "", "synonym"),  # synonym -> same tax_id as 816's scientific name
    (817, "Bacteroides fragilis", "", "scientific name"),
    (500, "Morganella", "", "scientific name"),
    (600, "Morganella", "", "scientific name"),
    (850, "Alcaligenes", "", "scientific name"),
    (850, "Providencia", "", "synonym"),  # cross-class collision with 860's scientific name below
    (860, "Providencia", "", "scientific name"),
    (1239, "Firmicutes", "", "scientific name"),
    (216851, "Faecalibacterium", "", "scientific name"),
    (1912216, "Cutibacterium", "", "scientific name"),
    (1747, "Cutibacterium acnes", "", "scientific name"),
    (1747, "Propionibacterium acnes", "", "synonym"),  # reclassification synonym -> same tax_id as 1747
]

EXPECTED_NAMES_ROWS = len(NAMES)
EXPECTED_NODES_ROWS = len(NODES)

#: tax_id constants, named for readability in test assertions.
TAXID_ROOT = 1
TAXID_BACTEROIDES_GENUS = 816
TAXID_BACTEROIDES_FRAGILIS = 817
TAXID_MORGANELLA_A = 500
TAXID_MORGANELLA_B = 600
TAXID_ALCALIGENES_WITH_PROVIDENCIA_SYNONYM = 850
TAXID_PROVIDENCIA_SCIENTIFIC = 860
TAXID_FIRMICUTES = 1239
TAXID_FAECALIBACTERIUM = 216851
TAXID_CUTIBACTERIUM_GENUS = 1912216
TAXID_CUTIBACTERIUM_ACNES = 1747


def _write_names_dmp(path: Path) -> None:
    lines = [f"{tax_id}\t|\t{name_txt}\t|\t{unique_name}\t|\t{name_class}\t|\n" for tax_id, name_txt, unique_name, name_class in NAMES]
    path.write_text("".join(lines), encoding="utf-8")


def _write_nodes_dmp(path: Path) -> None:
    # Real nodes.dmp has 13 fields per row (tax_id, parent_tax_id, rank, then 10
    # trailing columns: embl_code, division_id, inherited_div, genetic_code,
    # inherited_gc, mito_code, inherited_mgc, genbank_hidden, hidden_subtree,
    # comments). We emit the full width here on purpose: a `\t|\t`-joined 13-field
    # row tab-splits into 26 columns, so DuckDB's `read_csv` zero-pads its
    # positional column names to 2 digits (`column00`...) -- the exact condition
    # that broke a `column4`-style projection on the real dump but not on a
    # narrower fixture. The builder must ignore everything past the third field.
    trailing = "\t|\t".join([""] * 9 + ["0"])  # 10 dummy trailing fields
    lines = [
        f"{tax_id}\t|\t{parent_tax_id}\t|\t{rank}\t|\t{trailing}\t|\n"
        for tax_id, parent_tax_id, rank in NODES
    ]
    path.write_text("".join(lines), encoding="utf-8")


def write_synthetic_taxdump(root: Path) -> Path:
    """Write `names.dmp` + `nodes.dmp` into `root` (created if missing); returns `root`."""
    root.mkdir(parents=True, exist_ok=True)
    _write_names_dmp(root / "names.dmp")
    _write_nodes_dmp(root / "nodes.dmp")
    return root


def write_malformed_taxdump_duplicate_node_pk(root: Path) -> Path:
    """Like `write_synthetic_taxdump`, but `nodes.dmp` has an extra row re-using
    an already-present `tax_id` -- a `nodes(tax_id PRIMARY KEY, ...)`
    violation partway through a build, for exercising build-failure/atomicity
    behavior (`_build_from_files` must not corrupt/replace a good `out_path`
    when this blows up mid-build)."""
    root.mkdir(parents=True, exist_ok=True)
    _write_names_dmp(root / "names.dmp")
    _write_nodes_dmp(root / "nodes.dmp")
    with (root / "nodes.dmp").open("a", encoding="utf-8") as fh:
        # Re-uses TAXID_BACTEROIDES_GENUS (816), already written above.
        fh.write(f"{TAXID_BACTEROIDES_GENUS}\t|\t2\t|\tgenus\t|\t\t|\t0\t|\n")
    return root
