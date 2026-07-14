"""Structured logging (deployment observability) for the de-novo curator.

`configure_logging()` is the single entry point every CLI command that runs
the curator/eval pipeline calls at startup: it strips loguru's default sink
and installs exactly one -- a human-readable console sink by default, or a
`serialize=True` JSON-lines sink for `BUGSIGDB_LOG_FORMAT=json` -- and
reroutes stdlib `logging` (the noisy per-call chatter `httpx`/`litellm` log
through the stdlib `logging` module, not loguru) into that same sink via the
documented loguru `InterceptHandler` recipe.

**Not named `logging.py`** -- that would shadow the stdlib module every
other file in this package (and every third-party dependency: `httpx`,
`litellm`, `duckdb`, ...) imports.

**Why this module exists**: a real `curate --smoke` run's console output was
dominated by one `LiteLLM ... DeprecationWarning: temperature/top_p/top_k
...` line per model call (litellm's `verbose_logger` attaches its own raw
`StreamHandler` directly to the `"LiteLLM"` stdlib logger at import time --
see `_quiet_and_reroute_third_party_loggers`) -- the curator's own
stage-by-stage progress was unreadable underneath it. This module (a) mutes
that specific chatter to WARNING+ and (b) gives every curator stage a
structured, greppable/JSON-parseable event stream instead of ad-hoc
`print`/`Console.print` lines.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys

from loguru import logger

__all__ = ["configure_logging"]

#: Env vars a CLI invocation may rely on instead of passing `--log-format`/
#: `--log-level` explicitly; an explicit function argument always wins over
#: these (see `configure_logging`).
LOG_FORMAT_ENV_VAR = "BUGSIGDB_LOG_FORMAT"
LOG_LEVEL_ENV_VAR = "BUGSIGDB_LOG_LEVEL"

_DEFAULT_FORMAT = "console"
_DEFAULT_LEVEL = "INFO"
_VALID_FORMATS = ("console", "json")

#: Third-party stdlib loggers whose default INFO/DEBUG output drowns out the
#: curator's own structured events -- see module docstring. Only the level
#: floor is raised (to WARNING); a real ERROR/WARNING from these libraries
#: still comes through the same sink, nothing is dropped outright. `httpcore`
#: is httpx's own transport layer (equally chatty at INFO/DEBUG -- one "send
#: request headers"/"receive response" pair per HTTP call); `"LiteLLM"`
#: (capital, with the space-separated proxy/router variants) is litellm's
#: actual stdlib logger name (`litellm._logging.verbose_logger`), not the
#: lowercase `"litellm"` a caller might guess -- both are covered here.
_QUIET_LOGGER_NAMES = (
    "litellm",
    "LiteLLM",
    "LiteLLM Proxy",
    "LiteLLM Router",
    "httpx",
    "httpcore",
)


class _InterceptHandler(logging.Handler):
    """Reroutes stdlib `logging` records into loguru (loguru's documented recipe).

    Every third-party dependency the curator calls (`httpx`, `litellm`,
    `urllib3`, ...) logs through the stdlib `logging` module under the hood;
    without this handler installed on the root logger, none of those records
    ever reach the sink `configure_logging` installs -- they'd only ever
    appear via whatever ad-hoc handler(s) that library wires up for itself
    (see `_quiet_and_reroute_third_party_loggers`'s docstring for why
    litellm's own handler is explicitly stripped so this is the *only* path
    its output takes).
    """

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover -- exercised via logging calls in tests
        try:
            level: int | str = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the stdlib logging call site (skip frames inside `logging`
        # itself) so loguru attributes the record to its real origin rather
        # than to this handler.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _quiet_and_reroute_third_party_loggers() -> None:
    """Route stdlib `logging` into loguru, and mute `_QUIET_LOGGER_NAMES` to WARNING.

    `logging.basicConfig(..., level=0, force=True)` puts the root logger at
    level 0 ("process everything") and defers *all* actual level filtering
    to the loguru sink(s) `configure_logging` installs -- a single place to
    reason about verbosity instead of two independent level knobs.

    litellm attaches its own `StreamHandler` directly to the `"LiteLLM"`
    logger (and its Proxy/Router siblings) at import time, printing raw
    ANSI-colored text straight to a stream -- entirely bypassing loguru.
    `.handlers.clear()` strips that handler so propagation to the root
    logger's `_InterceptHandler` (and from there, loguru) is the *only*
    remaining path for that output, matching this module's "one sink" design.
    """
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in _QUIET_LOGGER_NAMES:
        third_party_logger = logging.getLogger(name)
        third_party_logger.handlers.clear()
        third_party_logger.propagate = True
        third_party_logger.setLevel(logging.WARNING)


def _console_formatter(record: dict) -> str:
    """Build a human-readable format string for one record: time/level/stage + message + kv fields.

    A callable `format` (rather than a static string) is required here
    because `record["extra"]`'s keys vary call to call (`logger.bind(...)`
    fields, plus whatever `logger.contextualize(...)` had open) -- a static
    `"{extra[stage]} ... {message}"` format would `KeyError` on any record
    missing one of those keys (e.g. an intercepted stdlib `logging` record,
    which carries no `stage` at all). `logger.configure(extra={"stage":
    "-"})` in `configure_logging` supplies the "-" fallback seen below.

    The returned string is itself passed through loguru's own `.format()`
    pass (it recognizes `{time}`/`{level}`/`{message}`/... placeholders and
    `<tag>` colorizing markup) -- so any *already-substituted* literal text
    this function embeds (the stage name, the rendered key=value suffix)
    must have its own `{`/`}` doubled first, or a logged value that happens
    to contain a brace would corrupt that second formatting pass.
    """
    extra = record["extra"]
    stage = str(extra.get("stage", "-")).replace("{", "{{").replace("}", "}}")
    kv_parts = [f"{key}={value}" for key, value in extra.items() if key != "stage"]
    kv_suffix = ("  " + " ".join(kv_parts)) if kv_parts else ""
    kv_suffix = kv_suffix.replace("{", "{{").replace("}", "}}")

    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
        f"<cyan>{stage: <10}</cyan> | <level>{{message}}</level>{kv_suffix}\n{{exception}}"
    )


def configure_logging(*, fmt: str | None = None, level: str | None = None) -> None:
    """Install the curator's one structured-logging sink; safe to call more than once.

    Resolution order for each of `fmt`/`level`: the explicit keyword
    argument (e.g. a CLI's `--log-format`/`--log-level`) wins if given,
    otherwise `BUGSIGDB_LOG_FORMAT`/`BUGSIGDB_LOG_LEVEL`, otherwise
    `"console"`/`"INFO"`.

    * `fmt="console"` (default): human-readable, colored automatically when
      the sink stream is a TTY (loguru's own auto-detection -- `colorize`
      is left unset), one line per record: time | level | stage | message,
      followed by any bound structured fields as `key=value` pairs.
    * `fmt="json"`: one JSON object per line (`serialize=True`) -- every
      bound/contextualized field is included under `record.extra`, ready
      for a log shipper/`jq` in a deployed environment.

    Idempotent: every call starts by removing *all* existing loguru sinks
    (`logger.remove()`), so calling this twice in one process (e.g. a test
    harness, or a CLI command that's invoked more than once in-process)
    always ends with exactly one sink installed, never a duplicate.
    """
    resolved_format = (fmt or os.environ.get(LOG_FORMAT_ENV_VAR) or _DEFAULT_FORMAT).strip().lower()
    if resolved_format not in _VALID_FORMATS:
        raise ValueError(
            f"invalid log format {resolved_format!r} (from --log-format/{LOG_FORMAT_ENV_VAR}); "
            f"must be one of {_VALID_FORMATS}"
        )
    resolved_level = (level or os.environ.get(LOG_LEVEL_ENV_VAR) or _DEFAULT_LEVEL).strip().upper()

    logger.remove()
    # A default so `_console_formatter`/JSON's `record.extra` never KeyErrors
    # on a record that never went through `logger.bind(stage=...)` (e.g. an
    # intercepted stdlib `logging` call).
    logger.configure(extra={"stage": "-"})

    if resolved_format == "json":
        logger.add(sys.stderr, level=resolved_level, serialize=True, backtrace=False, diagnose=False)
    else:
        logger.add(
            sys.stderr,
            level=resolved_level,
            format=_console_formatter,
            backtrace=False,
            diagnose=False,
        )

    _quiet_and_reroute_third_party_loggers()
