"""Typer CLI for the `bugsigdb` command.

Thin layer: parses arguments, drives the (async, for `export`) logic in
:mod:`bugsigdb_curation.export` / :mod:`bugsigdb_curation.validate`, and
renders output with `rich`. All actual HTTP/filesystem/validation logic lives
in those modules so it can be unit tested without a CLI in the loop.
"""

from __future__ import annotations

import asyncio
import json as json_module
import sys
from enum import Enum
from pathlib import Path

import httpx
import typer
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

from bugsigdb_curation.export import (
    DEFAULT_CONCURRENCY,
    ExportError,
    ExportFile,
    download_export_files,
    fetch_export_files,
    filter_files,
    human_size,
)
from bugsigdb_curation.validate import (
    InstanceResult,
    ValidationInputError,
    default_schema_path,
    validate_file,
)

app = typer.Typer(help="Download and validate BugSigDB curation data.")


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
    schema: Path = typer.Option(
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
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    all_results: list[InstanceResult] = []
    try:
        for path in files:
            all_results.extend(validate_file(path, target_class, schema_path))
    except ValidationInputError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if output_format is OutputFormat.json:
        print(json_module.dumps([_result_to_dict(r) for r in all_results], indent=2))
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
