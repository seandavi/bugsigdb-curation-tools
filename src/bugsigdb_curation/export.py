"""Download logic for BugSigDB export artifacts.

Fetches the root-level file listing of the `waldronlab/bugsigdbexports` GitHub
repository via the git trees API, classifies files into groups (`dump` for the
merged CSV exports, `gmt` for per-level/per-idtype signature sets), and streams
selected files to disk.

This module is pure I/O + data transformation and has no CLI/UI concerns —
those live in :mod:`bugsigdb_curation.cli`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import quote

import httpx

REPO = "waldronlab/bugsigdbexports"
GITHUB_TREE_API = "https://api.github.com/repos/{repo}/git/trees/{ref}"
RAW_BASE_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"

#: The two root-level files that make up the "dump" group.
DUMP_FILENAMES = frozenset({"full_dump.csv", "file_size.csv"})

DEFAULT_CHUNK_SIZE = 1 << 16  # 64 KiB
DEFAULT_CONCURRENCY = 4

Group = Literal["dump", "gmt"]
Select = Literal["dump", "gmt", "all"]

#: Called as (file_name, bytes_downloaded_so_far, total_bytes) while streaming,
#: and once with (file_name, size, size) when a file is skipped.
ProgressHook = Callable[[str, int, int], None]


class ExportError(RuntimeError):
    """Raised for user-facing export failures (bad ref, missing file, etc.)."""


@dataclass(frozen=True, slots=True)
class ExportFile:
    """A single downloadable root-level file from the export repo."""

    name: str
    path: str
    size: int
    group: Group


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """The outcome of processing one :class:`ExportFile`."""

    file: ExportFile
    dest: Path
    status: Literal["downloaded", "skipped"]
    bytes_written: int


def classify_root_path(path: str) -> Group | None:
    """Classify a root-level repo path into an export group, or None to ignore it."""
    if path.endswith(".gmt"):
        return "gmt"
    if path in DUMP_FILENAMES:
        return "dump"
    return None


def parse_tree(tree_json: dict[str, Any]) -> list[ExportFile]:
    """Parse a GitHub `git/trees/<ref>` API response into root-level ExportFile records.

    Only top-level blobs (no `/` in their path) that classify into a known group
    are included; directories (e.g. `.github`, `inst`) and files like `README.md`
    are silently skipped.
    """
    files: list[ExportFile] = []
    for entry in tree_json.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        if not path or "/" in path:
            continue
        group = classify_root_path(path)
        if group is None:
            continue
        files.append(ExportFile(name=path, path=path, size=int(entry.get("size", 0)), group=group))
    return files


def filter_files(files: list[ExportFile], select: Select) -> list[ExportFile]:
    """Filter export files by the requested selection group ('dump', 'gmt', or 'all')."""
    if select == "all":
        return list(files)
    return [f for f in files if f.group == select]


def build_raw_url(ref: str, path: str) -> str:
    """Build the raw.githubusercontent.com download URL for a file path at a given ref.

    Note: unlike the git trees API, raw.githubusercontent.com places `ref` directly
    in the URL path with no way to disambiguate it from the following `path`
    segments. A ref containing `/` (e.g. a branch named `release/1.0`) is
    inherently ambiguous here and is not handled specially — this only matters
    for `--select` values that need per-file raw URLs, not the tree listing.
    """
    return RAW_BASE_URL.format(repo=REPO, ref=ref, path=path)


def should_download(dest: Path, remote_size: int, force: bool) -> bool:
    """Decide whether a file needs (re)downloading.

    Downloads when forced, when the destination doesn't exist yet, or when its
    size doesn't match the remote size. Skips only when a same-sized file is
    already present and `force` is False.
    """
    if force:
        return True
    if not dest.exists():
        return True
    return dest.stat().st_size != remote_size


async def fetch_export_files(client: httpx.AsyncClient, ref: str) -> list[ExportFile]:
    """Fetch and parse the root-level export file listing for a given git ref.

    Raises :class:`ExportError` with a friendly message if `ref` doesn't exist or
    the API response isn't the JSON we expect.
    """
    # The trees API treats `ref` as a single path segment, so a ref containing
    # `/` (e.g. `release/1.0`) must be percent-encoded or it splits into extra
    # path segments and 404s misleadingly.
    url = GITHUB_TREE_API.format(repo=REPO, ref=quote(ref, safe=""))
    response = await client.get(url)
    if response.status_code == 404:
        raise ExportError(f"Ref {ref!r} was not found in {REPO} (HTTP 404). Check the --ref value.")
    response.raise_for_status()
    try:
        tree_json = response.json()
    except ValueError as exc:
        raise ExportError(f"Unexpected (non-JSON) response from the GitHub trees API: {exc}") from exc
    return parse_tree(tree_json)


async def download_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress_hook: ProgressHook | None = None,
    name: str = "",
) -> int:
    """Stream `url` to `dest` in chunks (never loading the whole body into memory).

    Writes to a temporary `<dest>.part` file and atomically renames it on
    success, so a failed/interrupted download never leaves a corrupt file at
    `dest`. Returns the number of bytes written.

    All blocking filesystem calls (directory creation, file open/write, and the
    final atomic rename) are offloaded to a worker thread via `asyncio.to_thread`
    so they don't block the event loop and starve other concurrent downloads.
    """
    await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
    tmp_dest = dest.with_name(dest.name + ".part")
    bytes_written = 0
    display_name = name or dest.name
    async with client.stream("GET", url) as response:
        if response.status_code == 404:
            raise ExportError(f"File not found at {url} (HTTP 404). Check the --ref value.")
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        try:
            fh = await asyncio.to_thread(tmp_dest.open, "wb")
            try:
                async for chunk in response.aiter_bytes(chunk_size):
                    await asyncio.to_thread(fh.write, chunk)
                    bytes_written += len(chunk)
                    if progress_hook is not None:
                        progress_hook(display_name, bytes_written, total or bytes_written)
            finally:
                await asyncio.to_thread(fh.close)
        except BaseException:
            await asyncio.to_thread(tmp_dest.unlink, True)
            raise
    await asyncio.to_thread(tmp_dest.replace, dest)
    return bytes_written


async def download_export_files(
    files: list[ExportFile],
    *,
    ref: str,
    output_dir: Path,
    force: bool,
    client: httpx.AsyncClient,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress_hook: ProgressHook | None = None,
) -> list[DownloadResult]:
    """Download (or skip) each file in `files`, bounded by a concurrency semaphore.

    Files whose destination already exists with a matching size are skipped
    (unless `force` is set) — this is decided per-file via `should_download`.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(file: ExportFile) -> DownloadResult:
        dest = output_dir / file.name
        if not should_download(dest, file.size, force):
            if progress_hook is not None:
                progress_hook(file.name, file.size, file.size)
            return DownloadResult(
                file=file, dest=dest, status="skipped", bytes_written=dest.stat().st_size
            )
        async with semaphore:
            url = build_raw_url(ref, file.path)
            written = await download_file(client, url, dest, progress_hook=progress_hook, name=file.name)
        return DownloadResult(file=file, dest=dest, status="downloaded", bytes_written=written)

    return list(await asyncio.gather(*(_one(f) for f in files)))


def human_size(num_bytes: int) -> str:
    """Format a byte count as a short human-readable string (e.g. '30.1 MB')."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
