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
  candidates exposed".

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
]

EXPECTED_NAMES_ROWS = len(NAMES)
EXPECTED_NODES_ROWS = len(NODES)

#: tax_id constants, named for readability in test assertions.
TAXID_ROOT = 1
TAXID_BACTEROIDES_GENUS = 816
TAXID_BACTEROIDES_FRAGILIS = 817
TAXID_MORGANELLA_A = 500
TAXID_MORGANELLA_B = 600


def _write_names_dmp(path: Path) -> None:
    lines = [f"{tax_id}\t|\t{name_txt}\t|\t{unique_name}\t|\t{name_class}\t|\n" for tax_id, name_txt, unique_name, name_class in NAMES]
    path.write_text("".join(lines), encoding="utf-8")


def _write_nodes_dmp(path: Path) -> None:
    # Real nodes.dmp has a dozen-odd trailing columns (embl code, division
    # id, ...); include a couple of dummy ones to make sure the parser
    # correctly ignores everything past the third field.
    lines = [
        f"{tax_id}\t|\t{parent_tax_id}\t|\t{rank}\t|\t\t|\t0\t|\n" for tax_id, parent_tax_id, rank in NODES
    ]
    path.write_text("".join(lines), encoding="utf-8")


def write_synthetic_taxdump(root: Path) -> Path:
    """Write `names.dmp` + `nodes.dmp` into `root` (created if missing); returns `root`."""
    root.mkdir(parents=True, exist_ok=True)
    _write_names_dmp(root / "names.dmp")
    _write_nodes_dmp(root / "nodes.dmp")
    return root
