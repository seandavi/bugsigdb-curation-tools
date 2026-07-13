"""The BugSigDB de-novo curation eval harness: gold join, taxid-set scorer
with synonym resolution, and reports (§4/§6d step 1 of
`docs/plans/de-novo-curation-workflow-plan.md`).

This package scores the LLM-pipeline's *output* (the `bugsigdb_curation.loader`
nested-dict shape) against the human-curated gold; it has no dependency on
the pipeline itself, so it is buildable and testable before the pipeline
exists (and reusable to compare any future pipeline architecture).
"""

from __future__ import annotations

from bugsigdb_curation.eval.gold import GoldExperiment, GoldSignature, GoldStudy, load_gold, source_type, to_nested_dict
from bugsigdb_curation.eval.report import render_html, render_markdown, write_jsonl, write_reports
from bugsigdb_curation.eval.score import AggregateScore, StudyScore, aggregate_scores, score_study
from bugsigdb_curation.eval.smoke import select_smoke, smoke_study_ids
from bugsigdb_curation.eval.taxonomy import TaxonomyResolver

__all__ = [
    "AggregateScore",
    "GoldExperiment",
    "GoldSignature",
    "GoldStudy",
    "StudyScore",
    "TaxonomyResolver",
    "aggregate_scores",
    "load_gold",
    "render_html",
    "render_markdown",
    "score_study",
    "select_smoke",
    "smoke_study_ids",
    "source_type",
    "to_nested_dict",
    "write_jsonl",
    "write_reports",
]
