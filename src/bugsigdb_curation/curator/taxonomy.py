"""S6 -- NCBI taxonomy normalization: a GENERAL NCBI authority, verification-only.

Per the workflow plan (§3, §6e): an LLM (S5b) may only *propose* a taxon name
and an `ncbi_id`; the identifier is only ever kept if this module's authority
lookup independently confirms it. This is deliberately **not**
`bugsigdb_curation.eval.taxonomy` -- that resolver reads gold (the curated
corpus's own taxid sets), which would leak which taxa the human curators
actually kept. This resolver only ever talks to (in order): its own cache
file (`data/curator/ncbi_taxonomy_cache.json` by default, distinct from the
eval harness's `data/eval/taxonomy_cache.json`), the local, general NCBI
`TaxonomyDB` built by `bugsigdb_curation.taxonomy` from the public taxdump
(not gold -- see that package's docstring), and live NCBI E-utilities as a
gap-fill for names the local DB doesn't have -- never a relational gold CSV,
never the eval package.

**Resolution order** (PR-2, "never guess" throughout): an in-memory/JSON
cache hit short-circuits everything; otherwise the local `TaxonomyDB` (fast,
offline, full synonym coverage) is tried first; only a local miss falls
through to a live `esearch.fcgi` call. A resolver with no `TaxonomyDB`
configured (see `load()`) falls back to live-only, with a one-time warning --
slower, but it still works with no DB built.

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
name doesn't exist). The local `TaxonomyDB` path (PR-2) is what actually
keeps most lookups off this rate-limited path in the first place -- live
E-utilities is now only reached for a genuine local-DB miss.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import httpx
from dotenv import load_dotenv

from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name
from bugsigdb_curation.taxonomy.paths import DB_PATH_ENV_VAR, resolve_optional_db_path

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

#: Cap on how long a server-supplied `Retry-After` (on a 429) is allowed to
#: delay a single retry -- deployment-friendly: an overly generous or
#: malicious value from the server can't stall a run indefinitely.
RETRY_AFTER_MAX_SECONDS = 30.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a `Retry-After` header value as a number of seconds, or `None`
    if the header is absent or not a plain seconds-delta (e.g. an HTTP-date
    form, which this deliberately doesn't attempt to parse -- the caller
    falls back to the exponential backoff schedule in that case)."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


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


def _load_optional_taxonomy_db(db_path: Path | str | None, db_release: str | None = None) -> TaxonomyDB | None:
    """Resolve + open the local taxonomy DB for `.load()`'s real construction
    path -- never raises. Returns `None` (with a one-time warning; Python's
    default warning filter dedupes repeats from this same call site) if no
    DB is configured/found, or if the resolved path fails to open (e.g. a
    corrupt/incomplete build, a truncated file, or a DB built with an
    incompatible DuckDB version -- the last two surface as `duckdb.Error`
    (`duckdb.IOException` et al., NOT a `ValueError`), not just the
    `FileNotFoundError`/`ValueError` `TaxonomyDB.__init__` itself raises) --
    either way the caller falls back to live-only E-utilities resolution
    instead of crashing.

    The "not found" warning is split in two (Copilot fix): an *explicit*
    `db_path`/`BUGSIGDB_TAXONOMY_DB` that just doesn't exist on disk names
    the actual path that was tried (a config/typo problem, not "nothing was
    configured"); only the genuinely-unconfigured case (no explicit path AND
    no cached `ncbi-taxdump-*.duckdb` found) gets the generic message.
    """
    resolved_path = resolve_optional_db_path(db_path, db_release)
    if resolved_path is None or not resolved_path.exists():
        explicit = db_path is not None or bool(os.environ.get(DB_PATH_ENV_VAR))
        if explicit:
            # `resolve_optional_db_path` always returns a concrete (non-None)
            # Path when `db_path` or the env var is set, so `resolved_path`
            # here is exactly the path that was tried.
            warnings.warn(
                f"configured taxonomy DB not found at {resolved_path} (from --taxonomy-db/"
                "BUGSIGDB_TAXONOMY_DB) -- NcbiTaxonomyResolver is falling back to live-only NCBI "
                "E-utilities resolution (slower). Build one with `bugsigdb taxonomy build`, or "
                "fix the configured path.",
                RuntimeWarning,
                stacklevel=3,
            )
        else:
            warnings.warn(
                "no local taxonomy DB found (no --taxonomy-db/BUGSIGDB_TAXONOMY_DB and no cached "
                "ncbi-taxdump-*.duckdb) -- NcbiTaxonomyResolver is falling back to live-only NCBI "
                "E-utilities resolution (slower). Build one with `bugsigdb taxonomy build`.",
                RuntimeWarning,
                stacklevel=3,
            )
        return None
    try:
        return TaxonomyDB(resolved_path)
    except (FileNotFoundError, ValueError, duckdb.Error) as exc:
        warnings.warn(
            f"failed to open local taxonomy DB at {resolved_path}: {exc} -- "
            "NcbiTaxonomyResolver is falling back to live-only NCBI E-utilities resolution.",
            RuntimeWarning,
            stacklevel=3,
        )
        return None


@dataclass
class NcbiTaxonomyResolver:
    """A name -> NCBI taxid resolver: cache -> local `TaxonomyDB` -> live E-utilities gap-fill.

    `cache` is keyed by `normalize_taxon_name(...)`; a cached `None` means
    "confirmed unresolved" (not "never looked up"), so a repeat lookup for a
    name neither the local DB nor NCBI recognizes is free rather than
    re-querying every call. Never seeded from any corpus/gold file -- every
    entry came from the local `TaxonomyDB` (the general NCBI taxdump, not
    gold), a live esearch, or was persisted from a previous run of this same
    resolver.

    `db` (PR-2) is a local, offline `TaxonomyDB` consulted before any network
    call; `None` means no DB is configured, in which case every lookup falls
    straight through to live E-utilities (see `load()`'s one-time warning
    for that case). Throttling only applies to actual network calls: a cache
    hit or a local-DB hit short-circuits `resolve_name` before
    `rate_limiter.acquire()` is ever reached, so neither consumes rate
    budget.
    """

    cache: dict[str, int | None] = field(default_factory=dict)
    cache_path: Path | None = DEFAULT_CACHE_PATH
    #: Normalized names confirmed to have no hit anywhere (local DB nor live).
    unresolved: set[str] = field(default_factory=set)
    #: A local, offline NCBI taxonomy DB (general taxdump, not gold) tried
    #: before any network call; `None` falls back to live-only resolution.
    db: TaxonomyDB | None = None
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
        db_path: Path | str | None = None,
        db_release: str | None = None,
        db: TaxonomyDB | None = None,
    ) -> NcbiTaxonomyResolver:
        """Build a resolver from a JSON cache file (missing/absent -> empty cache).

        `api_key`, if not given, is resolved via `resolve_ncbi_api_key()` --
        the real construction path (`curate`/`curate_async`) always goes
        through here, so a `.env`/env-var `NCBI_API_KEY` is picked up
        automatically without every caller having to thread it through.

        `db`, if not given, is resolved via `db_path`/`db_release` (CLI flag
        -> `BUGSIGDB_TAXONOMY_DB` -> `db_release`'s default cache path (if
        given) -> newest cached `ncbi-taxdump-*.duckdb` -> none) -- see
        `_load_optional_taxonomy_db`. No DB found/configured is not an
        error: the resolver falls back to live-only, with a one-time
        warning, rather than crashing a curator run that hasn't built one yet.
        """
        cache: dict[str, int | None] = {}
        if cache_path is not None and Path(cache_path).exists():
            raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            cache = {k: (int(v) if v is not None else None) for k, v in raw.items()}
        resolved_key = api_key if api_key is not None else resolve_ncbi_api_key()
        if db is None:
            db = _load_optional_taxonomy_db(db_path, db_release)
        return cls(
            cache=cache,
            cache_path=Path(cache_path) if cache_path is not None else None,
            api_key=resolved_key,
            db=db,
        )

    async def _get_with_retry(self, params: dict[str, str], *, client: httpx.AsyncClient) -> httpx.Response | None:
        """GET `NCBI_ESEARCH_URL` through the shared rate limiter, retrying a
        429/5xx with exponential backoff (`max_attempts` total tries).

        On a 429 that carries a `Retry-After` header (seconds), that value
        (capped at `RETRY_AFTER_MAX_SECONDS`) is used as this attempt's
        backoff delay instead of the exponential schedule -- deployment-
        friendly: NCBI's server-supplied guidance wins over a guess when
        it's given. Falls back to the exponential schedule when the header
        is absent or unparseable (e.g. an HTTP-date form), and always for a
        5xx (which never carries a meaningful `Retry-After` here).

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
                    wait = delay
                    if response.status_code == 429:
                        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                        if retry_after is not None:
                            wait = min(retry_after, RETRY_AFTER_MAX_SECONDS)
                    await self.rate_limiter.sleep(wait)
                    delay *= 2
                    continue
                return None
            response.raise_for_status()
            return response
        return None  # pragma: no cover -- loop always returns/continues above

    async def resolve_name(self, name: str, *, client: httpx.AsyncClient) -> int | None:
        """Resolve a bare taxon-name string to an NCBI taxid: cache -> local
        `TaxonomyDB` -> live esearch gap-fill.

        Cache hit (including a cached "confirmed unresolved" `None`) short-
        circuits without touching the local DB or the network. A local-DB
        hit (PR-2) short-circuits before any network call and is cached, so
        a repeat lookup for the same name is free. Only a local-DB miss (or
        no DB configured at all) falls through to live esearch. Never
        guesses: returns `None` (and records the normalized name in
        `.unresolved`) rather than inventing an id when nothing has a hit.

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

        if self.db is not None:
            resolution = self.db.resolve(name)
            if resolution is not None:
                self.cache[norm] = resolution.tax_id
                self.unresolved.discard(norm)
                return resolution.tax_id
            # Local DB has no hit for this name -- fall through to a live
            # esearch gap-fill below rather than caching "unresolved" yet;
            # only a live no-hit (or retry-exhaustion, see below) decides
            # that.

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

    def close(self) -> None:
        """Close the local `TaxonomyDB` handle, if one is open.

        No-op if `db` is `None` (live-only resolver) -- and safe to call
        more than once, since `TaxonomyDB.close()` itself guards against a
        double-close. A caller that owns this resolver's lifecycle (e.g.
        `curate_async`'s single-PMID path, or the CLI's `--smoke` batch loop
        that builds one shared resolver up front) should call this once
        it's done resolving, so the DuckDB connection doesn't outlive the
        run.
        """
        if self.db is not None:
            self.db.close()
