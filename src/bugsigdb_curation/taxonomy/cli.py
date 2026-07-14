"""`bugsigdb taxonomy` -- build and query the local DuckDB taxonomy resolver.

A Typer sub-app wired into the top-level `bugsigdb` CLI (see
`bugsigdb_curation.cli`), following that module's conventions: thin argument
parsing + `rich` rendering here, all real logic in `build.py`/`db.py`/
`paths.py` so it's unit-testable without a CLI in the loop.

This is only the standalone `build`/`lookup` surface; the curator and eval
scorer resolve through `TaxonomyDB` directly (see the package docstring),
not through this CLI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx
import typer
from rich.console import Console
from rich.table import Table

from bugsigdb_curation.taxonomy.build import (
    NCBI_TAXDUMP_ARCHIVE_URL_TEMPLATE,
    build_taxonomy_db,
    download_taxdump,
)
from bugsigdb_curation.taxonomy.db import TaxonomyDB
from bugsigdb_curation.taxonomy.paths import default_dumps_dir, resolve_db_path

taxonomy_app = typer.Typer(help="Build and query a local, offline NCBI taxonomy DB (DuckDB-backed).")


@taxonomy_app.command("build")
def build_command(
    taxdump: Path | None = typer.Option(
        None,
        "--taxdump",
        help=(
            "Local taxdump: a directory containing names.dmp+nodes.dmp, or a "
            ".tar.gz/.tgz/.zip archive containing them. Required unless --download is set."
        ),
    ),
    download: bool = typer.Option(
        False,
        "--download",
        help="Download the taxdump for --release from NCBI first (requires network egress).",
    ),
    release: str = typer.Option(
        ..., "--release", help="Release label recorded in meta and used to key the default cache paths."
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Output .duckdb path (default: CLI > BUGSIGDB_TAXONOMY_DB > XDG cache under BUGSIGDB_CACHE_DIR).",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Provenance note recorded in meta (default: the resolved taxdump path, or the NCBI download URL).",
    ),
) -> None:
    """Build a `.duckdb` taxonomy DB from a local (or, with --download, freshly-fetched) NCBI taxdump."""
    console = Console()
    error_console = Console(stderr=True)

    if not download and taxdump is None:
        error_console.print("[red]Error:[/red] --taxdump is required unless --download is set.")
        raise typer.Exit(code=2)

    out_path = resolve_db_path(out, release)
    build_timestamp = datetime.now(timezone.utc).isoformat()

    try:
        if download:
            dumps_dir = default_dumps_dir(release)
            archive_path = dumps_dir / "taxdmp.zip"
            try:
                download_taxdump(archive_path, release=release)
            except httpx.HTTPError as exc:
                error_console.print(f"[red]Error:[/red] download failed: {exc}")
                raise typer.Exit(code=1) from None
            stats = build_taxonomy_db(
                archive_path,
                out_path,
                release=release,
                source=source or NCBI_TAXDUMP_ARCHIVE_URL_TEMPLATE.format(release=release),
                build_timestamp=build_timestamp,
                extract_dir=dumps_dir / "extracted",
            )
        else:
            assert taxdump is not None  # guaranteed by the check above
            if not taxdump.exists():
                error_console.print(f"[red]Error:[/red] {taxdump} does not exist.")
                raise typer.Exit(code=1)
            extract_dir = None if taxdump.is_dir() else default_dumps_dir(release) / "extracted"
            stats = build_taxonomy_db(
                taxdump,
                out_path,
                release=release,
                source=source or str(taxdump),
                build_timestamp=build_timestamp,
                extract_dir=extract_dir,
            )
    except (FileNotFoundError, ValueError, duckdb.Error) as exc:
        # `_build_from_files` (see build.py) already guarantees a failed
        # build never touches `out_path` or leaves a `.tmp-*` file behind --
        # this is purely about turning a `duckdb.Error` (e.g. a PRIMARY KEY
        # violation from a malformed .dmp) into a clean message instead of a
        # bare traceback.
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    console.print(f"[green]Built taxonomy DB:[/green] {stats.out_path}")
    table = Table(title="Build provenance")
    table.add_column("field")
    table.add_column("value")
    table.add_row("release", stats.release)
    table.add_row("source", stats.source)
    table.add_row("build_timestamp", stats.build_timestamp)
    table.add_row("names rows", str(stats.names_rows))
    table.add_row("nodes rows", str(stats.nodes_rows))
    table.add_row("merged rows", str(stats.merged_rows))
    table.add_row("names.dmp sha256", stats.names_dmp_sha256)
    table.add_row("nodes.dmp sha256", stats.nodes_dmp_sha256)
    console.print(table)


@taxonomy_app.command("lookup")
def lookup_command(
    name: str | None = typer.Argument(
        None, help="Taxon name to resolve. Mutually exclusive with --taxid."
    ),
    taxid: int | None = typer.Option(
        None, "--taxid", help="Reverse lookup: print this tax_id's scientific name + lineage instead of resolving NAME."
    ),
    db: Path | None = typer.Option(
        None,
        "--db",
        help="Path to a built .duckdb (default: CLI > BUGSIGDB_TAXONOMY_DB > BUGSIGDB_CACHE_DIR/--release default).",
    ),
    release: str | None = typer.Option(
        None,
        "--release",
        help="Release label used to find the default cached DB (ignored once --db/BUGSIGDB_TAXONOMY_DB apply).",
    ),
) -> None:
    """Resolve a taxon NAME to its NCBI tax_id (or, with --taxid, print an id's lineage)."""
    console = Console()
    error_console = Console(stderr=True)

    if bool(name) == (taxid is not None):
        error_console.print("[red]Error:[/red] pass exactly one of NAME or --taxid.")
        raise typer.Exit(code=2)

    try:
        db_path = resolve_db_path(db, release)
    except (FileNotFoundError, ValueError) as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=2) from None

    if not db_path.exists():
        error_console.print(f"[red]Error:[/red] taxonomy DB not found: {db_path}")
        raise typer.Exit(code=1)

    with TaxonomyDB(db_path) as tdb:
        if taxid is not None:
            _print_taxid_lookup(tdb, taxid, console, error_console)
        else:
            assert name is not None  # guaranteed by the exactly-one-of check above
            _print_name_lookup(tdb, name, console, error_console)


