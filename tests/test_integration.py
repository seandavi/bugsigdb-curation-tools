"""Opt-in integration test that hits the real waldronlab/bugsigdbexports repo on GitHub.

Deselected by default (see `addopts = "-m 'not network'"` in pyproject.toml).
Run explicitly with:

    uv run pytest -m network tests/test_integration.py
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import httpx
import pytest

from bugsigdb_curation.export import build_raw_url, download_file, fetch_export_files, filter_files
from bugsigdb_curation.loader import load_studies, read_rows, summarize


@pytest.mark.network
def test_real_download_of_file_size_csv(tmp_path: Path):
    async def run() -> int:
        async with httpx.AsyncClient(timeout=30.0) as client:
            files = await fetch_export_files(client, "devel")
            dump_files = filter_files(files, "dump")
            target = next(f for f in dump_files if f.name == "file_size.csv")
            url = build_raw_url("devel", target.path)
            return await download_file(client, url, tmp_path / "file_size.csv")

    written = asyncio.run(run())
    dest = tmp_path / "file_size.csv"
    assert dest.exists()
    assert written > 0
    assert dest.stat().st_size == written


@pytest.mark.network
def test_real_full_dump_parses_without_error(tmp_path: Path):
    """Download the real full_dump.csv and parse its first ~200 data rows.

    Only a sanity check that the loader survives real-world data (encoding,
    stray delimiters, unexpected enum values, etc.) without raising — it does
    not assert exact counts since the live dump changes over time.
    """

    async def run() -> Path:
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = await fetch_export_files(client, "devel")
            dump_files = filter_files(files, "dump")
            target = next(f for f in dump_files if f.name == "full_dump.csv")
            url = build_raw_url("devel", target.path)
            dest = tmp_path / "full_dump.csv"
            await download_file(client, url, dest)
            return dest

    dest = asyncio.run(run())

    first_200_rows = list(itertools.islice(read_rows(dest), 200))
    assert len(first_200_rows) > 0

    studies = load_studies(first_200_rows)
    n_studies, n_experiments, n_signatures = summarize(studies)

    assert n_studies > 0
    assert n_experiments > 0
    assert n_signatures > 0
