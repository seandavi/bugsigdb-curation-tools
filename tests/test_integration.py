"""Opt-in integration test that hits the real waldronlab/bugsigdbexports repo on GitHub.

Deselected by default (see `addopts = "-m 'not network'"` in pyproject.toml).
Run explicitly with:

    uv run pytest -m network tests/test_integration.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from bugsigdb_curation.export import build_raw_url, download_file, fetch_export_files, filter_files


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
