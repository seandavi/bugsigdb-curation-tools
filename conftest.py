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

_FIGBENCH_DIR = Path(__file__).parent / "benchmarks" / "figure-extraction"
if str(_FIGBENCH_DIR) not in sys.path:
    sys.path.insert(0, str(_FIGBENCH_DIR))
