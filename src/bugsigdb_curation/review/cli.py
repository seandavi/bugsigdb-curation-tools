"""`bugsigdb review` -- build review packets, ingest reviewers' verdicts, report on them.

A Typer sub-app wired into the top-level CLI (`bugsigdb_curation.cli`). Thin argument handling and
rich output only; the logic lives in `packet.py` and `verdicts.py`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date as date_type
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from bugsigdb_curation.pmc_map import PmcMapError
from bugsigdb_curation.review.bundle import BundleError, build_bundle, write_bundle_tree, zip_bytes
from bugsigdb_curation.review.packet import (
    PacketEvidence,
    build_manifest,
    build_packet,
    builder_git_commit,
    fetch_packet_evidence,
    load_evidence,
    make_meta,
    save_evidence,
    study_pmid,
)
from bugsigdb_curation.review.verdicts import (
    DEFAULT_REVIEWS_DIR,
    ingest_verdict_files,
    load_reviews,
    render_report,
    tally_verdicts,
)
from bugsigdb_curation.validate import ValidationInputError, load_instances

review_app = typer.Typer(help="Share drafts with human reviewers as static HTML packets and collect their verdicts.")

console = Console()
error_console = Console(stderr=True)


def _load_json_object(path: Path, what: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        error_console.print(f"[red]Error:[/red] cannot read {what} {path}: {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    if not isinstance(data, dict):
        error_console.print(f"[red]Error:[/red] {what} {path} must hold a JSON object")
        raise typer.Exit(code=1)
    return data


async def _fetch(record: dict[str, Any], pmcid: str | None) -> PacketEvidence:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        return await fetch_packet_evidence(record, client=client, pmcid=pmcid)


@review_app.command("packet")
def packet_command(
    pred: Path = typer.Option(..., "--pred", help="A draft written by `bugsigdb curate` (one study, JSON or YAML)."),
    annotations: Path | None = typer.Option(
        None,
        "--annotations",
        help="The draft's sidecar (default: `<pred stem>.annotations.json` beside --pred, if it exists).",
    ),
    out: Path = typer.Option(..., "--out", help="Output directory; writes `<pmid>.html` and `<pmid>.manifest.json`."),
    offline: bool = typer.Option(
        False, "--offline", help="Do not fetch evidence (legends/figures/tables) from the network."
    ),
    evidence_dir: Path | None = typer.Option(
        None,
        "--evidence-dir",
        help="Evidence cache: reused when it holds this study's evidence, written after a fetch otherwise.",
    ),
    refresh_evidence: bool = typer.Option(
        False, "--refresh-evidence", help="Ignore an existing --evidence-dir cache: fetch again and replace it."
    ),
    pmcid: str | None = typer.Option(None, "--pmcid", help="PMCID of the paper (default: resolved from the PMID)."),
    model_label: str | None = typer.Option(
        None, "--model-label", help="Model that produced the draft, shown in the banner."
    ),
    design_label: str | None = typer.Option(None, "--design-label", help="Curator design that produced the draft."),
) -> None:
    """Write a self-contained HTML review packet (+ manifest) for one machine-generated draft."""
    try:
        instances = [i for i in load_instances(pred) if isinstance(i, dict)]
    except ValidationInputError as exc:
        error_console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    if len(instances) != 1:
        error_console.print(f"[red]Error:[/red] --pred must hold exactly one study, found {len(instances)}")
        raise typer.Exit(code=1)
    record = instances[0]

    sidecar = annotations if annotations is not None else pred.with_suffix(".annotations.json")
    if annotations is not None or sidecar.is_file():
        notes = _load_json_object(sidecar, "annotations")
    else:
        notes = {}

    if refresh_evidence and offline:
        error_console.print("[red]Error:[/red] --refresh-evidence cannot be used with --offline")
        raise typer.Exit(code=1)

    pmid = study_pmid(record)
    evidence: PacketEvidence | None = None
    if evidence_dir is not None and not refresh_evidence:
        evidence = load_evidence(evidence_dir / pmid)
        if evidence is not None:
            console.print(f"Using cached evidence from {evidence_dir / pmid}")
    if evidence is None and not offline:
        try:
            evidence = asyncio.run(_fetch(record, pmcid))
        except (httpx.HTTPError, PmcMapError) as exc:
            error_console.print(
                f"[yellow]Warning:[/yellow] could not fetch evidence ({escape(str(exc))}); building without it"
            )
        else:
            if evidence.degraded:
                error_console.print(
                    "[yellow]Warning:[/yellow] evidence is incomplete: "
                    + escape("; ".join(evidence.problems))
                    + ". Images, tables or the licence may be missing from this packet."
                    + (" Not cached; rerun to retry." if evidence_dir is not None else "")
                )
            elif evidence_dir is not None:
                save_evidence(evidence, evidence_dir / pmid)
    if evidence is None:
        console.print("[yellow]Built without evidence[/yellow]: reviewers will see links to the paper only.")

    try:
        meta = make_meta(
            record,
            model_label=model_label,
            design_label=design_label,
            pmcid=(evidence.pmcid if evidence and evidence.pmcid else None) or pmcid,
            builder_commit=builder_git_commit(),
        )
        page = build_packet(record, notes, evidence, meta)
    except ValueError as exc:
        error_console.print(f"[red]Error:[/red] cannot build a packet from {pred}: {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{meta.pmid}.html"
    manifest_path = out / f"{meta.pmid}.manifest.json"
    html_path.write_text(page, encoding="utf-8")
    manifest_path.write_text(
        json.dumps(build_manifest(record, notes, evidence, meta), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    console.print(f"[green]Wrote[/green] {html_path} and {manifest_path} (packet {meta.packet_id})")


@review_app.command("bundle")
def bundle_command(
    packets: Path = typer.Option(
        ..., "--packets", help="Directory of packets: `<pmid>.html` + `<pmid>.manifest.json`."
    ),
    out: Path = typer.Option(..., "--out", help="Output directory; gets `<name>/` and, with --zip, `<name>.zip`."),
    name: str | None = typer.Option(
        None, "--name", help="Bundle (and top folder) name; default bugsigdb-review-<date>."
    ),
    contact: str | None = typer.Option(
        None, "--contact", help='Who reviewers send their verdict files to, e.g. "Jane Doe <jane@example.org>".'
    ),
    zip_: bool = typer.Option(True, "--zip/--no-zip", help="Also write a reproducible `<name>.zip` of the bundle."),
    date: str | None = typer.Option(None, "--date", help="Build date, YYYY-MM-DD (default: today)."),
) -> None:
    """Combine a directory of review packets into ONE shareable bundle: index.html, packets, README, attribution."""
    built_on = date or date_type.today().isoformat()
    try:
        bundle = build_bundle(packets, name=name or f"bugsigdb-review-{built_on}", date=built_on, contact=contact)
        root = write_bundle_tree(bundle, out)
    except BundleError as exc:
        for problem in exc.problems:
            error_console.print(f"[red]Error:[/red] {escape(problem)}")
        raise typer.Exit(code=2) from None
    for warning in bundle.warnings:
        error_console.print(f"[yellow]Warning:[/yellow] {escape(warning)}")
    console.print(f"[green]Wrote[/green] {root} ({len(bundle.manifest['packets'])} packet(s))")
    if zip_:
        zip_path = out / f"{bundle.name}.zip"
        zip_path.write_bytes(zip_bytes(bundle))
        console.print(f"[green]Wrote[/green] {zip_path}")


@review_app.command("ingest")
def ingest_command(
    paths: list[Path] = typer.Argument(..., help="Verdict JSON file(s) exported by reviewers."),
    dest: Path = typer.Option(
        DEFAULT_REVIEWS_DIR, "--dest", help="Where validated verdicts are filed (`DEST/<pmid>/`)."
    ),
    manifests: Path | None = typer.Option(
        None, "--manifests", help="Directory of packet manifests (default: beside each verdict file)."
    ),
    force: bool = typer.Option(False, "--force", help="Ingest even when draft_sha256 does not match the manifest."),
) -> None:
    """Validate reviewers' verdict files and file them under DEST/<pmid>/."""
    results = ingest_verdict_files(paths, dest=dest, manifests_dir=manifests, force=force)
    for r in results:
        colour = {"ingested": "green", "duplicate": "yellow", "refused": "red", "invalid": "red", "conflict": "red"}[
            r.status
        ]
        console.print(f"[{colour}]{r.status}[/{colour}] {r.source}: {escape(r.message)}")
        for warning in r.warnings:
            console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")
    n_duplicate = sum(r.status == "duplicate" for r in results)
    n_bad = sum(r.status not in ("ingested", "duplicate") for r in results)
    already = f", {n_duplicate} already ingested" if n_duplicate else ""
    console.print(f"{len(results) - n_bad - n_duplicate} ingested{already}, {n_bad} not ingested")
    if n_bad:
        raise typer.Exit(code=1)


@review_app.command("report")
def report_command(
    reviews: Path = typer.Option(DEFAULT_REVIEWS_DIR, "--reviews", help="Directory of ingested verdicts."),
    out: Path = typer.Option(Path("report.md"), "--out", help="Markdown report to write."),
) -> None:
    """Aggregate ingested verdicts into a markdown report (per study and overall)."""
    if not reviews.is_dir():
        error_console.print(f"[red]Error:[/red] reviews directory not found: {reviews}")
        raise typer.Exit(code=1)
    verdicts, problems = load_reviews(reviews)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(tally_verdicts(verdicts), problems=problems), encoding="utf-8")
    console.print(f"[green]Wrote[/green] {out} ({len(verdicts)} verdict file(s), {len(problems)} skipped)")
