"""Build a persisted `.duckdb` taxonomy DB from an NCBI taxdump.

Input is a **local taxdump** (the tested, fully-offline path): either an
already-extracted directory containing `names.dmp` + `nodes.dmp`, or a
`taxdump.tar.gz`/`.tgz`/`.zip` archive containing them, which is extracted
first. A pinned, dated release can optionally be fetched from NCBI's public
FTP-over-HTTPS mirror via :func:`download_taxdump`, but that function is a
thin, explicitly-network-only helper -- nothing in this module's build path
calls it, and it is exercised only by an `@pytest.mark.network` test.

`.dmp` format: fields separated by `"\\t|\\t"`, each row terminated by
`"\\t|\\n"`. `names.dmp` columns: `tax_id, name_txt, unique_name, name_class`
(only the first, second, and fourth matter here). `nodes.dmp` columns:
`tax_id, parent_tax_id, rank, ...` (only the first three matter; the dozen-odd
trailing columns -- embl code, division id, etc. -- are ignored).

Does not read the current time itself: `build_timestamp` is always supplied
by the caller (the CLI), per this repo's convention of keeping "what time is
it" out of library code.
"""

from __future__ import annotations

import csv
import hashlib
import os
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb
import httpx

from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name

#: NCBI's rolling "current" taxdump (not a pinned release).
NCBI_TAXDUMP_CURRENT_URL = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz"

#: NCBI's dated-archive mirror, keyed by a `YYYY-MM-DD` release label.
NCBI_TAXDUMP_ARCHIVE_URL_TEMPLATE = "https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump_archive/taxdmp_{release}.zip"


@dataclass(frozen=True)
class BuildStats:
    """Provenance + row counts for a completed build, for the CLI to print."""

    release: str
    source: str
    build_timestamp: str
    out_path: Path
    names_rows: int
    nodes_rows: int
    names_dmp_sha256: str
    nodes_dmp_sha256: str


def _iter_dmp_rows(path: Path) -> Iterator[list[str]]:
    """Yield each `.dmp` row as a list of field strings.

    Handles the taxdump line format: fields joined by `"\\t|\\t"`, the row
    terminated by a trailing `"\\t|"` before the newline (stripped here so
    callers never see it as part of the last field).
    """
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            fields = line.split("\t|\t")
            if fields[-1].endswith("\t|"):
                fields[-1] = fields[-1][: -len("\t|")]
            yield fields


def parse_names(path: Path) -> Iterator[tuple[int, str, str, str]]:
    """Yield `(tax_id, name_txt, name_class, name_norm)` from a `names.dmp` file."""
    for fields in _iter_dmp_rows(path):
        tax_id = int(fields[0])
        name_txt = fields[1]
        name_class = fields[3]
        yield tax_id, name_txt, name_class, normalize_taxon_name(name_txt)


def parse_nodes(path: Path) -> Iterator[tuple[int, int, str]]:
    """Yield `(tax_id, parent_tax_id, rank)` from a `nodes.dmp` file."""
    for fields in _iter_dmp_rows(path):
        tax_id = int(fields[0])
        parent_tax_id = int(fields[1])
        rank = fields[2]
        yield tax_id, parent_tax_id, rank


#: DuckDB `read_csv` column spec for the staged `names.tsv` (must match
#: `_write_names_tsv`'s column order and the `names` table's DDL).
_NAMES_CSV_COLUMNS = "{'tax_id':'BIGINT','name_txt':'VARCHAR','name_class':'VARCHAR','name_norm':'VARCHAR'}"

#: Same, for the staged `nodes.tsv` / `nodes` table.
_NODES_CSV_COLUMNS = "{'tax_id':'BIGINT','parent_tax_id':'BIGINT','rank':'VARCHAR'}"


def _write_names_tsv(names_path: Path, tsv_path: Path) -> int:
    """Parse `names_path` and write a clean, quoted TSV staging file for bulk load.

    Returns the row count. `csv.QUOTE_ALL` + DuckDB's default `quote='"'`
    round-trips any name containing a literal tab, double quote, or comma
    (rare in real taxdump data, but the escaping is nearly free) without
    ambiguity against `read_csv`'s NULL handling of unquoted empty fields.
    """
    count = 0
    with tsv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n")
        for tax_id, name_txt, name_class, name_norm in parse_names(names_path):
            writer.writerow((tax_id, name_txt, name_class, name_norm))
            count += 1
    return count


