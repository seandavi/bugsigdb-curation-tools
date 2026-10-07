"""`curate()`: S0-S9 orchestration, Architecture-A linear single-worker form.

This is the walking skeleton for all three designs (workflow plan §6b): PMID
in, a schema-checked nested prediction record out, composing every stage
module in this package in a straight line -- S0 resolve -> S1 evidence -> S2
study -> S3 segment -> per-stub(S4 experiment -> S5a locate -> **S5b/S6,
dispatched by `design`** -> **S10, dispatched by `design`**) -> S8 assemble
-> S9 validate. Per the plan's design rules, this single-worker form is a
strict subset of Architecture B (it's "B with one Experiment Worker and no
fan-out") -- scaling up later doesn't require reworking any stage's
contract, just wrapping this loop in fan-out/fan-in.

**The `design` selector** (`curator.design.Design`) only ever changes the
S5b/S6 and S10 dispatch below -- S0-S4, S5a, S8, S9 are byte-identical
across all three designs:

* `fused-lean` (default): `curator.signature.extract_signatures` -- one
  model call fuses extraction + a tool-verified id proposal; S10 is
  structural-only (S9's own schema/CURIE gate, no extra call here). This is
  the original walking skeleton and its output is unchanged by this dispatch
  existing at all.
* `split-verify` / `split-panel`: `curator.ner.extract_names` (names +
  direction only) -> `curator.reconcile.reconcile_names` (deterministic
  `TaxonomyDB` resolution, disambiguation only on an ambiguous hit) for
  S5b/S6, then `curator.verify.verify_signatures` (`split-verify`) or
  `curator.panel.review_signatures` (`split-panel`) for S10.

**Data firewall (§6e):** `curate`/`curate_async` take only a `pmid` (+
model/config/design/http-client/cache overrides) -- no gold path exists
anywhere in this signature, and nothing in this module imports
`bugsigdb_curation.eval`. See `tests/test_curator_firewall.py`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from bugsigdb_curation.curator.assemble import assemble_record
from bugsigdb_curation.curator.design import DEFAULT_DESIGN, Design
from bugsigdb_curation.curator.evidence import (
    EvidenceBundle,
    assemble_evidence,
    fetch_figure_image,
)
from bugsigdb_curation.curator.experiment import ExperimentFields, extract_experiment
from bugsigdb_curation.curator.extract import StudyFields, extract_study
from bugsigdb_curation.curator.locate import LocatedArtifact, locate_artifacts
from bugsigdb_curation.curator.model import Model
from bugsigdb_curation.curator.ner import extract_names
from bugsigdb_curation.curator.ols import DEFAULT_CACHE_PATH as DEFAULT_OLS_CACHE_PATH
from bugsigdb_curation.curator.ols import OlsClient
from bugsigdb_curation.curator.panel import review_signatures
from bugsigdb_curation.curator.reconcile import ground_unresolved as ground_unresolved_taxa
from bugsigdb_curation.curator.reconcile import reconcile_names
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL, resolve
from bugsigdb_curation.curator.routing import DECISION_CALL_ERRORS, map_body_sites, rank_artifacts
from bugsigdb_curation.curator.segment import segment_experiments
from bugsigdb_curation.curator.signature import ExtractedSignature, extract_signatures
from bugsigdb_curation.curator.supplement_lever import DUPLICATE_MIN_TAXA, supplement_experiments, taxon_keys
from bugsigdb_curation.curator.taxonomy import DEFAULT_CACHE_PATH, NcbiTaxonomyResolver
from bugsigdb_curation.curator.verify import verify_signatures
from bugsigdb_curation.decision import DecisionModel
from bugsigdb_curation.validate import Problem, default_schema_path, validate_instance

#: The one source-config wired up for the walking skeleton (plan §6, decided
#: 2026-07-13: "First source config: text + main tables + figures ... from
#: the start"). Informational label only, like the eval CLI's `--config` --
#: there is no abstract-only/text-only mode to switch to yet in this
#: skeleton (a PMID with no PMCID falls back to a Study-only record, see
#: `curate_async`, rather than a genuinely different S1 fetch path).
DEFAULT_CONFIG = "text-tables-figures"


@dataclass(frozen=True, slots=True)
class CurationResult:
    """`curate()`'s full output: the prediction record + S9's verdict + light provenance.

    `record` is exactly the loader nested-dict shape and must stay
    schema-clean (see `curator.assemble`'s docstring on `closed=True`) --
    `pmid`/`pmcid`/`has_pmc`/`valid`/`problems`/`design`/`flags`
    intentionally live on this wrapper, not inside `record`, so a caller can
    inspect S9's verdict (or which design produced this record) without any
    of that provenance corrupting the very thing S9 is validating. An
    invalid record is still returned (never dropped), per the plan's S9
    design rule: "attach pass/fail + problems to the output."

    `design` records which of the three §6b designs produced this record
    (informational provenance, like `pmcid`/`has_pmc` -- never fed back into
    curation). `flags` are provenance-only strings the split-verify/
    split-panel A2 stages emit for anything dropped or left unresolved after
    their bounded repair loop exhausted (e.g. a taxon that never re-grounded,
    a direction that never converged) -- empty for `fused-lean`, which has no
    semantic A2 stage to flag anything.
    """

    pmid: str
    pmcid: str | None
    has_pmc: bool
    record: dict[str, Any]
    valid: bool
    problems: tuple[Problem, ...]
    design: Design = DEFAULT_DESIGN
    flags: tuple[str, ...] = field(default_factory=tuple)
    annotations: dict[str, Any] = field(default_factory=dict)


def _empty_study_fields(doi: str | None) -> StudyFields:
    return StudyFields(title=None, journal=None, year=None, authors=(), doi=doi, study_design=())


def _log_study_done(*, valid: bool, n_experiments: int, n_signatures: int, start: float) -> None:
    latency_ms = round((time.monotonic() - start) * 1000)
    logger.bind(event="study_done").info(
        "study done",
        valid=valid,
        n_experiments=n_experiments,
        n_signatures=n_signatures,
        latency_ms=latency_ms,
    )


def _build_source_context(experiment_fields: ExperimentFields, artifact: LocatedArtifact) -> str:
    """Assemble the "lineage/body-site/host cues" the plan's disambiguation
    and reviewer stages use to pick among ambiguous candidates / ground a
    recall taxon -- S4's already-extracted experiment metadata plus the
    S5a-located artifact's own provenance label. Deliberately cheap (no
    extra model call): everything here already exists by the time S5b runs.
    """
    parts = [
        f"{label}: {', '.join(value) if isinstance(value, tuple) else value}"
        for label, value in (
            ("host_species", experiment_fields.host_species),
            ("body_site", experiment_fields.body_site),
            ("condition", experiment_fields.condition),
        )
        if value
    ]
    parts.append(f"source: {artifact.provenance}")
    return "; ".join(parts)


async def _rank_or_none(
    bundle: EvidenceBundle, decision_model: DecisionModel | None, annotations: dict[str, Any] | None = None
) -> list[LocatedArtifact] | None:
    """The decision-model artifact ranking, or None (=> regex locate) when there is no decision
    model or its call fails. A routing failure must never abort a study -- but it must not be silent
    either: the failure is logged and, if `annotations` is given, recorded as `artifact_ranking_error`
    so a run that quietly became regex-only (e.g. a rejected token) is visible in the output."""
    if decision_model is None:
        return None
    try:
        return await rank_artifacts(bundle, decision_model)
    except DECISION_CALL_ERRORS as exc:
        logger.bind(stage="S5a").warning("decision-model ranking failed; using regex locate", error=repr(exc))
        if annotations is not None:
            annotations["artifact_ranking_error"] = repr(exc)
        return None


async def _body_site_terms(
    experiment_index: int,
    body_sites: Sequence[str],
    study_title: str,
    decision_model: DecisionModel,
    ols: OlsClient,
    annotations: dict[str, Any],
) -> list[dict[str, Any]]:
    """The S4 body-site -> UBERON mappings for one experiment as sidecar entries, or `[]` if the OLS
    search / decision call fails. Same best-effort contract as `_rank_or_none`: the failure is logged
    and appended to `annotations["body_site_terms_error"]` (one `{"experiment_index", "error"}` entry
    per failing experiment), never aborts the study, and only the expected transport/decision errors
    are absorbed (a bug still surfaces)."""
    try:
        mappings = await map_body_sites(
            body_sites, context_title=study_title, decision_model=decision_model, ols=ols
        )
    except DECISION_CALL_ERRORS as exc:
        logger.bind(stage="S4").warning("body-site ontology mapping failed; skipping", error=repr(exc))
        annotations.setdefault("body_site_terms_error", []).append(
            {"experiment_index": experiment_index, "error": repr(exc)}
        )
        return []
    return [{"experiment_index": experiment_index, **asdict(m)} for m in mappings]


async def _extract_experiment_signatures(
    bundle_artifact: LocatedArtifact,
    *,
    design: Design,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    client: httpx.AsyncClient,
    image_bytes: bytes | None,
    experiment_fields: ExperimentFields,
    ground_unresolved: bool = False,
    may_decline: bool = False,
    more_candidates: bool = False,
) -> tuple[list[ExtractedSignature], tuple[str, ...]]:
    """S5b/S6 + S10, dispatched by `design` -- the only per-design branch in
    the whole pipeline (see module docstring). Returns `(signatures, flags)`;
    `flags` is always empty for `fused-lean` (no semantic A2 stage to flag
    anything -- S9's structural validation runs unconditionally afterward,
    same as before this dispatch existed).

    `may_decline` puts the "return no taxa if this artifact does not report the comparison" escape hatch
    into the extractor (and reviewer) prompts -- set only when something else can be tried instead.
    `more_candidates` says a later candidate artifact exists: a split design whose extractor found nothing
    here then returns `([], ())` without calling the reviewer/verifier, so the decline really falls through
    (a reviewer re-reading the artifact on its own would find taxa and end the candidate search).

    `design` is coerced to a real `Design` member up front: `Design` is a
    `str` subclass so a plain string (e.g. a caller passing
    `design="split-verify"` straight through to `curate_async`/`curate`
    rather than the `Design` enum member) compares *equal* to the
    corresponding member but is never `is` it, and every branch below
    dispatches with `is`. A bare string would otherwise fall through every
    branch and trip the final `assert`. `curate_async` also coerces at its
    own entry (belt-and-braces), so this call already normally receives a
    `Design` by the time it gets here -- this stays defensive against being
    called directly with a plain string.
    """
    design = Design(design)
    groups = (experiment_fields.group_0_name, experiment_fields.group_1_name)
    if design is Design.fused_lean:
        signatures = await extract_signatures(
            bundle_artifact,
            model=model,
            resolver=resolver,
            client=client,
            image_bytes=image_bytes,
            groups=groups,
            may_decline=may_decline,
        )
        if ground_unresolved:  # opt-in: resolve names S6 could not verify an id for (split designs already do)
            signatures = await ground_unresolved_taxa(
                signatures,
                model=model,
                resolver=resolver,
                client=client,
                source_context=_build_source_context(experiment_fields, bundle_artifact),
            )
        return signatures, ()

    source_context = _build_source_context(experiment_fields, bundle_artifact)
    names = extract_names(bundle_artifact, model=model, image_bytes=image_bytes, groups=groups, may_decline=may_decline)
    signatures = await reconcile_names(
        names, model=model, resolver=resolver, client=client, source_context=source_context
    )
    if not signatures and more_candidates:
        return [], ()

    if design is Design.split_verify:
        return verify_signatures(signatures, artifact=bundle_artifact, model=model, image_bytes=image_bytes)

    assert design is Design.split_panel
    return await review_signatures(
        signatures,
        artifact=bundle_artifact,
        model=model,
        resolver=resolver,
        client=client,
        source_context=source_context,
        image_bytes=image_bytes,
        groups=groups,
        may_decline=may_decline,
    )


async def _figure_image_once(
    artifact: LocatedArtifact,
    *,
    client: httpx.AsyncClient,
    cache: dict[str, bytes | None],
    annotations: dict[str, Any],
) -> bytes | None:
    """The image of a figure artifact, downloaded at most once per study (`cache` is keyed by provenance).

    A figure with no image means S5b extracts from the legend alone -- often empty or wrong -- so say so
    loudly in the log and append its provenance to `annotations["figure_image_unavailable"]` (a list)
    rather than failing silently. A CDN hiccup must not abort the study. Tables have no image (None).
    """
    if artifact.kind != "figure" or artifact.figure is None:
        return None
    key = artifact.provenance
    if key in cache:
        return cache[key]
    image: bytes | None = None
    try:
        image = await fetch_figure_image(artifact.figure, client=client)
    except httpx.HTTPError as exc:
        logger.bind(stage="S5b").warning("figure image download failed", error=repr(exc))
    if image is None:
        logger.bind(stage="S5b").warning(
            "figure image unavailable; extracting from the legend alone", artifact=key
        )
        annotations.setdefault("figure_image_unavailable", []).append(key)
    cache[key] = image
    return image


_MainExperiment = tuple[ExperimentFields, list[ExtractedSignature], str | None]


def _drop_duplicate_signatures(
    experiments: list[_MainExperiment],
) -> tuple[list[_MainExperiment], list[dict[str, Any]]]:
    """Drop a later experiment's signature that copies an earlier one read from the SAME artifact.

    Duplicate = same direction and an identical taxon-key set (`supplement_lever.taxon_keys`) of at least
    `DUPLICATE_MIN_TAXA` taxa, from the same source artifact. The experiment itself is kept (it is a real
    comparison the paper may describe; one without signatures is honest). Returns the new experiment list
    and one `{"experiment_index", "source", "direction", "same_as_experiment_index"}` record per drop.
    """
    seen: list[tuple[int, str, str, frozenset[str]]] = []  # (experiment index, source, direction, taxon keys)
    dropped: list[dict[str, Any]] = []
    kept_experiments: list[_MainExperiment] = []
    for index, (fields, signatures, source) in enumerate(experiments):
        kept: list[ExtractedSignature] = []
        for signature in signatures:
            keys = taxon_keys(signature)
            earlier = next(
                (
                    i
                    for i, src, direction, earlier_keys in seen
                    if source is not None and src == source and direction == signature.direction and earlier_keys == keys
                ),
                None,
            )
            if len(keys) >= DUPLICATE_MIN_TAXA and earlier is not None:
                dropped.append(
                    {
                        "experiment_index": index,
                        "source": source,
                        "direction": signature.direction,
                        "same_as_experiment_index": earlier,
                    }
                )
                continue
            kept.append(signature)
            if source is not None:
                seen.append((index, source, signature.direction, keys))
        kept_experiments.append((fields, kept, source))
    return kept_experiments, dropped


async def curate_async(
    pmid: str,
    *,
    model: Model,
    config: str = DEFAULT_CONFIG,
    design: Design = DEFAULT_DESIGN,
    client: httpx.AsyncClient | None = None,
    email: str = DEFAULT_EMAIL,
    taxonomy_cache_path: Path | None = DEFAULT_CACHE_PATH,
    taxonomy_db_path: Path | None = None,
    taxonomy_db_release: str | None = None,
    resolver: NcbiTaxonomyResolver | None = None,
    run_id: str | None = None,
    decision_model: DecisionModel | None = None,
    ols_cache_path: Path | None = DEFAULT_OLS_CACHE_PATH,
    ols: OlsClient | None = None,
    supplements: bool = False,
    html_cache_dir: Path | None = None,
    ground_unresolved: bool = False,
) -> CurationResult:
    """S0-S9: turn a bare PMID into a validated nested prediction record.

    `decision_model`, if given, switches on the decision-model routing
    judgments (`curator.routing`): today S5a ranks artifacts by p(DA) instead
    of the keyword regex, and S4's free-text `body_site` labels are mapped to UBERON terms (recorded in
    `annotations["body_site_terms"]`; the record itself is untouched; `ols_cache_path` is the OLS4
    search cache, ignored once `ols` is given directly -- like `resolver`, a batch caller may share one
    `OlsClient` across studies and then owns saving its cache). Every judgment is best-effort -- a failed decision
    call is logged and the stage falls back to its no-decision-model
    behaviour -- and with `decision_model=None` the pipeline is unchanged.

    With a decision model each experiment tries the top-ranked artifacts (`locate_artifacts`) in rank order
    and takes the first that yields taxa (S5b may decline an artifact that does not report the comparison);
    the tries are recorded in `annotations["experiment_artifacts"]`, and a later experiment's signature that
    copies an earlier one from the same artifact is dropped (`annotations["duplicate_signatures_dropped"]`).

    `supplements=True` (needs `decision_model`, else `ValueError`) also reads the paper's supplementary files
    (`curator.supplement_lever`): the decision model screens each sheet/page, the units that pass are extracted
    and their experiments appended after the main-text ones (`signatures[].source` names the file and
    sheet/page); the screen, skipped files, errors and dropped duplicates are recorded in `annotations`.
    With `supplements=False` (the default) none of that runs and the record is unchanged.

    `ground_unresolved=True` (fused-lean only; off by default) resolves taxa whose model-proposed NCBI id could
    not be verified by NAME against the authority (`reconcile.ground_unresolved`: local DB / live gap-fill, LLM
    disambiguation only for homonyms), so fewer records fail S9's `ncbi_id` requirement. Nothing is guessed.

    `design` selects one of the three §6b designs (default `fused-lean`,
    today's original walking skeleton, unchanged) -- see the module
    docstring for what it does and does not affect.

    `client`/`resolver` are injectable (tests / callers that want to reuse a
    connection pool or a warm taxonomy cache across many PMIDs, e.g.
    `--smoke` batch mode); a fresh short-lived `httpx.AsyncClient` and an
    on-disk-cached `NcbiTaxonomyResolver` are created and torn down/persisted
    automatically when not given. `taxonomy_db_path`/`taxonomy_db_release`
    (ignored once `resolver` is given directly) are forwarded to
    `NcbiTaxonomyResolver.load()`'s own `db_path`/`db_release` resolution
    (CLI flag -> `BUGSIGDB_TAXONOMY_DB` -> newest cached DB -> live-only).

    `run_id`, if given (e.g. the CLI's `--smoke` batch loop generates one and
    passes the same value to every study), is bound into every log record
    this call emits alongside `study_id`/`pmid`, so a deployed run's whole
    log stream can be filtered/grouped by `run_id` -- see `obs.py`.

    `design` is coerced to a real `Design` member immediately: `Design` is a
    `str` subclass, so a caller may pass a plain string (`design=
    "split-verify"`) instead of the enum member and it compares equal --
    but every dispatch in this module (here and in
    `_extract_experiment_signatures`) uses `is`, and `CurationResult.design`
    is later read via `.design.value` (see `cli.py:_report_result`), which a
    bare `str` doesn't have. Coercing once here means the rest of this
    function, `_extract_experiment_signatures`, and every `CurationResult`
    built below always carry a genuine `Design` member.
    """
    design = Design(design)
    if supplements and decision_model is None:
        raise ValueError("supplements=True needs a decision_model (the supplement lever screens units with it)")
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
    owns_resolver = resolver is None
    if resolver is None:
        resolver = NcbiTaxonomyResolver.load(
            cache_path=taxonomy_cache_path, db_path=taxonomy_db_path, db_release=taxonomy_db_release
        )

    # OLS is only ever contacted for the body-site mapping, which needs a decision model.
    owns_ols = ols is None and decision_model is not None
    if owns_ols:
        ols = OlsClient.load(client, cache_path=ols_cache_path)

    start = time.monotonic()
    with logger.contextualize(study_id=pmid, pmid=pmid, run_id=run_id):
        try:
            resolved = await resolve(pmid, client=client, email=email)

            if not resolved.has_pmc:
                # Abstract-only stratum (plan §4a): S1's text+table+figure channel
                # has nothing to fetch from PMC. Emit a minimal, still
                # schema-checked Study-only record (no experiments) rather than
                # raising -- "do not silently drop invalid/incomplete records."
                record = assemble_record(resolved, _empty_study_fields(resolved.doi), [])
                problems = validate_instance(record, "Study", default_schema_path())
                logger.bind(stage="S9").info("validated", valid=not problems, n_problems=len(problems))
                _log_study_done(valid=not problems, n_experiments=0, n_signatures=0, start=start)
                return CurationResult(
                    pmid=pmid,
                    pmcid=None,
                    has_pmc=False,
                    record=record,
                    valid=not problems,
                    problems=tuple(problems),
                    design=design,
                )

            assert resolved.pmcid is not None  # has_pmc guarantees this
            with logger.contextualize(pmcid=resolved.pmcid):
                bundle = await assemble_evidence(pmid, resolved.pmcid, client=client, html_cache_dir=html_cache_dir)

                annotations: dict[str, Any] = {}
                study_fields = extract_study(bundle, resolved, model=model)
                stubs = segment_experiments(bundle, model=model)
                ranked = await _rank_or_none(bundle, decision_model, annotations)
                if ranked is not None:
                    annotations["artifact_ranking"] = [
                        {"artifact": a.provenance, "kind": a.kind, "p_da": a.p_da} for a in ranked
                    ]
                candidates = locate_artifacts(bundle, ranked)
                figure_images: dict[str, bytes | None] = {}  # each candidate figure's image: once per study
                # Something to fall back to (another experiment or candidate artifact) is what makes a decline useful.
                may_decline = len(stubs) > 1 or len(candidates) > 1

                experiments: list[_MainExperiment] = []
                experiment_artifacts: list[dict[str, Any]] = []
                flags: list[str] = []
                body_site_terms: list[dict[str, Any]] = []
                # NOTE: no per-experiment error isolation -- one bad ExperimentStub
                # (a raised exception from S4/S5a/S5b) aborts the whole study here.
                # Deferred to Architecture-B's fan-out (plan §2/§5), which isolates
                # each Experiment Worker; out of scope for this Design-1 skeleton.
                for stub in stubs:
                    experiment_fields = extract_experiment(bundle, stub, model=model)
                    if decision_model is not None and ols is not None:
                        body_site_terms += await _body_site_terms(
                            len(experiments),
                            experiment_fields.body_site,
                            study_fields.title or bundle.metadata.title or "",
                            decision_model,
                            ols,
                            annotations,
                        )

                    # Try the candidate artifacts in rank order; the first that yields taxa serves this
                    # experiment (a paper's comparisons are often reported by different artifacts).
                    signatures: list[ExtractedSignature] = []
                    source: str | None = candidates[0].provenance if candidates else None
                    tried: list[str] = []
                    for position, artifact in enumerate(candidates):
                        tried.append(artifact.provenance)
                        image_bytes = await _figure_image_once(
                            artifact, client=client, cache=figure_images, annotations=annotations
                        )
                        found, stage_flags = await _extract_experiment_signatures(
                            artifact,
                            design=design,
                            model=model,
                            resolver=resolver,
                            client=client,
                            image_bytes=image_bytes,
                            experiment_fields=experiment_fields,
                            ground_unresolved=ground_unresolved,
                            may_decline=may_decline,
                            more_candidates=position + 1 < len(candidates),
                        )
                        flags.extend(stage_flags)
                        if found:
                            signatures, source = found, artifact.provenance
                            break
                    if ranked:
                        experiment_artifacts.append(
                            {
                                "experiment_index": len(experiments),
                                "artifact_tried": tried,
                                "artifact_used": source if signatures else None,
                            }
                        )

                    experiments.append((experiment_fields, signatures, source))

                if body_site_terms:
                    annotations["body_site_terms"] = body_site_terms
                if experiment_artifacts:
                    annotations["experiment_artifacts"] = experiment_artifacts

                experiments, duplicates_dropped = _drop_duplicate_signatures(experiments)
                if duplicates_dropped:
                    annotations["duplicate_signatures_dropped"] = duplicates_dropped

                if supplements:
                    assert decision_model is not None  # checked at entry
                    experiments += await supplement_experiments(
                        resolved.pmcid,
                        client=client,
                        decision_model=decision_model,
                        model=model,
                        resolver=resolver,
                        study_title=study_fields.title or bundle.metadata.title or "",
                        main_experiments=list(experiments),
                        annotations=annotations,
                    )

                record = assemble_record(resolved, study_fields, experiments)
                problems = validate_instance(record, "Study", default_schema_path())
                logger.bind(stage="S9").info("validated", valid=not problems, n_problems=len(problems))

                n_signatures = sum(len(sigs) for _, sigs, _ in experiments)
                _log_study_done(
                    valid=not problems, n_experiments=len(experiments), n_signatures=n_signatures, start=start
                )

                return CurationResult(
                    pmid=pmid,
                    pmcid=resolved.pmcid,
                    has_pmc=True,
                    record=record,
                    valid=not problems,
                    problems=tuple(problems),
                    design=design,
                    flags=tuple(flags),
                    annotations=annotations,
                )
        finally:
            if owns_ols:
                assert ols is not None
                ols.save_cache()
            if owns_resolver:
                resolver.save_cache()
                # Close the resolver's local TaxonomyDB handle (if any) --
                # only when this call built the resolver itself; a caller-
                # supplied resolver (e.g. the CLI's `--smoke` batch loop, which
                # shares one resolver across many `curate_async` calls) owns
                # its own DB lifecycle and closes it once, after the whole
                # batch, not here.
                resolver.close()
            if owns_client:
                await client.aclose()


def curate(
    pmid: str, *, model: Model, config: str = DEFAULT_CONFIG, design: Design = DEFAULT_DESIGN, **kwargs: Any
) -> CurationResult:
    """Sync wrapper around `curate_async`, for the CLI and other non-async callers."""
    return asyncio.run(curate_async(pmid, model=model, config=config, design=design, **kwargs))
