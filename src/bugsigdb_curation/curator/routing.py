"""Decision-model routing for the curator: cheap, calibrated judgments around the strong model.

One module over the :class:`~bugsigdb_curation.decision.DecisionModel` seam (Cloudflare Clef /
Clef-flash). It holds the judgments the offline probe (`benchmarks/decision-probe/RESULTS.md`,
issue #19) found worth keeping, each as a function that takes plain curator data and returns plain
data -- no stage reaches into the decision API itself, and nothing here reads gold or imports
``eval``/``benchmarks`` (firewall, §6e).

Every judgment is a **best-effort optimization**: the callers fall back to today's behaviour when no
decision model is configured or a call fails (:func:`rank_artifacts` raises nothing the pipeline
can't absorb -- see ``curate_async``). Thresholds are constants sourced from the probe so they are
changed in one place.

Currently wired:

* :func:`rank_artifacts` -- S5a: p(is a per-taxon differential-abundance artifact) for every
  table/figure, replacing the DA-keyword regex as the ranker (probe: AUROC 0.96 vs the regex's
  P 0.39 / R 0.65). It ranks; it does not gate (precision at full recall was only 0.54).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from loguru import logger

from bugsigdb_curation.curator.evidence import EvidenceBundle
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.decision import DecisionModel, Noul

#: Concurrent decision calls per fan-out (bundles hold at most ~15 artifacts).
_CONCURRENCY = 6
#: Caption/legend characters sent per artifact (probe setting).
_TEXT_CHARS = 3000
#: Table rows sent per artifact (probe setting).
_TABLE_ROWS = 8

DA_ARTIFACT_QUESTION = Noul(
    "Does this table or figure report per-taxon differential-abundance results "
    "(taxa shown as higher or lower between groups)?",
    criteria={
        "true": "taxa are reported as differentially abundant between groups",
        "false": "no per-taxon differential-abundance result (e.g. diversity, composition, metadata)",
    },
)


def _artifact_state(artifact: LocatedArtifact, *, title: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {"paper": title or "", "kind": artifact.kind}
    if artifact.kind == "table" and artifact.table is not None:
        table = artifact.table
        state["artifact"] = table.label
        state["caption_or_legend"] = (table.caption or "")[:_TEXT_CHARS]
        if table.rows:
            state["first_rows"] = [" | ".join(row) for row in table.rows[:_TABLE_ROWS]]
    elif artifact.kind == "figure" and artifact.figure is not None:
        state["artifact"] = artifact.figure.label
        state["caption_or_legend"] = (artifact.figure.legend or "")[:_TEXT_CHARS]
    return state


def candidate_artifacts(bundle: EvidenceBundle) -> list[LocatedArtifact]:
    """Every table then every figure in the bundle, in document order."""
    return [LocatedArtifact(kind="table", table=t) for t in bundle.tables] + [
        LocatedArtifact(kind="figure", figure=f) for f in bundle.figures
    ]


async def rank_artifacts(bundle: EvidenceBundle, decision_model: DecisionModel) -> list[LocatedArtifact]:
    """S5a ranker: p(DA artifact) for every table/figure, highest first (ties keep document order).

    Text-only (caption/legend + a few table rows) -- the configuration the probe validated; figure
    images are not fetched here. Raises whatever the decision model raises; the pipeline absorbs
    that and falls back to the regex locate.
    """
    candidates = candidate_artifacts(bundle)
    if not candidates:
        return []
    title = bundle.metadata.title
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(artifact: LocatedArtifact) -> LocatedArtifact:
        async with sem:
            answers = await decision_model.decide(
                stage="s5a_locate",
                state=_artifact_state(artifact, title=title),
                questions={"is_da_artifact": DA_ARTIFACT_QUESTION},
            )
        answer = answers["is_da_artifact"]
        return replace(artifact, p_da=answer.p_yes)  # type: ignore[union-attr]

    # TaskGroup (not gather): the first failure cancels the sibling calls instead of letting them keep
    # spending decision calls after the pipeline has already fallen back to the regex.
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(a)) for a in candidates]
    except ExceptionGroup as group_error:
        raise group_error.exceptions[0] from None
    ranked = [task.result() for task in tasks]
    ordered = sorted(ranked, key=lambda a: -(a.p_da or 0.0))  # sorted() is stable: ties keep document order
    logger.bind(stage="S5a").info(
        "artifacts ranked", n=len(ordered), top=ordered[0].provenance, top_p_da=round(ordered[0].p_da or 0.0, 3)
    )
    return ordered
