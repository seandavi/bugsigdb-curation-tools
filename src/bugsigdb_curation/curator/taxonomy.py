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

**NCBI E-utilities etiquette (this module's other job).** NCBI asks
unauthenticated callers to stay at or under ~3 req/s and callers with an
API key to stay at or under ~10 req/s
(https://www.ncbi.nlm.nih.gov/books/NBK25497/); a real `curate --smoke` run
hammering `esearch.fcgi` with one uncapped request per taxon name got back
HTTP 429s on most studies. `_RateLimiter` enforces a minimum inter-request
interval (with a safety margin under either cap); `resolve_ncbi_api_key()`
finds an optional `NCBI_API_KEY` (which tightens that interval and is sent
as the `api_key` query param); and `NcbiTaxonomyResolver._get_with_retry`
retries a 429/5xx with exponential backoff before giving up on that one
taxon -- consistent with the "never guess" contract, a retry-exhausted
lookup returns `None` for that call without poisoning the cache (a
transient rate-limit failure is not the same thing as NCBI confirming the
name doesn't exist).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL

#: Rank prefixes appear double-underscored (MetaPhlAn, "g__Bacillus") or
#: single-underscored (LEfSe figure labels, "g_Bacillus"); strip either form.
#: (Deliberately duplicated from `bugsigdb_curation.eval.taxonomy` rather
#: than imported -- see this module's docstring on the firewall boundary.)
_RANK_PREFIX = re.compile(r"^[kdpcofgst]__?")
_WHITESPACE_OR_UNDERSCORE = re.compile(r"[\s_]+")

DEFAULT_CACHE_PATH = Path("data/curator/ncbi_taxonomy_cache.json")

NCBI_ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

#: This tool's etiquette identifier, sent as the `tool` query param on every
#: E-utilities call (matches `bugsigdb_curation.pmc_map.TOOL_NAME`).
TOOL_NAME = "bugsigdb-curation"

#: Env var names to try for an NCBI E-utilities API key, most-canonical
#: first. `NCBI_API_KEY` is NCBI's documented name (generated at
#: https://www.ncbi.nlm.nih.gov/account/settings/); `NCBI_EUTILS_API_KEY` is
#: accepted as an alias in case a caller's environment already uses a more
#: eutils-specific name. Mirrors `curator.model.resolve_google_api_key`'s
#: multi-name, `.env`-aware resolution -- see that function's docstring for
#: why `.env` is loaded first.
_NCBI_KEY_ENV_NAMES = ("NCBI_API_KEY", "NCBI_EUTILS_API_KEY")

#: NCBI's documented unauthenticated/authenticated caps are ~3 req/s and
#: ~10 req/s; these intervals sit comfortably under either (≈2.9 req/s and
#: ≈9.1 req/s) rather than riding the limit exactly.
DEFAULT_MIN_INTERVAL_NO_KEY = 0.35
DEFAULT_MIN_INTERVAL_WITH_KEY = 0.11

#: HTTP statuses `_get_with_retry` treats as transient/retryable.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def normalize_taxon_name(name: str) -> str:
    """Normalize a taxon label for lookup/comparison (see module docstring)."""
    n = name.strip()
    n = _RANK_PREFIX.sub("", n)
    n = n.replace("_", " ")
    n = _WHITESPACE_OR_UNDERSCORE.sub(" ", n)
    return n.strip().lower()


def resolve_ncbi_api_key() -> str | None:
    """Find a usable NCBI E-utilities API key in the environment, or None.

    Loads a repo-root (or CWD-upward) `.env` file first via `python-dotenv`
    (a no-op if none exists), then checks `_NCBI_KEY_ENV_NAMES` in order. An
    API key is never required -- E-utilities works unauthenticated at the
    lower ~3 req/s cap -- it's purely an optimization that raises the cap to
    ~10 req/s (see `DEFAULT_MIN_INTERVAL_WITH_KEY`). Never logs or returns
    anything about *which* var matched beyond the value itself -- callers
    must not print this.
    """
    load_dotenv()
    for name in _NCBI_KEY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass
class _RateLimiter:
    """An async minimum-interval rate limiter, shared across every
    E-utilities call one `NcbiTaxonomyResolver` instance makes.

    Concurrent callers serialize through `acquire()`'s `asyncio.Lock`, so
    even if several `resolve_name`/`verify_id` calls are in flight at once
    (e.g. `asyncio.gather`), the *actual network requests* they issue are
    still spaced at least `min_interval` apart. `clock`/`sleep` are
    injectable (default `time.monotonic`/`asyncio.sleep`) purely so tests
    can assert on requested wait durations without a real sleep.
    """

    min_interval: float
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _last_call: float | None = field(default=None, repr=False, compare=False)

    async def acquire(self) -> None:
        """Block (if needed) until at least `min_interval` has elapsed since
        the last acquisition, then record this acquisition's time."""
        async with self._lock:
            now = self.clock()
            if self._last_call is not None:
                due = self._last_call + self.min_interval
                if now < due:
                    await self.sleep(due - now)
                    now = due
            self._last_call = now


@dataclass
class NcbiTaxonomyResolver:
    """A name -> NCBI taxid resolver backed only by live E-utilities + a cache file.

    `cache` is keyed by `normalize_taxon_name(...)`; a cached `None` means
    "confirmed unresolved" (not "never looked up"), so a repeat lookup for a
    name NCBI doesn't recognize is free rather than re-hitting the network
    every call. Never seeded from any corpus/gold file -- every entry either
    came from a live esearch or was persisted from a previous run of this
    same resolver.

    Throttling only applies to actual network calls: a cache hit
    short-circuits `resolve_name` before `rate_limiter.acquire()` is ever
    reached, so it never consumes rate budget.
    """

    cache: dict[str, int | None] = field(default_factory=dict)
    cache_path: Path | None = DEFAULT_CACHE_PATH
    #: Normalized names esearch confirmed have no taxonomy-db hit.
    unresolved: set[str] = field(default_factory=set)
    #: An NCBI E-utilities API key, or None for unauthenticated use. Not
    #: auto-resolved from the environment here -- only `.load()` (the real
    #: construction path used by `curate`) does that; a bare
    #: `NcbiTaxonomyResolver(...)` stays hermetic to whatever `api_key` is
    #: passed explicitly, which is what every offline test relies on.
    api_key: str | None = None
    #: NCBI etiquette contact fields, sent on every E-utilities call.
    tool: str = TOOL_NAME
    email: str = DEFAULT_EMAIL
    #: Retry/backoff policy for a 429/5xx from `NCBI_ESEARCH_URL`.
    max_attempts: int = 3
    retry_base_delay: float = 0.5
    #: Shared across every call this resolver instance makes; built in
    #: `__post_init__` from `api_key` unless a caller injects one (tests
    #: use this to swap in a fake clock/sleep so limiter/backoff waits
    #: don't actually block).
    rate_limiter: _RateLimiter | None = None

    def __post_init__(self) -> None:
        if self.rate_limiter is None:
            min_interval = DEFAULT_MIN_INTERVAL_WITH_KEY if self.api_key else DEFAULT_MIN_INTERVAL_NO_KEY
            self.rate_limiter = _RateLimiter(min_interval=min_interval)

    @classmethod
    def load(
        cls,
        *,
        cache_path: Path | None = DEFAULT_CACHE_PATH,
        api_key: str | None = None,
    ) -> NcbiTaxonomyResolver:
        """Build a resolver from a JSON cache file (missing/absent -> empty cache).

        `api_key`, if not given, is resolved via `resolve_ncbi_api_key()` --
        the real construction path (`curate`/`curate_async`) always goes
        through here, so a `.env`/env-var `NCBI_API_KEY` is picked up
        automatically without every caller having to thread it through.
        """
        cache: dict[str, int | None] = {}
        if cache_path is not None and Path(cache_path).exists():
            raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            cache = {k: (int(v) if v is not None else None) for k, v in raw.items()}
        resolved_key = api_key if api_key is not None else resolve_ncbi_api_key()
        return cls(
            cache=cache,
            cache_path=Path(cache_path) if cache_path is not None else None,
            api_key=resolved_key,
        )

    async def _get_with_retry(self, params: dict[str, str], *, client: httpx.AsyncClient) -> httpx.Response | None:
        """GET `NCBI_ESEARCH_URL` through the shared rate limiter, retrying a
        429/5xx with exponential backoff (`max_attempts` total tries).

        Returns `None` -- never raises -- once retries are exhausted on a
        retryable status; a non-retryable HTTP error (`raise_for_status()`)
        or a transport-level exception (connection error, timeout, ...)
        propagates as-is, since those are "genuine unexpected errors", not
        the rate-limiting condition this method exists to absorb.
        """
        assert self.rate_limiter is not None  # set in __post_init__
        full_params = dict(params)
        full_params["tool"] = self.tool
        full_params["email"] = self.email
        if self.api_key:
            full_params["api_key"] = self.api_key

        delay = self.retry_base_delay
        for attempt in range(self.max_attempts):
            await self.rate_limiter.acquire()
            response = await client.get(NCBI_ESEARCH_URL, params=full_params)
            if response.status_code in _RETRYABLE_STATUSES:
                if attempt < self.max_attempts - 1:
                    await self.rate_limiter.sleep(delay)
                    delay *= 2
                    continue
                return None
            response.raise_for_status()
            return response
        return None  # pragma: no cover -- loop always returns/continues above

    async def resolve_name(self, name: str, *, client: httpx.AsyncClient) -> int | None:
        """Resolve a bare taxon-name string to an NCBI taxid via live esearch.

        Cache hit (including a cached "confirmed unresolved" `None`) short-
        circuits without a network call. Never guesses: returns `None` (and
        records the normalized name in `.unresolved`) rather than inventing
        an id when NCBI has no hit.

        If NCBI keeps returning 429/5xx through every retry, this also
        returns `None` for this call -- but, unlike a confirmed no-hit, it
        does *not* cache the `None` or add the name to `.unresolved`: a
        transient rate-limit failure isn't NCBI telling us the taxon
        doesn't exist, so a later lookup (this run or a future one) should
        still try the network again.
        """
        norm = normalize_taxon_name(name)
        if norm in self.cache:
            hit = self.cache[norm]
            if hit is None:
                self.unresolved.add(norm)
            return hit

        response = await self._get_with_retry(
            {"db": "taxonomy", "term": norm, "retmode": "json"}, client=client
        )
        if response is None:
            return None

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
