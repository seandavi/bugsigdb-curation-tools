"""Run a probe experiment against one or both decision models, then score it.

    uv run python benchmarks/decision-probe/run.py figbench                # both models
    uv run python benchmarks/decision-probe/run.py figbench --model clef-flash

Raw API calls go to runs/<date>_<model>/<experiment>.jsonl (gitignored);
parsed per-item results and the scored summary go to
runs/<date>_<model>/<experiment>.{results,summary}.json (checked in).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import typer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MODELS, archive_cost, open_model, run_dir, write_summary  # noqa: E402

app = typer.Typer(add_completion=False)


async def _run(experiment: str, model_name: str, date: str | None) -> None:
    mod = importlib.import_module(f"experiments.{experiment}")
    archive = run_dir(model_name, date) / f"{experiment}.jsonl"
    archive.unlink(missing_ok=True)  # one archive per (date, model, experiment) run
    async with open_model(model_name, experiment, date=date) as model:
        results = await mod.run(model)
    (run_dir(model_name, date) / f"{experiment}.results.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    summary = mod.score(results)
    summary["cost"] = archive_cost(model_name, experiment, date=date)
    path = write_summary(model_name, experiment, summary, date=date)
    typer.echo(f"{experiment} [{model_name}] -> {path}")


@app.command()
def main(experiment: str, model: list[str] = typer.Option(list(MODELS), "--model"), date: str = typer.Option(None)) -> None:
    for m in model:
        asyncio.run(_run(experiment, m, date))


if __name__ == "__main__":
    app()
