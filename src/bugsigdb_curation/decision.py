"""The DecisionModel seam: calibrated yes/no, pick-one and rating judgments.

A *decision model* (Cloudflare Clef / Clef-flash, TypeSafe Jev -- the "System
One" API) takes a ``state`` (text or JSON, plus up to 4 images on Clef) and up
to 64 typed questions, and returns a calibrated probability for every option
without generating text. This module is the transport seam for that API:

* the question / answer types (:class:`Noul`, :class:`Choice`, :class:`Score`
  and their ``*Answer`` counterparts),
* the :class:`DecisionModel` protocol every backend implements,
* :class:`ClefDecisionModel` -- the Workers AI backend, with throttle/backoff
  and a JSONL archive of every call (the reproducibility record, and later a
  fine-tuning dataset),
* :class:`MockDecisionModel` -- a deterministic offline stand-in for CI.

It is curator-neutral: it imports nothing from ``bugsigdb_curation.curator``
and reads no gold data (firewall, issue #19).

Wire format
-----------
Built from the published schemas (vendored under ``tests/data/clef/``):
https://developers.cloudflare.com/workers-ai/models/clef/schema-input.json and
``.../schema-output.json``. Where they differ from our Python types:

* every question carries a ``"type"`` discriminator (``noul`` / ``choice`` /
  ``score``) -- added by :func:`question_to_wire`;
* a yes/no answer arrives as ``{"type": "noul", "noul": p}``; we expose it as
  :attr:`NoulAnswer.p_yes`;
* a score answer also carries a ``legend``; we keep ``score``,
  ``probabilities`` (keyed by level index as a string) and ``confidence``;
* images are ``{"content_type", "base64"}`` objects (no remote URLs);
* the request body repeats the model id (``"model": "clef"``) as well as the
  URL path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv
from loguru import logger

DEFAULT_BASE_URL = "https://api.cloudflare.com/client/v4"
CLEF_MODELS = ("clef", "clef-flash")

#: Limits from the published input schema.
MAX_QUESTIONS = 64
MAX_IMAGES = 4
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS, MAX_SCORE_LEVELS = 2, 10
_QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
#: Cap on a server-supplied ``Retry-After`` so it can't stall a run.
RETRY_AFTER_MAX_SECONDS = 30.0


class DecisionModelError(RuntimeError):
    """A decision call could not produce a valid answer set."""


# --------------------------------------------------------------------------
# Questions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Noul:
    """A yes/no question. ``criteria`` optionally describes ``"true"``/``"false"``."""

    instructions: str | dict[str, Any]
    criteria: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Choice:
    """Pick one of 2-255 options; ``criteria`` maps option id -> description."""

    instructions: str | dict[str, Any]
    criteria: dict[str, str | dict[str, Any] | None]


@dataclass(frozen=True, slots=True)
class Score:
    """Rate on 2-10 ordered levels (lowest first, indexed from 0)."""

    instructions: str | dict[str, Any]
    criteria: tuple[str, ...]


Question = Noul | Choice | Score


# --------------------------------------------------------------------------
# Answers
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    p_yes: float


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    """``score`` is probability-weighted and can fall between levels;
    ``probabilities`` is keyed by level index (as a string)."""

    score: float
    probabilities: dict[str, float]
    confidence: float


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class DecisionModel(Protocol):
    """Any backend that answers typed questions about a state."""

    async def decide(
        self,
        *,
        stage: str,
        state: str | dict[str, Any] | list[Any],
        questions: Mapping[str, Question],
        images: Sequence[bytes] = (),
    ) -> dict[str, Answer]:
        """Answer every question, keyed by the question ids."""
        ...


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------


def question_to_wire(question: Question) -> dict[str, Any]:
    """Serialize one question to the published input-schema shape."""
    match question:
        case Noul():
            wire: dict[str, Any] = {"type": "noul", "instructions": question.instructions}
            if question.criteria is not None:
                wire["criteria"] = dict(question.criteria)
            return wire
        case Choice():
            return {"type": "choice", "instructions": question.instructions, "criteria": dict(question.criteria)}
        case Score():
            return {"type": "score", "instructions": question.instructions, "criteria": list(question.criteria)}
    raise TypeError(f"not a decision question: {question!r}")


def validate_questions(questions: Mapping[str, Question]) -> None:
    """Enforce the published limits locally so a bad request costs nothing."""
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise ValueError(f"need 1-{MAX_QUESTIONS} questions, got {len(questions)}")
    for qid, question in questions.items():
        if not _QUESTION_ID_RE.match(qid):
            raise ValueError(f"invalid question id {qid!r} (letters, digits, '_', '.', '-'; max 100 chars)")
        instructions = question.instructions
        if not instructions:
            raise ValueError(f"question {qid!r} has empty instructions")
        if isinstance(question, Choice):
            if not 2 <= len(question.criteria) <= MAX_CHOICE_OPTIONS:
                raise ValueError(f"choice {qid!r} needs 2-{MAX_CHOICE_OPTIONS} options, got {len(question.criteria)}")
            if any(not option for option in question.criteria):
                raise ValueError(f"choice {qid!r} has an empty option id")
        elif isinstance(question, Score):
            if not MIN_SCORE_LEVELS <= len(question.criteria) <= MAX_SCORE_LEVELS:
                raise ValueError(
                    f"score {qid!r} needs {MIN_SCORE_LEVELS}-{MAX_SCORE_LEVELS} levels, got {len(question.criteria)}"
                )


def sniff_image_type(data: bytes) -> str:
    """Content type of PNG / JPEG / WebP bytes (the only formats Clef accepts)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("unsupported image format (need PNG, JPEG or WebP)")


