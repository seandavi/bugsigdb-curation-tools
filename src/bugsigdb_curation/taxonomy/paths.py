"""Cache/DB path resolution for the taxonomy subpackage.

Precedence (highest first): **CLI flag > env var > XDG-cache default.**

The built `.duckdb` and the raw taxdump it's built from are a large (the
real NCBI taxdump is ~400MB uncompressed), regenerable, machine-global
cache -- not a per-checkout artifact. Each git worktree of this repo has its
own `data/` directory, so defaulting into `data/` would force a redundant
rebuild per worktree; defaulting into the XDG cache instead lets every
worktree/checkout on a machine share one build.

This lives in its own module (not `build.py`/`db.py`) so the curator/scorer
can reuse the exact same resolution logic in the follow-up PR that wires
this resolver in, without importing the build/lookup internals.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Relocates the whole `bugsigdb` cache root (taxonomy artifacts then live
#: under `<BUGSIGDB_CACHE_DIR>/taxonomy/`).
CACHE_DIR_ENV_VAR = "BUGSIGDB_CACHE_DIR"

#: Points directly at one built `.duckdb` file -- consulted by consumers
#: (e.g. `taxonomy lookup`) that want to pin a specific DB without also
#: overriding the whole cache root.
DB_PATH_ENV_VAR = "BUGSIGDB_TAXONOMY_DB"


def default_cache_root() -> Path:
    """The `bugsigdb` cache root.

    `$BUGSIGDB_CACHE_DIR` if set, else `${XDG_CACHE_HOME:-~/.cache}/bugsigdb`.
    Does not create the directory (callers that need it to exist should use
    :func:`taxonomy_cache_root`, which does).
    """
    override = os.environ.get(CACHE_DIR_ENV_VAR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "bugsigdb"


def taxonomy_cache_root() -> Path:
    """The taxonomy cache root, `<cache_root>/taxonomy/`, created on demand."""
    root = default_cache_root() / "taxonomy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_dumps_dir(release: str) -> Path:
    """Where a downloaded/extracted raw taxdump for `release` is cached.

    `<taxonomy_cache_root>/dumps/<release>/`. Created on demand.
    """
    d = taxonomy_cache_root() / "dumps" / release
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_db_path(release: str) -> Path:
    """The default built-DB path for `release`: `<taxonomy_cache_root>/ncbi-taxdump-<release>.duckdb`.

    Does not create the file; `taxonomy_cache_root()` has already ensured
    the parent directory exists.
    """
    return taxonomy_cache_root() / f"ncbi-taxdump-{release}.duckdb"


def resolve_db_path(cli_path: Path | str | None, release: str | None = None) -> Path:
    """Resolve the `.duckdb` path to use: CLI flag > `BUGSIGDB_TAXONOMY_DB` > default.

    `release` is only consulted for the default (`<taxonomy_cache_root>/
    ncbi-taxdump-<release>.duckdb`); it's ignored once a CLI path or the env
    var wins. If resolution would fall through to the default with no
    `release` given, falls back to locating a single cached `.duckdb` under
    the taxonomy cache root: `FileNotFoundError` if there is none, or
    `ValueError` (naming the candidates) if there is more than one and the
    caller needs to disambiguate via `--db`, `--release`, or
    `BUGSIGDB_TAXONOMY_DB`.
    """
    if cli_path is not None:
        return Path(cli_path).expanduser()

    env_path = os.environ.get(DB_PATH_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()

    if release is not None:
        return default_db_path(release)

    candidates = sorted(taxonomy_cache_root().glob("ncbi-taxdump-*.duckdb"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(
            "no --db path, no BUGSIGDB_TAXONOMY_DB, no --release, and no cached "
            f".duckdb found under {taxonomy_cache_root()} -- run `bugsigdb taxonomy "
            "build` first, or pass --db / --release explicitly."
        )
    names = ", ".join(str(p) for p in candidates)
    raise ValueError(
        f"multiple cached .duckdb files found under {taxonomy_cache_root()} ({names}); "
        "disambiguate with --db, --release, or BUGSIGDB_TAXONOMY_DB."
    )
