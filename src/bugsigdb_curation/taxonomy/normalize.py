"""Taxon-name normalization, shared by `build.py` (writing `name_norm`) and
`db.py` (normalizing a query name before the lookup).

Deliberately mirrors `bugsigdb_curation.curator.taxonomy.normalize_taxon_name`
field-for-field (lowercase, strip a leading rank prefix, underscores -> spaces,
collapse whitespace, strip) so a name normalizes the same way whether it's
resolved via the live E-utilities path or this local DB. Duplicated rather
than imported -- matching this codebase's existing convention (see that
module's docstring) of keeping each resolver's normalization self-contained
rather than sharing a runtime dependency across package boundaries.
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
