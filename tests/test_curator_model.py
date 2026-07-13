"""Unit tests for `bugsigdb_curation.curator.model` -- the Model seam.

`MockModel` tests are pure/offline. The `LiteLLMModel` tests mock
`litellm.completion` (never touch the network) and assert the adapter sends
the right model string and builds the documented multimodal message shape.
One `@pytest.mark.network` test does a single tiny real completion and is
skipped unless a usable Google AI Studio key is present in the environment.
"""

from __future__ import annotations

import os

import litellm
import pytest

from bugsigdb_curation.curator.model import (
    DEFAULT_MODEL,
    LiteLLMModel,
    ModelError,
    MockModel,
    _GOOGLE_KEY_ENV_NAMES,
    _parse_json_loose,
    build_image_content,
    build_text_content,
    resolve_google_api_key,
)

# --- MockModel ---------------------------------------------------------------------------


def test_mock_model_returns_default_response_for_known_stage():
    model = MockModel()
    result = model.complete(stage="study_design", messages=[{"role": "user", "content": "x"}])
    assert result == {"study_design": ["case-control"]}


def test_mock_model_is_deterministic_across_calls():
    model = MockModel()
    a = model.complete(stage="segment", messages=[])
    b = model.complete(stage="segment", messages=[{"role": "user", "content": "different"}])
    assert a == b


def test_mock_model_records_calls():
    model = MockModel()
    messages = [{"role": "user", "content": "hello"}]
    model.complete(stage="study_design", messages=messages)
    assert model.calls == [{"stage": "study_design", "messages": messages}]


def test_mock_model_override_via_responses_dict():
    model = MockModel(responses={"study_design": {"study_design": ["laboratory experiment"]}})
    result = model.complete(stage="study_design", messages=[])
    assert result == {"study_design": ["laboratory experiment"]}


def test_mock_model_override_via_callable():
    model = MockModel(responses={"study_design": lambda messages: {"n_messages": len(messages)}})
    result = model.complete(stage="study_design", messages=[{"role": "user", "content": "a"}, {"role": "user", "content": "b"}])
    assert result == {"n_messages": 2}


def test_mock_model_raises_for_unknown_stage():
    model = MockModel()
    with pytest.raises(ModelError):
        model.complete(stage="not_a_real_stage", messages=[])


# --- message-building helpers -------------------------------------------------------------


def test_build_text_content():
    assert build_text_content("hello") == {"type": "text", "text": "hello"}


def test_build_image_content_encodes_base64_data_url():
    block = build_image_content(b"fake-bytes", mime_type="image/png")
    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    import base64

    encoded = url.split(",", 1)[1]
    assert base64.b64decode(encoded) == b"fake-bytes"


# --- _parse_json_loose ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('  {"a": 1}  \n', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure, here you go:\n```\n{"a": 1}\n```\nHope that helps!', {"a": 1}),
        ('Here is the JSON: {"a": 1} -- as requested.', {"a": 1}),
        ("not json at all", None),
        ("[1, 2, 3]", None),  # a JSON array is not an object; every stage contract is an object
    ],
)
def test_parse_json_loose(text, expected):
    assert _parse_json_loose(text) == expected


# --- resolve_google_api_key ------------------------------------------------------------------


def test_resolve_google_api_key_checks_names_in_priority_order(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env to accidentally pick up
    for name in _GOOGLE_KEY_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    assert resolve_google_api_key() is None

    monkeypatch.setenv("GOOGLE_GENERATIVE_AI_API_KEY", "fallback-key")
    assert resolve_google_api_key() == "fallback-key"

    monkeypatch.setenv("GOOGLE_API_KEY", "canonical-key")
    assert resolve_google_api_key() == "canonical-key"


# --- LiteLLMModel (mocked litellm.completion, no network) ------------------------------------


def _fake_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_litellm_model_defaults_to_gemini_flash_lite():
    model = LiteLLMModel(api_key="test-key")
    assert model.model == DEFAULT_MODEL


def test_litellm_model_calls_completion_with_model_string_and_json_mode(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _fake_response('{"ok": true}')

    monkeypatch.setattr(litellm, "completion", fake_completion)
    model = LiteLLMModel(model="gemini/gemini-3.1-flash-lite", api_key="test-key")

    result = model.complete(stage="study_design", messages=[{"role": "user", "content": "classify this"}])

    assert result == {"ok": True}
    assert captured["model"] == "gemini/gemini-3.1-flash-lite"
    assert captured["api_key"] == "test-key"
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["messages"][0]["content"] == "classify this"


def test_litellm_model_sends_multimodal_message_shape_for_figure_vision(monkeypatch):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _fake_response('{"taxa": []}')

    monkeypatch.setattr(litellm, "completion", fake_completion)
    model = LiteLLMModel(api_key="test-key")

    messages = [
        {
            "role": "user",
            "content": [
                build_text_content("Extract taxa from this LEfSe figure."),
                build_image_content(b"\x89PNG-fake-bytes", mime_type="image/png"),
            ],
        }
    ]
    model.complete(stage="signature_extract", messages=messages)

    sent_content = captured["messages"][0]["content"]
    assert isinstance(sent_content, list)
    assert sent_content[0] == {"type": "text", "text": "Extract taxa from this LEfSe figure."}
    assert sent_content[1]["type"] == "image_url"
    assert sent_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_litellm_model_retries_once_on_malformed_json_then_succeeds(monkeypatch):
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _fake_response("not json, sorry")
        return _fake_response('{"recovered": true}')

    monkeypatch.setattr(litellm, "completion", fake_completion)
    model = LiteLLMModel(api_key="test-key")

    result = model.complete(stage="study_design", messages=[{"role": "user", "content": "x"}])

    assert result == {"recovered": True}
    assert len(calls) == 2
    # the retry appends a "reply with only JSON" nudge, doesn't replace history
    assert len(calls[1]["messages"]) == len(calls[0]["messages"]) + 1


def test_litellm_model_raises_model_error_after_retry_still_malformed(monkeypatch):
    def fake_completion(**kwargs):
        return _fake_response("still not json")

    monkeypatch.setattr(litellm, "completion", fake_completion)
    model = LiteLLMModel(api_key="test-key")

    with pytest.raises(ModelError):
        model.complete(stage="study_design", messages=[{"role": "user", "content": "x"}])


# --- opt-in live smoke test -------------------------------------------------------------------


def _live_key_available() -> bool:
    return any(os.environ.get(name) for name in _GOOGLE_KEY_ENV_NAMES)


@pytest.mark.network
@pytest.mark.skipif(not _live_key_available(), reason="no Google AI Studio key in environment")
def test_litellm_model_live_completion_smoke():
    """One real, tiny completion through the adapter -- proof the real path works.

    Deselected by default (`-m 'not network'`); run explicitly with
    `uv run pytest -m network tests/test_curator_model.py`.
    """
    model = LiteLLMModel()
    result = model.complete(
        stage="study_design",
        messages=[
            {
                "role": "user",
                "content": 'Reply with exactly this JSON object and nothing else: {"ok": true}',
            }
        ],
    )
    assert result.get("ok") is True
