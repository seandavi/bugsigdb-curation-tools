"""Repo-root pytest conftest.

`benchmarks/figure-extraction/retrieve.py` is a standalone module (not part
of the installed `bugsigdb_curation` package — it's tooling for the figure
benchmark, not the curation pipeline), so it isn't importable via the normal
package path. Add its directory to `sys.path` so `tests/test_figbench_retrieve.py`
can do a plain `import retrieve`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from loguru import logger

_FIGBENCH_DIR = Path(__file__).parent / "benchmarks" / "figure-extraction"
if str(_FIGBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_FIGBENCH_DIR))


@pytest.fixture(autouse=True)
def _no_ambient_ncbi_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most offline tests construct `NcbiTaxonomyResolver` via `.load()` (the
    real path `curate`/`curate_async` uses), which auto-resolves
    `NCBI_API_KEY`/`NCBI_EUTILS_API_KEY` from the environment (see
    `bugsigdb_curation.curator.taxonomy.resolve_ncbi_api_key`). Clear both
    for every test by default so a developer's/CI's ambient shell env can
    never silently add an `api_key` param to a mocked esearch request and
    break URL matching -- tests that specifically want to exercise API-key
    resolution set it back with `monkeypatch.setenv(...)` themselves.

    Clearing the env vars alone isn't enough: `resolve_ncbi_api_key()` (and
    `resolve_google_api_key()` in `curator.model`) call `load_dotenv()`
    first, whose default `find_dotenv` walks up from the *module file*'s
    directory (not CWD) looking for a `.env` -- so a real repo-root `.env`
    gets loaded and silently refills the very env vars this fixture just
    cleared, regardless of CWD or monkeypatch's env sandboxing. Patch both
    modules' `load_dotenv` to a no-op so every test that reaches `.load()`
    (directly or via `curate`/`curate_async`) is hermetic against whatever
    `.env` happens to exist on disk, not just against the ambient shell env.
    Same class of bug as the Google-key fix in commit 5ba07fe, generalized
    here so it protects every test by default instead of test-by-test."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EUTILS_API_KEY", raising=False)
    monkeypatch.setattr("bugsigdb_curation.curator.taxonomy.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr("bugsigdb_curation.curator.model.load_dotenv", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolated_taxonomy_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PR-2 wired `NcbiTaxonomyResolver.load()` / `TaxonomyResolver.load()` /
    the `curate`/`eval score` CLI commands to auto-resolve a local taxonomy
    `.duckdb` via `taxonomy.paths.resolve_optional_db_path`'s cache-dir
    fallback (`BUGSIGDB_TAXONOMY_DB` -> newest cached `ncbi-taxdump-*.duckdb`
    under `BUGSIGDB_CACHE_DIR`/XDG default). Left alone, that fallback would
    silently pick up a real, machine-local taxonomy DB -- e.g. this repo's
    own `bugsigdb taxonomy build` output on a developer's machine -- making
    tests depend on host state they never asked for. Point `BUGSIGDB_CACHE_DIR`
    at a fresh per-test `tmp_path` subdirectory by default so that fallback
    always finds nothing (-> `None`, live-only/no-DB fallback) unless a test
    explicitly builds/points at its own fixture DB. Same rationale as
    `_no_ambient_ncbi_api_key` above, just for the taxonomy DB instead of the
    API key. Tests that specifically exercise `BUGSIGDB_CACHE_DIR`/
    `BUGSIGDB_TAXONOMY_DB` resolution (e.g. test_taxonomy_paths.py,
    test_taxonomy_cli.py) override this via their own monkeypatch afterward
    -- autouse fixtures defined in a parent conftest run before same-scope
    ones in a test module, so a later `monkeypatch.setenv`/`delenv` in the
    test file always wins.
    """
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "isolated-bugsigdb-cache"))
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)


_QUIETED_LOGGER_NAMES = ("litellm", "LiteLLM", "LiteLLM Proxy", "LiteLLM Router", "httpx", "httpcore")


@pytest.fixture(autouse=True)
def _reset_global_logging_state():
    """Undo `bugsigdb_curation.obs.configure_logging`'s process-wide side effects after each test.

    `configure_logging` (exercised by any `curate`/`eval score` CLI test via
    `typer.testing.CliRunner`) mutates two global singletons on purpose --
    loguru's sink registry and the stdlib root logger's handlers -- since
    that's exactly what a CLI's logging setup is supposed to do for the rest
    of *that process's* life. Inside one pytest session, though, that
    leftover state otherwise survives into unrelated later tests: loguru's
    sink was added pointing at `sys.stderr` as `CliRunner.invoke()` had
    swapped it in (a buffer that invoke() itself closes on exit), so any log
    record reaching that stale sink afterward (e.g. a totally unrelated
    test's own `logging.info(...)` call, now routed through the
    `InterceptHandler` `configure_logging` installed on the root logger)
    fails to write -- which loguru handles by printing its own "Logging
    error" traceback to the *real* stderr, corrupting whatever unrelated
    `CliRunner` invocation happens to be capturing stderr at that moment
    (see `tests/test_cli_validate.py`'s pre-fix failures). Reset both
    after every test, regardless of whether that test touched logging at
    all, so no test ever depends on run order here.
    """
    yield
    logger.remove()
    logger.add(sys.stderr)  # loguru's own stock default sink

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    for name in _QUIETED_LOGGER_NAMES:
        third_party_logger = logging.getLogger(name)
        third_party_logger.handlers.clear()
        third_party_logger.setLevel(logging.NOTSET)
        third_party_logger.propagate = True
