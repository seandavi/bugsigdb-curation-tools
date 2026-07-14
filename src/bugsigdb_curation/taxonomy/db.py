"""Read-only lookups against a built taxonomy `.duckdb` (see `build.py`).

`TaxonomyDB` is cheap to open (DuckDB reads the file lazily) and safe to
reuse across many lookups within a process -- open once, call `resolve`/
`lineage`/`rank`/`genus_of` as many times as needed, then `close()` (or use
it as a context manager).

**Ambiguity policy** (never guess): a name may map to more than one
`tax_id` (a true homonym across taxa) or multiple rows for the same
`tax_id` (a scientific name plus synonyms). `resolve()` always prefers a
`name_class == "scientific name"` row; if that still leaves more than one
distinct `tax_id`, it returns a deterministic pick (the smallest `tax_id`
among the scientific-name rows) but sets `ambiguous=True` and populates
`candidates` with the full sorted set of distinct `tax_id`s that matched,
so a future disambiguation step has everything it needs without re-querying.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb

from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name

#: The name_class NCBI uses for a taxon's canonical scientific name; every
#: other class (synonym, common name, authority, etc.) is a lower-priority
#: alias of the same tax_id.
SCIENTIFIC_NAME_CLASS = "scientific name"

#: The rank string NCBI's nodes.dmp uses for a genus-level node.
GENUS_RANK = "genus"


@dataclass(frozen=True)
class Resolution:
    """The result of resolving a taxon name to an NCBI tax_id."""

    tax_id: int
    matched_name_txt: str
    name_class: str
    rank: str | None
    ambiguous: bool = False
    candidates: tuple[int, ...] = ()


class TaxonomyDB:
    """A read-only handle onto a built taxonomy `.duckdb`."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"taxonomy DB not found: {self.path}")
        self._con = duckdb.connect(str(self.path), read_only=True)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> TaxonomyDB:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- meta -----------------------------------------------------------

    def meta(self) -> dict[str, str]:
        """The `meta` table as a `{key: value}` dict (release, source, row counts, checksums, ...)."""
        rows = self._con.execute("SELECT key, value FROM meta").fetchall()
        return dict(rows)

    # -- name resolution --------------------------------------------------

    def resolve(self, name: str) -> Resolution | None:
        """Resolve a taxon name to an NCBI tax_id, or `None` if nothing matches.

        See the module docstring for the ambiguity policy.
        """
        norm = normalize_taxon_name(name)
        rows = self._con.execute(
            "SELECT tax_id, name_txt, name_class FROM names WHERE name_norm = ? ORDER BY tax_id, name_class",
            [norm],
        ).fetchall()
        if not rows:
            return None

        candidate_tax_ids = tuple(sorted({r[0] for r in rows}))
        ambiguous = len(candidate_tax_ids) > 1

        scientific_rows = [r for r in rows if r[2] == SCIENTIFIC_NAME_CLASS]
        pool = scientific_rows if scientific_rows else rows
        chosen_tax_id, chosen_name_txt, chosen_name_class = min(pool, key=lambda r: (r[0], r[1]))

        return Resolution(
            tax_id=chosen_tax_id,
            matched_name_txt=chosen_name_txt,
            name_class=chosen_name_class,
            rank=self.rank(chosen_tax_id),
            ambiguous=ambiguous,
            candidates=candidate_tax_ids,
        )

    # -- lineage / rank -----------------------------------------------------

    def rank(self, tax_id: int) -> str | None:
        """The rank string for `tax_id` (e.g. `"genus"`, `"species"`), or `None` if unknown."""
        row = self._con.execute("SELECT rank FROM nodes WHERE tax_id = ?", [tax_id]).fetchone()
        return row[0] if row is not None else None

    def scientific_name(self, tax_id: int) -> str | None:
        """The `"scientific name"`-class name for `tax_id`, or `None` if unknown."""
        row = self._con.execute(
            "SELECT name_txt FROM names WHERE tax_id = ? AND name_class = ? ORDER BY name_txt LIMIT 1",
            [tax_id, SCIENTIFIC_NAME_CLASS],
        ).fetchone()
        return row[0] if row is not None else None

    def _ancestor_chain(self, tax_id: int) -> Iterator[tuple[int, str | None]]:
        """Yield `(tax_id, rank)` starting at `tax_id` itself and walking up
        `parent_tax_id` to the root (a node whose `parent_tax_id` equals its
        own `tax_id`, as NCBI's root node does). A visited-set guards against
        a malformed/cyclic taxdump rather than looping forever."""
        visited: set[int] = set()
        current = tax_id
        while current is not None and current not in visited:
            visited.add(current)
            row = self._con.execute(
                "SELECT tax_id, parent_tax_id, rank FROM nodes WHERE tax_id = ?", [current]
            ).fetchone()
            if row is None:
                return
            node_tax_id, parent_tax_id, node_rank = row
            yield node_tax_id, node_rank
            if parent_tax_id == node_tax_id:
                return
            current = parent_tax_id

    def lineage(self, tax_id: int) -> list[tuple[int, str | None, str | None]]:
        """The full ancestry of `tax_id`, root-first and the queried taxon last:
        `[(tax_id, rank, name), ...]`. Empty if `tax_id` isn't in `nodes`."""
        chain = list(self._ancestor_chain(tax_id))
        chain.reverse()
        return [(t, r, self.scientific_name(t)) for t, r in chain]

    def genus_of(self, tax_id: int) -> int | None:
        """The nearest ancestor of `tax_id` (inclusive of `tax_id` itself) with
        rank `"genus"`, or `None` if none is found walking up to the root."""
        for node_tax_id, node_rank in self._ancestor_chain(tax_id):
            if node_rank == GENUS_RANK:
                return node_tax_id
        return None
