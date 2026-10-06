"""Tests for the OLS4 candidate-retrieval client (`curator.ols`). Offline: `pytest_httpx` only."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.curator.ols import OLS_SEARCH_URL, OlsClient, describe
from bugsigdb_curation.curator.taxonomy import RETRY_AFTER_MAX_SECONDS, _RateLimiter

DOCS = [
    {"obo_id": "UBERON:0001988", "label": "feces", "description": ["Excreted waste."], "exact_synonyms": ["faeces"]},
    {"obo_id": "UBERON:0000160", "label": "intestine"},
]


def _url(query: str, ontology: str = "uberon", rows: int = 10) -> httpx.URL:
    return httpx.URL(OLS_SEARCH_URL).copy_merge_params(
        {"q": query, "ontology": ontology, "rows": str(rows), "type": "class", "queryFields": "label,synonym,short_form,obo_id"}
    )


def _client(tmp_path=None, **kwargs) -> tuple[OlsClient, list[float]]:
    """An OlsClient on a fresh httpx client (pytest_httpx intercepts at the transport, so never closed here)."""
    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ols = OlsClient(client=httpx.AsyncClient(), cache_path=tmp_path, rate_limiter=_RateLimiter(min_interval=0.0, sleep=fast_sleep), **kwargs)
    return ols, sleeps


async def _search(ols: OlsClient, query: str = "Feces", **kwargs):
    return await ols.search(query, "uberon", **kwargs)


def test_search_sends_the_probe_params_and_returns_docs_in_rank_order(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), json={"response": {"docs": DOCS}})
    ols, _ = _client()
    assert asyncio.run(_search(ols)) == DOCS


def test_cache_hit_does_not_touch_the_network(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), json={"response": {"docs": DOCS}})
    ols, _ = _client()

    async def twice():
        first = await ols.search("Feces", "uberon")
        return first, await ols.search("Feces", "uberon")

    first, second = asyncio.run(twice())
    assert first == second == DOCS
    assert len(httpx_mock.get_requests()) == 1


def test_cache_key_includes_ontology_and_rows(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), json={"response": {"docs": DOCS}})
    httpx_mock.add_response(url=_url("Feces", rows=3), json={"response": {"docs": DOCS[:1]}})
    httpx_mock.add_response(url=_url("Feces", ontology="efo"), json={"response": {"docs": DOCS[1:]}})
    ols, _ = _client()
    assert len(asyncio.run(_search(ols))) == 2
    assert len(asyncio.run(_search(ols, rows=3))) == 1
    assert asyncio.run(ols.search("Feces", "efo")) == DOCS[1:]
    assert set(ols.cache) == {"uberon|Feces|10", "uberon|Feces|3", "efo|Feces|10"}
    assert len(httpx_mock.get_requests()) == 3  # no key collided into a cache hit


def test_cache_is_persisted_and_reloaded(httpx_mock: HTTPXMock, tmp_path):
    httpx_mock.add_response(url=_url("Feces"), json={"response": {"docs": DOCS}})
    path = tmp_path / "sub" / "ols_cache.json"
    ols = OlsClient.load(httpx.AsyncClient(), cache_path=path)
    assert ols.cache == {}  # missing file -> empty
    asyncio.run(_search(ols))
    ols.save_cache()
    assert json.loads(path.read_text()) == {"uberon|Feces|10": DOCS}
    reloaded = OlsClient.load(httpx.AsyncClient(), cache_path=path)
    assert asyncio.run(_search(reloaded)) == DOCS
    assert len(httpx_mock.get_requests()) == 1  # the reload answered from disk


def test_save_cache_is_a_noop_without_a_path():
    OlsClient(client=httpx.AsyncClient(), cache_path=None).save_cache()


def test_429_is_retried_honouring_retry_after(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), status_code=429, headers={"Retry-After": "2"})
    httpx_mock.add_response(url=_url("Feces"), json={"response": {"docs": DOCS}})
    ols, sleeps = _client(retry_base_delay=0.01)
    assert asyncio.run(_search(ols)) == DOCS
    assert sleeps == [2.0]


def test_retry_after_is_capped(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), status_code=429, headers={"Retry-After": "3600"})
    httpx_mock.add_response(url=_url("Feces"), json={"response": {"docs": DOCS}})
    ols, sleeps = _client()
    assert asyncio.run(_search(ols)) == DOCS
    assert sleeps == [RETRY_AFTER_MAX_SECONDS] == [30.0]


def test_transport_error_propagates_and_is_not_cached(httpx_mock: HTTPXMock):
    httpx_mock.add_exception(httpx.ConnectError("no route"), url=_url("Feces"))
    ols, _ = _client()
    with pytest.raises(httpx.ConnectError):
        asyncio.run(_search(ols))
    assert ols.cache == {}


@pytest.mark.parametrize(
    "body",
    [{}, {"response": {}}, {"response": {"docs": "x"}}, {"response": {"docs": [DOCS[0], "x"]}}, ["not", "an", "object"]],
)
def test_unexpected_response_shape_is_a_value_error_and_not_cached(httpx_mock: HTTPXMock, body):
    httpx_mock.add_response(url=_url("Feces"), json=body)
    ols, _ = _client()
    with pytest.raises(ValueError, match="unexpected OLS response shape"):
        asyncio.run(_search(ols))
    assert ols.cache == {}


def test_non_json_body_is_a_value_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), text="<html>maintenance</html>")
    ols, _ = _client()
    with pytest.raises(ValueError):
        asyncio.run(_search(ols))
    assert ols.cache == {}


@pytest.mark.parametrize("content", ["{not json", "", '["a", "list"]', '{"k": "not a list"}'])
def test_corrupt_cache_file_loads_as_empty_with_a_warning(tmp_path, content):
    from loguru import logger

    path = tmp_path / "ols_cache.json"
    path.write_text(content, encoding="utf-8")
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING")
    try:
        ols = OlsClient.load(httpx.AsyncClient(), cache_path=path)
    finally:
        logger.remove(sink)
    assert ols.cache == {}
    assert any("ols cache" in m.lower() for m in messages)


def test_save_cache_is_atomic(tmp_path, monkeypatch):
    import os

    path = tmp_path / "ols_cache.json"
    path.write_text('{"old": []}', encoding="utf-8")
    ols = OlsClient(client=httpx.AsyncClient(), cache={"new": []}, cache_path=path)

    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        assert json.loads(Path(src).read_text()) == {"new": []}  # fully written before it is swapped in
        assert json.loads(path.read_text()) == {"old": []}  # the live file was untouched until now
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    ols.save_cache()
    assert replaced and replaced[0][1] == str(path) and Path(replaced[0][0]).parent == path.parent
    assert json.loads(path.read_text()) == {"new": []}
    assert [p.name for p in tmp_path.iterdir()] == ["ols_cache.json"]  # no temp file left behind


def test_a_failed_save_leaves_the_old_cache_intact_and_no_temp_file(tmp_path, monkeypatch):
    import os

    path = tmp_path / "ols_cache.json"
    path.write_text('{"old": []}', encoding="utf-8")
    ols = OlsClient(client=httpx.AsyncClient(), cache={"new": []}, cache_path=path)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ols.save_cache()
    assert json.loads(path.read_text()) == {"old": []}
    assert [p.name for p in tmp_path.iterdir()] == ["ols_cache.json"]


def test_5xx_backs_off_exponentially_then_raises_without_caching(httpx_mock: HTTPXMock):
    for _ in range(3):
        httpx_mock.add_response(url=_url("Feces"), status_code=503)
    ols, sleeps = _client(retry_base_delay=0.5)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_search(ols))
    assert sleeps == [0.5, 1.0]
    assert ols.cache == {}  # an outage is not "no such term"


def test_non_retryable_status_raises_immediately(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("Feces"), status_code=400)
    ols, sleeps = _client()
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_search(ols))
    assert sleeps == []


def test_empty_result_is_a_cached_answer(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=_url("zzz"), json={"response": {"docs": []}})
    ols, _ = _client()
    assert asyncio.run(_search(ols, "zzz")) == []
    assert ols.cache == {"uberon|zzz|10": []}


def test_describe_truncates_and_omits_empty_fields():
    doc = {"label": "feces", "description": ["d" * 500], "exact_synonyms": [f"s{i}" for i in range(9)]}
    out = describe(doc)
    assert out["label"] == "feces" and len(out["definition"]) == 300 and out["synonyms"] == [f"s{i}" for i in range(5)]
    assert describe({"label": "x"}) == {"label": "x"}
    assert describe({}) == {"label": ""}