def _print_name_lookup(tdb: TaxonomyDB, name: str, console: Console, error_console: Console) -> None:
    resolution = tdb.resolve(name)
    if resolution is None:
        error_console.print(f"[yellow]No match for {name!r}.[/yellow]")
        raise typer.Exit(code=1)

    console.print(f"tax_id: [bold]{resolution.tax_id}[/bold]")
    console.print(f"matched name: {resolution.matched_name_txt}  (name_class: {resolution.name_class})")
    console.print(f"rank: {resolution.rank or '[dim]unknown[/dim]'}")
    if resolution.ambiguous:
        candidates = ", ".join(str(c) for c in resolution.candidates)
        console.print(
            f"[yellow]ambiguous:[/yellow] {len(resolution.candidates)} distinct tax_ids matched "
            f"({candidates}); the scientific-name-preferred pick above was chosen deterministically."
        )


def _print_taxid_lookup(tdb: TaxonomyDB, taxid: int, console: Console, error_console: Console) -> None:
    rank = tdb.rank(taxid)
    if rank is None:
        error_console.print(f"[yellow]Unknown tax_id {taxid}.[/yellow]")
        raise typer.Exit(code=1)

    name = tdb.scientific_name(taxid)
    console.print(f"tax_id: [bold]{taxid}[/bold]")
    console.print(f"scientific name: {name or '[dim]unknown[/dim]'}")
    console.print(f"rank: {rank}")

    table = Table(title="Lineage (root -> taxon)")
    table.add_column("tax_id")
    table.add_column("rank")
    table.add_column("name")
    for t, r, n in tdb.lineage(taxid):
        table.add_row(str(t), r or "", n or "")
    console.print(table)
