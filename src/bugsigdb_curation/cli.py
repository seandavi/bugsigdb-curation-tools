"""Typer CLI for the `bugsigdb` command.

Thin layer: parses arguments, drives the (async, for `export`) logic in
:mod:`bugsigdb_curation.export` / :mod:`bugsigdb_curation.validate`, and
renders output with `rich`. All actual HTTP/filesystem/validation logic lives
in those modules so it can be unit tested without a CLI in the loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from enum import Enum
from pathlib import Path

import httpx
import typer
import yaml
from loguru import logger
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from bugsigdb_curation.curator.design import DEFAULT_DESIGN as CURATE_DEFAULT_DESIGN
from bugsigdb_curation.curator.design import Design
from bugsigdb_curation.curator.model import DEFAULT_MODEL as CURATE_DEFAULT_MODEL
from bugsigdb_curation.curator.model import LiteLLMModel, Model, MockModel
from bugsigdb_curation.curator.pipeline import CurationResult, curate_async
from bugsigdb_curation.curator.pipeline import DEFAULT_CONFIG as CURATE_DEFAULT_CONFIG
from bugsigdb_curation.curator.resolve import DEFAULT_EMAIL as CURATE_DEFAULT_EMAIL
from bugsigdb_curation.curator.resolve import resolve as resolve_pmid
from bugsigdb_curation.curator.smoke import smoke_study_ids
from bugsigdb_curation.curator.taxonomy import DEFAULT_CACHE_PATH as CURATE_DEFAULT_TAXONOMY_CACHE
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver
from bugsigdb_curation.eval.gold import load_gold, to_nested_dict
from bugsigdb_curation.eval.report import ScoringError, write_reports
from bugsigdb_curation.eval.score import StudyScore, aggregate_scores, score_study
from bugsigdb_curation.eval.smoke import select_smoke
from bugsigdb_curation.eval.taxonomy import DEFAULT_CACHE_PATH as DEFAULT_TAXONOMY_CACHE_PATH
from bugsigdb_curation.eval.taxonomy import TaxonomyResolver
from bugsigdb_curation.export import (
    DEFAULT_CONCURRENCY,
    ExportError,
    ExportFile,
    download_export_files,
    fetch_export_files,
    filter_files,
    human_size,
)
from bugsigdb_curation.loader import load_studies, summarize
from bugsigdb_curation.obs import configure_logging
from bugsigdb_curation.pmc_map import (
    DEFAULT_CONCURRENCY as PMC_MAP_DEFAULT_CONCURRENCY,
)
from bugsigdb_curation.pmc_map import (
    PmcMapError,
    compute_coverage,
    convert_pmids,
    distinct_pmids,
    join_results,
    read_study_pmids,
    write_mapping_csv,
)
from bugsigdb_curation.split import split_full_dump
from bugsigdb_curation.supplements import SupplementFile, fetch_supplements, supplement_to_text
from bugsigdb_curation.taxonomy.cli import taxonomy_app
from bugsigdb_curation.validate import (
    InstanceResult,
    ValidationInputError,
    default_schema_path,
    load_instances,
    validate_file,
)

app = typer.Typer(help="Download, split, and validate BugSigDB curation data.")


@app.callback()
def _main() -> None:
    """BugSigDB curation CLI.

    An explicit callback is required so Typer keeps `export` as a named
    subcommand (a Typer app with only one command otherwise collapses to
    invoking it directly, without the subcommand name).
    """


class SelectGroup(str, Enum):
    """Which group(s) of export files to operate on."""

    dump = "dump"
    gmt = "gmt"
    all = "all"


class LogFormat(str, Enum):
    """Structured-log sink format for `configure_logging` (`bugsigdb_curation.obs`)."""

    console = "console"
    json = "json"


#: Shared `typer.Option`s for `curate`/`eval score` -- both call
#: `configure_logging()` at startup; `None` (the default for either) means
#: "let `configure_logging` fall back to BUGSIGDB_LOG_FORMAT/BUGSIGDB_LOG_LEVEL
#: env vars, then its own console/INFO defaults" -- see obs.py.
_LOG_FORMAT_OPTION = typer.Option(
    None,
    "--log-format",
    help="Structured-log sink format: console (default) or json. Overrides BUGSIGDB_LOG_FORMAT.",
)
_LOG_LEVEL_OPTION = typer.Option(
    None,
    "--log-level",
    help="Log level, e.g. INFO/DEBUG/WARNING (default INFO). Overrides BUGSIGDB_LOG_LEVEL.",
)


DEFAULT_OUTPUT_DIR = Path("data/exports")
DEFAULT_REF = "devel"


@app.command("export")
def export_command(
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR,
        "--output-dir",
        "-o",
        help="Directory to write downloaded files to (created if missing).",
    ),
    select: SelectGroup = typer.Option(
        SelectGroup.dump,
        "--select",
        "-s",
        help="Which file group(s) to fetch: dump (full_dump.csv + file_size.csv), gmt, or all.",
    ),
    ref: str = typer.Option(
        DEFAULT_REF,
        "--ref",
        help="Git ref (branch/tag) of waldronlab/bugsigdbexports to fetch from.",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite existing files even if their size already matches."
    ),
    list_only: bool = typer.Option(
        False, "--list", "-l", help="List available files and exit without downloading."
    ),
) -> None:
    """Download BugSigDB export files (full_dump.csv, file_size.csv, and/or GMT signature sets)."""
    error_console = Console(stderr=True)
    try:
        asyncio.run(_run(output_dir, select.value, ref, force, list_only))
    except ExportError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Error:[/red] request failed: {exc}")
        raise typer.Exit(code=1) from None


async def _run(output_dir: Path, select: str, ref: str, force: bool, list_only: bool) -> None:
    console = Console()
    async with httpx.AsyncClient(timeout=30.0) as client:
        all_files = await fetch_export_files(client, ref)
        files = filter_files(all_files, select)  # type: ignore[arg-type]

        if not files:
            console.print(f"[yellow]No files found for --select {select} at ref {ref!r}.[/yellow]")
            return

        if list_only:
            _print_file_table(files, ref, console)
            return

        # output_dir is created by download_export_files() itself.
        is_tty = sys.stdout.isatty()
        with Progress(
            TextColumn("[bold blue]{task.fields[name]}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            disable=not is_tty,
        ) as progress:
            task_ids = {
                f.name: progress.add_task("download", name=f.name, total=max(f.size, 1)) for f in files
            }

            def on_progress(name: str, downloaded: int, total: int) -> None:
                progress.update(task_ids[name], completed=downloaded, total=total or max(downloaded, 1))

            try:
                results = await download_export_files(
                    files,
                    ref=ref,
                    output_dir=output_dir,
                    force=force,
                    client=client,
                    concurrency=DEFAULT_CONCURRENCY,
                    progress_hook=on_progress,
                )
            except ExportError as exc:
                # One file failing aborts the whole gather(), but others may have
                # already finished writing to output_dir before that happened.
                completed = sorted(f.name for f in files if (output_dir / f.name).exists())
                if completed:
                    raise ExportError(
                        f"{exc} Note: {', '.join(completed)} may have already been saved to "
                        f"{output_dir} before this error occurred."
                    ) from exc
                raise

        downloaded = [r for r in results if r.status == "downloaded"]
        skipped = [r for r in results if r.status == "skipped"]
        if skipped:
            names = ", ".join(r.file.name for r in skipped)
            console.print(f"[dim]Skipped (already up to date): {names}[/dim]")
        if downloaded:
            names = ", ".join(r.file.name for r in downloaded)
            console.print(f"[green]Downloaded:[/green] {names}")


def _print_file_table(files: list[ExportFile], ref: str, console: Console) -> None:
    table = Table(title=f"Available export files ({ref})")
    table.add_column("Name")
    table.add_column("Group")
    table.add_column("Size", justify="right")
    for f in sorted(files, key=lambda f: (f.group, f.name)):
        table.add_row(f.name, f.group, human_size(f.size))
    console.print(table)


@app.command("split")
def split_command(
    input_file: Path = typer.Option(
        DEFAULT_OUTPUT_DIR / "full_dump.csv",
        "--input",
        "-i",
        help="Path to the flat full_dump.csv file to split.",
    ),
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR / "relational",
        "--output-dir",
        "-o",
        help="Directory to write relational CSV files to (created if missing).",
    ),
) -> None:
    """Split a flat full_dump.csv export into normalized relational CSV files."""
    console = Console()
    error_console = Console(stderr=True)
    try:
        counts = split_full_dump(input_file, output_dir)
        console.print("[green]Successfully split full dump into relational CSVs:[/green]")
        for filename, count in counts.items():
            console.print(f"  - [bold]{filename}[/bold]: {count} rows written")
    except FileNotFoundError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None


DEFAULT_STUDIES_CSV = Path("data/exports/relational/studies.csv")
DEFAULT_PMC_MAP_OUTPUT = Path("data/eval/pmid_pmcid_map.csv")
DEFAULT_PMC_MAP_EMAIL = "seandavi@gmail.com"


@app.command("pmc-map")
def pmc_map_command(
    input_file: Path = typer.Option(
        DEFAULT_STUDIES_CSV,
        "--input",
        "-i",
        help="Path to a relational studies.csv (as produced by `bugsigdb split`).",
    ),
    output_file: Path = typer.Option(
        DEFAULT_PMC_MAP_OUTPUT,
        "--output",
        "-o",
        help="Path to write the study_id/pmid/pmcid/doi/has_pmc mapping CSV to (created if missing).",
    ),
    email: str = typer.Option(
        DEFAULT_PMC_MAP_EMAIL,
        "--email",
        help="Contact email sent to NCBI's idconv API (their etiquette for unauthenticated use).",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help=(
            "Only convert the first N distinct PMIDs (handy for a quick/test run). "
            "Study rows whose PMID falls outside that subset are excluded from the "
            "output CSV (a note with the excluded count is printed to stderr)."
        ),
    ),
) -> None:
    """Map curated BugSigDB study PMIDs to PubMed Central IDs (PMCIDs).

    Queries the NCBI PMC ID Converter API to determine which curated
    studies' PMIDs have a corresponding PMC full-text article, producing a
    gold/eval set for de-novo curation workflows that need full text.
    """
    error_console = Console(stderr=True)
    if not input_file.exists():
        error_console.print(f"[red]Error:[/red] {input_file} does not exist.")
        raise typer.Exit(code=1)

    try:
        asyncio.run(_run_pmc_map(input_file, output_file, email, limit))
    except PmcMapError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Error:[/red] request failed: {exc}")
        raise typer.Exit(code=1) from None


async def _run_pmc_map(input_file: Path, output_file: Path, email: str, limit: int | None) -> None:
    console = Console()
    error_console = Console(stderr=True)

    rows = read_study_pmids(input_file)
    ids = distinct_pmids(rows)
    if limit is not None:
        ids = ids[:limit]

    async with httpx.AsyncClient(timeout=30.0) as client:
        records = await convert_pmids(ids, email=email, client=client, concurrency=PMC_MAP_DEFAULT_CONCURRENCY)

    mapped = join_results(rows, records)
    write_mapping_csv(mapped, output_file)
    console.print(f"[green]Wrote {len(mapped)} rows to {output_file}[/green]")

    if limit is not None and len(mapped) < len(rows):
        excluded = len(rows) - len(mapped)
        error_console.print(f"Note: {excluded} study row(s) excluded (PMID outside --limit).")

    stats = compute_coverage(records)
    error_console.print(
        f"{stats.total} PMIDs: {stats.with_pmc} with PMCID ({stats.coverage_pct:.1f}%), "
        f"{stats.without_pmc} without."
    )


class OutputFormat(str, Enum):
    """Rendering format for `bugsigdb validate` results."""

    text = "text"
    json = "json"


DEFAULT_TARGET_CLASS = "Study"


@app.command("validate")
def validate_command(
    files: list[Path] = typer.Argument(
        ..., help="Instance file(s) (YAML or JSON) to validate. Each may hold a single object or a list."
    ),
    target_class: str = typer.Option(
        DEFAULT_TARGET_CLASS,
        "--target-class",
        "-C",
        help="LinkML class to validate each instance against.",
    ),
    schema: Path | None = typer.Option(
        None,
        "--schema",
        "-s",
        help="Override the LinkML schema (default: the packaged schema/bugsigdb.yaml).",
    ),
    output_format: OutputFormat = typer.Option(
        OutputFormat.text, "--format", help="Output format: 'text' (rich table) or 'json'."
    ),
) -> None:
    """Validate curated BugSigDB instance file(s) against the LinkML schema.

    Exit code 0 if every instance in every file is valid; 1 if any instance
    fails schema validation; 2 for usage/IO errors (file not found,
    unparseable YAML/JSON, unknown --target-class, or bad --schema path).
    """
    error_console = Console(stderr=True)
    try:
        schema_path = schema if schema is not None else default_schema_path()
    except FileNotFoundError as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from None

    all_results: list[InstanceResult] = []
    try:
        for path in files:
            all_results.extend(validate_file(path, target_class, schema_path))
    except ValidationInputError as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from None

    if output_format is OutputFormat.json:
        print(json.dumps([_result_to_dict(r) for r in all_results], indent=2))
    else:
        _render_validation_text(all_results, Console())

    has_errors = any(not r.valid for r in all_results)
    raise typer.Exit(code=1 if has_errors else 0)


def _result_to_dict(result: InstanceResult) -> dict:
    return {
        "file": str(result.file),
        "index": result.index,
        "target_class": result.target_class,
        "valid": result.valid,
        "problems": [
            {
                "severity": p.severity,
                "message": p.message,
                "instantiates": p.instantiates,
                "path": p.path,
            }
            for p in result.problems
        ],
    }


def _render_validation_text(results: list[InstanceResult], console: Console) -> None:
    if not results:
        console.print("[yellow]No instances found to validate.[/yellow]")
        return

    total = len(results)
    valid_count = sum(1 for r in results if r.valid)

    for result in results:
        label = f"{result.file}[{result.index}]" if len(results) > 1 or result.index > 0 else str(result.file)
        label = escape(label)
        if result.valid:
            console.print(f"[green]PASS[/green] {label} ({result.target_class}: valid)")
            continue
        console.print(f"[red]FAIL[/red] {label} ({result.target_class}: {len(result.problems)} problem(s))")
        for problem in result.problems:
            # `problem.message` already ends in "... in /json/pointer/path" (from
            # the LinkML jsonschema plugin), so it's not repeated here; `path` is
            # still exposed as its own field in --format json for machine use.
            console.print(f"    [red]{problem.severity}[/red]: {escape(problem.message)}")

    if valid_count == total:
        console.print(f"\n[green]{valid_count}/{total} valid.[/green]")
    else:
        console.print(f"\n[red]{valid_count}/{total} valid.[/red]")


class LoadFormat(str, Enum):
    """Output serialization format for `bugsigdb load`."""

    yaml = "yaml"
    json = "json"


@app.command("load")
def load_command(
    csv_path: Path = typer.Argument(
        ..., help="Path to a BugSigDB full_dump.csv export (or a sample of it)."
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="File to write the nested studies to (default: stdout).",
    ),
    format: LoadFormat = typer.Option(
        LoadFormat.yaml, "--format", help="Output serialization format."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        help="Only load the first N studies (handy for sampling the full 30 MB dump).",
    ),
) -> None:
    """Parse a full_dump.csv export into nested Study -> Experiment -> Signature records."""
    error_console = Console(stderr=True)
    if not csv_path.exists():
        error_console.print(f"[red]Error:[/red] {csv_path} does not exist.")
        raise typer.Exit(code=1)

    studies = load_studies(csv_path, limit=limit)
    n_studies, n_experiments, n_signatures = summarize(studies)

    if format is LoadFormat.json:
        text = json.dumps(studies, indent=2, ensure_ascii=False) + "\n"
    else:
        text = yaml.safe_dump(studies, sort_keys=False, allow_unicode=True)

    if output is not None:
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    error_console.print(f"{n_studies} studies, {n_experiments} experiments, {n_signatures} signatures")


# ---------------------------------------------------------------------------
# curate: the de-novo curator (linear single-worker, --design selects one of
# fused-lean/split-verify/split-panel, see curator.design)
# ---------------------------------------------------------------------------


def _build_model(mock: bool, model_name: str) -> Model:
    model = MockModel() if mock else LiteLLMModel(model=model_name)
    logger.bind(stage="init").info("model backend", model="mock" if mock else model_name, mock=mock)
    return model


def _report_result(result: CurationResult) -> None:
    """Log a structured curate-result summary for `--pmid` mode.

    The heavier stage-by-stage trail (S0-S9) already came out of
    `curate_async` itself as it ran (see `curator/pipeline.py` and the
    individual stage modules) -- this is just the CLI's own final-outcome
    line, replacing the old ad-hoc `Console.print(f"PMID {pmid}: ...")`.
    """
    logger.bind(stage="cli", event="curate_result").info(
        "curate result",
        pmid=result.pmid,
        design=result.design.value,
        valid=result.valid,
        has_pmc=result.has_pmc,
        n_experiments=len(result.record.get("experiments", []) or []),
        n_problems=len(result.problems),
        n_flags=len(result.flags),
    )
    for problem in result.problems:
        logger.bind(stage="cli").warning(
            "validation problem", pmid=result.pmid, severity=problem.severity, message=problem.message
        )
    for flag in result.flags:
        logger.bind(stage="cli").warning("design flag", pmid=result.pmid, design=result.design.value, flag=flag)


@app.command("curate")
def curate_command(
    pmid: str | None = typer.Option(
        None, "--pmid", help="PMID to curate into a prediction record. Mutually exclusive with --smoke."
    ),
    model_name: str = typer.Option(
        CURATE_DEFAULT_MODEL, "--model", help="LiteLLM model id for the real backend (ignored with --mock)."
    ),
    config: str = typer.Option(
        CURATE_DEFAULT_CONFIG, "--config", help="Source-config label recorded informationally; not yet switchable."
    ),
    design: Design = typer.Option(
        CURATE_DEFAULT_DESIGN,
        "--design",
        help=(
            "Curator stage-design (workflow plan §6b): fused-lean (default, one fused "
            "extract+id-propose call), split-verify (split NER/reconcile + adversarial "
            "verifier), or split-panel (split NER/reconcile + independent reviewer panel)."
        ),
    ),
    mock: bool = typer.Option(
        False, "--mock", help="Use MockModel: deterministic, fully offline, no API key required."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Output path: a file for a single --pmid (default: stdout), or a directory for --smoke (required).",
    ),
    smoke: bool = typer.Option(
        False, "--smoke", help="Batch mode: curate every study in the curator's own smoke-set ID list."
    ),
    output_format: LoadFormat = typer.Option(
        LoadFormat.json, "--format", help="Serialization format (single --pmid mode only; --smoke always writes JSON)."
    ),
    email: str = typer.Option(
        CURATE_DEFAULT_EMAIL, "--email", help="Contact email sent to NCBI APIs (idconv, E-utilities)."
    ),
    taxonomy_cache: Path = typer.Option(
        CURATE_DEFAULT_TAXONOMY_CACHE,
        "--taxonomy-cache",
        help="The curator's own NCBI-taxonomy resolver cache (distinct from the eval harness's cache).",
    ),
    taxonomy_db: Path | None = typer.Option(
        None,
        "--taxonomy-db",
        help=(
            "Local taxonomy .duckdb path, tried before live NCBI E-utilities (default: "
            "BUGSIGDB_TAXONOMY_DB > newest cached ncbi-taxdump-*.duckdb > none, i.e. live-only)."
        ),
    ),
    taxonomy_release: str | None = typer.Option(
        None,
        "--taxonomy-release",
        help="Release label for locating the default cached taxonomy DB (ignored once --taxonomy-db/BUGSIGDB_TAXONOMY_DB apply).",
    ),
    log_format: LogFormat | None = _LOG_FORMAT_OPTION,
    log_level: str | None = _LOG_LEVEL_OPTION,
) -> None:
    """Curate a PMID into a schema-checked de-novo prediction record.

    Takes only a PMID (+ model/config/`--design`) -- no gold path of any
    kind (see the workflow plan §6e's data firewall). `--design` selects
    one of the three §6b stage-designs (default `fused-lean`, the original
    walking skeleton). Writes the nested prediction record in exactly the
    shape `bugsigdb eval score` consumes.
    """
    configure_logging(fmt=log_format.value if log_format is not None else None, level=log_level)

    console = Console()
    error_console = Console(stderr=True)

    if bool(pmid) == bool(smoke):
        error_console.print("[red]Error:[/red] pass exactly one of --pmid or --smoke.")
        raise typer.Exit(code=2)

    model = _build_model(mock, model_name)
    run_id = uuid.uuid4().hex[:12]

    if smoke:
        if out is None:
            error_console.print("[red]Error:[/red] --smoke requires --out (a directory).")
            raise typer.Exit(code=2)
        asyncio.run(
            _run_curate_smoke(
                model, config, design, email, taxonomy_cache, taxonomy_db, taxonomy_release, out, console, run_id
            )
        )
        return

    assert pmid is not None  # guaranteed by the exactly-one-of check above
    asyncio.run(
        _run_curate_one(
            pmid,
            model,
            config,
            design,
            email,
            taxonomy_cache,
            taxonomy_db,
            taxonomy_release,
            out,
            output_format,
            console,
            error_console,
            run_id,
        )
    )


async def _run_curate_one(
    pmid: str,
    model: Model,
    config: str,
    design: Design,
    email: str,
    taxonomy_cache: Path,
    taxonomy_db: Path | None,
    taxonomy_release: str | None,
    out: Path | None,
    output_format: LoadFormat,
    console: Console,
    error_console: Console,
    run_id: str | None = None,
) -> None:
    try:
        result = await curate_async(
            pmid,
            model=model,
            config=config,
            design=design,
            email=email,
            taxonomy_cache_path=taxonomy_cache,
            taxonomy_db_path=taxonomy_db,
            taxonomy_db_release=taxonomy_release,
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 -- surface any stage failure as a clean CLI error, not a traceback
        error_console.print(f"[red]Error curating PMID {pmid}:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None

    if output_format is LoadFormat.json:
        text = json.dumps(result.record, indent=2, ensure_ascii=False) + "\n"
    else:
        text = yaml.safe_dump(result.record, sort_keys=False, allow_unicode=True)

    if out is not None:
        out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    _report_result(result)
    if not result.valid:
        raise typer.Exit(code=1)


async def _run_curate_smoke(
    model: Model,
    config: str,
    design: Design,
    email: str,
    taxonomy_cache: Path,
    taxonomy_db: Path | None,
    taxonomy_release: str | None,
    out_dir: Path,
    console: Console,
    run_id: str | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = smoke_study_ids()
    logger.bind(stage="cli", run_id=run_id).info("smoke run starting", n_studies=len(ids))

    n_valid = 0
    n_errors = 0
    # One shared client for the whole batch (reused connection pool/keep-
    # alive) instead of curate_async creating and tearing down a fresh
    # client per study -- fewer connections churned, less NCBI/PMC
    # rate-limit exposure. Per-study error isolation stays intact: a bad
    # study is caught and skipped without closing the shared client.
    #
    # Likewise, one shared NcbiTaxonomyResolver for the whole batch instead
    # of curate_async building a fresh one (fresh _RateLimiter + empty
    # cache) per study: a resolver built per-study only throttles calls
    # *within* one study, and taxa resolved for an earlier study aren't
    # cached for a later one -- both defeat the point of throttling/caching
    # across a --smoke run and are the actual cause of the 429 storm this
    # loop otherwise still risks. save_cache() runs once after the loop
    # instead of once per study.
    resolver = NcbiTaxonomyResolver.load(cache_path=taxonomy_cache, db_path=taxonomy_db, db_release=taxonomy_release)
    async with httpx.AsyncClient(timeout=30.0) as client:
        for study_id in ids:
            try:
                result = await curate_async(
                    study_id,
                    model=model,
                    config=config,
                    design=design,
                    client=client,
                    email=email,
                    taxonomy_cache_path=taxonomy_cache,
                    resolver=resolver,
                    run_id=run_id,
                )
            except Exception as exc:  # noqa: BLE001 -- one bad study must not abort the whole batch
                n_errors += 1
                logger.bind(stage="cli", study_id=study_id, run_id=run_id).error(
                    "study curation failed", error=f"{type(exc).__name__}: {exc}"
                )
                continue

            (out_dir / f"{study_id}.json").write_text(
                json.dumps(result.record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            # Per-study progress is already in the structured log stream
            # (curate_async's own S0-S9 events and its final `study_done`,
            # all bound with this study's study_id/run_id) -- no separate
            # CLI-side per-study console line needed; only the batch's
            # concise human summary below stays a `console.print`.
            if result.valid:
                n_valid += 1

    resolver.save_cache()
    resolver.close()  # this loop owns the shared resolver's TaxonomyDB handle; close it once, here.
    logger.bind(stage="cli", run_id=run_id).info(
        "smoke run finished", n_studies=len(ids), n_valid=n_valid, n_errors=n_errors
    )
    console.print(
        f"[green]Curated {len(ids)} studies -> {out_dir}[/green] ({n_valid} valid, {n_errors} error(s))"
    )


# ---------------------------------------------------------------------------
# eval subcommand group
# ---------------------------------------------------------------------------

eval_app = typer.Typer(help="Score de-novo curator predictions against the BugSigDB gold corpus.")
app.add_typer(eval_app, name="eval")

# `taxonomy build`/`taxonomy lookup` -- the standalone local NCBI taxonomy DB
# subpackage (build.py/db.py/paths.py), also wired (PR-2) into `curate`'s and
# `eval score`'s own resolvers via `--taxonomy-db`/`--taxonomy-release`
# below; see bugsigdb_curation.taxonomy's package docstring.
app.add_typer(taxonomy_app, name="taxonomy")

DEFAULT_RELATIONAL_DIR = Path("data/exports/relational")
DEFAULT_PMC_MAP_PATH = Path("data/eval/pmid_pmcid_map.csv")


class EvalConfig(str, Enum):
    """Source-config label recorded in the eval report (§6c); informational
    only -- the harness scores whatever a prediction contains regardless of
    which config produced it."""

    abstract_only = "abstract-only"
    text_tables = "text-tables"
    text_tables_figures = "text-tables-figures"


def _load_predictions(pred_path: Path) -> dict[str, dict]:
    """Load prediction file(s) into `{study_id: predicted_study_dict}`.

    **The prediction-record contract**: each file is YAML or JSON (reusing
    `validate.load_instances`, so either a bare object or a list of objects
    is accepted) holding one or more studies in the `bugsigdb_curation.loader`
    nested-dict shape (`Study -> experiments[] -> signatures[] -> taxa[]`).
    A study is matched to its gold counterpart by, in order: (1) a
    `study_id` key on the study object, (2) a `uid` key (the loader's own
    identifier slot name), (3) if the file holds exactly one study and
    neither key is present, the file's stem (e.g. `predictions/21850056.json`
    -> study_id `"21850056"`). `--pred` may be a single file or a directory
    of `.json`/`.yaml`/`.yml` files.
    """
    files = (
        sorted(p for p in pred_path.iterdir() if p.suffix.lower() in {".json", ".yaml", ".yml"})
        if pred_path.is_dir()
        else [pred_path]
    )

    predictions: dict[str, dict] = {}
    for f in files:
        instances = [instance for instance in load_instances(f) if isinstance(instance, dict)]
        for instance in instances:
            study_id = instance.get("study_id") or instance.get("uid")
            if study_id is None and len(instances) == 1:
                study_id = f.stem
            if study_id is not None:
                predictions[str(study_id)] = instance
    return predictions


@eval_app.command("score")
def eval_score_command(
    pred: Path = typer.Option(
        ..., "--pred", help="Prediction file or directory (loader nested-shape JSON/YAML)."
    ),
    relational: Path = typer.Option(
        DEFAULT_RELATIONAL_DIR, "--relational", help="Relational gold CSV directory (from `bugsigdb split`)."
    ),
    pmc_map: Path = typer.Option(
        DEFAULT_PMC_MAP_PATH, "--pmc-map", help="pmid_pmcid_map.csv path (from `bugsigdb pmc-map`)."
    ),
    out: Path = typer.Option(..., "--out", help="Output directory for scores.jsonl/report.md/report.html."),
    smoke: bool = typer.Option(False, "--smoke", help="Score only the curated ~20-study smoke set."),
    config: EvalConfig | None = typer.Option(
        None, "--config", help="Source-config label to note in the run (informational only)."
    ),
    taxonomy_cache: Path = typer.Option(
        DEFAULT_TAXONOMY_CACHE_PATH, "--taxonomy-cache", help="Taxonomy resolver JSON cache path."
    ),
    taxonomy_db: Path | None = typer.Option(
        None,
        "--taxonomy-db",
        help=(
            "Local taxonomy .duckdb path for resolving predicted taxon names (default: "
            "BUGSIGDB_TAXONOMY_DB > newest cached ncbi-taxdump-*.duckdb > none)."
        ),
    ),
    taxonomy_release: str | None = typer.Option(
        None,
        "--taxonomy-release",
        help="Release label for locating the default cached taxonomy DB (ignored once --taxonomy-db/BUGSIGDB_TAXONOMY_DB apply).",
    ),
    log_format: LogFormat | None = _LOG_FORMAT_OPTION,
    log_level: str | None = _LOG_LEVEL_OPTION,
) -> None:
    """Score predictions (loader nested-shape) against the gold corpus."""
    configure_logging(fmt=log_format.value if log_format is not None else None, level=log_level)

    console = Console()
    error_console = Console(stderr=True)

    if not relational.exists():
        error_console.print(f"[red]Error:[/red] {relational} does not exist.")
        raise typer.Exit(code=1)
    if not pred.exists():
        error_console.print(f"[red]Error:[/red] {pred} does not exist.")
        raise typer.Exit(code=1)

    gold = load_gold(relational, pmc_map)
    if smoke:
        gold = select_smoke(gold)

    predictions = _load_predictions(pred)
    # PR-2: predicted taxon names resolve through the general NCBI TaxonomyDB,
    # not a taxa.csv seed built from gold -- see bugsigdb_curation.eval.taxonomy.
    resolver = TaxonomyResolver.load(cache_path=taxonomy_cache, db_path=taxonomy_db, db_release=taxonomy_release)
    # Fix 2: unlike the curator (which falls back to live NCBI E-utilities),
    # `eval score`'s offline scoring path has NO network fallback -- a
    # missing/broken local DB silently disables every name-based
    # sub-score (genus-lenient P/R/F1, name->ID accuracy) for the whole
    # run. `TaxonomyResolver.load()` already emits a one-time
    # `RuntimeWarning` for this (mirroring the curator); surface it loudly
    # here too, since a warning is easy to miss in a batch/CI run, and
    # again in the written report (`write_reports(local_taxonomy_db_available=...)`
    # below) so it's visible after the fact, not just in this run's console.
    local_taxonomy_db_available = resolver.db is not None
    if not local_taxonomy_db_available:
        error_console.print(
            "[red]WARNING: no local taxonomy DB found.[/red] Name-based taxon resolution "
            "(genus-lenient P/R/F1 and name→ID sub-scores) is disabled/degraded for this "
            "run -- only predictions that already carry a numeric ncbi_id resolve at all. "
            "Build one with `bugsigdb taxonomy build`, or pass --taxonomy-db/BUGSIGDB_TAXONOMY_DB."
        )

    # Score every selected gold study, not just the ones with a prediction
    # (Blocker 2 / §4d "same corpus, same split"): a study the pipeline
    # failed or skipped must still count against the aggregate as a full
    # miss, not silently vanish from the denominator. `score_study` treats
    # `predicted=None` as an empty prediction.
    selected_ids = sorted(gold)
    if not selected_ids:
        error_console.print("[yellow]No gold studies selected; nothing to score.[/yellow]")

    missing_prediction_ids = sorted(study_id for study_id in selected_ids if study_id not in predictions)

    study_scores: list[StudyScore] = []
    scoring_errors: list[ScoringError] = []
    for study_id in selected_ids:
        try:
            study_scores.append(score_study(gold[study_id], predictions.get(study_id), resolver))
        except Exception as exc:  # noqa: BLE001 -- per-study isolation: one bad prediction must not abort the run
            scoring_errors.append({"study_id": study_id, "error": f"{type(exc).__name__}: {exc}"})

    aggregate = aggregate_scores(study_scores)
    paths = write_reports(
        study_scores,
        aggregate,
        out,
        missing_prediction_ids=missing_prediction_ids,
        scoring_errors=scoring_errors,
        local_taxonomy_db_available=local_taxonomy_db_available,
    )
    resolver.save_cache()
    resolver.close()

    # Diagnostic dump of predicted taxon names the resolver could never map
    # to a taxid -- useful to distinguish a hallucinated taxon from a gap in
    # the resolver's own coverage.
    if resolver.unresolved:
        (out / "unresolved_taxa.txt").write_text(
            "\n".join(sorted(resolver.unresolved)) + "\n", encoding="utf-8"
        )

    config_note = f" [config={config.value}]" if config else ""
    console.print(f"[green]Scored {len(study_scores)} studies{config_note}.[/green]")
    if missing_prediction_ids:
        console.print(
            f"[yellow]  {len(missing_prediction_ids)} gold studies had no prediction "
            "(scored as a full miss; see report's Missing predictions section).[/yellow]"
        )
    if scoring_errors:
        console.print(
            f"[red]  {len(scoring_errors)} predictions raised while scoring "
            "(see report's Scoring errors section).[/red]"
        )
    console.print(
        f"  micro taxa F1: {aggregate.micro_taxa.f1:.3f}   "
        f"macro taxa F1: {aggregate.macro_taxa_f1:.3f}   "
        f"direction acc: {aggregate.direction_accuracy:.1%}"
    )
    if not local_taxonomy_db_available:
        console.print(
            "[red]  no local taxonomy DB -- name-based sub-scores above are disabled/degraded[/red]"
        )
    console.print(
        f"  resolution coverage: {aggregate.n_unresolved_pred_taxa} predicted taxon name(s) "
        f"unresolved, {aggregate.n_unresolved_gold_taxa} gold tax_id(s) unresolved to a name "
        "(Fix 2b; see report)"
    )
    console.print(f"  wrote {paths['jsonl']}, {paths['md']}, {paths['html']}")


@eval_app.command("gold")
def eval_gold_command(
    relational: Path = typer.Option(
        DEFAULT_RELATIONAL_DIR, "--relational", help="Relational gold CSV directory (from `bugsigdb split`)."
    ),
    pmc_map: Path = typer.Option(
        DEFAULT_PMC_MAP_PATH, "--pmc-map", help="pmid_pmcid_map.csv path (from `bugsigdb pmc-map`)."
    ),
    smoke: bool = typer.Option(True, "--smoke/--full", help="Dump only the curated smoke set (default) or the full gold corpus."),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="File to write the nested gold studies to (default: stdout)."
    ),
) -> None:
    """Dump gold studies in the loader nested-dict shape (handy for authoring predictions)."""
    error_console = Console(stderr=True)
    if not relational.exists():
        error_console.print(f"[red]Error:[/red] {relational} does not exist.")
        raise typer.Exit(code=1)

    gold = load_gold(relational, pmc_map)
    if smoke:
        gold = select_smoke(gold)

    nested = [to_nested_dict(g) for g in gold.values()]
    text = yaml.safe_dump(nested, sort_keys=False, allow_unicode=True)

    if output is not None:
        output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)

    error_console.print(f"Dumped {len(nested)} gold studies.")


# ---------------------------------------------------------------------------
# supplements: standalone supplement fetch/parse -- NOT wired into the
# curator pipeline (`curator/pipeline.py`, `curator/evidence.py`); that's a
# deliberate follow-up PR. Read-only, no gold access.
# ---------------------------------------------------------------------------


@app.command("supplements")
def supplements_command(
    pmid: str | None = typer.Option(
        None, "--pmid", help="PMID to resolve to a PMCID before fetching supplements. Mutually exclusive with --pmcid."
    ),
    pmcid: str | None = typer.Option(
        None, "--pmcid", help="PMCID to fetch supplements for directly. Mutually exclusive with --pmid."
    ),
    email: str = typer.Option(
        CURATE_DEFAULT_EMAIL,
        "--email",
        help="Contact email sent to NCBI's idconv API when resolving --pmid (their etiquette for unauthenticated use).",
    ),
    dump: Path | None = typer.Option(
        None,
        "--dump",
        help="Directory to write each fetched file's raw bytes (and parsed text, where available) to, for inspection.",
    ),
    log_format: LogFormat | None = _LOG_FORMAT_OPTION,
    log_level: str | None = _LOG_LEVEL_OPTION,
) -> None:
    """Fetch and list a paper's supplementary files (standalone; read-only, no gold access).

    Resolves `--pmid` to a PMCID via the same public NCBI idconv path S0 uses
    (`bugsigdb_curation.curator.resolve.resolve`), or accepts `--pmcid`
    directly to skip resolution. Fetches the EuropePMC ``supplementaryFiles``
    ZIP for that PMCID (`bugsigdb_curation.supplements.fetch_supplements`)
    and prints a table of filename / media type / size / text-preview
    availability. This command only reads public EuropePMC/NCBI REST data --
    never a cached/gold file -- and is not wired into `bugsigdb curate`.
    """
    configure_logging(fmt=log_format.value if log_format is not None else None, level=log_level)
    console = Console()
    error_console = Console(stderr=True)

    if bool(pmid) == bool(pmcid):
        error_console.print("[red]Error:[/red] pass exactly one of --pmid or --pmcid.")
        raise typer.Exit(code=2)

    try:
        asyncio.run(_run_supplements(pmid, pmcid, email, dump, console, error_console))
    except PmcMapError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None
    except httpx.HTTPError as exc:
        error_console.print(f"[red]Error:[/red] request failed: {exc}")
        raise typer.Exit(code=1) from None


async def _run_supplements(
    pmid: str | None,
    pmcid: str | None,
    email: str,
    dump: Path | None,
    console: Console,
    error_console: Console,
) -> None:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resolved_pmcid = pmcid
        if resolved_pmcid is None:
            assert pmid is not None  # guaranteed by the exactly-one-of check in supplements_command
            resolved = await resolve_pmid(pmid, client=client, email=email)
            if resolved.pmcid is None:
                error_console.print(f"[yellow]PMID {pmid} has no PMCID (not in PMC); nothing to fetch.[/yellow]")
                return
            resolved_pmcid = resolved.pmcid
            console.print(f"[dim]Resolved PMID {pmid} -> {resolved_pmcid}[/dim]")

        files = await fetch_supplements(resolved_pmcid, client=client)

    if not files:
        console.print(f"[yellow]No supplementary files found for {resolved_pmcid}.[/yellow]")
        return

    _print_supplements_table(files, resolved_pmcid, console)

    if dump is not None:
        _dump_supplements(files, dump)
        console.print(f"[green]Dumped {len(files)} file(s) to {dump}[/green]")


def _text_preview_flag(f: SupplementFile) -> str:
    if f.media_type == "pdf":
        return "n/a (native doc)"
    return "yes" if supplement_to_text(f) else "no"


def _print_supplements_table(files: list[SupplementFile], pmcid: str, console: Console) -> None:
    table = Table(title=f"Supplementary files ({pmcid})")
    table.add_column("Filename")
    table.add_column("Media type")
    table.add_column("Size", justify="right")
    table.add_column("Text preview")
    for f in files:
        table.add_row(f.filename, f.media_type, human_size(len(f.raw_bytes)), _text_preview_flag(f))
    console.print(table)


def _dump_supplements(files: list[SupplementFile], dump_dir: Path) -> None:
    dump_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        (dump_dir / f.filename).write_bytes(f.raw_bytes)
        text = supplement_to_text(f)
        if text is not None:
            (dump_dir / f"{f.filename}.txt").write_text(text, encoding="utf-8")
