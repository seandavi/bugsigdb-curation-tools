"""Unit tests for bugsigdb_curation.export — the pure/testable download logic."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.export import (
    ExportError,
    ExportFile,
    build_raw_url,
    classify_root_path,
    download_export_files,
    download_file,
    fetch_export_files,
    filter_files,
    human_size,
    parse_tree,
    should_download,
)

SAMPLE_TREE = {
    "sha": "abc123",
    "tree": [
        {"path": "README.md", "type": "blob", "size": 500},
        {"path": "RELEASE_PROCESS.md", "type": "blob", "size": 200},
        {"path": ".zenodo.json", "type": "blob", "size": 100},
        {"path": ".github", "type": "tree"},
        {"path": "inst", "type": "tree"},
        {"path": "full_dump.csv", "type": "blob", "size": 30_000_000},
        {"path": "file_size.csv", "type": "blob", "size": 65_000},
        {"path": "bugsigdb_signatures_genus_ncbi.gmt", "type": "blob", "size": 12_345},
        {"path": "bugsigdb_signatures_species_metaphlan_exact.gmt", "type": "blob", "size": 6789},
    ],
}


def test_parse_tree_classifies_and_filters_root_files():
    files = parse_tree(SAMPLE_TREE)
    names = {f.name for f in files}
    assert names == {
        "full_dump.csv",
        "file_size.csv",
        "bugsigdb_signatures_genus_ncbi.gmt",
        "bugsigdb_signatures_species_metaphlan_exact.gmt",
    }
    groups = {f.name: f.group for f in files}
    assert groups["full_dump.csv"] == "dump"
    assert groups["file_size.csv"] == "dump"
    assert groups["bugsigdb_signatures_genus_ncbi.gmt"] == "gmt"
    assert groups["bugsigdb_signatures_species_metaphlan_exact.gmt"] == "gmt"


def test_parse_tree_ignores_non_root_and_non_blob_entries():
    files = parse_tree(
        {
            "tree": [
                {"path": "sub/full_dump.csv", "type": "blob", "size": 10},
                {"path": "inst", "type": "tree"},
            ]
        }
    )
    assert files == []


@pytest.mark.parametrize(
    "path,expected",
    [
        ("full_dump.csv", "dump"),
        ("file_size.csv", "dump"),
        ("bugsigdb_signatures_mixed_taxname.gmt", "gmt"),
        ("bugsigdb_signatures_genus_ncbi_exact.gmt", "gmt"),
        ("README.md", None),
        (".zenodo.json", None),
        ("RELEASE_PROCESS.md", None),
    ],
)
def test_classify_root_path(path, expected):
    assert classify_root_path(path) == expected


def _make_files():
    return [
        ExportFile(name="full_dump.csv", path="full_dump.csv", size=100, group="dump"),
        ExportFile(name="file_size.csv", path="file_size.csv", size=10, group="dump"),
        ExportFile(name="a.gmt", path="a.gmt", size=5, group="gmt"),
    ]


def test_filter_files_dump():
    result = filter_files(_make_files(), "dump")
    assert {f.name for f in result} == {"full_dump.csv", "file_size.csv"}


def test_filter_files_gmt():
    result = filter_files(_make_files(), "gmt")
    assert {f.name for f in result} == {"a.gmt"}


def test_filter_files_all():
    result = filter_files(_make_files(), "all")
    assert len(result) == 3


def test_build_raw_url():
    url = build_raw_url("devel", "full_dump.csv")
    assert url == "https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/full_dump.csv"


def test_build_raw_url_custom_ref():
    url = build_raw_url("v1.2.3", "file_size.csv")
    assert url == "https://raw.githubusercontent.com/waldronlab/bugsigdbexports/v1.2.3/file_size.csv"


def test_should_download_missing_file(tmp_path):
    dest = tmp_path / "missing.csv"
    assert should_download(dest, remote_size=100, force=False) is True


def test_should_download_matching_size_skips(tmp_path):
    dest = tmp_path / "file.csv"
    dest.write_bytes(b"x" * 100)
    assert should_download(dest, remote_size=100, force=False) is False


def test_should_download_mismatched_size_downloads(tmp_path):
    dest = tmp_path / "file.csv"
    dest.write_bytes(b"x" * 50)
    assert should_download(dest, remote_size=100, force=False) is True


def test_should_download_force_always_true(tmp_path):
    dest = tmp_path / "file.csv"
    dest.write_bytes(b"x" * 100)
    assert should_download(dest, remote_size=100, force=True) is True


def test_fetch_export_files_parses_response(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/waldronlab/bugsigdbexports/git/trees/devel",
        json=SAMPLE_TREE,
    )

    async def run() -> list[ExportFile]:
        async with httpx.AsyncClient() as client:
            return await fetch_export_files(client, "devel")

    files = asyncio.run(run())
    assert any(f.name == "full_dump.csv" for f in files)


def test_fetch_export_files_raises_friendly_error_on_404(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/waldronlab/bugsigdbexports/git/trees/nonexistent-ref",
        status_code=404,
    )

    async def run() -> list[ExportFile]:
        async with httpx.AsyncClient() as client:
            return await fetch_export_files(client, "nonexistent-ref")

    with pytest.raises(ExportError, match="nonexistent-ref"):
        asyncio.run(run())


def test_download_file_streams_to_disk(tmp_path, httpx_mock: HTTPXMock):
    content = b"a,b,c\n1,2,3\n" * 1000
    url = "https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/file_size.csv"
    httpx_mock.add_response(url=url, content=content)
    dest = tmp_path / "file_size.csv"

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await download_file(client, url, dest)

    written = asyncio.run(run())
    assert written == len(content)
    assert dest.read_bytes() == content
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_file_raises_friendly_error_on_404(tmp_path, httpx_mock: HTTPXMock):
    url = "https://raw.githubusercontent.com/waldronlab/bugsigdbexports/bogus/full_dump.csv"
    httpx_mock.add_response(url=url, status_code=404)
    dest = tmp_path / "full_dump.csv"

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await download_file(client, url, dest)

    with pytest.raises(ExportError):
        asyncio.run(run())


def test_download_export_files_skips_matching_and_downloads_rest(tmp_path, httpx_mock: HTTPXMock):
    files = [
        ExportFile(name="file_size.csv", path="file_size.csv", size=5, group="dump"),
        ExportFile(name="full_dump.csv", path="full_dump.csv", size=11, group="dump"),
    ]
    existing = tmp_path / "file_size.csv"
    existing.write_bytes(b"x" * 5)  # matches size -> should be skipped, no HTTP call made

    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/full_dump.csv",
        content=b"hello world",
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await download_export_files(
                files, ref="devel", output_dir=tmp_path, force=False, client=client
            )

    results = asyncio.run(run())
    statuses = {r.file.name: r.status for r in results}
    assert statuses == {"file_size.csv": "skipped", "full_dump.csv": "downloaded"}
    assert (tmp_path / "full_dump.csv").read_bytes() == b"hello world"


def test_download_export_files_force_redownloads(tmp_path, httpx_mock: HTTPXMock):
    files = [ExportFile(name="file_size.csv", path="file_size.csv", size=5, group="dump")]
    existing = tmp_path / "file_size.csv"
    existing.write_bytes(b"x" * 5)  # matches size, but --force should re-download anyway

    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/file_size.csv",
        content=b"fresh",
    )

    async def run():
        async with httpx.AsyncClient() as client:
            return await download_export_files(
                files, ref="devel", output_dir=tmp_path, force=True, client=client
            )

    results = asyncio.run(run())
    assert results[0].status == "downloaded"
    assert (tmp_path / "file_size.csv").read_bytes() == b"fresh"


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (500, "500 B"),
        (2048, "2.0 KB"),
        (30_000_000, "28.6 MB"),
    ],
)
def test_human_size(num_bytes, expected):
    assert human_size(num_bytes) == expected
