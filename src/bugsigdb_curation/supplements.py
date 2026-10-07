"""Supplement identification + fetch + parse (standalone -- not wired into the curator).

For a PMCID, a paper's supplementary files are:

1. **Named** in the EuropePMC ``fullTextXML`` (the same document
   `bugsigdb_curation.retrieval.fetch_fulltext_xml` already fetches), as
   ``<supplementary-material>`` elements, e.g.::

       <supplementary-material id="MOESM1" xmlns:xlink="http://www.w3.org/1999/xlink">
         <label>Supplementary Material 1</label>
         <caption><p>Uncropped gels.</p></caption>
         <media xlink:href="41598_2021_99379_MOESM1_ESM.pdf" mimetype="application" mime-subtype="pdf"/>
       </supplementary-material>

   (:func:`parse_supplement_refs`).
2. **Fetchable as a single ZIP** from EuropePMC's ``supplementaryFiles`` REST
   endpoint (:data:`EUROPEPMC_SUPPLEMENTARY_FILES_URL`,
   :func:`fetch_supplement_zip`), containing every supplementary file (and
   often ancillary figure images) for the article. Not every PMCID has one --
   EuropePMC 404s in that case, tolerated the same way
   `bugsigdb_curation.curator.evidence.assemble_evidence` tolerates a missing
   `fullTextXML` (best-effort, logged, not raised).

`bugsigdb_curation.curator.supplement_lever` builds on this module (fetch,
unpack, text rendering) for the opt-in ``--supplements`` lever; this module
itself stays free of curator imports. It also never imports
`bugsigdb_curation.eval` and never reads a gold path, in keeping with the
workflow plan's data firewall (§6e) -- this is a retrieval module, not a
curator module, but there's no reason for it to go anywhere near gold data
either.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import time
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

# `includeInlineImage=false` leaves out the figure images Europe PMC would otherwise bundle (we get figures
# from PMC separately). The bundling is the slow part: with the default, 34620922's ZIP stalled past a 240 s
# deadline; without, the same ZIP (just the supplementary PDF) arrives in ~3 s.
EUROPEPMC_SUPPLEMENTARY_FILES_URL = (
    "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles?includeInlineImage=false"
)

_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

#: Filename extension -> our small media-type vocabulary. Matched
#: case-insensitively against `Path(filename).suffix`; anything not listed
#: (including no extension) falls back to "image" for common image
#: extensions or "other" otherwise -- see `_media_type_for_filename`.
_EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "pdf",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "csv",
    ".tsv": "tsv",
    ".docx": "docx",
    ".doc": "docx",
}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp", ".svg"}


def _media_type_for_filename(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _EXTENSION_MEDIA_TYPES:
        return _EXTENSION_MEDIA_TYPES[ext]
    if ext in _IMAGE_EXTENSIONS:
        return "image"
    return "other"


def _text_content(elem: ET.Element) -> str:
    """Flatten an element's text (incl. nested tags) to a string."""
    return "".join(elem.itertext())


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class SupplementRef:
    """One `<supplementary-material>` entry named in a fullTextXML document.

    This is metadata only -- the filename it names, plus label/caption --
    not the file's bytes (those come from the separate ZIP fetch, see
    :func:`fetch_supplement_zip`).
    """

    supplement_id: str  # `<supplementary-material id="...">`, or a synthesized "supp-N" fallback
    filename: str | None  # `<media xlink:href="...">`, None if absent
    label: str  # `<label>` text, "" if absent
    caption: str  # `<caption>` text, tags stripped, whitespace collapsed, "" if absent


def parse_supplement_refs(xml_text: str) -> list[SupplementRef]:
    """Parse all `<supplementary-material>` elements out of a fullTextXML document.

    Mirrors `bugsigdb_curation.retrieval.parse_fulltext_figures`'s approach:
    same JATS namespace handling (`xlink:href` via the fully-qualified tag
    name), same "still produce an entry even if a piece is missing" policy.
    Returns `[]` for a document with none.
    """
    root = ET.fromstring(xml_text)
    entries: list[SupplementRef] = []
    for index, supp in enumerate(root.iter("supplementary-material")):
        # `<label>`/`<caption>` are searched as DESCENDANTS, not direct
        # children: real EuropePMC JATS nests the caption inside the
        # `<media>` element (`<supplementary-material><media><caption><p>..`),
        # often with inline markup like `<bold>Additional file 1:</bold>`
        # (which `_text_content`'s itertext flattens). A direct-child
        # `supp.find("caption")` silently returned "" on every real document.
        label_el = supp.find(".//label")
        label = _normalize_ws(_text_content(label_el)) if label_el is not None else ""

        caption_el = supp.find(".//caption")
        caption = _normalize_ws(_text_content(caption_el)) if caption_el is not None else ""

        media_el = supp.find(".//media")
        filename = media_el.get(_XLINK_HREF) if media_el is not None else None

        supplement_id = supp.get("id") or f"supp-{index}"
        entries.append(
            SupplementRef(
                supplement_id=supplement_id,
                filename=filename,
                label=label,
                caption=caption,
            )
        )
    return entries


