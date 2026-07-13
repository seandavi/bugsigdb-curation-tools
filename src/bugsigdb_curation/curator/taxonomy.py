"""S6 -- NCBI taxonomy normalization: a GENERAL NCBI authority, verification-only.

Per the workflow plan (§3, §6e): an LLM (S5b) may only *propose* a taxon name
and an `ncbi_id`; the identifier is only ever kept if this module's authority
lookup independently confirms it. This is deliberately **not**
`bugsigdb_curation.eval.taxonomy` -- that resolver seeds itself from the
gold `taxa.csv` (the curated corpus's own taxa set), which would leak which
taxa the human curators actually kept. This resolver only ever talks to the
live NCBI E-utilities taxonomy database and its own cache file
(`data/curator/ncbi_taxonomy_cache.json` by default, distinct from the eval
harness's `data/eval/taxonomy_cache.json`) -- never a relational CSV, never
the eval package.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

#: Rank prefixes appear double-underscored (MetaPhlAn, "g__Bacillus") or
#: single-underscored (LEfSe figure labels, "g_Bacillus"); strip either form.
#: (Deliberately duplicated from `bugsigdb_curation.eval.taxonomy` rather
#: than imported -- see this module's docstring on the firewall boundary.)
_RANK_PREFIX = re.compile(r"^[kdpcofgst]__?")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")

DEFAULT_CACHE_PATH = Path("data/curator/ncbi_taxonomy_cache.json")

NCBI_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"


def normalize_taxon_name(name: str) -> str:
    """Normalize a taxon label for lookup/comparison (see module docstring)."""
    n = name.strip()
    n = _RANK_PREFIX.sub("", n)
    n = n.replace("_", " ")
    n = _WHITESPACE_OR_UNDERSCORE.sub(" ", n)
    return n.strip().lower()


@dataclass
class NcbiTaxonomyResolver:
    """A name -> NCBI taxid resolver backed only by live E-utilities + a cache file.

    `cache` is keyed by `normalize_taxon_name(...)`; a cached `None` means
    "confirmed unresolved" (not "never looked up"), so a repeat lookup for a
    name NCBI doesn't recognize is free rather than re-hitting the network
    every call. Never seeded from any corpus/gold file -- every entry either
    came from a live esearch or was persisted from a previous run of this
    same resolver.
    """

    cache: dict[str, int | None] = field(default_factory=dict)
    cache_path: Path | None = DEFAULT_CACHE_PATH
    #: Normalized names esearch confirmed have no taxonomy-db hit.
    unresolved: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, *, cache_path: Path | None = DEFAULT_CACHE_PATH) -> NcbiTaxonomyResolver:
        """Build a resolver from a JSON cache file (missing/absent -> empty cache)."""
        cache: dict[str, int | None] = {}
        if cache_path is not None and Path(cache_path).exists():
            raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            cache = {k: (int(v) if v is not None else None) for k, v in raw.items()}
        return cls(cache=cache, cache_path=Path(cache_path) if cache_path is not None else None)

    async def resolve_name(self, name: str, *, client: httpx.AsyncClient) -> int | None:
        """Resolve a bare taxon-name string to an NCBI taxid via live esearch.

        Cache hit (including a cached "confirmed unresolved" `None`) short-
        circuits without a network call. Never guesses: returns `None` (and
        records the normalized name in `.unresolved`) rather than inventing
        an id when NCBI has no hit.
        """
        norm = normalize_taxon_name(name)
        if norm in self.cache:
            hit = self.cache[norm]
            if hit is None:
                self.unresolved.add(norm)
            return hit

        response = await client.get(
            NCBI_ESEARCH_URL,
            params={"db": "taxonomy", "term": name, "retmode": "json"},
        )
        response.raise_for_status()
        data = response.json()
        ids = data.get("esearchresult", {}).get("idlist", [])
        taxid = int(ids[0]) if ids else None

        self.cache[norm] = taxid
        if taxid is None:
            self.unresolved.add(norm)
        else:
            self.unresolved.discard(norm)
        return taxid

    async def verify_id(self, name: str, proposed_id: int | str, *, client: httpx.AsyncClient) -> bool:
        """S6's verification gate: confirm `proposed_id` is the authority's id for `name`.

        Design-1's S5b (fused extract) proposes both a taxon name AND an
        `ncbi_id` in one model call; this is the *only* place that proposal
        is allowed to survive into the record -- accepted iff the authority's
        own resolution of the (independently-verified) name string equals
        the proposed id. A mismatch or unresolved name means "never guess":
        the caller must drop the proposed id and keep the taxon unresolved.
        """
        try:
            proposed = int(proposed_id)
        except (TypeError, ValueError):
            return False
        resolved = await self.resolve_name(name, client=client)
        return resolved is not None and resolved == proposed

    def save_cache(self, path: Path | None = None) -> None:
        """Persist the in-memory cache to `path` (default: `self.cache_path`); no-op if neither set."""
        target = Path(path) if path is not None else self.cache_path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")