def build_request(
    *,
    model: str,
    state: str | dict[str, Any] | list[Any],
    questions: Mapping[str, Question],
    images: Sequence[bytes] = (),
) -> dict[str, Any]:
    """Assemble the request body for the System One ``run`` endpoint."""
    if model not in CLEF_MODELS:
        raise ValueError(f"model must be one of {CLEF_MODELS}, got {model!r}")
    if len(images) > MAX_IMAGES:
        raise ValueError(f"at most {MAX_IMAGES} images per call, got {len(images)}")
    validate_questions(questions)
    body: dict[str, Any] = {
        "model": model,
        "state": state,
        "questions": {qid: question_to_wire(q) for qid, q in questions.items()},
    }
    if images:
        body["images"] = [
            {"content_type": sniff_image_type(img), "base64": base64.b64encode(img).decode("ascii")} for img in images
        ]
    return body


def parse_answer(wire: Mapping[str, Any]) -> Answer:
    """Parse one answer object from the published output-schema shape."""
    kind = wire.get("type")
    try:
        if kind == "noul":
            return NoulAnswer(p_yes=float(wire["noul"]))
        if kind == "choice":
            return ChoiceAnswer(
                choice=str(wire["choice"]),
                probabilities={str(k): float(v) for k, v in wire["probabilities"].items()},
                confidence=float(wire["confidence"]),
            )
        if kind == "score":
            return ScoreAnswer(
                score=float(wire["score"]),
                probabilities={str(k): float(v) for k, v in wire["probabilities"].items()},
                confidence=float(wire["confidence"]),
            )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DecisionModelError(f"malformed {kind} answer: {exc!r}") from exc
    raise DecisionModelError(f"unknown answer type {kind!r}")


_ANSWER_TYPE_FOR: dict[type, type] = {Noul: NoulAnswer, Choice: ChoiceAnswer, Score: ScoreAnswer}


