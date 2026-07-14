"""Repo-root pytest conftest.

`benchmarks/figure-extraction/retrieve.py` is a standalone module (not part
of the installed `bugsigdb_curation` package — it's tooling for the figure
benchmark, not the curation pipeline), so it isn't importable via the normal
package path. Add its directory to `sys.path` so `tests/test_figbench_retrieve.py`
can do a plain `import retrieve`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
