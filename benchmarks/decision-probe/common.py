"""Shared plumbing for the decision-model probe (scorer-side; may read gold).

* ``open_model`` -- a ``ClefDecisionModel`` whose raw calls are archived under
  ``runs/<date>_<model>/<experiment>.jsonl`` (gitignored; scored summaries are
  checked in as ``runs/<date>_<model>/<experiment>.summary.json``).
* ``bounded_gather`` -- run many ``decide`` calls with modest concurrency.
* Pure-python metrics: AUROC, AUPRC, Brier, threshold-at-recall, accuracy.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import httpx

from bugsigdb_curation.decision import ClefDecisionModel

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNS = HERE / "runs"
LABELS = HERE / "labels"
RELATIONAL = REPO / "data" / "exports" / "relational"
PMC_MAP = REPO / "data" / "eval" / "pmid_pmcid_map.csv"
MANIFEST_PATH = REPO / "benchmarks" / "figure-extraction" / "manifest.json"
MODELS = ("clef", "clef-flash")

T = TypeVar("T")


def run_dir(model: str, date: str | None = None) -> Path:
    d = RUNS / f"{date or dt.date.today().isoformat()}_{model}"
    d.mkdir(parents=True, exist_ok=True)
    return d


@asynccontextmanager
async def open_model(model: str, experiment: str, *, date: str | None = None):
    """Yield a ClefDecisionModel archiving to runs/<date>_<model>/<experiment>.jsonl."""
    archive = run_dir(model, date) / f"{experiment}.jsonl"
    async with httpx.AsyncClient(timeout=90) as client:
        yield ClefDecisionModel.from_env(client=client, model=model, archive=archive, min_interval=0.05)


async def bounded_gather(items: Sequence[T], fn: Callable[[T], Awaitable[Any]], limit: int = 8) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def one(item: T) -> Any:
        async with sem:
            return await fn(item)

    return await asyncio.gather(*(one(i) for i in items))


def write_summary(model: str, experiment: str, summary: dict[str, Any], *, date: str | None = None) -> Path:
    path = run_dir(model, date) / f"{experiment}.summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n")
    return path


def archive_cost(model: str, experiment: str, *, date: str | None = None) -> dict[str, float]:
    """Total input tokens / calls / mean latency from the raw archive."""
    path = run_dir(model, date) / f"{experiment}.jsonl"
    toks = lat = n = 0
    if path.exists():
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            if rec.get("error"):
                continue
            n += 1
            toks += (rec.get("usage") or {}).get("input_tokens", 0)
            lat += rec.get("latency_ms", 0)
    return {"calls": n, "input_tokens": toks, "mean_latency_ms": round(lat / n, 1) if n else 0.0}


# --- metrics ---------------------------------------------------------------


def auroc(y: Sequence[int], p: Sequence[float]) -> float | None:
    """Rank-sum AUROC with tie handling; None if only one class present."""
    pos = [s for s, t in zip(p, y) if t]
    neg = [s for s, t in zip(p, y) if not t]
    if not pos or not neg:
        return None
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def auprc(y: Sequence[int], p: Sequence[float]) -> float | None:
    """Average precision (step-wise), None if no positives."""
    n_pos = sum(y)
    if not n_pos:
        return None
    order = sorted(range(len(y)), key=lambda i: -p[i])
    tp = 0
    ap = 0.0
    for rank, i in enumerate(order, 1):
        if y[i]:
            tp += 1
            ap += tp / rank
    return ap / n_pos


def brier(y: Sequence[int], p: Sequence[float]) -> float:
    return sum((s - t) ** 2 for s, t in zip(p, y)) / len(y)


def prf_at(y: Sequence[int], p: Sequence[float], tau: float) -> dict[str, float]:
    tp = sum(1 for s, t in zip(p, y) if s >= tau and t)
    fp = sum(1 for s, t in zip(p, y) if s >= tau and not t)
    fn = sum(1 for s, t in zip(p, y) if s < tau and t)
    prec = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    return {"tau": tau, "precision": prec, "recall": rec, "n_routed": tp + fp}


def threshold_for_recall(y: Sequence[int], p: Sequence[float], target: float = 0.95) -> dict[str, float] | None:
    """Highest threshold whose recall >= target, with the precision it buys."""
    best = None
    for tau in sorted(set(p), reverse=True):
        r = prf_at(y, p, tau)
        if r["recall"] >= target:
            best = r
            break
    return best


def binary_report(y: Sequence[int], p: Sequence[float], *, recall_target: float = 0.95) -> dict[str, Any]:
    return {
        "n": len(y),
        "n_pos": int(sum(y)),
        "auroc": auroc(y, p),
        "auprc": auprc(y, p),
        "brier": brier(y, p),
        "at_0.5": prf_at(y, p, 0.5),
        f"at_recall_{recall_target}": threshold_for_recall(y, p, recall_target),
    }


def coverage_at_confidence(correct: Sequence[bool], conf: Sequence[float], taus: Iterable[float]) -> list[dict[str, float]]:
    """Accuracy and coverage if we auto-accept only answers with confidence >= tau."""
    rows = []
    for tau in taus:
        kept = [c for c, s in zip(correct, conf) if s >= tau]
        rows.append(
            {
                "tau": tau,
                "coverage": len(kept) / len(correct) if correct else 0.0,
                "accuracy": sum(kept) / len(kept) if kept else float("nan"),
                "n": len(kept),
            }
        )
    return rows
