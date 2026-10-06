"""Re-score archived results without calling the API.

    uv run python benchmarks/decision-probe/rescore.py            # every runs/*/*.results.json
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RUNS, archive_cost  # noqa: E402

for results_path in sorted(RUNS.glob("*/*.results.json")):
    experiment = results_path.name.removesuffix(".results.json")
    date_model = results_path.parent.name
    date, model = date_model.split("_", 1)
    mod = importlib.import_module(f"experiments.{experiment}")
    summary = mod.score(json.loads(results_path.read_text()))
    summary["cost"] = archive_cost(model, experiment, date=date)
    out = results_path.with_name(f"{experiment}.summary.json")
    # keep cost from the original run if the raw archive is no longer on disk
    if not summary["cost"]["calls"] and out.exists():
        summary["cost"] = json.loads(out.read_text()).get("cost", summary["cost"])
    out.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    print("rescored", results_path.relative_to(RUNS))
