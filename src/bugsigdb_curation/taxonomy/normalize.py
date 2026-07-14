"""Taxon-name normalization, shared by `build.py` (writing `name_norm`) and
`db.py` (normalizing a query name before the lookup).

`bugsigdb_curation.curator.taxonomy.normalize_taxon_name` (S6) imports this
function directly (PR-2: single Python source of truth, since `curator.taxonomy`
now also resolves through this package's local `TaxonomyDB`) -- so a name
normalizes identically whether it's resolved via the live E-utilities path or
the local DB. `bugsigdb_curation.eval.taxonomy` keeps its own copy rather than
importing this one: it's the gold-aware side of the data firewall (§6e), and
this taxonomy package -- while itself gold-free -- is deliberately kept
import-agnostic between the curator and eval packages rather than becoming a
third place either side has to trust not to (someday) grow a gold dependency.
"""

from __future__ import annotations

import re

#: Rank prefixes appear double-underscored (MetaPhlAn, "g__Bacillus") or
#: single-underscored (LEfSe figure labels, "g_Bacillus"); strip either form.
_RANK_PREFIX = re.compile(r"^[kdpcofgst]__?")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")


def normalize_taxon_name(name: str) -> str:
    """Normalize a taxon label for lookup/comparison.

    Strips a MetaPhlAn/LEfSe rank prefix, replaces underscores with spaces,
    collapses whitespace, and lowercases -- e.g. "g__Faecalibacterium" and
    "Faecalibacterium" both normalize to "faecalibacterium".
    """
    n = name.strip()
    n = _RANK_PREFIX.sub("", n)
    n = n.replace("_", " ")
    n = _WHITESPACE_OR_UNDERSCORE.sub(" ", n)
    return n.strip().lower()


def name_norm_sql(expr: str) -> str:
    """Return a DuckDB SQL expression normalizing `expr` exactly as
    :func:`normalize_taxon_name` does, so `name_norm` is computed identically
    at build time (over `read_csv` columns) and at query time (over the bound
    query name) -- build == query by construction, zero drift.

    Translates :func:`normalize_taxon_name`'s steps in the same order:

    1. ``trim(expr)``                              -- ``name.strip()``
    2. strip a leading rank prefix (case-sensitive, leading-anchored, so it
       mirrors the ``^[kdpcofgst]__?`` Python regex applied *before*
       lowercasing) -- ``_RANK_PREFIX.sub("", n)``
    3. ``replace('_', ' ')``                       -- ``n.replace("_", " ")``
    4. collapse ``[\\s_]+`` runs to a single space -- ``_WHITESPACE_OR_UNDERSCORE.sub``
    5. ``lower(trim(...))``                         -- ``n.strip().lower()``

    ``expr`` is spliced verbatim, so pass a column name (``"name_txt"``) or a
    bind placeholder (``"?"``) -- never untrusted SQL. DuckDB's regex engine
    is RE2, whose ``\\s`` is ASCII-only ``[\\t\\n\\f\\r ]``; that matches
    Python's behavior for the ASCII whitespace taxon names actually contain.
    """
    stripped = f"trim({expr})"
    deprefixed = rf"regexp_replace({stripped}, '^[kdpcofgst]__?', '')"
    underscored = rf"replace({deprefixed}, '_', ' ')"
    collapsed = rf"regexp_replace({underscored}, '[\s_]+', ' ', 'g')"
    return rf"lower(trim({collapsed}))"