@dataclass(frozen=True, slots=True)
class SupplementFile:
    """One unpacked supplementary file, with its raw bytes."""

    filename: str
    media_type: str  # "pdf" | "xlsx" | "csv" | "tsv" | "docx" | "image" | "other"
    raw_bytes: bytes


#: ZIP members never worth reading: videos, EPS vector art and nested archives.
_SKIPPED_EXTENSIONS = frozenset(
    {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg", ".m4v", ".webm", ".eps", ".zip"}
)

#: Defaults of `fetch_supplement_zip`'s download guard. The ZIP is generated on request and cannot be fetched
#: member by member, so a paper with big videos next to its small tables (37864204: 262 MB in ~11 minutes) can only
#: be had whole. The body is spooled to disk, so the byte cap is disk / zip-bomb insurance, not a memory limit.
MAX_ZIP_BYTES = 1024 * 1024 * 1024
#: The real protection against a hung server. EuropePMC assembles the ZIP on request (~30 s to the first byte) and
#: then serves it at roughly 400 KB/s, so 25 minutes covers the biggest ZIP seen so far with room to spare.
ZIP_TIMEOUT_SECONDS = 1500.0
#: Bodies up to this size stay in memory; a bigger one is rolled over to a temp file (removed when the handle closes).
SPOOL_MAX_BYTES = 32 * 1024 * 1024
#: A long download logs its progress at INFO every this many bytes.
ZIP_PROGRESS_LOG_BYTES = 50 * 1024 * 1024
#: Default cap on one unpacked member (uncompressed size), on all members together, and on the member count.
MAX_MEMBER_BYTES = 25 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ZIP_MEMBERS = 500

#: Where fetch-level skip reasons are filed in a `skipped` collector (there is no member name to attach them to).
ZIP_SKIP_NAME = "(supplementary files zip)"
#: ... and the one aggregate note for members left unread because of the member-count limit.
ZIP_MEMBERS_SKIP_NAME = "(supplementary files zip members)"
#: The `ZIP_SKIP_NAME` reason for a PMCID EuropePMC has no supplementary ZIP for: a normal outcome, not a failure.
NO_SUPPLEMENTS_REASON = "no supplementary files (HTTP 404)"

#: What reading one member can raise for a damaged, truncated, encrypted or unsupported-compression entry
#: (`BadZipFile` covers a CRC mismatch).
_MEMBER_READ_ERRORS = (zlib.error, EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile)


def _unique_filename(filename: str, seen: dict[str, int]) -> str:
    """`filename`, or ``"<stem> (n)<suffix>"`` when another member of the archive already used that basename."""
    count = seen.get(filename, 0) + 1
    seen[filename] = count
    if count == 1:
        return filename
    path = Path(filename)
    return f"{path.stem} ({count}){path.suffix}"


def unpack_supplement_zip(
    zip_source: bytes | BinaryIO,
    *,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_total_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
    max_members: int = MAX_ZIP_MEMBERS,
    skipped: list[tuple[str, str]] | None = None,
) -> list[SupplementFile]:
    """Unzip a supplementary-files archive into `SupplementFile`s.

    `zip_source` is the archive's bytes or a seekable binary file (what `fetch_supplement_zip` returns); the
    caller keeps ownership of a file object and closes it. Members are read one at a time, so only the ones
    kept (each under `max_member_bytes`) ever enter memory; the size of a skipped member never counts toward
    `max_total_bytes`.

    Directory entries are skipped, as are members that are not useful to a reader (video, EPS,
    nested ZIP), larger than `max_member_bytes`, or that would take the unpacked total over
    `max_total_bytes`; only the first `max_members` members are considered. A member that cannot be
    read (corrupt stream, truncated, encrypted, unsupported compression) is skipped too. Each skip is
    appended to `skipped` (when given) as ``(filename, reason)``. Two members with the same basename in
    different folders get distinct names (``S1.xlsx``, ``S1 (2).xlsx``). `media_type` is derived from
    the entry's filename extension (see `_media_type_for_filename`) -- the zip's own entries carry no
    separate content-type metadata.
    """

    def skip(name: str, reason: str) -> None:
        if skipped is not None:
            skipped.append((name, reason))

    files: list[SupplementFile] = []
    seen: dict[str, int] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(zip_source) if isinstance(zip_source, bytes) else zip_source) as zf:
        members = [info for info in zf.infolist() if not info.is_dir()]
        for info in members[:max_members]:
            filename = _unique_filename(Path(info.filename).name, seen)
            if Path(filename).suffix.lower() in _SKIPPED_EXTENSIONS:
                skip(filename, "file type not useful (video, EPS or nested archive)")
            elif info.file_size > max_member_bytes:
                skip(filename, f"larger than {max_member_bytes} bytes ({info.file_size})")
            elif total + info.file_size > max_total_bytes:
                skip(filename, f"would take the unpacked total over {max_total_bytes} bytes")
            else:
                try:
                    raw_bytes = zf.read(info.filename)
                except _MEMBER_READ_ERRORS as exc:
                    skip(filename, f"unreadable member: {type(exc).__name__}: {exc}")
                    continue
                total += len(raw_bytes)
                files.append(
                    SupplementFile(
                        filename=filename,
                        media_type=_media_type_for_filename(filename),
                        raw_bytes=raw_bytes,
                    )
                )
        if len(members) > max_members:
            skip(ZIP_MEMBERS_SKIP_NAME, f"{len(members) - max_members} further members not read (limit {max_members})")
    return files


