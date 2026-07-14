"""The Model seam: an LLM abstraction every curator stage calls through.

Two implementations:

* :class:`LiteLLMModel` -- the real backend, wrapping `litellm.completion`.
  Per the PI's L016 decision (LiteLLM, Google-first: no Anthropic key, so
  Gemini via Google AI Studio is the default; Claude-if-ever would route via
  `vertex_ai/claude-*`, never the bare Anthropic API), the default model is
  **`gemini/gemini-3.1-flash-lite`** -- cheap and multimodal, so the same
  backend serves both the text extraction stages and the S5b figure-vision
  path (a Gemini message list can mix text and image content blocks).
* :class:`MockModel` -- a deterministic, offline stand-in that returns a
  canned per-`stage` response (built-in defaults, overridable), so the full
  pipeline runs end-to-end in CI with no API key at all. Every stage-level
  test and the walking-skeleton end-to-end test inject this.

Both implement the same `Model.complete(*, stage, messages) -> dict`
contract: `messages` is an OpenAI/litellm-style chat list (each message's
`content` is either a plain string or a list of `{"type": "text", ...}` /
`{"type": "image_url", ...}` blocks -- see :func:`build_text_content` /
:func:`build_image_content`); the return value is always a parsed JSON
object (never a string), since every stage speaks JSON-in/JSON-out.
"""

from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

import litellm
from dotenv import load_dotenv

#: Google-first per L016: cheap, current, and multimodal (serves S5b's
#: figure-vision path with the same backend as the text stages).
DEFAULT_MODEL = "gemini/gemini-3.1-flash-lite"

#: Env var names to try for a Google AI Studio key, most-canonical first.
#: `GOOGLE_API_KEY` is what LiteLLM's `gemini/` provider reads *natively*
#: (as does `GEMINI_API_KEY`) -- if either is set, litellm can pick it up on
#: its own and passing `api_key=` explicitly is merely redundant-but-harmless.
#: `GOOGLE_GENERATIVE_AI_API_KEY` is NOT one of litellm's native names (it's
#: the name this dev sandbox happens to export via `~/.profile`), so when
#: that's the only one present it must be threaded through explicitly as
#: `api_key=` to `litellm.completion` rather than relied on implicitly.
_GOOGLE_KEY_ENV_NAMES = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY")


class ModelError(RuntimeError):
    """Raised when a `Model.complete` call cannot produce parseable JSON."""


def resolve_google_api_key() -> str | None:
    """Find a usable Google AI Studio key in the environment, or None.

    Loads a repo-root (or CWD-upward) `.env` file first via `python-dotenv`
    (a no-op if none exists), then checks `_GOOGLE_KEY_ENV_NAMES` in order.
    Never logs or returns anything about *which* var matched beyond the
    value itself -- callers must not print this.
    """
    load_dotenv()
    for name in _GOOGLE_KEY_ENV_NAMES:
        value = os.environ.get(name)
        if value:
            return value
    return None


def build_text_content(text: str) -> dict[str, Any]:
    """One text content block for a multimodal-style chat message."""
    return {"type": "text", "text": text}


def build_image_content(image_bytes: bytes, *, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """One image content block (base64 data URL) for a multimodal chat message.

    This is the shape litellm/OpenAI-style multimodal messages expect, and
    Gemini (the default backend) accepts it directly -- see
    https://docs.litellm.ai/docs/completion/vision.
    """
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}}


# ---------------------------------------------------------------------------
# the abstraction
# ---------------------------------------------------------------------------


class Model(ABC):
    """LLM completion abstraction every curator stage calls through.

    `stage` names the calling pipeline stage (e.g. `"study_design"`,
    `"segment"`, `"experiment_metadata"`, `"signature_extract"`) -- used by
    `MockModel` to select a canned response, and by `LiteLLMModel` only for
    error messages/logging (it does not change model behavior).
    """

    @abstractmethod
    def complete(self, *, stage: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Run one JSON-mode completion for `stage`; returns the parsed JSON object."""


# ---------------------------------------------------------------------------
# real backend
# ---------------------------------------------------------------------------

#: Matches a fenced ```json ... ``` (or bare ``` ... ```) block, in case a
#: model wraps its JSON in markdown despite the JSON-mode request.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    """Parse `text` as a JSON object, tolerating markdown fences/surrounding prose.

    Tries, in order: (1) the whole string as-is, (2) the contents of a
    ```json fenced block if present, (3) the first balanced `{...}`
    substring found by bracket counting. Returns None (never raises) if none
    of these yield a JSON *object* (a JSON array/scalar is not accepted --
    every stage's contract is an object).
    """
    candidates: list[str] = [text.strip()]
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    start = text.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


@dataclass
class LiteLLMModel(Model):
    """Real `Model` backend: wraps `litellm.completion`.

    Requests JSON-mode via `response_format={"type": "json_object"}` and
    parses tolerantly (`_parse_json_loose`); if that still fails, retries
    once with an explicit "reply with ONLY JSON" follow-up message before
    raising `ModelError`. `api_key`, if not given, is resolved via
    `resolve_google_api_key()` at construction time and threaded through
    explicitly to every call (see that function's docstring for why this
    matters even though litellm can sometimes find a key on its own).
    """

    model: str = DEFAULT_MODEL
    api_key: str | None = None
    temperature: float = 0.0
    #: Injectable for tests that want to construct with a fake completion fn
    #: directly; if left None, `_call` looks up `litellm.completion` fresh on
    #: every call, so a test can equally just `monkeypatch.setattr(litellm,
    #: "completion", fake)` on an already-constructed instance.
    _completion_fn: Callable[..., Any] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = resolve_google_api_key()

    def complete(self, *, stage: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        text = self._call(messages)
        parsed = _parse_json_loose(text)
        if parsed is not None:
            return parsed

        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "Your previous reply was not valid JSON. Reply again with ONLY a single "
                    "valid JSON object -- no prose, no markdown code fences."
                ),
            },
        ]
        text = self._call(retry_messages)
        parsed = _parse_json_loose(text)
        if parsed is None:
            raise ModelError(
                f"Model {self.model!r} returned malformed JSON for stage {stage!r} "
                f"after one retry: {text[:500]!r}"
            )
        return parsed

    def _call(self, messages: list[dict[str, Any]]) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        fn = self._completion_fn or litellm.completion
        response = fn(**kwargs)
        choice = response["choices"][0]
        content = choice["message"]["content"]
        return content or ""


