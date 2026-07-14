"""`curate()`: S0-S9 orchestration, Architecture-A linear single-worker form.

This is Design-1 (Fused-Lean)'s walking skeleton: PMID in, a schema-checked
nested prediction record out, composing every stage module in this package
in a straight line (S0 resolve -> S1 evidence -> S2 study -> S3 segment ->
per-stub(S4 experiment -> S5a locate -> S5b fused extract+verify) -> S8
assemble -> S9 validate). Per the plan's design rules, this single-worker
form is a strict subset of Architecture B (it's "B with one Experiment
Worker and no fan-out") -- scaling up later doesn't require reworking any
stage's contract, just wrapping this loop in fan-out/fan-in.

**Data firewall (§6e):** `curate`/`curate_async` take only a `pmid` (+
model/config/http-client/cache overrides) -- no gold path exists anywhere in
this signature, and nothing in this module imports `bugsigdb_curation.eval`.
See `tests/test_curator_firewall.py`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from bugsigdb_curation.curator.assemble import assemble_record
from bugsigdb_curation.curator.evidence import assemble_evidence, fetch_figure_image
from bugsigdb_curation.curator.experiment import ExperimentFields, extract_experiment
from bugsigdb_curation.curator.extract import StudyFields, extract_study
from bugsigdb_curation.curator.locate import locate_artifact
from bugsigdb_curation.curator.model import Model
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL, resolve
from bugsigdb_curation.curator.segment import segment_experiments
from bugsigdb_curation.curator.signature import ExtractedSignature, extract_signatures
from bugsigdb_curation.curator.taxonomy import DEFAULT_CACHE_PATH, NcbiTaxonomyResolver
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
    `pmid`/`pmcid`/`has_pmc`/`valid`/`problems` intentionally live on this
    wrapper, not inside `record`, so a caller can inspect S9's verdict
    without that verdict itself corrupting the very thing it's validating.
    An invalid record is still returned (never dropped), per the plan's
    S9 design rule: "attach pass/fail + problems to the output."
    """

    pmid: str
    pmcid: str | None
    has_pmc: bool
    record: dict[str, Any]
    valid: bool
    problems: tuple[Problem, ...]


def _empty_study_fields(doi: str | None) -> StudyFields:
    return StudyFields(title=None, journal=None, year=None, authors=(), doi=doi, study_design=())


async def curate_async(
    pmid: str,
    *,
    model: Model,
    config: str = DEFAULT_CONFIG,
    client: httpx.AsyncClient | None = None,
    email: str = DEFAULT_EMAIL,
    taxonomy_cache_path: Path | None = DEFAULT_CACHE_PATH,
    taxonomy_db_path: Path | None = None,
    taxonomy_db_release: str | None = None,
    resolver: NcbiTaxonomyResolver | None = None,
) -> CurationResult:
    """S0-S9: turn a bare PMID into a validated nested prediction record.

    `client`/`resolver` are injectable (tests / callers that want to reuse a
    connection pool or a warm taxonomy cache across many PMIDs, e.g.
    `--smoke` batch mode); a fresh short-lived `httpx.AsyncClient` and an
    on-disk-cached `NcbiTaxonomyResolver` are created and torn down/persisted
    automatically when not given. `taxonomy_db_path`/`taxonomy_db_release`
    (ignored once `resolver` is given directly) are forwarded to
    `NcbiTaxonomyResolver.load()`'s own `db_path`/`db_release` resolution
    (CLI flag -> `BUGSIGDB_TAXONOMY_DB` -> newest cached DB -> live-only).
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
    owns_resolver = resolver is None
    if resolver is None:
        resolver = NcbiTaxonomyResolver.load(
            cache_path=taxonomy_cache_path, db_path=taxonomy_db_path, db_release=taxonomy_db_release
        )

    try:
        resolved = await resolve(pmid, client=client, email=email)

        if not resolved.has_pmc:
            # Abstract-only stratum (plan §4a): S1's text+table+figure channel
            # has nothing to fetch from PMC. Emit a minimal, still
            # schema-checked Study-only record (no experiments) rather than
            # raising -- "do not silently drop invalid/incomplete records."
            record = assemble_record(resolved, _empty_study_fields(resolved.doi), [])
            problems = validate_instance(record, "Study", default_schema_path())
            return CurationResult(
                pmid=pmid, pmcid=None, has_pmc=False, record=record, valid=not problems, problems=tuple(problems)
            )

        assert resolved.pmcid is not None  # has_pmc guarantees this
        bundle = await assemble_evidence(pmid, resolved.pmcid, client=client)

        study_fields = extract_study(bundle, resolved, model=model)
        stubs = segment_experiments(bundle, model=model)
        artifact = locate_artifact(bundle)

        experiments: list[tuple[ExperimentFields, list[ExtractedSignature], str | None]] = []
        # NOTE: no per-experiment error isolation -- one bad ExperimentStub
        # (a raised exception from S4/S5a/S5b) aborts the whole study here.
        # Deferred to Architecture-B's fan-out (plan §2/§5), which isolates
        # each Experiment Worker; out of scope for this Design-1 skeleton.
        for stub in stubs:
            experiment_fields = extract_experiment(bundle, stub, model=model)

            signatures: list[ExtractedSignature] = []
            source: str | None = None
            if artifact is not None:
                image_bytes = None
                if artifact.kind == "figure" and artifact.figure is not None:
                    image_bytes = await fetch_figure_image(artifact.figure, client=client)
                signatures = await extract_signatures(
                    artifact, model=model, resolver=resolver, client=client, image_bytes=image_bytes
                )
                source = artifact.provenance

            experiments.append((experiment_fields, signatures, source))

        record = assemble_record(resolved, study_fields, experiments)
        problems = validate_instance(record, "Study", default_schema_path())

        return CurationResult(
            pmid=pmid,
            pmcid=resolved.pmcid,
            has_pmc=True,
            record=record,
            valid=not problems,
            problems=tuple(problems),
        )
    finally:
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


def curate(pmid: str, *, model: Model, config: str = DEFAULT_CONFIG, **kwargs: Any) -> CurationResult:
    """Sync wrapper around `curate_async`, for the CLI and other non-async callers."""
    return asyncio.run(curate_async(pmid, model=model, config=config, **kwargs))
