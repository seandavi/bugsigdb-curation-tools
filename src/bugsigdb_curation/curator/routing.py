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
* :func:`map_body_sites` -- S4: map each free-text ``body_site`` label to a UBERON term as a
  ``choice`` over OLS4 candidates (probe: recall@10 0.995; at confidence >= 0.7 accuracy ~0.99 at
  57-69 % coverage). A sidecar annotation -- the record keeps S4's free text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx
from loguru import logger

from bugsigdb_curation.curator.evidence import EvidenceBundle
from bugsigdb_curation.curator.locate import LocatedArtifact
from bugsigdb_curation.curator.ols import OlsClient, describe
from bugsigdb_curation.decision import Choice, ChoiceAnswer, DecisionModel, DecisionModelError, Noul

#: What a failed decision call can raise: the seam's own error (every Clef HTTP/schema failure), transport
#: errors from the HTTP client, and ValueError from request validation or an unexpected OLS response shape.
#: Anything else is a bug and must surface rather than silently turn into the no-decision-model behaviour.
DECISION_CALL_ERRORS = (DecisionModelError, httpx.HTTPError, ValueError)

#: Concurrent decision calls per fan-out (bundles hold at most ~15 artifacts).
_CONCURRENCY = 6
#: Caption/legend characters sent per artifact (probe setting).
_TEXT_CHARS = 3000
#: Table rows sent per artifact (probe setting).
_TABLE_ROWS = 8

#: A body-site mapping is only "mapped" at or above this choice confidence (probe: accuracy ~0.99 there).
ONTOLOGY_CONFIDENCE_THRESHOLD = 0.7
#: The extra option every ontology choice carries, so the model can decline all candidates.
NONE_OF_THESE = "none_of_these"
BODY_SITE_ONTOLOGY = "uberon"

DA_ARTIFACT_QUESTION = Noul(
    "Does this table or figure report per-taxon differential-abundance results "
    "(taxa shown as higher or lower between groups)?",
    criteria={
        "true": "taxa are reported as differentially abundant between groups",
        "false": "no per-taxon differential-abundance result (e.g. diversity, composition, metadata)",
    },
)


def unwrap_fan_out_failure(
    group_error: ExceptionGroup, expected: tuple[type[BaseException], ...] = DECISION_CALL_ERRORS
) -> BaseException:
    """The exception to re-raise from a failed TaskGroup fan-out.

    The first member when every member is an `expected` failure (the pipeline absorbs those); otherwise the
    first member that is *not* -- a sibling's bug must never be masked by another sibling's routine failure.
    """
    unexpected = [e for e in group_error.exceptions if not isinstance(e, expected)]
    return (unexpected or group_error.exceptions)[0]


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
        raise unwrap_fan_out_failure(group_error) from None
    ranked = [task.result() for task in tasks]
    ordered = sorted(ranked, key=lambda a: -(a.p_da or 0.0))  # sorted() is stable: ties keep document order
    logger.bind(stage="S5a").info(
        "artifacts ranked", n=len(ordered), top=ordered[0].provenance, top_p_da=round(ordered[0].p_da or 0.0, 3)
    )
    return ordered


@dataclass(frozen=True, slots=True)
class OntologyMapping:
    """One free-text label's ontology mapping.

    ``status``: ``mapped`` (term chosen at confidence >= :data:`ONTOLOGY_CONFIDENCE_THRESHOLD`),
    ``low_confidence`` (term chosen below it; ``term_id``/``confidence`` kept so a reviewer can see the
    guess), ``unmapped`` (the model chose :data:`NONE_OF_THESE`) or ``no_candidates`` (OLS found nothing,
    so no decision was made).
    """

    label: str
    term_id: str | None
    term_label: str | None
    confidence: float | None
    status: Literal["mapped", "low_confidence", "unmapped", "no_candidates"]
    candidates: tuple[str, ...]


async def _map_body_site(label: str, context_title: str, decision_model: DecisionModel, ols: OlsClient) -> OntologyMapping:
    docs = [d for d in await ols.search(label, BODY_SITE_ONTOLOGY) if d.get("obo_id")]
    candidates = tuple(d["obo_id"] for d in docs)
    if not docs:
        return OntologyMapping(label, None, None, None, "no_candidates", candidates)
    options: dict[str, Any] = {d["obo_id"]: describe(d) for d in docs}
    options[NONE_OF_THESE] = "none of the candidate terms is a good match"
    answers = await decision_model.decide(
        stage="s4_ontology",
        state={"field": "body_site", "label": label, "study_title": context_title},
        questions={
            "term": Choice(
                {
                    "question": "Which ontology term does this body site label refer to?",
                    "label": label,
                    "study_title": context_title,
                },
                options,
            )
        },
    )
    answer = answers["term"]
    assert isinstance(answer, ChoiceAnswer)
    if answer.choice == NONE_OF_THESE:
        return OntologyMapping(label, None, None, answer.confidence, "unmapped", candidates)
    if answer.choice not in candidates:
        raise DecisionModelError(f"s4_ontology: choice {answer.choice!r} not among offered options")
    term_label = next(d.get("label") for d in docs if d["obo_id"] == answer.choice)
    status = "mapped" if answer.confidence >= ONTOLOGY_CONFIDENCE_THRESHOLD else "low_confidence"
    return OntologyMapping(label, answer.choice, term_label, answer.confidence, status, candidates)


async def map_body_sites(
    labels: Sequence[str], *, context_title: str, decision_model: DecisionModel, ols: OlsClient
) -> list[OntologyMapping]:
    """S4 body-site -> UBERON: OLS4 retrieves candidates, the decision model chooses among them.

    One decision per distinct (stripped, non-blank) label, returned in first-seen order. The model only
    ever picks from retrieved candidates (or :data:`NONE_OF_THESE`); it never produces an id. Raises
    whatever OLS or the decision model raises; the pipeline absorbs that and omits the annotation.
    """
    distinct = list(dict.fromkeys(label.strip() for label in labels if label.strip()))
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(label: str) -> OntologyMapping:
        async with sem:
            return await _map_body_site(label, context_title, decision_model, ols)

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(label)) for label in distinct]
    except ExceptionGroup as group_error:
        raise unwrap_fan_out_failure(group_error) from None
    return [task.result() for task in tasks]
