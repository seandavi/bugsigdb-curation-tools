"""Name -> NCBI taxid resolution, so scoring lands on taxid **sets**, not
string luck.

Resolution order (never guesses -- unresolved is tracked, not invented):

1. an exact/normalized-name hit in the on-disk JSON cache (persists across
   runs, so expensive/manual resolutions are paid once and reused -- mirrors
   the plan's "resolved once, cached, reused across the corpus" design);
2. the local, general NCBI `TaxonomyDB` (`bugsigdb_curation.taxonomy`, built
   from the public taxdump -- **not** gold), tried offline before any
   network call (PR-2: replaces the old `taxa.csv`-derived seed map -- a
   firewall-cleanliness win, since gold is no longer read to resolve a
   *prediction's* taxon names, only to load the gold taxid sets themselves);
3. optional NCBI E-utilities gap-fill (`resolve_name_online`), which is never
   required for the offline scoring path and is only exercised by
   `@pytest.mark.network` tests / an explicit opt-in CLI pass.

A prediction taxon that already carries an integer `ncbi_id` is used
directly -- no name resolution needed, since it's already the kind of value
S6 (the authority-verified normalization stage) is meant to produce.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from bugsigdb_curation.taxonomy.db import TaxonomyDB
from bugsigdb_curation.taxonomy.paths import resolve_optional_db_path

#: Rank prefixes appear double-underscored (MetaPhlAn, "g__Bacillus") or
#: single-underscored (LEfSe figure labels, "g_Bacillus"); strip either form.
#: Mirrors `benchmarks/figure-extraction/score.py::normalize`. Deliberately
#: duplicated from `bugsigdb_curation.taxonomy.normalize` rather than
#: imported (unlike `curator.taxonomy`, PR-2 scoped that dedup to the
#: curator side only) -- this module is the gold-aware side of the data
#: firewall (§6e) and keeps its own normalization self-contained rather than
#: adding a shared runtime dependency across that boundary.
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
    """A name->taxid resolver backed by the local `TaxonomyDB` and a persistent cache.

    `db` (PR-2, general NCBI taxdump -- not gold) and `cache` (the on-disk
    JSON file) are both consulted by normalized name; `cache` takes priority
    so a manually-corrected or network-gap-filled resolution can override the
    DB's value for the same normalized name -- this is also how two synonyms
    (e.g. "Propionibacterium acnes" / "Cutibacterium acnes", which the local
    DB itself already unifies via NCBI's own synonym rows, but a corpus
    export might still spell either way) get reconciled to one taxid once
    seeded into the cache.
    """

    #: A local, offline NCBI taxonomy DB (general taxdump, not gold);
    #: `None` means offline resolution never has a local hit (falls straight
    #: to `.unresolved`, same as an empty seed map did pre-PR-2).
    db: TaxonomyDB | None = None
    cache: dict[str, int | None] = field(default_factory=dict)
    cache_path: Path | None = None
    #: Normalized names that failed to resolve via `resolve_name` (offline).
    unresolved: set[str] = field(default_factory=set)
    #: normalized-name reverse lookup, keyed by tax_id -- lets a resolved
    #: taxid be turned back into a display/genus-lenient name even when a
    #: prediction only supplied an id. Populated lazily from `db` (PR-2:
    #: no longer bulk-preloaded from `taxa.csv`); tests may also seed it
    #: directly for a resolver built with no `db` at all.
    id_to_name: dict[int, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        *,
        cache_path: Path | None = DEFAULT_CACHE_PATH,
        db_path: Path | str | None = None,
        db_release: str | None = None,
        db: TaxonomyDB | None = None,
    ) -> TaxonomyResolver:
        """Build a resolver from a local `TaxonomyDB` and a JSON cache file.

        Both sources are optional: `db`, if not given, is resolved via
        `db_path`/`db_release` (CLI flag -> `BUGSIGDB_TAXONOMY_DB` ->
        `db_release`'s default cache path (if given) -> newest cached
        `ncbi-taxdump-*.duckdb` -> none, mirroring
        `curator.taxonomy.NcbiTaxonomyResolver.load`'s resolution) and stays
        `None` (fine for tests / a machine with no DB built yet -- offline
        resolution then always misses) if nothing resolves or the resolved
        path fails to open. A missing/absent `cache_path` yields an empty
        cache (nothing has been resolved yet).
        """
        if db is None:
            resolved_path = resolve_optional_db_path(db_path, db_release)
            if resolved_path is not None and resolved_path.exists():
                try:
                    db = TaxonomyDB(resolved_path)
                except (FileNotFoundError, ValueError):
                    db = None

        cache: dict[str, int | None] = {}
        if cache_path is not None and Path(cache_path).exists():
            raw_cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            cache = {k: (int(v) if v is not None else None) for k, v in raw_cache.items()}

        return cls(
            db=db,
            cache=cache,
            cache_path=Path(cache_path) if cache_path is not None else None,
        )

    def resolve_name(self, name: str) -> int | None:
        """Resolve a bare taxon-name string to an NCBI taxid, offline only.

        Cache hit (including a cached "confirmed unresolved" `None`) wins
        over the local `TaxonomyDB`. A DB hit is cached (so a repeat lookup
        is free) and also backfills `id_to_name`. Returns `None` and records
        the normalized name in `.unresolved` if neither has it -- this
        method never touches the network; see `resolve_name_online` for the
        gap-fill path.
        """
        norm = normalize_taxon_name(name)
        if norm in self.cache:
            hit = self.cache[norm]
            if hit is None:
                self.unresolved.add(norm)
            return hit
        if self.db is not None:
            resolution = self.db.resolve(name)
            if resolution is not None:
                self.cache[norm] = resolution.tax_id
                self.id_to_name.setdefault(resolution.tax_id, norm)
                self.unresolved.discard(norm)
                return resolution.tax_id
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

    def name_of_id(self, ncbi_id: int) -> str | None:
        """Normalized name for a resolved taxid, via `id_to_name` (populated
        by a prior `resolve_name` hit, or seeded directly e.g. by a test),
        falling back to the local `TaxonomyDB`'s scientific name (PR-2:
        replaces the old bulk `taxa.csv` reverse map -- this now works for
        *any* tax_id the local DB knows, not just the corpus's own curated
        set). Returns `None` if neither has it (e.g. no `db` configured and
        this id was never resolved/seeded)."""
        if ncbi_id in self.id_to_name:
            return self.id_to_name[ncbi_id]
        if self.db is not None:
            scientific_name = self.db.scientific_name(ncbi_id)
            if scientific_name is not None:
                norm = normalize_taxon_name(scientific_name)
                self.id_to_name[ncbi_id] = norm
                return norm
        return None

    def genus_of_id(self, ncbi_id: int) -> str | None:
        """Genus token for a taxid, via `name_of_id`'s reverse name lookup.

        This is a *string* genus token (first word of the resolved name),
        not a true taxonomic-rank lookup -- mirrors the same limitation
        `benchmarks/figure-extraction/score.py::genus_of` documents. Returns
        `None` for an id this resolver can't find a name for at all (e.g. no
        `db` configured and the id was never resolved/seeded).
        """
        name = self.name_of_id(ncbi_id)
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
