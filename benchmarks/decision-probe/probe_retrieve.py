"""Fetch + cache the probe's inputs: evidence bundles, figure images, supplements.

Everything lands under ``data/decision-probe/<pmid>/`` (gitignored). Idempotent:
re-running skips what is already on disk. Scorer-side tooling -- may sit next
to gold, but it only *retrieves paper content*; it reads no gold itself.

    uv run python benchmarks/decision-probe/probe_retrieve.py            # all 19 smoke PMIDs
    uv run python benchmarks/decision-probe/probe_retrieve.py 34620922   # one PMID
"""

from __future__ import annotations

import asyncio
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
from loguru import logger

from bugsigdb_curation.curator.evidence import assemble_evidence, fetch_figure_image
from bugsigdb_curation.curator.smoke import smoke_study_ids
from bugsigdb_curation.retrieval import fetch_fulltext_xml
from bugsigdb_curation.supplements import fetch_supplements, parse_supplement_refs

REPO = Path(__file__).resolve().parents[2]
CACHE = REPO / "data" / "decision-probe"
PMC_MAP = REPO / "data" / "eval" / "pmid_pmcid_map.csv"
#: The two big supplement-heavy papers (issue #19 inputs).
SUPPLEMENT_PMIDS = ("34620922", "37864204")


def pmcid_for(pmid: str) -> str:
    with PMC_MAP.open() as fh:
        for row in csv.DictReader(fh):
            if row["pmid"] == pmid and row["pmcid"]:
                return row["pmcid"]
    raise KeyError(f"no PMCID for {pmid}")


def study_dir(pmid: str) -> Path:
    return CACHE / pmid


def load_bundle(pmid: str) -> dict:
    """The cached bundle as plain JSON (metadata, tables, figures, supplement refs)."""
    return json.loads((study_dir(pmid) / "bundle.json").read_text())


def figure_path(pmid: str, figure_id: str) -> Path | None:
    hits = sorted((study_dir(pmid) / "figures").glob(f"{figure_id}.*"))
    return hits[0] if hits else None


async def retrieve_study(pmid: str, client: httpx.AsyncClient) -> None:
    pmcid = pmcid_for(pmid)
    out = study_dir(pmid)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    bundle_file = out / "bundle.json"
    if not bundle_file.exists():
        bundle = await assemble_evidence(pmid, pmcid, client=client)
        for attempt in range(4):  # EuropePMC fullTextXML 500s transiently
            try:
                xml = await fetch_fulltext_xml(client, pmcid)
                break
            except httpx.HTTPStatusError:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
        payload = {
            "pmid": pmid,
            "pmcid": pmcid,
            "title": bundle.metadata.title if hasattr(bundle.metadata, "title") else None,
            "abstract": getattr(bundle.metadata, "abstract", None),
            "tables": [asdict(t) for t in bundle.tables],
            "figures": [asdict(f) for f in bundle.figures],
            "supplement_refs": [asdict(r) for r in parse_supplement_refs(xml)],
        }
        bundle_file.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
        (out / "fulltext.xml").write_text(xml)
        figures = bundle.figures
    else:
        figures = ()
        payload = json.loads(bundle_file.read_text())
    for fig in figures:
        if figure_path(pmid, fig.figure_id) or not fig.blob_url:
            continue
        data = await fetch_figure_image(fig, client=client)
        if data:
            suffix = Path(fig.graphic_filename or "x.jpg").suffix or ".jpg"
            (out / "figures" / f"{fig.figure_id}{suffix}").write_bytes(data)
    if pmid in SUPPLEMENT_PMIDS and not any((out / "supplements").glob("*")):
        files = await fetch_supplements(pmcid, client=client)
        (out / "supplements").mkdir(exist_ok=True)
        for f in files:
            (out / "supplements" / Path(f.filename).name).write_bytes(f.raw_bytes)
        logger.info("{} supplements for {}", len(files), pmid)


async def main(pmids: list[str]) -> None:
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for pmid in pmids:
            try:
                await retrieve_study(pmid, client)
                logger.info("ok {}", pmid)
            except Exception as exc:  # keep going; report at the end
                logger.error("FAILED {}: {!r}", pmid, exc)
            await asyncio.sleep(0.4)




# --- figure-extraction benchmark images (P2 figure type / P3 orientation) -----------------

MANIFEST = REPO / "benchmarks" / "figure-extraction" / "manifest.json"
FIGBENCH_DIR = CACHE / "figbench"


def figbench_image_path(entry: dict) -> Path:
    return FIGBENCH_DIR / f"{entry['pmid']}{Path(entry['figure_filename']).suffix}"


async def retrieve_figbench() -> None:
    """Download the 15 benchmark figures from their manifest blob URLs."""
    from bugsigdb_curation.retrieval import fetch_image_bytes

    FIGBENCH_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        for entry in json.loads(MANIFEST.read_text()):
            path = figbench_image_path(entry)
            if path.exists():
                continue
            path.write_bytes(await fetch_image_bytes(client, entry["blob_url"]))
            logger.info("figbench {} -> {}", entry["pmid"], path.name)
            await asyncio.sleep(0.4)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["figbench"]:
        asyncio.run(retrieve_figbench())
    else:
        asyncio.run(main(args or smoke_study_ids()))
