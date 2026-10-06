"""EBI OLS4 term search: the deterministic candidate retrieval for ontology mapping.

The decision model never invents an ontology id: it only *chooses* among terms this client
retrieved (`curator.routing.map_body_sites`; probe: `benchmarks/decision-probe/RESULTS.md` P5,
issue #19). Candidates come from an OLS4 search over one ontology with the free-text label as the
query -- never from gold -- and are returned in OLS's own rank order.

Mirrors `curator.taxonomy`'s network idiom: a JSON-file cache (default `data/curator/ols_cache.json`,
persisted with `save_cache()`), a shared `_RateLimiter`, and 429/5xx exponential backoff that honours
`Retry-After`. Unlike the taxonomy resolver (which can answer "unresolved"), a retry-exhausted search
raises `httpx.HTTPStatusError`: an empty candidate list means "OLS has no such term", and a transient
outage must not be mistaken for it or cached as it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from bugsigdb_curation.curator.taxonomy import (
    _RETRYABLE_STATUSES,
    RETRY_AFTER_MAX_SECONDS,
    _parse_retry_after,
    _RateLimiter,
)

DEFAULT_CACHE_PATH = Path("data/curator/ols_cache.json")

OLS_SEARCH_URL = "https://www.ebi.ac.uk/ols4/api/search"

#: Candidates retrieved per query (probe setting: recall@10 was 0.995 for UBERON body sites).
DEFAULT_ROWS = 10

#: Minimum seconds between OLS requests; EBI publishes no hard cap, so stay politely low.
DEFAULT_MIN_INTERVAL = 0.1

#: Definition characters / synonyms kept per candidate when describing it to the decision model.
_DEFINITION_CHARS = 300
_MAX_SYNONYMS = 5


def describe(doc: dict[str, Any]) -> dict[str, Any]:
    """An OLS doc as the decision model sees it: label, definition (<=300 chars), <=5 exact synonyms."""
    out: dict[str, Any] = {"label": doc.get("label", "")}
    definition = (doc.get("description") or [""])[0][:_DEFINITION_CHARS]
    if definition:
        out["definition"] = definition
    synonyms = (doc.get("exact_synonyms") or [])[:_MAX_SYNONYMS]
    if synonyms:
        out["synonyms"] = synonyms
    return out


@dataclass
class OlsClient:
    """Cached, rate-limited OLS4 search. `cache` is keyed `ontology|query|rows`; a hit never touches the network.

    `client` is the (shared, caller-owned) HTTP client the searches go through.
    """

    client: httpx.AsyncClient
    cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cache_path: Path | None = DEFAULT_CACHE_PATH
    #: Retry/backoff policy for a 429/5xx from `OLS_SEARCH_URL`.
    max_attempts: int = 3
    retry_base_delay: float = 0.5
    #: Shared across every call this client makes; tests inject a fake clock/sleep.
    rate_limiter: _RateLimiter = field(default_factory=lambda: _RateLimiter(min_interval=DEFAULT_MIN_INTERVAL))

    @classmethod
    def load(cls, client: httpx.AsyncClient, *, cache_path: Path | None = DEFAULT_CACHE_PATH) -> OlsClient:
        """Build a client from a JSON cache file (missing -> empty cache)."""
        cache: dict[str, list[dict[str, Any]]] = {}
        if cache_path is not None and Path(cache_path).exists():
            cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        return cls(client=client, cache=cache, cache_path=Path(cache_path) if cache_path is not None else None)

    async def search(self, query: str, ontology: str, *, rows: int = DEFAULT_ROWS) -> list[dict[str, Any]]:
        """Candidate term docs for `query` in `ontology`, in OLS rank order (possibly empty).

        Raises `httpx.HTTPStatusError` for a non-retryable error or once retries on a 429/5xx are
        exhausted; transport errors propagate as-is. Failures are never cached.
        """
        key = f"{ontology}|{query}|{rows}"
        if key in self.cache:
            return self.cache[key]

        params = {
            "q": query,
            "ontology": ontology,
            "rows": str(rows),
            "type": "class",
            "queryFields": "label,synonym,short_form,obo_id",
        }
        delay = self.retry_base_delay
        for attempt in range(self.max_attempts):
            await self.rate_limiter.acquire()
            response = await self.client.get(OLS_SEARCH_URL, params=params)
            logger.bind(stage="S4").debug(
                "ols search", http_status=response.status_code, attempt=attempt + 1, max_attempts=self.max_attempts
            )
            if response.status_code in _RETRYABLE_STATUSES and attempt < self.max_attempts - 1:
                wait = delay
                if response.status_code == 429:
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    if retry_after is not None:
                        wait = min(retry_after, RETRY_AFTER_MAX_SECONDS)
                await self.rate_limiter.sleep(wait)
                delay *= 2
                continue
            response.raise_for_status()
            docs: list[dict[str, Any]] = response.json()["response"]["docs"]
            self.cache[key] = docs
            return docs
        raise AssertionError("unreachable: the final attempt always returns or raises")  # pragma: no cover

    def save_cache(self, path: Path | None = None) -> None:
        """Persist the in-memory cache to `path` (default: `self.cache_path`); no-op if neither set."""
        target = Path(path) if path is not None else self.cache_path
        if target is None:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.cache, indent=2, sort_keys=True), encoding="utf-8")
