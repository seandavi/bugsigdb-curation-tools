"""Tests for the DecisionModel seam (`bugsigdb_curation.decision`).

Request/response shapes are validated against the published Clef JSON
schemas vendored in `tests/data/clef/`. No test here touches the network
except the opt-in `@pytest.mark.network` smoke.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import httpx
import jsonschema
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.decision import (
    Choice,
    ChoiceAnswer,
    ClefDecisionModel,
    DecisionModelError,
    MockDecisionModel,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    build_request,
    parse_response,
    redact_images,
)

DATA = Path(__file__).parent / "data" / "clef"
INPUT_SCHEMA = json.loads((DATA / "schema-input.json").read_text())
OUTPUT_SCHEMA = json.loads((DATA / "schema-output.json").read_text())

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TOKEN = "s3cret-token-value"
URL = "https://api.cloudflare.com/client/v4/accounts/acct/ai/run/@cf/cloudflare/clef"

QUESTIONS = {
    "is_da": Noul("Does this sheet hold per-taxon DA results?", criteria={"true": "yes", "false": "no"}),
    "kind": Choice("What is it?", {"da_table": "a DA table", "methods": None}),
    "groups": Score("How many groups?", ("2", "3", "4+")),
}

RESULT = {
    "model": "clef",
    "answers": {
        "is_da": {"type": "noul", "noul": 0.93},
        "kind": {
            "type": "choice",
            "choice": "da_table",
            "probabilities": {"da_table": 0.9, "methods": 0.1},
            "confidence": 0.8,
        },
        "groups": {
            "type": "score",
            "score": 0.4,
            "legend": {"0": "2", "1": "3", "2": "4+"},
            "probabilities": {"0": 0.7, "1": 0.2, "2": 0.1},
            "confidence": 0.6,
        },
    },
    "usage": {"input_tokens": 123, "output_tokens": 0},
}


def _model(client: httpx.AsyncClient, **kw) -> ClefDecisionModel:
    kw.setdefault("sleep", _no_sleep)
    return ClefDecisionModel("acct", TOKEN, client=client, **kw)


async def _no_sleep(_: float) -> None:
    return None


async def _decide(model: ClefDecisionModel, **kw):
    return await model.decide(stage="t", state={"sheet": "S1"}, questions=QUESTIONS, **kw)


def _run(coro):
    return asyncio.run(coro)


# --- wire format -----------------------------------------------------------


def test_request_validates_against_published_input_schema():
    body = build_request(model="clef-flash", state={"a": 1}, questions=QUESTIONS, images=[PNG])
    jsonschema.validate(body, INPUT_SCHEMA)
    assert body["questions"]["groups"] == {"type": "score", "instructions": "How many groups?", "criteria": ["2", "3", "4+"]}
    assert body["questions"]["is_da"]["criteria"] == {"true": "yes", "false": "no"}
    assert body["images"][0]["content_type"] == "image/png"
    assert base64.b64decode(body["images"][0]["base64"]) == PNG


def test_noul_without_criteria_omits_the_key():
    body = build_request(model="clef", state="x", questions={"q": Noul("ok?")})
    assert "criteria" not in body["questions"]["q"]
    jsonschema.validate(body, INPUT_SCHEMA)


def test_published_output_example_validates_and_parses():
    jsonschema.validate(RESULT, OUTPUT_SCHEMA)
    answers = parse_response(RESULT, QUESTIONS)
    assert answers["is_da"] == NoulAnswer(p_yes=0.93)
    assert answers["kind"] == ChoiceAnswer("da_table", {"da_table": 0.9, "methods": 0.1}, 0.8)
    assert answers["groups"] == ScoreAnswer(0.4, {"0": 0.7, "1": 0.2, "2": 0.1}, 0.6)


@pytest.mark.parametrize(
    "questions, images, match",
    [
        ({}, (), "1-64 questions"),
        ({f"q{i}": Noul("x") for i in range(65)}, (), "1-64 questions"),
        ({"bad id": Noul("x")}, (), "invalid question id"),
        ({"q": Noul("")}, (), "empty instructions"),
        ({"q": Choice("x", {"only": None})}, (), "2-255 options"),
        ({"q": Score("x", ("a",))}, (), "2-10 levels"),
        ({"q": Noul("x")}, (PNG,) * 5, "at most 4 images"),
        ({"q": Noul("x")}, (b"GIF89a",), "unsupported image"),
    ],
)
def test_local_validation_rejects_bad_requests(questions, images, match):
    with pytest.raises(ValueError, match=match):
        build_request(model="clef", state="x", questions=questions, images=images)


def test_unknown_model_rejected():
    with pytest.raises(ValueError, match="model must be"):
        build_request(model="jev", state="x", questions={"q": Noul("x")})


def test_parse_response_rejects_missing_and_mistyped_answers():
    with pytest.raises(DecisionModelError, match="no answer for question 'kind'"):
        parse_response({"answers": {"is_da": RESULT["answers"]["is_da"]}}, {"is_da": QUESTIONS["is_da"], "kind": QUESTIONS["kind"]})
    with pytest.raises(DecisionModelError, match="but got NoulAnswer"):
        parse_response({"answers": {"kind": RESULT["answers"]["is_da"]}}, {"kind": QUESTIONS["kind"]})


# --- ClefDecisionModel -----------------------------------------------------


def test_decide_posts_authorised_request_and_parses_enveloped_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, json={"result": RESULT, "success": True, "errors": []})

    async def go():
        async with httpx.AsyncClient() as client:
            return await _decide(_model(client), images=[PNG])

    answers = _run(go())
    assert answers["is_da"].p_yes == 0.93
    req = httpx_mock.get_request()
    assert req.headers["Authorization"] == f"Bearer {TOKEN}"
    sent = json.loads(req.content)
    jsonschema.validate(sent, INPUT_SCHEMA)
    assert sent["model"] == "clef"


def test_clef_flash_uses_its_own_endpoint(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL + "-flash", json={"result": RESULT, "success": True})

    async def go():
        async with httpx.AsyncClient() as client:
            return await _decide(_model(client, model="clef-flash"))

    assert "is_da" in _run(go())


def test_bare_unenveloped_response_also_parses(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, json=RESULT)

    async def go():
        async with httpx.AsyncClient() as client:
            return await _decide(_model(client))

    assert "kind" in _run(go())


def test_archive_records_call_without_image_bytes_or_token(httpx_mock: HTTPXMock, tmp_path: Path):
    httpx_mock.add_response(url=URL, json={"result": RESULT, "success": True})
    archive = tmp_path / "runs" / "calls.jsonl"

    async def go():
        async with httpx.AsyncClient() as client:
            await _decide(_model(client, archive=archive), images=[PNG])

    _run(go())
    text = archive.read_text()
    assert TOKEN not in text
    assert base64.b64encode(PNG).decode() not in text
    (record,) = [json.loads(line) for line in text.splitlines()]
    assert record["stage"] == "t" and record["model"] == "clef"
    assert record["usage"] == {"input_tokens": 123, "output_tokens": 0}
    assert record["latency_ms"] >= 0 and record["attempts"] == 1 and record["error"] is None
    (img,) = record["request"]["images"]
    assert set(img) == {"content_type", "sha256", "size"} and img["size"] == len(PNG)
    assert record["response"]["result"]["answers"]["is_da"]["noul"] == 0.93


def test_redact_images_leaves_imageless_bodies_alone():
    body = build_request(model="clef", state="x", questions={"q": Noul("x")})
    assert redact_images(body) == body


def test_retries_429_then_succeeds_honouring_retry_after(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "7"})
    httpx_mock.add_response(url=URL, status_code=503)
    httpx_mock.add_response(url=URL, json={"result": RESULT, "success": True})
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async def go():
        async with httpx.AsyncClient() as client:
            return await _decide(_model(client, sleep=sleep, retry_base_delay=1.0))

    assert "is_da" in _run(go())
    assert sleeps == [7.0, 2.0]  # Retry-After wins once; then exponential (1 -> 2)


def test_retry_after_is_capped(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "9999"})
    httpx_mock.add_response(url=URL, json={"result": RESULT, "success": True})
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async def go():
        async with httpx.AsyncClient() as client:
            await _decide(_model(client, sleep=sleep))

    _run(go())
    assert sleeps == [30.0]


def test_exhausted_retries_raise_and_are_archived(httpx_mock: HTTPXMock, tmp_path: Path):
    httpx_mock.add_response(url=URL, status_code=500, text="boom", is_reusable=True)
    archive = tmp_path / "calls.jsonl"

    async def go():
        async with httpx.AsyncClient() as client:
            await _decide(_model(client, archive=archive, max_attempts=2))

    with pytest.raises(DecisionModelError, match="HTTP 500"):
        _run(go())
    assert len(httpx_mock.get_requests()) == 2
    (record,) = [json.loads(line) for line in archive.read_text().splitlines()]
    assert record["attempts"] == 2 and "HTTP 500" in record["error"]


def test_non_retryable_error_raises_immediately(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, status_code=400, text="bad request")

    async def go():
        async with httpx.AsyncClient() as client:
            await _decide(_model(client))

    with pytest.raises(DecisionModelError, match="HTTP 400"):
        _run(go())
    assert len(httpx_mock.get_requests()) == 1


def test_api_level_failure_in_200_body_raises(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, json={"success": False, "errors": [{"message": "nope"}], "result": None})

    async def go():
        async with httpx.AsyncClient() as client:
            await _decide(_model(client))

    with pytest.raises(DecisionModelError, match="API reported failure"):
        _run(go())


def test_min_interval_spaces_consecutive_calls(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=URL, json={"result": RESULT, "success": True}, is_reusable=True)
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    async def go():
        async with httpx.AsyncClient() as client:
            model = _model(client, sleep=sleep, clock=lambda: now[0], min_interval=0.5)
            await _decide(model)
            await _decide(model)

    _run(go())
    assert sleeps == [pytest.approx(0.5)]


def test_repr_hides_token():
    async def go():
        async with httpx.AsyncClient() as client:
            return repr(_model(client))

    assert TOKEN not in _run(go())


def test_from_env_requires_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bugsigdb_curation.decision.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    async def go():
        async with httpx.AsyncClient() as client:
            ClefDecisionModel.from_env(client=client)

    with pytest.raises(DecisionModelError, match="CLOUDFLARE_ACCOUNT_ID"):
        _run(go())


# --- MockDecisionModel -----------------------------------------------------


def test_mock_returns_canned_answers_and_records_calls():
    canned = parse_response(RESULT, QUESTIONS)
    mock = MockDecisionModel({"s": canned})
    out = _run(mock.decide(stage="s", state="st", questions=QUESTIONS, images=[PNG]))
    assert out == canned
    assert mock.calls[0]["stage"] == "s" and mock.calls[0]["images"] == [PNG]


def test_mock_callable_varies_by_state():
    def answers(state, questions):
        return {"q": NoulAnswer(0.9 if "yes" in state else 0.1)}

    mock = MockDecisionModel({"s": answers})
    qs = {"q": Noul("x")}
    assert _run(mock.decide(stage="s", state="yes", questions=qs))["q"].p_yes == 0.9
    assert _run(mock.decide(stage="s", state="no", questions=qs))["q"].p_yes == 0.1


def test_mock_enforces_the_same_contract_as_the_real_backend():
    qs = {"q": Noul("x")}
    with pytest.raises(DecisionModelError, match="no canned answers"):
        _run(MockDecisionModel().decide(stage="s", state="x", questions=qs))
    with pytest.raises(DecisionModelError, match="no canned answer for question"):
        _run(MockDecisionModel({"s": {}}).decide(stage="s", state="x", questions=qs))
    with pytest.raises(DecisionModelError, match="but got ChoiceAnswer"):
        _run(MockDecisionModel({"s": {"q": ChoiceAnswer("a", {"a": 1.0}, 1.0)}}).decide(stage="s", state="x", questions=qs))
    with pytest.raises(ValueError):
        _run(MockDecisionModel({"s": {}}).decide(stage="s", state="x", questions={}))


# --- live smoke (opt-in) ---------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize("model", ["clef", "clef-flash"])
def test_live_smoke(model: str):
    from dotenv import load_dotenv

    load_dotenv()
    if not (os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN")):
        pytest.skip("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN not set")

    async def go():
        async with httpx.AsyncClient(timeout=60) as client:
            m = ClefDecisionModel.from_env(client=client, model=model)
            return await m.decide(
                stage="smoke",
                state={"sheet": "S22", "header": ["taxon", "LDA", "p"], "rows": [["Bacteroides", 4.1, 0.001]]},
                questions={
                    "is_da": Noul("Does this sheet contain a per-taxon differential-abundance result?"),
                    "kind": Choice("What is this sheet?", {"da_taxa_table": None, "methods_text": None, "other": None}),
                },
            )

    answers = _run(go())
    assert 0.0 <= answers["is_da"].p_yes <= 1.0
    assert answers["kind"].choice in {"da_taxa_table", "methods_text", "other"}
    assert answers["is_da"].p_yes > 0.5  # an obvious DA table
