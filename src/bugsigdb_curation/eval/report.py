"""Reports for eval-harness scoring: per-study JSONL, an aggregate markdown
report, and a self-contained HTML report (no external assets/dependencies).
Table style mirrors `benchmarks/figure-extraction/score.py`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from bugsigdb_curation.eval.score import AggregateScore, EXPERIMENT_COUNT_BUCKETS, StudyScore

_SOURCE_TYPES = ("main-table", "figure", "supplement", "other")


def _study_to_dict(score: StudyScore) -> dict:
    """`StudyScore` -> a plain JSON-serializable dict (one JSONL line)."""
    return asdict(score)


def write_jsonl(study_scores: list[StudyScore], path: Path) -> Path:
    """Write one JSON object per line, one per study, sorted by study_id."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for s in sorted(study_scores, key=lambda s: s.study_id):
            f.write(json.dumps(_study_to_dict(s)) + "\n")
    return path


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_markdown(study_scores: list[StudyScore], aggregate: AggregateScore) -> str:
    """Render the aggregate markdown report (headline stats + stratified
    breakdowns + a per-study table)."""
    lines = [
        "# BugSigDB Eval Harness Report",
        "",
        f"- Studies scored: **{aggregate.n_studies}**",
        f"- Micro taxa-set F1: **{aggregate.micro_taxa.f1:.3f}** "
        f"(P {aggregate.micro_taxa.precision:.3f} / R {aggregate.micro_taxa.recall:.3f} / "
        f"Jaccard {aggregate.micro_taxa.jaccard:.3f})",
        f"- Macro taxa-set F1: **{aggregate.macro_taxa_f1:.3f}**",
        f"- Direction accuracy (matched signatures): **{_pct(aggregate.direction_accuracy)}**",
        f"- Name→ID accuracy (of gold taxa the prediction also named): **{_pct(aggregate.name_to_id_accuracy)}**",
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

    return "\n".join(lines) + "\n"


def _esc(value: object) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _prf1_row(label: str, n: int, p) -> str:
    return (
        f"<tr><td>{_esc(label)}</td><td>{n}</td><td>{p.precision:.3f}</td>"
        f"<td>{p.recall:.3f}</td><td>{p.f1:.3f}</td><td>{p.jaccard:.3f}</td></tr>"
    )


def render_html(study_scores: list[StudyScore], aggregate: AggregateScore) -> str:
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
  <div class="stat"><span class="n">{aggregate.micro_taxa.f1:.3f}</span><span class="l">micro taxa F1</span></div>
  <div class="stat"><span class="n">{aggregate.macro_taxa_f1:.3f}</span><span class="l">macro taxa F1</span></div>
  <div class="stat"><span class="n">{_pct(aggregate.direction_accuracy)}</span><span class="l">direction accuracy</span></div>
  <div class="stat"><span class="n">{_pct(aggregate.name_to_id_accuracy)}</span><span class="l">name&rarr;ID accuracy</span></div>
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
</body>
</html>
"""


def write_reports(study_scores: list[StudyScore], aggregate: AggregateScore, out_dir: Path) -> dict[str, Path]:
    """Write JSONL + markdown + HTML reports into `out_dir`; returns their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "jsonl": write_jsonl(study_scores, out_dir / "scores.jsonl"),
        "md": out_dir / "report.md",
        "html": out_dir / "report.html",
    }
    paths["md"].write_text(render_markdown(study_scores, aggregate), encoding="utf-8")
    paths["html"].write_text(render_html(study_scores, aggregate), encoding="utf-8")
    return paths
