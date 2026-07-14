"""Unit tests for `bugsigdb_curation.obs.configure_logging`.

`_reset_global_logging_state` (repo-root `conftest.py`, autouse) tears down
loguru's sinks and the stdlib root logger's handlers after every test, so
each test here starts from a clean slate regardless of run order.
"""

from __future__ import annotations

import json
import logging

import pytest
from loguru import logger

from bugsigdb_curation.obs import configure_logging


def _sink_count() -> int:
    # Private loguru API, test-only: the public surface is "how many times
    # did I call logger.add", which configure_logging deliberately hides
    # (it's an implementation detail, not something a caller threads
    # through) -- this is the only way to assert "exactly one sink" from
    # outside the module.
    return len(logger._core.handlers)  # noqa: SLF001


# --- sink installation / idempotency -----------------------------------------------------------


def test_configure_logging_installs_exactly_one_sink():
    configure_logging(fmt="console", level="INFO")
    assert _sink_count() == 1


def test_configure_logging_is_idempotent_across_repeated_calls():
    configure_logging(fmt="console", level="INFO")
    configure_logging(fmt="json", level="DEBUG")
    configure_logging(fmt="console", level="WARNING")
    assert _sink_count() == 1


def test_configure_logging_rejects_unknown_format():
    with pytest.raises(ValueError, match="invalid log format"):
        configure_logging(fmt="xml", level="INFO")


# --- console format -----------------------------------------------------------------------------


def test_configure_logging_console_output_is_human_readable_with_stage_and_fields(capsys):
    configure_logging(fmt="console", level="INFO")
    logger.bind(stage="S0").info("resolved", pmcid="PMC123", has_pmc=True)

    captured = capsys.readouterr()
    assert "INFO" in captured.err
    assert "S0" in captured.err
    assert "resolved" in captured.err
    assert "pmcid=PMC123" in captured.err
    assert "has_pmc=True" in captured.err
    # never emitted to stdout -- stdout stays clean for e.g. `curate --pmid`'s
    # own JSON/YAML record output.
    assert captured.out == ""


def test_configure_logging_console_output_survives_braces_in_a_bound_value(capsys):
    """A logged value containing literal `{`/`}` (e.g. a taxon name/JSON
    fragment) must not corrupt the console formatter's own `.format()` pass
    -- see `_console_formatter`'s docstring on brace-escaping."""
    configure_logging(fmt="console", level="INFO")
    logger.bind(stage="S5b").info("signatures extracted", note="{weird}")

    captured = capsys.readouterr()
    assert "note={weird}" in captured.err


# --- json format ----------------------------------------------------------------------------


def test_configure_logging_json_emits_one_parseable_object_per_line_with_bound_fields(capsys):
    configure_logging(fmt="json", level="INFO")
    with logger.contextualize(study_id="21850056", pmid="21850056"):
        logger.bind(stage="S9").info("validated", valid=True, n_problems=0)

    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    extra = parsed["record"]["extra"]
    assert extra["stage"] == "S9"
    assert extra["study_id"] == "21850056"
    assert extra["valid"] is True
    assert extra["n_problems"] == 0
    assert parsed["record"]["message"] == "validated"


# --- level filtering / env resolution ----------------------------------------------------------


def test_configure_logging_level_filters_below_threshold(capsys):
    configure_logging(fmt="console", level="WARNING")
    logger.info("should be suppressed")
    logger.warning("should show")

    captured = capsys.readouterr().err
    assert "should be suppressed" not in captured
    assert "should show" in captured


def test_configure_logging_explicit_args_override_env_vars(monkeypatch, capsys):
    monkeypatch.setenv("BUGSIGDB_LOG_FORMAT", "json")
    monkeypatch.setenv("BUGSIGDB_LOG_LEVEL", "ERROR")

    configure_logging(fmt="console", level="INFO")
    logger.info("visible because the explicit INFO level wins over env ERROR")

    captured = capsys.readouterr().err
    assert "visible because" in captured
    # console (not JSON) format: a bare human-readable line, not a `{...}` object.
    assert not captured.strip().startswith("{")


def test_configure_logging_falls_back_to_env_vars_when_no_explicit_args(monkeypatch, capsys):
    monkeypatch.setenv("BUGSIGDB_LOG_FORMAT", "json")
    monkeypatch.setenv("BUGSIGDB_LOG_LEVEL", "INFO")

    configure_logging()
    logger.info("hello")

    captured = capsys.readouterr().err.strip()
    parsed = json.loads(captured)
    assert parsed["record"]["message"] == "hello"


# --- stdlib logging interception -----------------------------------------------------------------


def test_stdlib_logging_is_routed_through_loguru(capsys):
    configure_logging(fmt="console", level="INFO")
    logging.getLogger("some.other.library").info("a stdlib log record")

    captured = capsys.readouterr().err
    assert "a stdlib log record" in captured


def test_litellm_and_httpx_info_records_are_suppressed_to_warning(capsys):
    configure_logging(fmt="console", level="INFO")
    logging.getLogger("litellm").info("noisy per-call deprecation-style chatter")
    logging.getLogger("LiteLLM").info("noisy per-call deprecation-style chatter")
    logging.getLogger("httpx").info("HTTP Request: GET https://example/ \"200 OK\"")
    logging.getLogger("litellm").warning("a real litellm warning")

    captured = capsys.readouterr().err
    assert "noisy per-call deprecation-style chatter" not in captured
    assert "HTTP Request" not in captured
    assert "a real litellm warning" in captured

    assert logging.getLogger("litellm").level == logging.WARNING
    assert logging.getLogger("httpx").level == logging.WARNING