# ---------------------------------------------------------------------------
# offline mock backend
# ---------------------------------------------------------------------------

#: Built-in canned responses so `MockModel()` (no config) is enough to run
#: the whole pipeline end-to-end offline. Deliberately generic/fixed rather
#: than input-sensitive -- a thin walking skeleton, not a simulation of
#: real model reasoning; see module docstring and the pipeline's own tests
#: for where per-test overrides plug in via `MockModel(responses={...})`.
DEFAULT_MOCK_RESPONSES: dict[str, dict[str, Any]] = {
    "study_design": {"study_design": ["case-control"]},
    "segment": {
        "experiments": [
            {"index": 0, "description": "Cases vs. controls, fecal microbiome, 16S."},
        ]
    },
    "experiment_metadata": {
        "host_species": "Homo sapiens",
        "body_site": ["Feces"],
        "condition": ["Disease"],
        "group_0_name": "Control",
        "group_1_name": "Case",
        "sequencing_type": "16S",
        "statistical_test": ["LEfSe"],
        "mht_correction": False,
    },
    "signature_extract": {
        "taxa": [
            {"name": "Faecalibacterium prausnitzii", "direction": "decreased", "proposed_ncbi_id": 853},
            {"name": "Escherichia coli", "direction": "increased", "proposed_ncbi_id": 562},
        ]
    },
    # --- split A1 (curator.ner / curator.reconcile), used by split-verify/split-panel ---
    # Names-only mirror of "signature_extract" above, minus the id proposal
    # (split A1's whole point -- ids come only from curator.reconcile's
    # TaxonomyDB lookup, never a model guess).
    "signature_ner": {
        "taxa": [
            {"name": "Faecalibacterium prausnitzii", "direction": "decreased"},
            {"name": "Escherichia coli", "direction": "increased"},
        ]
    },
    # "never guess": the fixed default declines to pick among ambiguous
    # candidates it can't actually see (a real disambiguation call always
    # gets the true candidate set; a test exercising a real pick supplies
    # its own `responses={"taxon_disambiguate": ...}` override).
    "taxon_disambiguate": {"chosen_tax_id": None},
    # --- split-verify's A2 (curator.verify) ---
    "verify_taxon_in_source": {
        "results": [
            {"name": "Faecalibacterium prausnitzii", "in_source": True},
            {"name": "Escherichia coli", "in_source": True},
        ]
    },
    # Fixed (not taxon-aware, per this dict's own "generic/fixed" design --
    # see module docstring): agrees with "signature_ner"'s "decreased" claim
    # for Faecalibacterium prausnitzii; for Escherichia coli (claimed
    # "increased") this default disagrees and the bounded repair loop
    # stabilizes on a flip to "decreased" -- still a schema-valid record,
    # just not byte-identical to fused-lean's default output (expected: this
    # is a materially different design). A test asserting exact
    # confirm/flip/drop behavior supplies its own override.
    "verify_direction": {"direction": "decreased"},
    # --- split-panel's A2 (curator.panel) ---
    # Identical to "signature_ner" by default, so the reviewer agrees with
    # the extractor on every taxon out of the box (no reconciliation/ground-
    # check calls needed for the default happy path); a test exercising
    # disagreement/recall supplies its own override.
    "review_signature": {
        "taxa": [
            {"name": "Faecalibacterium prausnitzii", "direction": "decreased"},
            {"name": "Escherichia coli", "direction": "increased"},
        ]
    },
    "review_reconcile_direction": {"direction": "decreased"},
    "review_ground_check": {
        "results": [
            {"name": "Faecalibacterium prausnitzii", "in_source": True},
            {"name": "Escherichia coli", "in_source": True},
        ]
    },
}


@dataclass
class MockModel(Model):
    """Deterministic offline `Model`: canned per-`stage` responses, no network.

    `responses` overrides `DEFAULT_MOCK_RESPONSES` per stage; a value may be
    a plain dict (returned as-is every call) or a callable
    `(messages) -> dict` for tests that need to vary the response by input.
    Every call is recorded to `.calls` (`{"stage": ..., "messages": ...}`)
    so tests can assert what a stage sent (e.g. multimodal image content).
    """

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, *, stage: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append({"stage": stage, "messages": messages})
        if stage in self.responses:
            value = self.responses[stage]
            return value(messages) if callable(value) else value
        if stage in DEFAULT_MOCK_RESPONSES:
            return DEFAULT_MOCK_RESPONSES[stage]
        raise ModelError(f"MockModel has no canned response for stage {stage!r}")
