"""Reports for eval-harness scoring: per-study JSONL, an aggregate markdown
report, and a self-contained HTML report (no external assets/dependencies).
Table style mirrors `benchmarks/figure-extraction/score.py`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TypedDict

from bugsigdb_curation.eval.score import AggregateScore, EXPERIMENT_COUNT_BUCKETS, StudyScore

_SOURCE_TYPES = ("main-table", "figure", "supplement", "other")


class ScoringError(TypedDict):
    """One study that raised while being scored (see `cli.py`'s per-study
    try/except); `error` is `str(exception)`, not a full traceback."""

    study_id: str
    error: str


def _study_to_dict(score: StudyScore) -> dict:
    """`StudyScore` -> a plain JSON-serializable dict (one JSONL line)."""
    return {"record_type": "study_score", **asdict(score)}


def write_jsonl(
    study_scores: list[StudyScore],
    path: Path,
    *,
    missing_prediction_ids: Sequence[str] = (),
    scoring_errors: Sequence[ScoringError] = (),
) -> Path:
    """Write one JSON object per line, sorted by study_id: one per scored
    study, then one per missing-prediction study_id, then one per
    scoring-error -- each tagged with a `record_type` discriminator so a
    reader can tell a real score apart from these two "did not score
    cleanly" buckets (Blocker 2 / the per-study exception isolation: a
    missing prediction or a malformed one must be visible, not silently
    folded into or dropped from the aggregate)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in sorted(study_scores, key=lambda s: s.study_id):
            f.write(json.dumps(_study_to_dict(s)) + "\n")
        for study_id in sorted(missing_prediction_ids):
            f.write(json.dumps({"record_type": "missing_prediction", "study_id": study_id}) + "\n")
        for err in sorted(scoring_errors, key=lambda e: e["study_id"]):
            f.write(json.dumps({"record_type": "scoring_error", **err}) + "\n")
    return path


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_markdown(
    study_scores: list[StudyScore],
    aggregate: AggregateScore,
    *,
    missing_prediction_ids: Sequence[str] = (),
    scoring_errors: Sequence[ScoringError] = (),
) -> str:
    """Render the aggregate markdown report (headline stats + stratified
    breakdowns + a per-study table)."""
    lines = [
        "# BugSigDB Eval Harness Report",
        "",
        f"- Studies scored: **{aggregate.n_studies}**",
        f"- Missing predictions (gold study, no prediction -- scored as a full miss): "
        f"**{len(missing_prediction_ids)}**",
        f"- Scoring errors (prediction raised while scoring -- excluded from all metrics above): "
        f"**{len(scoring_errors)}**",
        f"- Micro taxa-set F1: **{aggregate.micro_taxa.f1:.3f}** "
        f"(P {aggregate.micro_taxa.precision:.3f} / R {aggregate.micro_taxa.recall:.3f} / "
        f"Jaccard {aggregate.micro_taxa.jaccard:.3f})",
        f"- Macro taxa-set F1: **{aggregate.macro_taxa_f1:.3f}**",
        f"- Direction accuracy (matched signatures): **{_pct(aggregate.direction_accuracy)}**",
        f"- Name→ID accuracy (of gold taxa the prediction also named): **{_pct(aggregate.name_to_id_accuracy)}**",
        f"- Over-segmentation (corpus total, §4b): **{aggregate.over_segmentation}**",
        f"- Under-segmentation (corpus total, §4b): **{aggregate.under_segmentation}**",
        "",
        "## By source type",
        "",
        "(headline taxa-set P/R/F1/Jaccard, micro-averaged, cross-tabulated by gold `source` -- §4c)",
        "",
        "| source type | n gold taxa | P | R | F1 | Jaccard |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for st in _SOURCE_TYPES:
        if st in aggregate.by_source_type:
            p = aggregate.by_source_type[st]
            lines.append(f"| {st} | {p.tp + p.fn} | {p.precision:.3f} | {p.recall:.3f} | {p.f1:.3f} | {p.jaccard:.3f} |")

    lines += [
        "",
        "## By experiment-count bucket",
        "",
        "| bucket | n gold taxa | P | R | F1 | Jaccard |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for bucket in EXPERIMENT_COUNT_BUCKETS:
        if bucket in aggregate.by_experiment_bucket:
            p = aggregate.by_experiment_bucket[bucket]
            lines.append(f"| {bucket} | {p.tp + p.fn} | {p.precision:.3f} | {p.recall:.3f} | {p.f1:.3f} | {p.jaccard:.3f} |")

    lines += [
        "",
        "## Per-study",
        "",
        "(experiment alignment quality reported separately from taxa/direction accuracy -- §4b/§4d)",
        "",
        "| study_id | has_pmc | gold exp | pred exp | matched | over-seg | under-seg | "
        "taxa F1 (micro) | genus F1 (micro) | direction acc |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in sorted(study_scores, key=lambda s: s.study_id):
        a = s.experiment_alignment
        has_pmc = "?" if s.has_pmc is None else str(s.has_pmc)
        dacc = f"{s.direction_correct}/{s.direction_total}" if s.direction_total else "-"
        lines.append(
            f"| {s.study_id} | {has_pmc} | {s.n_gold_experiments} | {s.n_pred_experiments} | "
            f"{len(a.matched)} | {a.over_segmentation} | {a.under_segmentation} | "
            f"{s.micro_taxa.f1:.3f} | {s.micro_genus.f1:.3f} | {dacc} |"
        )

    lines += [
        "",
        "## Missing predictions",
        "",
        "(gold studies in the selected set with no matching prediction -- scored as a full "
        "miss above, not dropped from the corpus; §4d \"same corpus, same split\")",
        "",
    ]
    if missing_prediction_ids:
        lines += [f"- {study_id}" for study_id in sorted(missing_prediction_ids)]
    else:
        lines.append("(none)")

    lines += [
        "",
        "## Scoring errors",
        "",
        "(predictions that raised an exception while scoring -- excluded from every metric "
        "above rather than aborting the run)",
        "",
        "| study_id | error |",
        "|---|---|",
    ]
    for err in sorted(scoring_errors, key=lambda e: e["study_id"]):
        safe_error = err["error"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {err['study_id']} | {safe_error} |")
    if not scoring_errors:
        lines.append("| (none) | |")

    return "\n".join(lines) + "\n"


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prf1_row(label: str, n: int, p) -> str:
    return (
        f"<tr><td>{_esc(label)}</td><td>{n}</td><td>{p.precision:.3f}</td>"
        f"<td>{p.recall:.3f}</td><td>{p.f1:.3f}</td><td>{p.jaccard:.3f}</td></tr>"
    )


def render_html(
    study_scores: list[StudyScore],
    aggregate: AggregateScore,
    *,
    missing_prediction_ids: Sequence[str] = (),
    scoring_errors: Sequence[ScoringError] = (),
) -> str:
    """Render a self-contained HTML report (inline CSS, no external assets)."""
    source_rows = "".join(
        _prf1_row(st, aggregate.by_source_type[st].tp + aggregate.by_source_type[st].fn, aggregate.by_source_type[st])
        for st in _SOURCE_TYPES
        if st in aggregate.by_source_type
    )
    bucket_rows = "".join(
        _prf1_row(b, aggregate.by_experiment_bucket[b].tp + aggregate.by_experiment_bucket[b].fn, aggregate.by_experiment_bucket[b])
        for b in EXPERIMENT_COUNT_BUCKETS
        if b in aggregate.by_experiment_bucket
    )
    study_rows = "".join(
        f"<tr><td>{_esc(s.study_id)}</td><td>{s.has_pmc if s.has_pmc is not None else '?'}</td>"
        f"<td>{s.n_gold_experiments}</td><td>{s.n_pred_experiments}</td>"
        f"<td>{len(s.experiment_alignment.matched)}</td>"
        f"<td>{s.experiment_alignment.over_segmentation}</td><td>{s.experiment_alignment.under_segmentation}</td>"
        f"<td>{s.micro_taxa.f1:.3f}</td><td>{s.micro_genus.f1:.3f}</td>"
        f"<td>{s.direction_correct}/{s.direction_total}</td></tr>"
        for s in sorted(study_scores, key=lambda s: s.study_id)
    )
    missing_prediction_items = "".join(f"<li>{_esc(sid)}</li>" for sid in sorted(missing_prediction_ids)) or "<li>(none)</li>"
    scoring_error_rows = "".join(
        f"<tr><td>{_esc(err['study_id'])}</td><td>{_esc(err['error'])}</td></tr>"
        for err in sorted(scoring_errors, key=lambda e: e["study_id"])
    ) or "<tr><td>(none)</td><td></td></tr>"

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>BugSigDB Eval Harness Report</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; margin: 2rem;
          max-width: 72rem; }}
  h1, h2 {{ border-bottom: 1px solid currentColor; padding-bottom: .25rem; opacity: .95; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }}
  th, td {{ border: 1px solid rgba(128,128,128,.4); padding: .4rem .6rem; text-align: right; font-size: .9rem; }}
  th:first-child, td:first-child {{ text-align: left; }}
  th {{ background: rgba(128,128,128,.12); }}
  .headline {{ display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .stat {{ background: rgba(128,128,128,.12); border-radius: 8px; padding: .75rem 1rem; min-width: 8rem; }}
  .stat .n {{ font-size: 1.4rem; font-weight: 600; display: block; }}
  .stat .l {{ font-size: .8rem; opacity: .7; }}
  .note {{ font-size: .85rem; opacity: .7; margin: -.5rem 0 .75rem; }}
</style>
</head>
<body>
<h1>BugSigDB Eval Harness Report</h1>
<div class="headline">
  <div class="stat"><span class="n">{aggregate.n_studies}</span><span class="l">studies scored</span></div>
  <div class="stat"><span class="n">{len(missing_prediction_ids)}</span><span class="l">missing predictions</span></div>
  <div class="stat"><span class="n">{len(scoring_errors)}</span><span class="l">scoring errors</span></div>
  <div class="stat"><span class="n">{aggregate.micro_taxa.f1:.3f}</span><span class="l">micro taxa F1</span></div>
  <div class="stat"><span class="n">{aggregate.macro_taxa_f1:.3f}</span><span class="l">macro taxa F1</span></div>
  <div class="stat"><span class="n">{_pct(aggregate.direction_accuracy)}</span><span class="l">direction accuracy</span></div>
  <div class="stat"><span class="n">{_pct(aggregate.name_to_id_accuracy)}</span><span class="l">name&rarr;ID accuracy</span></div>
  <div class="stat"><span class="n">{aggregate.over_segmentation}</span><span class="l">over-segmentation (corpus)</span></div>
  <div class="stat"><span class="n">{aggregate.under_segmentation}</span><span class="l">under-segmentation (corpus)</span></div>
</div>

<h2>By source type</h2>
<p class="note">headline taxa-set P/R/F1/Jaccard, micro-averaged, cross-tabulated by gold <code>source</code> (§4c)</p>
<table>
<tr><th>source type</th><th>n gold taxa</th><th>P</th><th>R</th><th>F1</th><th>Jaccard</th></tr>
{source_rows}
</table>

<h2>By experiment-count bucket</h2>
<table>
<tr><th>bucket</th><th>n gold taxa</th><th>P</th><th>R</th><th>F1</th><th>Jaccard</th></tr>
{bucket_rows}
</table>

<h2>Per-study</h2>
<p class="note">experiment alignment quality reported separately from taxa/direction accuracy (§4b/§4d)</p>
<table>
<tr><th>study_id</th><th>has_pmc</th><th>gold exp</th><th>pred exp</th><th>matched</th>
<th>over-seg</th><th>under-seg</th><th>taxa F1</th><th>genus F1</th><th>direction acc</th></tr>
{study_rows}
</table>

<h2>Missing predictions</h2>
<p class="note">gold studies in the selected set with no matching prediction -- scored as a full
miss above, not dropped from the corpus (§4d "same corpus, same split")</p>
<ul>
{missing_prediction_items}
</ul>

<h2>Scoring errors</h2>
<p class="note">predictions that raised an exception while scoring -- excluded from every metric
above rather than aborting the run</p>
<table>
<tr><th>study_id</th><th>error</th></tr>
{scoring_error_rows}
</table>
</body>
</html>
"""


def write_reports(
    study_scores: list[StudyScore],
    aggregate: AggregateScore,
    out_dir: Path,
    *,
    missing_prediction_ids: Sequence[str] = (),
    scoring_errors: Sequence[ScoringError] = (),
) -> dict[str, Path]:
    """Write JSONL + markdown + HTML reports into `out_dir`; returns their paths.

    `missing_prediction_ids` (gold studies the prediction set had nothing
    for) and `scoring_errors` (predictions that raised while scoring) are
    surfaced as their own buckets in every report rather than silently
    folded into or dropped from the aggregate -- see `cli.py`'s scoring loop.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "jsonl": write_jsonl(
            study_scores,
            out_dir / "scores.jsonl",
            missing_prediction_ids=missing_prediction_ids,
            scoring_errors=scoring_errors,
        ),
        "md": out_dir / "report.md",
        "html": out_dir / "report.html",
    }
    paths["md"].write_text(
        render_markdown(
            study_scores, aggregate, missing_prediction_ids=missing_prediction_ids, scoring_errors=scoring_errors
        ),
        encoding="utf-8",
    )
    paths["html"].write_text(
        render_html(
            study_scores, aggregate, missing_prediction_ids=missing_prediction_ids, scoring_errors=scoring_errors
        ),
        encoding="utf-8",
    )
    return paths
