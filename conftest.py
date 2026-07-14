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
    resolution set it back with `monkeypatch.setenv(...)` themselves."""
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EUTILS_API_KEY", raising=False)