def _write_nodes_tsv(nodes_path: Path, tsv_path: Path) -> int:
    """Parse `nodes_path` and write a clean, quoted TSV staging file for bulk load.
    Returns the row count. See `_write_names_tsv` for the quoting rationale."""
    count = 0
    with tsv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n")
        for tax_id, parent_tax_id, rank in parse_nodes(nodes_path):
            writer.writerow((tax_id, parent_tax_id, rank))
            count += 1
    return count


def find_dmp_files(root: Path) -> tuple[Path, Path]:
    """Locate `names.dmp` and `nodes.dmp` under `root` (searched recursively,
    since archives sometimes extract into a nested subdirectory)."""
    names_matches = sorted(root.rglob("names.dmp"))
    nodes_matches = sorted(root.rglob("nodes.dmp"))
    if not names_matches:
        raise FileNotFoundError(f"no names.dmp found under {root}")
    if not nodes_matches:
        raise FileNotFoundError(f"no nodes.dmp found under {root}")
    return names_matches[0], nodes_matches[0]


def extract_taxdump(archive_path: Path, dest_dir: Path) -> Path:
    """Extract a `taxdump.tar.gz`/`.tgz`/`.zip` archive into `dest_dir`; returns `dest_dir`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    name_lower = archive_path.name.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest_dir)
    elif suffix == ".tgz" or name_lower.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as tf:
            try:
                tf.extractall(dest_dir, filter="data")  # py>=3.11.4: reject unsafe members
            except TypeError:
                tf.extractall(dest_dir)  # older 3.11.x without the `filter` kwarg
    else:
        raise ValueError(f"unsupported archive type: {archive_path.name!r} (expected .tar.gz/.tgz or .zip)")
    return dest_dir


def download_taxdump(dest_path: Path, *, release: str | None = None, timeout: float = 60.0) -> Path:
    """Download a taxdump archive from NCBI's FTP-over-HTTPS mirror to `dest_path`.

    Fetches the pinned dated archive for `release` if given, else the
    rolling "current" `taxdump.tar.gz`. Network egress may be blocked in
    sandboxed environments -- this is intentionally the *only* function in
    the package that makes a network call; the local-path build route
    (`build_taxonomy_db`) never calls it, and it's exercised only by an
    `@pytest.mark.network` test that skips cleanly when egress is blocked.
    """
    url = NCBI_TAXDUMP_ARCHIVE_URL_TEMPLATE.format(release=release) if release else NCBI_TAXDUMP_CURRENT_URL
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        with dest_path.open("wb") as fh:
            for chunk in response.iter_bytes():
                fh.write(chunk)
    return dest_path


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_from_files(
    names_path: Path,
    nodes_path: Path,
    out_path: Path,
    *,
    release: str,
    source: str,
    build_timestamp: str,
) -> BuildStats:
    """Build into a temp file next to `out_path` and only `os.replace()` it into
    place on full success -- `out_path` (pre-existing or absent) is never
    touched before that. On any exception the temp `.duckdb` (and its TSV
    staging dir) are removed and the exception re-raised, so a mid-build
    failure never leaves a corrupt/empty DB where a good one used to be.
    """
    names_checksum = _sha256_of(names_path)
    nodes_checksum = _sha256_of(nodes_path)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_db_path = out_path.with_name(f"{out_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    try:
        with tempfile.TemporaryDirectory(prefix="bugsigdb-taxbuild-", dir=out_path.parent) as staging_dir:
            staging = Path(staging_dir)
            names_tsv = staging / "names.tsv"
            nodes_tsv = staging / "nodes.tsv"

            # Stream each .dmp through the existing (tested) Python parser
            # into a clean, quoted TSV, then bulk-load it via DuckDB's
            # native `read_csv` -- an order of magnitude+ faster than
            # `executemany`'s row-at-a-time round trips over NCBI's
            # multi-million-row files, and dependency-free (no pandas/
            # pyarrow needed for DuckDB's DataFrame-based Appender).
            names_rows = _write_names_tsv(names_path, names_tsv)
            nodes_rows = _write_nodes_tsv(nodes_path, nodes_tsv)

            con = duckdb.connect(str(tmp_db_path))
            try:
                con.execute(
                    "CREATE TABLE names (tax_id BIGINT, name_txt VARCHAR, name_class VARCHAR, name_norm VARCHAR)"
                )
                con.execute(
                    "CREATE TABLE nodes (tax_id BIGINT PRIMARY KEY, parent_tax_id BIGINT, rank VARCHAR)"
                )

                con.execute(
                    f"INSERT INTO names SELECT * FROM read_csv(?, delim='\t', header=false, quote='\"', "
                    f"columns={_NAMES_CSV_COLUMNS})",
                    [str(names_tsv)],
                )
                con.execute(
                    f"INSERT INTO nodes SELECT * FROM read_csv(?, delim='\t', header=false, quote='\"', "
                    f"columns={_NODES_CSV_COLUMNS})",
                    [str(nodes_tsv)],
                )

                con.execute("CREATE INDEX idx_names_name_norm ON names(name_norm)")

                # Written last, after everything else has succeeded: an
                # incomplete/missing `meta` table is exactly what
                # `TaxonomyDB` treats as a corrupt build (see db.py).
                con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
                meta_rows = [
                    ("release", release),
                    ("source", source),
                    ("build_timestamp", build_timestamp),
                    ("names_rows", str(names_rows)),
                    ("nodes_rows", str(nodes_rows)),
                    ("names_dmp_filename", names_path.name),
                    ("nodes_dmp_filename", nodes_path.name),
                    ("names_dmp_sha256", names_checksum),
                    ("nodes_dmp_sha256", nodes_checksum),
                ]
                con.executemany("INSERT INTO meta VALUES (?, ?)", meta_rows)
            finally:
                con.close()
    except BaseException:
        tmp_db_path.unlink(missing_ok=True)
        raise

    os.replace(tmp_db_path, out_path)

    return BuildStats(
        release=release,
        source=source,
        build_timestamp=build_timestamp,
        out_path=out_path,
        names_rows=names_rows,
        nodes_rows=nodes_rows,
        names_dmp_sha256=names_checksum,
        nodes_dmp_sha256=nodes_checksum,
    )


def build_taxonomy_db(
    taxdump_path: Path,
    out_path: Path,
    *,
    release: str,
    source: str,
    build_timestamp: str,
    extract_dir: Path | None = None,
) -> BuildStats:
    """Build a `.duckdb` taxonomy DB from a local taxdump.

    `taxdump_path` is either a directory already containing `names.dmp` +
    `nodes.dmp` (searched recursively, so a nested layout is fine), or a
    `.tar.gz`/`.tgz`/`.zip` archive containing them -- extracted into
    `extract_dir` if given, else a temporary directory that's cleaned up
    before this returns. Never touches the network.

    Writes three tables to `out_path` (overwritten if it already exists):
    `names(tax_id, name_txt, name_class, name_norm)` (indexed on
    `name_norm`), `nodes(tax_id PRIMARY KEY, parent_tax_id, rank)`, and
    `meta(key, value)` recording `release`, `source`, `build_timestamp`, row
    counts, and a sha256 checksum of each input `.dmp` file -- enough to
    reproduce or audit the build later.
    """
    taxdump_path = Path(taxdump_path)
    if taxdump_path.is_dir():
        names_path, nodes_path = find_dmp_files(taxdump_path)
        return _build_from_files(
            names_path, nodes_path, out_path, release=release, source=source, build_timestamp=build_timestamp
        )

    if extract_dir is not None:
        extracted = extract_taxdump(taxdump_path, extract_dir)
        names_path, nodes_path = find_dmp_files(extracted)
        return _build_from_files(
            names_path, nodes_path, out_path, release=release, source=source, build_timestamp=build_timestamp
        )

    with tempfile.TemporaryDirectory(prefix="bugsigdb-taxdump-") as tmp:
        extracted = extract_taxdump(taxdump_path, Path(tmp))
        names_path, nodes_path = find_dmp_files(extracted)
        return _build_from_files(
            names_path, nodes_path, out_path, release=release, source=source, build_timestamp=build_timestamp
        )