def parse_response(result: Mapping[str, Any], questions: Mapping[str, Question]) -> dict[str, Answer]:
    """Parse ``result["answers"]``, checking each question got a matching answer."""
    raw = result.get("answers")
    if not isinstance(raw, Mapping):
        raise DecisionModelError("response has no 'answers' object")
    answers: dict[str, Answer] = {}
    for qid, question in questions.items():
        if qid not in raw:
            raise DecisionModelError(f"no answer for question {qid!r}")
        answer = parse_answer(raw[qid])
        if not isinstance(answer, _ANSWER_TYPE_FOR[type(question)]):
            raise DecisionModelError(f"question {qid!r} is {type(question).__name__} but got {type(answer).__name__}")
        answers[qid] = answer
    return answers


def redact_images(body: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of a request body with image bytes replaced by sha256 + size."""
    redacted = dict(body)
    images = body.get("images")
    if images:
        redacted["images"] = []
        for img in images:
            raw = base64.b64decode(img["base64"])
            redacted["images"].append(
                {"content_type": img["content_type"], "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
            )
    return redacted


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


class ClefDecisionModel:
    """Cloudflare Workers AI backend for Clef / Clef-flash.

    ``client`` is injected so callers own its lifecycle (and tests can mock
    it). Calls are spaced at least ``min_interval`` seconds apart and a
    429/5xx is retried with exponential backoff (a ``Retry-After`` on a 429
    wins, capped at :data:`RETRY_AFTER_MAX_SECONDS`). When ``archive`` is set,
    every call -- success or failure -- appends one JSONL record: stage,
    request (images replaced by sha256 + size), response, usage, latency and
    model. The API token is only ever in the ``Authorization`` header; it is
    never logged or archived.

    ``base_url`` / ``url`` exist so a Jev (or other System One) backend is a
    base-URL + auth swap.
    """

    def __init__(
        self,
        account_id: str,
        token: str,
        *,
        client: httpx.AsyncClient,
        model: str = "clef",
        archive: Path | None = None,
        base_url: str = DEFAULT_BASE_URL,
        url: str | None = None,
        min_interval: float = 0.0,
        max_attempts: int = 4,
        retry_base_delay: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if model not in CLEF_MODELS:
            raise ValueError(f"model must be one of {CLEF_MODELS}, got {model!r}")
        self.model = model
        self.archive = Path(archive) if archive is not None else None
        self.url = url or f"{base_url.rstrip('/')}/accounts/{account_id}/ai/run/@cf/cloudflare/{model}"
        self._token = token
        self._client = client
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._archive_lock = asyncio.Lock()
        self._last_request = float("-inf")

    @classmethod
    def from_env(cls, *, client: httpx.AsyncClient, model: str = "clef", **kwargs: Any) -> ClefDecisionModel:
        """Build from ``CLOUDFLARE_ACCOUNT_ID`` / ``CLOUDFLARE_API_TOKEN`` (``.env`` honoured)."""
        load_dotenv()
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        if not account_id or not token:
            raise DecisionModelError("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN must be set")
        return cls(account_id, token, client=client, model=model, **kwargs)

    def __repr__(self) -> str:  # never expose the token
        return f"ClefDecisionModel(model={self.model!r}, url={self.url!r})"

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_request + self._min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last_request = self._clock()

    async def _post_with_retry(self, body: dict[str, Any]) -> tuple[httpx.Response, int]:
        headers = {"Authorization": f"Bearer {self._token}"}
        delay = self._retry_base_delay
        for attempt in range(1, self._max_attempts + 1):
            await self._throttle()
            response = await self._client.post(self.url, json=body, headers=headers)
            if response.status_code not in _RETRYABLE_STATUSES:
                return response, attempt
            logger.bind(stage="decision").debug(
                "decision call retryable", http_status=response.status_code, attempt=attempt
            )
            if attempt == self._max_attempts:
                return response, attempt
            wait = delay
            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None:
                    wait = min(retry_after, RETRY_AFTER_MAX_SECONDS)
            await self._sleep(wait)
            delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover

    async def decide(
        self,
        *,
        stage: str,
        state: str | dict[str, Any] | list[Any],
        questions: Mapping[str, Question],
        images: Sequence[bytes] = (),
    ) -> dict[str, Answer]:
        body = build_request(model=self.model, state=state, questions=questions, images=images)
        started = time.perf_counter()
        response, attempts = await self._post_with_retry(body)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)

        payload: Any = None
        error: str | None = None
        if response.status_code >= 400:
            error = f"HTTP {response.status_code}: {response.text[:500]}"
        else:
            try:
                payload = response.json()
            except ValueError:
                error = f"non-JSON response (HTTP {response.status_code})"
        result: Mapping[str, Any] = {}
        if error is None:
            # The REST API wraps the model output as {"result": ..., "success": ...}.
            result = payload.get("result", payload) if isinstance(payload, dict) else {}
            if not isinstance(result, Mapping):
                result = {}
            if isinstance(payload, dict) and payload.get("success") is False:
                error = f"API reported failure: {payload.get('errors')}"

        answers: dict[str, Answer] = {}
        if error is None:
            try:
                answers = parse_response(result, questions)
            except DecisionModelError as exc:
                error = str(exc)

        await self._archive_call(
            stage=stage,
            body=body,
            response=payload if payload is not None else response.text[:2000],
            usage=result.get("usage"),
            latency_ms=latency_ms,
            attempts=attempts,
            error=error,
        )
        if error is not None:
            raise DecisionModelError(f"{stage}: {error}")
        return answers

    async def _archive_call(
        self,
        *,
        stage: str,
        body: Mapping[str, Any],
        response: Any,
        usage: Any,
        latency_ms: float,
        attempts: int,
        error: str | None,
    ) -> None:
        archive = self.archive
        if archive is None:
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": stage,
            "model": self.model,
            "request": redact_images(body),
            "response": response,
            "usage": usage,
            "latency_ms": latency_ms,
            "attempts": attempts,
            "error": error,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"

        def _append() -> None:
            archive.parent.mkdir(parents=True, exist_ok=True)
            with archive.open("a", encoding="utf-8") as fh:
                fh.write(line)

        async with self._archive_lock:
            await asyncio.to_thread(_append)


AnswersForStage = (
    Mapping[str, Answer]
    | Callable[[str | dict[str, Any] | list[Any], Mapping[str, Question]], Mapping[str, Answer]]
)


@dataclass
class MockDecisionModel:
    """Deterministic offline :class:`DecisionModel`: canned answers per ``stage``.

    ``answers_by_stage[stage]`` is either a ``{question_id: Answer}`` mapping
    returned as-is, or a callable ``(state, questions) -> mapping`` for tests
    that must vary by input. Every call is recorded in ``.calls`` so tests can
    assert what a stage sent. An unknown stage, or a missing/mistyped answer,
    raises :class:`DecisionModelError` -- the same contract the real backend
    enforces.
    """

    answers_by_stage: dict[str, AnswersForStage] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def decide(
        self,
        *,
        stage: str,
        state: str | dict[str, Any] | list[Any],
        questions: Mapping[str, Question],
        images: Sequence[bytes] = (),
    ) -> dict[str, Answer]:
        validate_questions(questions)
        self.calls.append({"stage": stage, "state": state, "questions": dict(questions), "images": list(images)})
        if stage not in self.answers_by_stage:
            raise DecisionModelError(f"MockDecisionModel has no canned answers for stage {stage!r}")
        value = self.answers_by_stage[stage]
        canned = value(state, questions) if callable(value) else value
        answers: dict[str, Answer] = {}
        for qid, question in questions.items():
            if qid not in canned:
                raise DecisionModelError(f"no canned answer for question {qid!r} in stage {stage!r}")
            answer = canned[qid]
            if not isinstance(answer, _ANSWER_TYPE_FOR[type(question)]):
                raise DecisionModelError(f"question {qid!r} is {type(question).__name__} but got {type(answer).__name__}")
            answers[qid] = answer
        return answers
