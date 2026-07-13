"""End-to-end tests for the `bugsigdb export` CLI command, HTTP fully mocked."""

from __future__ import annotations

from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from bugsigdb_curation.cli import app

runner = CliRunner()

SAMPLE_TREE = {
    "tree": [
        {"path": "README.md", "type": "blob", "size": 500},
        {"path": "full_dump.csv", "type": "blob", "size": 11},
        {"path": "file_size.csv", "type": "blob", "size": 5},
        {"path": "bugsigdb_signatures_genus_ncbi.gmt", "type": "blob", "size": 7},
    ],
}


def _mock_tree(httpx_mock: HTTPXMock, ref: str = "devel", tree: dict | None = None) -> None:
    httpx_mock.add_response(
        url=f"https://api.github.com/repos/waldronlab/bugsigdbexports/git/trees/{ref}",
        json=tree if tree is not None else SAMPLE_TREE,
    )


def test_list_prints_files_and_downloads_nothing(tmp_path, httpx_mock: HTTPXMock):
    _mock_tree(httpx_mock)
    output_dir = tmp_path / "exports"

    result = runner.invoke(app, ["export", "--list", "--output-dir", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert "full_dump.csv" in result.output
    assert "file_size.csv" in result.output
    assert not output_dir.exists()


def test_export_default_select_downloads_dump_group(tmp_path, httpx_mock: HTTPXMock):
    _mock_tree(httpx_mock)
    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/full_dump.csv",
        content=b"hello world",
    )
    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/file_size.csv",
        content=b"abcde",
    )
    output_dir = tmp_path / "exports"

    result = runner.invoke(app, ["export", "--output-dir", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert (output_dir / "full_dump.csv").read_bytes() == b"hello world"
    assert (output_dir / "file_size.csv").read_bytes() == b"abcde"
    assert not (output_dir / "bugsigdb_signatures_genus_ncbi.gmt").exists()


def test_export_select_gmt_downloads_only_gmt(tmp_path, httpx_mock: HTTPXMock):
    _mock_tree(httpx_mock)
    httpx_mock.add_response(
        url=(
            "https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/"
            "bugsigdb_signatures_genus_ncbi.gmt"
        ),
        content=b"gmt-content",
    )
    output_dir = tmp_path / "exports"

    result = runner.invoke(app, ["export", "--select", "gmt", "--output-dir", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert (output_dir / "bugsigdb_signatures_genus_ncbi.gmt").read_bytes() == b"gmt-content"
    assert not (output_dir / "full_dump.csv").exists()


def test_export_select_all_downloads_everything(tmp_path, httpx_mock: HTTPXMock):
    _mock_tree(httpx_mock)
    for name, content in (
        ("full_dump.csv", b"hello world"),
        ("file_size.csv", b"abcde"),
        ("bugsigdb_signatures_genus_ncbi.gmt", b"gmt-content"),
    ):
        httpx_mock.add_response(
            url=f"https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/{name}",
            content=content,
        )
    output_dir = tmp_path / "exports"

    result = runner.invoke(app, ["export", "--select", "all", "--output-dir", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert (output_dir / "full_dump.csv").exists()
    assert (output_dir / "file_size.csv").exists()
    assert (output_dir / "bugsigdb_signatures_genus_ncbi.gmt").exists()


def test_export_skips_existing_matching_file(tmp_path, httpx_mock: HTTPXMock):
    _mock_tree(httpx_mock)
    output_dir = tmp_path / "exports"
    output_dir.mkdir(parents=True)
    (output_dir / "full_dump.csv").write_bytes(b"hello world")  # 11 bytes, matches mocked size
    (output_dir / "file_size.csv").write_bytes(b"wrong")  # 5 bytes, matches mocked size

    # No raw-file mocks registered: if the CLI tried to download either file (instead of
    # skipping), pytest-httpx would raise for an unmatched request.
    result = runner.invoke(app, ["export", "--output-dir", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert (output_dir / "full_dump.csv").read_bytes() == b"hello world"
    assert (output_dir / "file_size.csv").read_bytes() == b"wrong"


def test_export_force_redownloads_existing_file(tmp_path, httpx_mock: HTTPXMock):
    _mock_tree(httpx_mock)
    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/full_dump.csv",
        content=b"HELLO WORLD",
    )
    httpx_mock.add_response(
        url="https://raw.githubusercontent.com/waldronlab/bugsigdbexports/devel/file_size.csv",
        content=b"ABCDE",
    )
    output_dir = tmp_path / "exports"
    output_dir.mkdir(parents=True)
    (output_dir / "full_dump.csv").write_bytes(b"hello world")  # same size as mock => would skip
    (output_dir / "file_size.csv").write_bytes(b"abcde")

    result = runner.invoke(app, ["export", "--force", "--output-dir", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert (output_dir / "full_dump.csv").read_bytes() == b"HELLO WORLD"
    assert (output_dir / "file_size.csv").read_bytes() == b"ABCDE"


def test_export_bad_ref_returns_error(tmp_path, httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="https://api.github.com/repos/waldronlab/bugsigdbexports/git/trees/bogus-ref",
        status_code=404,
    )
    output_dir = tmp_path / "exports"

    result = runner.invoke(app, ["export", "--ref", "bogus-ref", "--output-dir", str(output_dir)])

    assert result.exit_code != 0
    assert "bogus-ref" in result.output
    assert not output_dir.exists()
