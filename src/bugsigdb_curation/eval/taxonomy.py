"""Name -> NCBI taxid resolution, so scoring lands on taxid **sets**, not
string luck.

Resolution order (never guesses -- unresolved is tracked, not invented):

1. an exact/normalized-name hit in the on-disk JSON cache (persists across
   runs, so expensive/manual resolutions are paid once and reused -- mirrors
   the plan's "resolved once, cached, reused across the corpus" design);
2. the bundled seed map built from `taxa.csv` (name -> ncbi_id), so the
   corpus's own ~9k curated taxa resolve fully offline;
3. optional NCBI E-utilities gap-fill (`resolve_name_online`), which is never
   required for the offline scoring path and is only exercised by
   `@pytest.mark.network` tests / an explicit opt-in CLI pass.

A prediction taxon that already carries an integer `ncbi_id` is used
directly -- no name resolution needed, since it's already the kind of value
S6 (the authority-verified normalization stage) is meant to produce.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

#: Rank prefixes appear double-underscored (MetaPhlAn, "g__Bacillus") or
#: single-underscored (LEfSe figure labels, "g_Bacillus"); strip either form.
#: Mirrors `benchmarks/figure-extraction/score.py::normalize`.
_RANK_PREFIX = re.compile(r"^[kdpcofgst]__?")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")

DEFAULT_CACHE_PATH = Path("data/eval/taxonomy_cache.json")

NCBI_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


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


def genus_token(normalized_name: str) -> str:
    """Genus token of an already-normalized name (its first word)."""
    return normalized_name.split(" ")[0] if normalized_name else ""


@dataclass
class TaxonomyResolver:
    """A name->taxid resolver backed by a corpus seed map and a persistent cache.

    `seed` (built from `taxa.csv`) and `cache` (the on-disk JSON file) are
    both keyed by `normalize_taxon_name(...)`. `cache` takes priority so a
    manually-corrected or network-gap-filled resolution can override the
    seed map's value for the same normalized name -- this is also how two
    synonyms (e.g. "Propionibacterium acnes" / "Cutibacterium acnes", which
    the seed map alone cannot unify since it only knows the corpus's own
    curated spelling) get reconciled to one taxid once seeded into the cache.
    """

    seed: dict[str, int] = field(default_factory=dict)
    cache: dict[str, int | None] = field(default_factory=dict)
    cache_path: Path | None = None
    #: Normalized names that failed to resolve via `resolve_name` (offline).
    unresolved: set[str] = field(default_factory=set)
    #: normalized name -> normalized name, built from `taxa.csv`, letting
    #: predicted/gold ncbi_ids be turned back into a display/genus-lenient
    #: name even when the prediction only supplied an id.
    id_to_name: dict[int, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        *,
        taxa_csv: Path | None = None,
        cache_path: Path | None = DEFAULT_CACHE_PATH,
    ) -> TaxonomyResolver:
        """Build a resolver from `taxa.csv` (the seed map) and a JSON cache file.

        Both sources are optional: a missing `taxa_csv` yields an empty seed
        (fine for tests), and a missing/absent `cache_path` yields an empty
        cache (nothing has been resolved yet).
        """
        seed: dict[str, int] = {}
        id_to_name: dict[int, str] = {}
        if taxa_csv is not None and Path(taxa_csv).exists():
            with Path(taxa_csv).open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = (row.get("taxon_name") or "").strip()
                    ncbi_raw = (row.get("ncbi_id") or "").strip()
                    if not name or not ncbi_raw.isdigit():
                        continue
                    ncbi_id = int(ncbi_raw)
                    norm = normalize_taxon_name(name)
                    seed[norm] = ncbi_id
                    id_to_name.setdefault(ncbi_id, norm)

        cache: dict[str, int | None] = {}
        if cache_path is not None and Path(cache_path).exists():
            raw_cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            cache = {k: (int(v) if v is not None else None) for k, v in raw_cache.items()}

        return cls(
            seed=seed,
            cache=cache,
            cache_path=Path(cache_path) if cache_path is not None else None,
            id_to_name=id_to_name,
        )

    def resolve_name(self, name: str) -> int | None:
        """Resolve a bare taxon-name string to an NCBI taxid, offline only.

        Cache hit (including a cached "confirmed unresolved" `None`) wins
        over the seed map. Returns `None` and records the normalized name in
        `.unresolved` if neither has it -- this method never touches the
        network; see `resolve_name_online` for the gap-fill path.
        """
        norm = normalize_taxon_name(name)
        if norm in self.cache:
            hit = self.cache[norm]
            if hit is None:
                self.unresolved.add(norm)
            return hit
        if norm in self.seed:
            return self.seed[norm]
        self.unresolved.add(norm)
        return None

    def resolve_taxon(self, taxon: dict[str, Any]) -> int | None:
        """Resolve a prediction taxon dict (loader nested-shape `Taxon`).

        A taxon that already carries an integer (or numeric-string)
        `ncbi_id` is used as-is -- it's already verified/authoritative, no
        name lookup needed. Otherwise resolves `taxon_name` via
        `resolve_name`.
        """
        ncbi_id = taxon.get("ncbi_id")
        if isinstance(ncbi_id, bool):  # bool is an int subclass; exclude explicitly
            return None
        if isinstance(ncbi_id, int):
            return ncbi_id
        if isinstance(ncbi_id, str) and ncbi_id.strip().isdigit():
            return int(ncbi_id.strip())
        name = taxon.get("taxon_name")
        if not name:
            return None
        return self.resolve_name(name)

    def genus_of_id(self, ncbi_id: int) -> str | None:
        """Genus token for a taxid, via the reverse `taxa.csv` name lookup.

        This is a *string* genus token (first word of the corpus's own name
        for that id), not a true taxonomic-rank lookup -- the corpus export
        carries no rank field, so this mirrors the same limitation
        `benchmarks/figure-extraction/score.py::genus_of` documents. Returns
        `None` for an id this resolver has never seen a name for (e.g. an
        id-only prediction outside the corpus's ~9k seed taxa).
        """
        name = self.id_to_name.get(ncbi_id)
        return genus_token(name) if name else None

    def add_resolution(self, name: str, ncbi_id: int | None) -> None:
        """Record a resolution (e.g. from `resolve_name_online`) into the
        in-memory cache; call `save_cache()` to persist it to disk."""
        norm = normalize_taxon_name(name)
        self.cache[norm] = ncbi_id
        self.unresolved.discard(norm)

    def save_cache(self, path: Path | None = None) -> None:
        """Persist the in-memory cache to `path` (default: `self.cache_path`).

        No-op if neither is set (e.g. a resolver built for a one-off/test
        run with no cache file configured).
        """
        target = Path(path) if path is not None else self.cache_path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")

    async def resolve_name_online(self, name: str, client: httpx.AsyncClient) -> int | None:
        """NCBI E-utilities gap-fill: `esearch` the taxonomy db for `name`.

        Never required for the offline scoring path (`resolve_name` /
        `resolve_taxon` never call this). Caches whatever it finds (including
        a confirmed miss, as `None`) via `add_resolution` so a repeat lookup
        for the same name is free. Only exercised by
        `@pytest.mark.network`-marked tests or an explicit opt-in gap-fill
        pass -- never by `bugsigdb eval score`'s default offline path.
        """
        norm = normalize_taxon_name(name)
        if norm in self.cache:
            return self.cache[norm]
        response = await client.get(
            NCBI_ESEARCH_URL,
            params={"db": "taxonomy", "term": name, "retmode": "json"},
        )
        response.raise_for_status()
        data = response.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
        taxid = int(ids[0]) if ids else None
        self.add_resolution(name, taxid)
        return taxid
