"""Typer CLI for downloading BugSigDB export files.

Thin layer: parses arguments, drives the async download logic in
:mod:`bugsigdb_curation.export`, and renders progress/output with `rich`. All
actual HTTP/filesystem logic lives in `export.py` so it can be unit tested
without a CLI in the loop.
"""

from __future__ import annotations

import asyncio
import sys
from enum import Enum
from pathlib import Path

import httpx
import typer
from rich.console import Console
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

app = typer.Typer(help="Download BugSigDB export artifacts from waldronlab/bugsigdbexports.")


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

        output_dir.mkdir(parents=True, exist_ok=True)

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

            results = await download_export_files(
                files,
                ref=ref,
                output_dir=output_dir,
                force=force,
                client=client,
                concurrency=DEFAULT_CONCURRENCY,
                progress_hook=on_progress,
            )

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
