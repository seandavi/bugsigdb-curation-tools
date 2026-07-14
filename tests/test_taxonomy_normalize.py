"""Parity tests for `bugsigdb_curation.taxonomy.normalize`.

The taxonomy DB computes `name_norm` at build time with the SQL expression
from :func:`name_norm_sql` and matches a query name at lookup time with the
*same* expression (see `build.py` and `db.py::resolve`). This asserts that
SQL expression stays byte-for-byte identical to the Python
:func:`normalize_taxon_name` it mirrors, so the two never drift.

Note: DuckDB's regex engine is RE2, whose `\\s` is ASCII-only
(`[\\t\\n\\f\\r ]`); that's exactly the whitespace real taxon labels contain,
so the ASCII-vs-Unicode difference is immaterial for this data.
"""

from __future__ import annotations

import duckdb
import pytest

from bugsigdb_curation.taxonomy.normalize import name_norm_sql, normalize_taxon_name

# Representative sample: rank prefixes (`g__`/`s_`, plus a case-sensitivity
# probe `G__` that must NOT be stripped), underscores, leading/trailing/
# multiple internal spaces, mixed case, and the empty/whitespace-only strings.
_PARITY_SAMPLE = [
    "Faecalibacterium",
    "g__Faecalibacterium",
    "g_Faecalibacterium",
    "s__Escherichia_coli",
    "s_Escherichia coli",
    "k__Bacteria",
    "  Bacteroides   fragilis  ",
    "Escherichia_coli",
    "MiXeD_CaSe",
    "G__Uppercase",  # case-sensitive prefix regex: uppercase G is NOT a prefix
    "t__strain_xyz",
    "no_prefix_here",
    "",
    "   ",
]


@pytest.mark.parametrize("name", _PARITY_SAMPLE)
def test_name_norm_sql_matches_python_normalize(name: str):
    con = duckdb.connect()
    try:
        (sql_result,) = con.execute(f"SELECT {name_norm_sql('?')}", [name]).fetchone()
    finally:
        con.close()
    assert sql_result == normalize_taxon_name(name)
