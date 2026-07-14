"""Unit tests for `bugsigdb_curation.taxonomy.paths` (CLI flag > env var > XDG default).

Monkeypatches `HOME`/`XDG_CACHE_HOME`/`BUGSIGDB_CACHE_DIR`/`BUGSIGDB_TAXONOMY_DB`
and points everything at `tmp_path` -- never touches the real `~/.cache`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bugsigdb_curation.taxonomy.paths import (
    default_cache_root,
    default_db_path,
    default_dumps_dir,
    resolve_db_path,
    resolve_optional_db_path,
    taxonomy_cache_root,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with none of the taxonomy env vars set, so each
    test opts in to exactly the ones it's exercising."""
    monkeypatch.delenv("BUGSIGDB_CACHE_DIR", raising=False)
    monkeypatch.delenv("BUGSIGDB_TAXONOMY_DB", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)


def test_default_cache_root_falls_back_to_home_dot_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_cache_root() == tmp_path / ".cache" / "bugsigdb"


def test_default_cache_root_honors_xdg_cache_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    xdg = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    assert default_cache_root() == xdg / "bugsigdb"


def test_default_cache_root_honors_bugsigdb_cache_dir_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    xdg = tmp_path / "xdg-cache"
    override = tmp_path / "custom-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))  # should be ignored: BUGSIGDB_CACHE_DIR wins
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(override))
    assert default_cache_root() == override


def test_default_cache_root_expands_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", "~/custom-cache")
    assert default_cache_root() == tmp_path / "custom-cache"


def test_taxonomy_cache_root_creates_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    root = taxonomy_cache_root()
    assert root == tmp_path / "cache" / "taxonomy"
    assert root.is_dir()


def test_default_dumps_dir_and_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    dumps_dir = default_dumps_dir("2026-07-14")
    assert dumps_dir == tmp_path / "cache" / "taxonomy" / "dumps" / "2026-07-14"
    assert dumps_dir.is_dir()
    assert default_db_path("2026-07-14") == tmp_path / "cache" / "taxonomy" / "ncbi-taxdump-2026-07-14.duckdb"


def test_resolve_db_path_cli_flag_wins_over_everything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("BUGSIGDB_TAXONOMY_DB", str(tmp_path / "env.duckdb"))
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    cli_path = tmp_path / "explicit.duckdb"
    assert resolve_db_path(cli_path, release="r1") == cli_path


def test_resolve_db_path_env_db_wins_over_cache_dir_and_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_TAXONOMY_DB", str(tmp_path / "env.duckdb"))
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    assert resolve_db_path(None, release="r1") == tmp_path / "env.duckdb"


def test_resolve_db_path_falls_back_to_default_under_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    assert resolve_db_path(None, release="2026-07-14") == (
        tmp_path / "cache" / "taxonomy" / "ncbi-taxdump-2026-07-14.duckdb"
    )


def test_resolve_db_path_expands_cli_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    resolved = resolve_db_path(Path("~/somewhere.duckdb"), release="r1")
    assert resolved == tmp_path / "somewhere.duckdb"


def test_resolve_db_path_with_no_release_and_exactly_one_cached_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    only_db = default_db_path("r1")
    only_db.touch()
    assert resolve_db_path(None) == only_db


def test_resolve_db_path_with_no_release_and_no_cached_db_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    with pytest.raises(FileNotFoundError):
        resolve_db_path(None)


def test_resolve_db_path_with_no_release_and_multiple_cached_dbs_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    default_db_path("r1").touch()
    default_db_path("r2").touch()
    with pytest.raises(ValueError, match="multiple cached"):
        resolve_db_path(None)


# --- resolve_optional_db_path: newest-cached-DB fallback (Fix 3) ---------------------------


def test_resolve_optional_db_path_no_candidates_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    assert resolve_optional_db_path(None) is None


def test_resolve_optional_db_path_single_candidate_returns_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    only_db = default_db_path("2026-01-01")
    only_db.touch()
    assert resolve_optional_db_path(None) == only_db


def test_resolve_optional_db_path_picks_newest_by_mtime_not_by_filename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Newest-BY-MTIME wins, not newest-by-filename: deliberately give the
    alphabetically EARLIER-dated filename the LATER mtime, and assert it
    still wins -- proving the pick is driven by mtime, not name order."""
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    alphabetically_later_name = default_db_path("2026-06-01")
    alphabetically_earlier_name = default_db_path("2026-01-01")
    alphabetically_later_name.touch()
    alphabetically_earlier_name.touch()
    now = time.time()
    os.utime(alphabetically_later_name, (now - 100, now - 100))  # older mtime
    os.utime(alphabetically_earlier_name, (now, now))  # newer mtime

    assert resolve_optional_db_path(None) == alphabetically_earlier_name


def test_resolve_optional_db_path_mtime_tie_breaks_toward_newer_release_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Fix 3: on a genuine mtime TIE (e.g. a batch-extracted taxdump whose
    `.duckdb` files share a filesystem-resolution-limited mtime),
    `max(candidates, key=mtime)` over an alphabetically-sorted candidate
    list silently kept the FIRST max-mtime candidate it saw -- the
    lexicographically SMALLEST filename, i.e. the OLDER dated release. The
    fix must instead deterministically pick the lexicographically GREATEST
    filename (here, the newer dated release) on a genuine tie."""
    monkeypatch.setenv("BUGSIGDB_CACHE_DIR", str(tmp_path / "cache"))
    older_release = default_db_path("2026-01-01")
    newer_release = default_db_path("2026-06-01")
    older_release.touch()
    newer_release.touch()
    tied_time = time.time()
    os.utime(older_release, (tied_time, tied_time))
    os.utime(newer_release, (tied_time, tied_time))

    assert resolve_optional_db_path(None) == newer_release