# --- thin network I/O (not covered by pure-parser tests) --------------------------------


async def fetch_supplement_zip(
    pmcid: str,
    *,
    client: httpx.AsyncClient,
    max_bytes: int = MAX_ZIP_BYTES,
    timeout: float = ZIP_TIMEOUT_SECONDS,
    skipped: list[tuple[str, str]] | None = None,
) -> BinaryIO | None:
    """GET the EuropePMC supplementary-files ZIP for `pmcid`, or None if unavailable.

    On success returns a seekable binary file positioned at the start, for `unpack_supplement_zip` (or
    `zipfile.ZipFile`) to read. The body is spooled to disk past `SPOOL_MAX_BYTES`, so a ZIP of hundreds of
    megabytes never sits in memory; the caller owns the handle and must close it (closing deletes the temp
    file). On every other exit (None, an exception, cancellation) the temp file is already gone.

    Best-effort, mirroring `assemble_evidence`'s fullTextXML 404 tolerance
    (`bugsigdb_curation.curator.evidence`): a 404 (no supplementary files for
    this PMCID) is a normal outcome, not a failure -- logged at INFO and
    returns None. Any other HTTP error, a 200 response whose `Content-Type`
    isn't a zip, or a download that exceeds the guard is logged as a WARNING
    and also returns None rather than raising -- this is a best-effort
    enrichment channel, never something that should abort a caller's run.
    Whenever None is returned, the reason (404 / not a zip / too large / timeout / HTTP or
    transport error) is appended to `skipped` (when given) as ``(ZIP_SKIP_NAME, reason)``, so
    "no supplements exist" is distinguishable from "lost to a guard".

    The body is streamed and abandoned as soon as it passes `max_bytes` (a
    declared `Content-Length` over the cap is refused before any body is read)
    or the whole download passes `timeout` seconds. Progress is logged every `ZIP_PROGRESS_LOG_BYTES`.
    """
    log = logger.bind(stage="supplements")
    url = EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid=pmcid)

    def none(reason: str) -> None:
        if skipped is not None:
            skipped.append((ZIP_SKIP_NAME, reason))

    spool: BinaryIO | None = None
    try:
        async with asyncio.timeout(timeout):
            # EuropePMC assembles the ZIP on request (first byte can take ~30 s), so the shared client's
            # per-operation timeout must not preempt the overall deadline enforced above.
            async with client.stream("GET", url, timeout=httpx.Timeout(timeout)) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "zip" not in content_type.lower():
                    log.warning(
                        "supplementary files response was not a zip",
                        pmcid=pmcid,
                        content_type=content_type,
                    )
                    return none(f"response was not a zip (content-type {content_type!r})")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    log.warning(
                        "supplementary files zip too large; skipping", pmcid=pmcid, content_length=int(declared), max_bytes=max_bytes
                    )
                    return none(f"zip too large ({declared} bytes declared; limit {max_bytes})")
                spool = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
                received = 0
                next_progress = ZIP_PROGRESS_LOG_BYTES
                started = time.monotonic()
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        log.warning(
                            "supplementary files zip exceeded the size cap; aborting download", pmcid=pmcid, max_bytes=max_bytes
                        )
                        return none(f"zip too large (download passed the {max_bytes} byte limit)")
                    spool.write(chunk)
                    if received >= next_progress:
                        log.info(
                            "downloading supplementary files zip",
                            pmcid=pmcid,
                            mb_received=round(received / 1e6),
                            elapsed_s=round(time.monotonic() - started),
                        )
                        next_progress = (received // ZIP_PROGRESS_LOG_BYTES + 1) * ZIP_PROGRESS_LOG_BYTES
                spool.seek(0)
                handed_over, spool = spool, None
                return handed_over
    except TimeoutError:
        log.warning("supplementary files download timed out; skipping", pmcid=pmcid, timeout=timeout)
        return none(f"download timed out after {timeout:g} s")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            log.info("no supplementary files for pmcid", pmcid=pmcid)
            return none(NO_SUPPLEMENTS_REASON)
        log.warning("supplementary files fetch failed", pmcid=pmcid, status_code=status)
        return none(f"fetch failed: HTTP {status}")
    except httpx.HTTPError as exc:
        log.warning("supplementary files fetch failed", pmcid=pmcid, error=str(exc))
        return none(f"fetch failed: {type(exc).__name__}: {exc}")
    finally:
        if spool is not None:  # not handed to the caller: failed, over the cap, cancelled
            spool.close()


async def fetch_supplements(
    pmcid: str, *, client: httpx.AsyncClient, skipped: list[tuple[str, str]] | None = None
) -> list[SupplementFile]:
    """High-level: fetch the supplementary-files ZIP for `pmcid` and unpack it.

    Returns `[]` (not an error) when there's no ZIP to unpack -- either
    because EuropePMC has none for this PMCID, or the fetch otherwise failed
    best-effort (see `fetch_supplement_zip`). The fetch failure reason and the
    members skipped while unpacking are appended to `skipped`. Unpacking is
    CPU-bound, so it runs in a worker thread; the downloaded temp file is
    closed (deleted) afterwards whatever happens.
    """
    zip_file = await fetch_supplement_zip(pmcid, client=client, skipped=skipped)
    if zip_file is None:
        return []
    with zip_file:
        return await asyncio.to_thread(unpack_supplement_zip, zip_file, skipped=skipped)


# --- model-ready content: text rendering + document content blocks ----------------------


def supplement_to_text(f: SupplementFile) -> str | None:
    """Render a tabular/doc supplementary file as plain text for an LLM's text context.

    - "xlsx": every sheet -> a text grid (sheet-name header line, then
      tab-separated rows).
    - "csv"/"tsv": a single text grid (tab-joined rows).
    - "docx": paragraphs, then table cell text, newline-joined.
    - "pdf"/"image"/"other": None -- PDFs are handled as native document
      blobs (see `supplement_to_model_document`), not text; images have no
      text rendering here at all.

    Defensive: a malformed file logs a warning and returns None rather than
    raising -- this is best-effort enrichment, the same policy as the fetch
    functions above.
    """
    log = logger.bind(stage="supplements")
    try:
        if f.media_type == "xlsx":
            return _xlsx_to_text(f.raw_bytes)
        if f.media_type in ("csv", "tsv"):
            return _delimited_to_text(f.raw_bytes, delimiter="\t" if f.media_type == "tsv" else ",")
        if f.media_type == "docx":
            return _docx_to_text(f.raw_bytes)
    except Exception as exc:  # noqa: BLE001 -- malformed input must degrade to None, never raise
        log.warning("failed to render supplement to text", filename=f.filename, error=str(exc))
        return None
    return None


def _xlsx_to_text(raw_bytes: bytes) -> str:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    try:
        sheets_text = []
        for sheet in workbook.worksheets:
            lines = [f"# {sheet.title}"]
            for row in sheet.iter_rows(values_only=True):
                lines.append("\t".join("" if cell is None else str(cell) for cell in row))
            sheets_text.append("\n".join(lines))
        return "\n\n".join(sheets_text)
    finally:
        workbook.close()


def _delimited_to_text(raw_bytes: bytes, *, delimiter: str) -> str:
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return "\n".join("\t".join(row) for row in reader)


def _docx_to_text(raw_bytes: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(raw_bytes))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(part for part in parts if part is not None)


def supplement_to_model_document(f: SupplementFile) -> dict | None:
    """Render a PDF supplementary file as a LiteLLM document content block.

    Returns `{"type": "file", "file": {"file_data": "data:application/pdf;base64,<...>"}}`
    for "pdf"; None for every other media type (they either have a text
    rendering via `supplement_to_text`, or no model-ready rendering at all,
    e.g. "image"/"other").
    """
    if f.media_type != "pdf":
        return None
    encoded = base64.b64encode(f.raw_bytes).decode("ascii")
    return {"type": "file", "file": {"file_data": f"data:application/pdf;base64,{encoded}"}}
