"""The supplement lever: read a paper's supplementary files with a cheap screen in front of the strong model.

Opt-in (``curate --decision-model ... --supplements``). Supplementary files hold most of the differential-
abundance results BugSigDB curators record (the 34620922 supplement alone is 47 pages), but are mostly
irrelevant pages, sample sheets and matrices. The probe (`benchmarks/decision-probe/RESULTS.md`, P1/P4,
issue #19) found a decision model screens them well enough to put in front of the generative model:

1. :func:`supplement_units` splits the unpacked files into *units* -- one per xlsx sheet, per PDF page,
   per csv/docx.
2. :func:`screen_units` (stage ``s1b_screen``) asks three questions per unit: does it hold a per-taxon
   differential-abundance result (routed iff p >= :data:`SCREEN_THRESHOLD`; probe: recall 1.0 at precision
   0.77-1.0), what kind of content is it, and how many groups does the comparison involve.
3. Routed units get one generative extraction call (stage ``supplement_extract``) listing every two-group
   comparison; a unit the screen labelled ``multi_group_one_vs_rest`` gets the one-vs-rest prompt instead and
   is expanded deterministically in code into one experiment per group (:func:`expand_one_vs_rest`).
4. Names are resolved by the existing S6-reconcile path (`curator.reconcile`) -- ids come from the taxonomy
   authority only -- and :func:`drop_duplicate_experiments` removes anything the main text already reports.

Best-effort like the rest of the routing layer: a failed decision/IO/model call is logged and recorded in
``annotations`` and never aborts the study; only the expected failure types are absorbed (a bug still
surfaces). The question wording below is the probe's, reimplemented here because curator modules may not
import ``benchmarks``/``eval`` (firewall, §6e).
"""

from __future__ import annotations

import asyncio
import csv
import io
import itertools
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import httpx
from loguru import logger

from bugsigdb_curation.curator.experiment import ExperimentFields
from bugsigdb_curation.curator.model import Model, ModelError, build_image_content, build_text_content
from bugsigdb_curation.curator.ner import NamedTaxon
from bugsigdb_curation.curator.reconcile import reconcile_names
from bugsigdb_curation.curator.routing import DECISION_CALL_ERRORS, unwrap_fan_out_failure
from bugsigdb_curation.curator.signature import ExtractedSignature
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver
from bugsigdb_curation.decision import Choice, ChoiceAnswer, DecisionModel, Noul, NoulAnswer
from bugsigdb_curation.loader import SEQUENCING_TYPE_VALUES, STATISTICAL_TEST_VALUES, normalize_enum
from bugsigdb_curation.supplements import ZIP_SKIP_NAME, SupplementFile, fetch_supplements, supplement_to_text
from bugsigdb_curation.taxonomy.normalize import normalize_taxon_name

SCREEN_STAGE = "s1b_screen"
EXTRACT_STAGE = "supplement_extract"

#: A unit is routed to the strong model iff p(has_da_results) is at least this (probe: recall 1.0 at precision
#: 0.77-1.0 on pages/sheets with clef -- cheap to over-include, costly to miss).
SCREEN_THRESHOLD = 0.5
#: The only arity answer that switches a unit to the one-vs-rest prompt + deterministic expansion
#: (probe: 3/3 detected, 0 false positives -- but only 3 positives, so every use is logged).
ONE_VS_REST = "multi_group_one_vs_rest"

#: Limits on what is accepted from one unit's extraction answer (cuts are recorded in ``supplement_truncated``)
#: and on how many units one study may screen at all (the rest are recorded as skipped).
_MAX_COMPARISONS_PER_UNIT = 50
_MAX_TAXA_PER_COMPARISON = 500
_MAX_NAME_CHARS = 200
_MAX_UNITS_SCREENED = 400
#: One-vs-rest expansion needs at least this many distinct groups; with fewer, the screen's arity label is
#: distrusted (probe: only 3 positives) and the unit is read as a two-group table instead.
MIN_ONE_VS_REST_GROUPS = 3

#: Concurrent decision calls while screening / generative calls while extracting.
_SCREEN_CONCURRENCY = 6
_EXTRACT_CONCURRENCY = 4

#: Screening state (probe setting): the header plus the first rows, cells cut to 60 chars, first 15 columns.
_SCREEN_ROWS = 30
_SCREEN_CELL_CHARS = 60
_SCREEN_COLUMNS = 15
#: Extraction text for a sheet/csv: up to this many rows, cells cut to 200 chars, first 40 columns -- and at most
#: `_EXTRACT_TOTAL_CHARS` characters in all, so a 400 x 40 x 200 sheet cannot blow the model's context window.
_EXTRACT_ROWS = 400
_EXTRACT_CELL_CHARS = 200
_EXTRACT_COLUMNS = 40
_EXTRACT_TOTAL_CHARS = 60_000
#: Rows scanned (blank ones included) while looking for `_EXTRACT_ROWS` non-blank ones.
_SCAN_ROWS = _EXTRACT_ROWS * 4
#: Characters of docx / PDF-page text sent to a model.
_TEXT_CHARS = 12_000
#: A PDF page with at least this much extractable text is read as text; otherwise it is rendered.
_PDF_TEXT_MIN_CHARS = 200
#: Pages read per PDF (guards a pathological several-hundred-page file; the rest are recorded as skipped).
_MAX_PDF_PAGES = 200
#: The Workers AI pre-flight token estimate scales with the *encoded* image size (probe: a 0.8 MB PNG page was
#: rejected as ~274k tokens against a 65k context; <= 200 KB JPEGs averaged ~1.6k), so pages are JPEGs under this.
MAX_IMAGE_BYTES = 200_000
#: A rendered page's longest side is at most this many pixels, whatever the page's physical size.
_MAX_IMAGE_SIDE_PX = 2000
#: (dpi, JPEG quality) attempts in order: the probe's 100 dpi at decreasing quality, then a lower resolution for
#: a pathological (photographic / noisy) page that still does not fit. The dpi is further capped per page so the
#: longest side stays within `_MAX_IMAGE_SIDE_PX`.
_RENDER_ATTEMPTS = ((100, 80), (100, 65), (100, 50), (100, 35), (72, 35), (50, 35))
#: Formats the old binary Office readers produced; neither openpyxl nor python-docx can read them.
_LEGACY_SUFFIXES = (".xls", ".doc")

#: What a lever step can raise for a failed call or a bad generative response, beyond a bug in our code.
#: `ModelCallError` (a transport/provider failure or malformed completion) is a `ModelError`, so it is covered.
_EXPECTED_ERRORS = (*DECISION_CALL_ERRORS, ModelError)
#: Jaccard overlap (of resolved taxon sets, same direction) at or above which a supplement signature is
#: considered already reported (by the main text or an earlier supplement experiment) ...
DUPLICATE_JACCARD = 0.5
#: ... provided both sets have at least this many taxa (a 1-2 taxon set trivially matches).
DUPLICATE_MIN_TAXA = 3

UnitKind = Literal["sheet", "delimited", "docx", "pdf_text", "pdf_image"]


@dataclass(frozen=True, slots=True)
class SupplementUnit:
    """One screenable, extractable piece of a supplementary file.

    `label` is the sheet name / ``"page N"`` (empty for a whole-file csv/docx); `text` is the extraction text
    (tab-joined rows for tables, plain text for documents, the sparse page text for an image page);
    `image` is the rendered JPEG of a text-free PDF page; `truncated` is True when rows, columns, cell text or
    document characters were cut to fit the extraction limits (the model never saw the whole unit).
    """

    id: str
    filename: str
    label: str
    kind: UnitKind
    text: str
    image: bytes | None
    page: int | None = None
    truncated: bool = False

    @property
    def provenance(self) -> str:
        """``"<filename> :: <sheet or page N>"`` -- the signature ``source`` of everything extracted from it."""
        return f"{self.filename} :: {self.label}" if self.label else self.filename


# --- units ---------------------------------------------------------------------------------------------


class _Unreadable(Exception):
    """A third-party reader (openpyxl, csv, pymupdf) could not open or parse part of a supplement."""


_T = TypeVar("_T")


def _read(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Call a third-party open/parse function; whatever it raises on a malformed file becomes :class:`_Unreadable`.

    Only the library call is guarded -- a bug in our own code around it must still surface.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- a malformed third-party file must not abort the study
        raise _Unreadable(f"{type(exc).__name__}: {exc}") from exc


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    text = f"{value:.4g}" if isinstance(value, float) else str(value)
    return re.sub(r"[\t\r\n]+", " ", text).strip()


def _rows_to_text(rows: Sequence[Sequence[Any]]) -> tuple[str, bool]:
    """Tab-joined, blank-row-free text of a table, and whether anything was cut to fit.

    Limits: :data:`_EXTRACT_ROWS` rows, :data:`_EXTRACT_COLUMNS` columns, :data:`_EXTRACT_CELL_CHARS` per cell and
    :data:`_EXTRACT_TOTAL_CHARS` in all; :data:`_SCAN_ROWS` rows are looked at while hunting for non-blank ones.
    """
    lines: list[str] = []
    chars = 0
    truncated = False
    for scanned, row in enumerate(rows, 1):
        cells_full = [_cell_text(c) for c in row]
        if scanned > _SCAN_ROWS:
            truncated = any(cells_full)
            break
        cells = [c[:_EXTRACT_CELL_CHARS] for c in cells_full[:_EXTRACT_COLUMNS]]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        line = "\t".join(cells)
        if len(lines) >= _EXTRACT_ROWS or chars + len(line) + 1 > _EXTRACT_TOTAL_CHARS:
            truncated = True
            break
        truncated |= any(len(c) > _EXTRACT_CELL_CHARS for c in cells_full[:_EXTRACT_COLUMNS]) or any(
            cells_full[_EXTRACT_COLUMNS:]
        )
        lines.append(line)
        chars += len(line) + 1
    return "\n".join(lines), truncated


def _sheet_rows(sheet: Any) -> list[tuple[Any, ...]]:
    # One column beyond the limit is read so a wider sheet is noticed (and flagged truncated).
    return list(sheet.iter_rows(values_only=True, max_row=_SCAN_ROWS + 1, max_col=_EXTRACT_COLUMNS + 1))


def _xlsx_units(f: SupplementFile, skipped: list[tuple[str, str]]) -> list[SupplementUnit]:
    import openpyxl

    workbook = _read(openpyxl.load_workbook, io.BytesIO(f.raw_bytes), read_only=True, data_only=True)
    try:
        units = []
        for sheet in _read(lambda: list(workbook.worksheets)):
            try:
                rows = _read(_sheet_rows, sheet)
            except _Unreadable as exc:
                skipped.append((f"{f.filename} :: {sheet.title}", f"unreadable: {exc}"))
                continue
            text, truncated = _rows_to_text(rows)
            if not text:
                skipped.append((f"{f.filename} :: {sheet.title}", "empty sheet"))
                continue
            units.append(
                SupplementUnit(
                    f"{f.filename}::{sheet.title}", f.filename, sheet.title, "sheet", text, None, truncated=truncated
                )
            )
        return units
    finally:
        workbook.close()


def _delimited_rows(raw_bytes: bytes, delimiter: str) -> list[list[str]]:
    decoded = raw_bytes.decode("utf-8-sig", errors="replace")
    return list(itertools.islice(csv.reader(io.StringIO(decoded), delimiter=delimiter), _SCAN_ROWS + 1))


def _delimited_unit(f: SupplementFile) -> list[SupplementUnit]:
    rows = _read(_delimited_rows, f.raw_bytes, "\t" if f.media_type == "tsv" else ",")
    text, truncated = _rows_to_text(rows)
    return [SupplementUnit(f.filename, f.filename, "", "delimited", text, None, truncated=truncated)] if text else []


def _jpeg(page: Any, dpi: int, quality: int) -> bytes:
    return page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=quality)


def _render_page(page: Any) -> bytes | None:
    """The page as a JPEG of at most :data:`MAX_IMAGE_BYTES`, or None when no attempt fits.

    The dpi is capped so the longest side stays within :data:`_MAX_IMAGE_SIDE_PX` pixels, whatever the page size.
    """
    rect = _read(lambda: page.rect)
    longest_points = max(rect.width, rect.height)
    max_dpi = max(1, int(_MAX_IMAGE_SIDE_PX * 72 / longest_points)) if longest_points > 0 else 100
    for dpi, quality in dict.fromkeys((min(dpi, max_dpi), quality) for dpi, quality in _RENDER_ATTEMPTS):
        data = _read(_jpeg, page, dpi, quality)
        if len(data) <= MAX_IMAGE_BYTES:
            return data
    return None


def _pdf_units(f: SupplementFile, skipped: list[tuple[str, str]]) -> list[SupplementUnit]:
    import pymupdf

    units = []
    doc = _read(pymupdf.open, stream=f.raw_bytes, filetype="pdf")
    with doc:
        if doc.page_count > _MAX_PDF_PAGES:
            skipped.append((f.filename, f"pages beyond {_MAX_PDF_PAGES} not read ({doc.page_count} pages)"))
        for number in range(1, min(doc.page_count, _MAX_PDF_PAGES) + 1):
            label, unit_id = f"page {number}", f"{f.filename}::page {number}"
            try:
                page = _read(doc.load_page, number - 1)
                text = _read(page.get_text).strip()
                if len(text) >= _PDF_TEXT_MIN_CHARS:
                    units.append(
                        SupplementUnit(
                            unit_id, f.filename, label, "pdf_text", text[:_TEXT_CHARS], None, number,
                            truncated=len(text) > _TEXT_CHARS,
                        )
                    )
                    continue
                image = _render_page(page)
            except _Unreadable as exc:
                skipped.append((f"{f.filename} :: {label}", f"unreadable: {exc}"))
                continue
            if image is None:
                skipped.append((f"{f.filename} :: {label}", f"page image over {MAX_IMAGE_BYTES} bytes at the lowest quality"))
                continue
            units.append(SupplementUnit(unit_id, f.filename, label, "pdf_image", text, image, number))
    return units


def supplement_units(
    files: Sequence[SupplementFile], *, skipped: list[tuple[str, str]] | None = None
) -> list[SupplementUnit]:
    """Split unpacked supplementary files into units: xlsx -> one per sheet, csv/tsv and docx -> one, PDF -> one per page.

    A PDF page with at least 200 characters of extractable text is a text unit; a text-free page is rendered to a
    JPEG of at most :data:`MAX_IMAGE_BYTES` (skipped if it cannot be made to fit). Images, legacy ``.xls``/``.doc``
    and other file types are not read; an unreadable or empty file/sheet/page is skipped too. Every skip is
    appended to `skipped` (when given) as ``(file, reason)``. Only the third-party open/parse calls are guarded;
    this is CPU-bound, so callers on an event loop run it in a worker thread.
    """
    notes: list[tuple[str, str]] = skipped if skipped is not None else []
    units: list[SupplementUnit] = []
    for f in files:
        suffix = Path(f.filename).suffix.lower()
        try:
            if suffix in _LEGACY_SUFFIXES:
                notes.append((f.filename, f"legacy {suffix} format not supported (re-save as .xlsx / .docx)"))
            elif f.media_type == "xlsx":
                units += _xlsx_units(f, notes)
            elif f.media_type in ("csv", "tsv"):
                units += _delimited_unit(f)
            elif f.media_type == "docx":
                text = supplement_to_text(f)
                if text is None:
                    notes.append((f.filename, "unreadable docx"))
                elif text.strip():
                    body = text.strip()
                    units.append(
                        SupplementUnit(
                            f.filename, f.filename, "", "docx", body[:_TEXT_CHARS], None, truncated=len(body) > _TEXT_CHARS
                        )
                    )
            elif f.media_type == "pdf":
                units += _pdf_units(f, notes)
            else:
                notes.append((f.filename, f"{f.media_type} file not read"))
        except _Unreadable as exc:
            logger.bind(stage="S1b").warning("unreadable supplement file", filename=f.filename, error=str(exc))
            notes.append((f.filename, f"unreadable: {exc}"))
    return units


# --- screening (A3) ------------------------------------------------------------------------------------

_CONTENT_KINDS = {
    "da_taxa_table": "a table of taxa with differential-abundance statistics (LDA score, fold change, p/q values)",
    "abundance_matrix": "a taxa x samples abundance / count matrix with no per-taxon test result",
    "diversity_or_ordination_stats": "alpha/beta diversity, ordination or PERMANOVA / mixed-model statistics",
    "sample_metadata": "sample, subject or site information",
    "methods_text": "prose: methods, results discussion, legends",
    "raw_or_sequence": "raw sequence data, variants, genotypes",
    "figure_or_image": "a figure or image",
    "other": "anything else, including summary counts of significant taxa",
}
_ARITY = {
    "two_group": "each result table compares exactly two groups",
    ONE_VS_REST: "three or more groups compared at once; each group's enriched taxa are listed against all the others",
    "multi_group_all_pairwise": "three or more groups with every pairwise comparison reported separately",
    "not_a_comparison": "no group comparison is reported",
}
_HAS_DA_QUESTION = Noul(
    "Does this content contain a per-taxon differential-abundance result (for example LEfSe/LDA, fold change, "
    "or p/q-values for individual taxa)?",
    criteria={
        "true": "individual taxa are listed with a statistic showing they differ between groups",
        "false": "no per-taxon differential-abundance result",
    },
)
SCREEN_QUESTIONS = {
    "has_da_results": _HAS_DA_QUESTION,
    "content_kind": Choice("What kind of content is this?", _CONTENT_KINDS),
    "arity": Choice("How many groups does the comparison in this content involve?", _ARITY),
}


class SupplementScreenError(Exception):
    """A decision call for one unit failed (the expected, absorbable kind); `__cause__` is the original error."""

    def __init__(self, unit_id: str) -> None:
        super().__init__(unit_id)
        self.unit_id = unit_id


@dataclass(frozen=True, slots=True)
class ScreenedUnit:
    """One unit's screening answers and the routing verdict."""

    unit: SupplementUnit
    p_da: float
    content_kind: str
    arity: str
    routed: bool

    def annotation(self) -> dict[str, Any]:
        return {
            "id": self.unit.id,
            "p_da": self.p_da,
            "content_kind": self.content_kind,
            "arity": self.arity,
            "routed": self.routed,
            "truncated": self.unit.truncated,
        }


def screen_state(unit: SupplementUnit) -> dict[str, Any]:
    """What the decision model sees of a unit (probe setting): a table's header + first rows, a page/document's text."""
    state: dict[str, Any] = {"file": unit.filename}
    if unit.kind in ("sheet", "delimited"):
        if unit.label:
            state["sheet"] = unit.label
        state["first_rows"] = [
            [c[:_SCREEN_CELL_CHARS] for c in line.split("\t")[:_SCREEN_COLUMNS]]
            for line in unit.text.splitlines()[:_SCREEN_ROWS]
        ]
    elif unit.kind == "docx":
        state["text"] = unit.text
    else:
        state["page"] = unit.page
        if unit.kind == "pdf_text":
            state["page_text"] = unit.text
    return state


async def screen_units(units: Sequence[SupplementUnit], decision_model: DecisionModel) -> list[ScreenedUnit]:
    """A3: one decision call per unit, in input order; a unit is routed iff p(has_da_results) >= :data:`SCREEN_THRESHOLD`.

    Raises :class:`SupplementScreenError` (naming the unit) when a decision call fails in the expected ways
    (:data:`DECISION_CALL_ERRORS`); the first failure cancels the sibling calls rather than letting them keep
    spending. A failure that is a bug in our code propagates as itself, never masked by a sibling's routine failure.
    """
    sem = asyncio.Semaphore(_SCREEN_CONCURRENCY)

    async def one(unit: SupplementUnit) -> ScreenedUnit:
        async with sem:
            try:
                answers = await decision_model.decide(
                    stage=SCREEN_STAGE,
                    state=screen_state(unit),
                    questions=SCREEN_QUESTIONS,
                    images=[unit.image] if unit.image is not None else (),
                )
            except DECISION_CALL_ERRORS as exc:
                raise SupplementScreenError(unit.id) from exc
        has_da, kind, arity = answers["has_da_results"], answers["content_kind"], answers["arity"]
        assert isinstance(has_da, NoulAnswer) and isinstance(kind, ChoiceAnswer) and isinstance(arity, ChoiceAnswer)
        return ScreenedUnit(unit, has_da.p_yes, kind.choice, arity.choice, has_da.p_yes >= SCREEN_THRESHOLD)

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(one(u)) for u in units]
    except ExceptionGroup as group_error:
        failure = unwrap_fan_out_failure(group_error, (SupplementScreenError,))
        raise failure from failure.__cause__  # keep the original decision error on the named failure
    return [task.result() for task in tasks]



# --- extraction (generative) ---------------------------------------------------------------------------

#: BugSigDB's group convention, in `curator.artifact_text.group_orientation_text`'s wording: it holds for ~95% of
#: curated experiments where one group is recognisably the control (and flipping it flips directions wholesale, L031).
_GROUP_CONVENTION = (
    "BugSigDB's group convention: Group 0 is the REFERENCE group (control / baseline / unexposed / the comparator) "
    "and Group 1 the CASE group (exposed / treated / the group of interest), and every result reports taxa INCREASED "
    "or DECREASED in Group 1 relative to Group 0 -- INCREASED means more abundant in Group 1 than in Group 0, "
    "DECREASED means less abundant in Group 1 than in Group 0. Apply this even when the source lists the cases first "
    "(e.g. 'ATB vs HC' -> group_0 = HC, group_1 = ATB). In a figure or page image, use the legend and headers to "
    "decide which colour or side belongs to which group -- never assume the left/top/first-listed group is Group 1."
)

_TWO_GROUP_PROMPT = (
    "You are extracting differential-abundance results from the supplementary material of a microbiome research "
    "paper{title}, for BugSigDB curation.\n\n"
    "{source}\n\n"
    "List EVERY two-group differential-abundance comparison reported in it: per-taxon results such as LEfSe/LDA, "
    "fold change, or p/q-values. Ignore anything that is not a per-taxon differential-abundance result (sample "
    "metadata, diversity statistics, abundance matrices with no test result). For each comparison give:\n"
    "- group_0_name and group_1_name: short names for the two compared groups. {convention}\n"
    "- body_site: the anatomical site(s) sampled if stated (list of free-text strings), else []\n"
    "- condition: the disease/condition label(s) if stated (list of free-text strings), else []\n"
    "- host_species, sequencing_type, statistical_test, mht_correction: ONLY if this content itself states them "
    "for the comparison, else null (or [] for statistical_test) -- they are then taken from the main text. "
    "host_species is the organism studied (e.g. \"Homo sapiens\"); sequencing_type is EXACTLY one of {sequencing_types}; "
    "statistical_test is a list, each EXACTLY one of {statistical_tests}; mht_correction is true if a "
    "multiple-hypothesis-testing correction was applied, false if explicitly not.\n"
    "- taxa: every taxon reported as significantly different, with its name exactly as written (do NOT propose "
    "NCBI Taxonomy ids -- they are resolved separately) and its direction, \"increased\" or \"decreased\" in "
    "Group 1 relative to Group 0.\n\n"
    'Return ONLY a JSON object: {{"comparisons": [{{"group_0_name": "...", "group_1_name": "...", '
    '"body_site": [...], "condition": [...], "host_species": ..., "sequencing_type": ..., "statistical_test": [...], '
    '"mht_correction": ..., "taxa": [{{"name": "...", "direction": "increased"|"decreased"}}, ...]}}, ...]}}\n\n'
    "{content}"
)

_ONE_VS_REST_PROMPT = (
    "You are extracting differential-abundance results from the supplementary material of a microbiome research "
    "paper{title}, for BugSigDB curation.\n\n"
    "{source}\n\n"
    "This content compares three or more groups at once, listing for each group the taxa enriched in it against all "
    "the other groups. For EVERY group, list the taxa ENRICHED in that group versus all other groups, with each "
    "taxon name exactly as written (do NOT propose NCBI Taxonomy ids -- they are resolved separately) and the "
    "group's name as written. Do not list taxa that are depleted in a group, and ignore anything that is not a "
    "per-taxon differential-abundance result.\n\n"
    'Return ONLY a JSON object: {{"groups": [{{"name": "<group name>", "taxa": [{{"name": "<taxon name>"}}, ...]}}, '
    "...]}}\n\n"
    "{content}"
)


@dataclass(frozen=True, slots=True)
class SupplementComparison:
    """One comparison extracted from a unit, names only (ids come from the taxonomy authority afterwards).

    `host_species`, `sequencing_type`, `statistical_test` and `mht_correction` are only what the supplement itself
    stated; absent ones fall back to the main text's first experiment (:func:`resolve_comparison`).
    """

    group_0_name: str | None
    group_1_name: str | None
    body_site: tuple[str, ...]
    condition: tuple[str, ...]
    taxa: tuple[NamedTaxon, ...]
    host_species: str | None = None
    sequencing_type: str | None = None
    statistical_test: tuple[str, ...] = ()
    mht_correction: bool | None = None


@dataclass
class ExtractionNotes:
    """What :func:`extract_comparisons` reports besides the comparisons (an out-parameter, like ``skipped``).

    `cuts` are human-readable notes on what was dropped from the model's answer to respect the caps;
    `one_vs_rest_rejected_groups` is the distinct-group count when a one-vs-rest answer had fewer than
    :data:`MIN_ONE_VS_REST_GROUPS` groups (the unit was then read as a two-group table), else None.
    """

    cuts: list[str] = field(default_factory=list)
    one_vs_rest_rejected_groups: int | None = None


def build_extract_messages(unit: SupplementUnit, *, study_title: str, one_vs_rest: bool) -> list[dict]:
    """The extraction prompt for one unit: its text, or its page image plus whatever text the page has."""
    if unit.image is not None:
        source = f"The attached image is a page of the supplementary file ({unit.provenance})."
        content = f"Text extracted from the page (may be empty):\n{unit.text}" if unit.text else ""
    else:
        source = f"The content below is from the supplementary file ({unit.provenance})."
        content = f"Content:\n{unit.text}"
    template = _ONE_VS_REST_PROMPT if one_vs_rest else _TWO_GROUP_PROMPT
    prompt = template.format(
        title=f' ("{study_title}")' if study_title else "",
        source=source,
        convention=_GROUP_CONVENTION,
        sequencing_types=sorted(SEQUENCING_TYPE_VALUES),
        statistical_tests=sorted(STATISTICAL_TEST_VALUES),
        content=content,
    )
    blocks = [build_text_content(prompt)]
    if unit.image is not None:
        blocks.append(build_image_content(unit.image))
    return [{"role": "user", "content": blocks}]


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return tuple(str(v).strip() for v in items if v is not None and str(v).strip())


def _name(value: Any, cuts: Counter[str]) -> str:
    """A model-supplied name, stripped (so a whitespace-only one is empty) and cut to :data:`_MAX_NAME_CHARS`."""
    text = str(value).strip() if value is not None else ""
    if len(text) > _MAX_NAME_CHARS:
        cuts["names"] += 1
        text = text[:_MAX_NAME_CHARS].rstrip()
    return text


def _cut_notes(cuts: Counter[str]) -> list[str]:
    notes = {
        "comparisons": f"{cuts['comparisons']} comparison(s) beyond the first {_MAX_COMPARISONS_PER_UNIT} dropped",
        "taxa": f"{cuts['taxa']} taxon name(s) beyond the first {_MAX_TAXA_PER_COMPARISON} per comparison dropped",
        "names": f"{cuts['names']} name(s) cut to {_MAX_NAME_CHARS} characters",
    }
    return [note for key, note in notes.items() if cuts[key]]


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _statistical_tests(value: Any) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return tuple(v for v in (normalize_enum(_optional_str(x), STATISTICAL_TEST_VALUES) for x in items) if v)


def _two_group_comparisons(response: dict[str, Any], cuts: Counter[str]) -> list[SupplementComparison]:
    comparisons: list[SupplementComparison] = []
    raw = response.get("comparisons")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        taxa: list[NamedTaxon] = []
        raw_taxa = item.get("taxa")
        for t in raw_taxa if isinstance(raw_taxa, list) else []:
            if not isinstance(t, dict):
                continue
            name = _name(t.get("name"), cuts)
            direction = str(t.get("direction", "")).strip().lower()
            if not name or direction not in ("increased", "decreased"):
                continue
            if len(taxa) >= _MAX_TAXA_PER_COMPARISON:
                cuts["taxa"] += 1
                continue
            taxa.append(NamedTaxon(name=name, direction=direction))  # type: ignore[arg-type]
        if not taxa:
            continue
        if len(comparisons) >= _MAX_COMPARISONS_PER_UNIT:
            cuts["comparisons"] += 1
            continue
        mht = item.get("mht_correction")
        comparisons.append(
            SupplementComparison(
                group_0_name=_name(item.get("group_0_name"), cuts) or None,
                group_1_name=_name(item.get("group_1_name"), cuts) or None,
                body_site=_strings(item.get("body_site")),
                condition=_strings(item.get("condition")),
                taxa=tuple(taxa),
                host_species=_name(item.get("host_species"), cuts) or None,
                sequencing_type=normalize_enum(_optional_str(item.get("sequencing_type")), SEQUENCING_TYPE_VALUES),
                statistical_test=_statistical_tests(item.get("statistical_test")),
                mht_correction=mht if isinstance(mht, bool) else None,
            )
        )
    return comparisons


def expand_one_vs_rest(groups: Sequence[tuple[str, Sequence[str]]]) -> list[SupplementComparison]:
    """A4, deterministic: one experiment per group, ``all other groups (not X)`` (group 0) vs ``X`` (group 1), with
    the group's enriched taxa as a single ``increased`` signature. A group with no taxa yields nothing."""
    return [
        SupplementComparison(
            group_0_name=f"all other groups (not {name})",
            group_1_name=name,
            body_site=(),
            condition=(),
            taxa=tuple(NamedTaxon(name=t, direction="increased") for t in taxa),
        )
        for name, taxa in groups
        if taxa
    ]


def _one_vs_rest_groups(response: dict[str, Any], cuts: Counter[str]) -> list[tuple[str, list[str]]]:
    """The answer's distinct, non-empty group names (first-seen order) with their taxa; a repeated name merges its taxa."""
    groups: dict[str, list[str]] = {}
    raw = response.get("groups")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = _name(item.get("name"), cuts)
        if not name:
            continue
        if name not in groups and len(groups) >= _MAX_COMPARISONS_PER_UNIT:
            cuts["comparisons"] += 1
            continue
        names = groups.setdefault(name, [])
        raw_taxa = item.get("taxa")
        for t in raw_taxa if isinstance(raw_taxa, list) else []:
            taxon = _name(t.get("name") if isinstance(t, dict) else t, cuts)
            if not taxon or taxon in names:
                continue
            if len(names) >= _MAX_TAXA_PER_COMPARISON:
                cuts["taxa"] += 1
                continue
            names.append(taxon)
    return list(groups.items())


def extract_comparisons(
    unit: SupplementUnit,
    *,
    model: Model,
    study_title: str = "",
    one_vs_rest: bool = False,
    notes: ExtractionNotes | None = None,
) -> list[SupplementComparison]:
    """One generative call (stage ``supplement_extract``) for a routed unit.

    Two-group prompt by default; `one_vs_rest=True` (only for a unit the screen labelled
    :data:`ONE_VS_REST`) asks for each group's enriched taxa and expands them in code
    (:func:`expand_one_vs_rest`) -- but only when the answer has at least :data:`MIN_ONE_VS_REST_GROUPS`
    distinct groups; with fewer the label is rejected (recorded in `notes`) and the unit is read again with the
    two-group prompt. The answer is capped (:data:`_MAX_COMPARISONS_PER_UNIT` comparisons,
    :data:`_MAX_TAXA_PER_COMPARISON` taxa each, :data:`_MAX_NAME_CHARS`-character names) and the cuts are
    appended to `notes.cuts`. Raises :class:`~bugsigdb_curation.curator.model.ModelError` for an unparseable
    response or a failed call; a parseable but oddly-shaped one yields what could be read.
    """
    cuts: Counter[str] = Counter()
    response = model.complete(
        stage=EXTRACT_STAGE, messages=build_extract_messages(unit, study_title=study_title, one_vs_rest=one_vs_rest)
    )
    comparisons: list[SupplementComparison]
    if one_vs_rest:
        groups = _one_vs_rest_groups(response, cuts)
        if len(groups) >= MIN_ONE_VS_REST_GROUPS:
            comparisons = expand_one_vs_rest(groups)
        else:
            logger.bind(stage="S1b").warning(
                "one-vs-rest label rejected: fewer than three distinct groups; reading the unit as two-group",
                unit=unit.id,
                n_groups=len(groups),
            )
            if notes is not None:
                notes.one_vs_rest_rejected_groups = len(groups)
            cuts = Counter()
            response = model.complete(
                stage=EXTRACT_STAGE, messages=build_extract_messages(unit, study_title=study_title, one_vs_rest=False)
            )
            comparisons = _two_group_comparisons(response, cuts)
    else:
        comparisons = _two_group_comparisons(response, cuts)
    if notes is not None:
        notes.cuts.extend(_cut_notes(cuts))
    return comparisons


# --- resolution, merge, dedupe -------------------------------------------------------------------------

ExperimentRecord = tuple[ExperimentFields, list[ExtractedSignature], str | None]


def _source_context(fields: ExperimentFields, provenance: str) -> str:
    parts = [
        f"{label}: {', '.join(value) if isinstance(value, tuple) else value}"
        for label, value in (("body_site", fields.body_site), ("condition", fields.condition), ("host_species", fields.host_species))
        if value
    ]
    return "; ".join([*parts, f"source: {provenance}"])


#: The experiment fields a supplement comparison may state itself; the rest of S4's fields come from the supplement
#: only when stated (body_site / condition, groups) or not at all.
_INHERITABLE_FIELDS = ("host_species", "sequencing_type", "statistical_test", "mht_correction")


def _stated(value: Any) -> bool:
    return value is not None and value != ()


def inherited_field_names(comparison: SupplementComparison, defaults: ExperimentFields | None) -> list[str]:
    """The :data:`_INHERITABLE_FIELDS` the comparison did not state and that `defaults` (main experiment 0) supplies."""
    if defaults is None:
        return []
    return [
        name
        for name in _INHERITABLE_FIELDS
        if not _stated(getattr(comparison, name)) and _stated(getattr(defaults, name))
    ]


async def resolve_comparison(
    comparison: SupplementComparison,
    unit: SupplementUnit,
    *,
    defaults: ExperimentFields | None,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    client: httpx.AsyncClient,
) -> ExperimentRecord:
    """S6 on one comparison's names via `reconcile_names`, as an experiment record sourced from the unit.

    Host / sequencing / test / correction fields come from the comparison when the supplement stated them, else
    from `defaults` (the main text's S4 experiment 0; see :func:`inherited_field_names`); body_site / condition
    only if the supplement stated them.
    """

    def pick(name: str) -> Any:
        own = getattr(comparison, name)
        return own if _stated(own) or defaults is None else getattr(defaults, name)

    fields = ExperimentFields(
        host_species=pick("host_species"),
        body_site=comparison.body_site,
        condition=comparison.condition,
        group_0_name=comparison.group_0_name,
        group_1_name=comparison.group_1_name,
        sequencing_type=pick("sequencing_type"),
        statistical_test=pick("statistical_test"),
        mht_correction=pick("mht_correction"),
    )
    signatures = await reconcile_names(
        list(comparison.taxa),
        model=model,
        resolver=resolver,
        client=client,
        source_context=_source_context(fields, unit.provenance),
    )
    return fields, signatures, unit.provenance


def _taxon_keys(signature: ExtractedSignature) -> frozenset[str]:
    """A signature's taxa as comparable keys: the NCBI id when resolved (so synonyms agree), else the normalized name."""
    return frozenset(
        f"ncbi:{t.ncbi_id}" if t.ncbi_id is not None else normalize_taxon_name(t.taxon_name) for t in signature.taxa
    )


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard overlap of two taxon sets, or 0.0 when either has fewer than :data:`DUPLICATE_MIN_TAXA` taxa."""
    if min(len(a), len(b)) < DUPLICATE_MIN_TAXA:
        return 0.0
    return len(a & b) / len(a | b)


def drop_duplicate_experiments(
    supplement: Sequence[ExperimentRecord], main: Sequence[ExperimentRecord]
) -> tuple[list[ExperimentRecord], list[dict[str, Any]]]:
    """Drop supplement signatures the main text -- or an earlier supplement experiment -- already reports.

    Decided per signature: one is a duplicate when it overlaps a signature *of the same direction* in the main text
    or in an earlier *kept* supplement experiment with Jaccard >= :data:`DUPLICATE_JACCARD` over resolved taxa, both
    sets having at least :data:`DUPLICATE_MIN_TAXA` taxa. A duplicate signature is removed; the experiment is
    dropped only when every one of its signatures was. Returns ``(kept, dropped)``; each `dropped` entry (one per
    dropped signature) names the source, groups, direction, the matched main experiment index (None for a
    supplement match, which is then named in ``matched_supplement``), the overlap and whether the whole
    experiment went (``experiment_dropped``). A kept experiment keeps its original `ExperimentFields` object.
    """
    pool: list[tuple[dict[str, Any], str, frozenset[str]]] = [
        ({"main_experiment_index": index}, sig.direction, _taxon_keys(sig))
        for index, (_, sigs, _) in enumerate(main)
        for sig in sigs
    ]
    kept: list[ExperimentRecord] = []
    dropped: list[dict[str, Any]] = []
    for fields, signatures, source in supplement:
        remaining: list[ExtractedSignature] = []
        entries: list[dict[str, Any]] = []
        for sig in signatures:
            keys = _taxon_keys(sig)
            match = next(
                (
                    (where, jaccard)
                    for where, direction, pooled in pool
                    if direction == sig.direction and (jaccard := _overlap(keys, pooled)) >= DUPLICATE_JACCARD
                ),
                None,
            )
            if match is None:
                remaining.append(sig)
                continue
            where, jaccard = match
            entries.append(
                {
                    "source": source,
                    "group_0_name": fields.group_0_name,
                    "group_1_name": fields.group_1_name,
                    "direction": sig.direction,
                    "main_experiment_index": where.get("main_experiment_index"),
                    "jaccard": round(jaccard, 3),
                    **({"matched_supplement": where["supplement"]} if "supplement" in where else {}),
                }
            )
        dropped += [{**entry, "experiment_dropped": not remaining} for entry in entries]
        if not remaining:
            continue
        kept.append((fields, remaining, source) if entries else (fields, signatures, source))
        origin = {"supplement": {"source": source, "group_0_name": fields.group_0_name, "group_1_name": fields.group_1_name}}
        pool += [(origin, sig.direction, _taxon_keys(sig)) for sig in remaining]
    return kept, dropped


# --- the lever ---------------------------------------------------------------------------------------


async def supplement_experiments(
    pmcid: str,
    *,
    client: httpx.AsyncClient,
    decision_model: DecisionModel,
    model: Model,
    resolver: NcbiTaxonomyResolver,
    study_title: str,
    main_experiments: Sequence[ExperimentRecord],
    annotations: dict[str, Any],
) -> list[ExperimentRecord]:
    """The whole lever: fetch -> units -> screen -> extract routed units -> resolve -> dedupe.

    Returns the supplement-derived experiments to append after the main-text ones. Best-effort: the expected
    failures (network, decision, bad generative response) are logged and recorded in `annotations`
    (``supplement_screen_error`` / ``supplement_extract_error`` as lists of ``{unit, error}``) and never abort the
    study; a bug in our code still propagates. Also records ``supplement_screen`` (per unit ``{id, p_da,
    content_kind, arity, routed, truncated}``), ``supplement_skipped`` (``{file, reason}``) and
    ``supplement_dropped_duplicates``, ``supplement_truncated`` (``{unit, cut}``: what was cut from a model answer),
    ``supplement_one_vs_rest_rejected`` (``{unit, n_groups}``) and ``supplement_inherited_fields`` (``{source,
    group_1_name, fields, from_main_experiment}`` per kept experiment that took host / sequencing / test /
    correction fields from main-text experiment 0).
    """
    log = logger.bind(stage="S1b")
    skipped: list[tuple[str, str]] = []
    try:
        files = await fetch_supplements(pmcid, client=client, skipped=skipped)
    except zipfile.BadZipFile as exc:
        log.warning("supplement zip is corrupt; skipping", error=repr(exc))
        files = []
        skipped.append((ZIP_SKIP_NAME, f"corrupt zip: {exc}"))
    if not files and not skipped:
        skipped.append((ZIP_SKIP_NAME, "the zip held no files"))
    units = await asyncio.to_thread(supplement_units, files, skipped=skipped)
    if len(units) > _MAX_UNITS_SCREENED:
        for filename, n_cut in Counter(u.filename for u in units[_MAX_UNITS_SCREENED:]).items():
            skipped.append((filename, f"{n_cut} unit(s) not screened (limit {_MAX_UNITS_SCREENED} units per study)"))
        units = units[:_MAX_UNITS_SCREENED]
    if skipped:
        annotations["supplement_skipped"] = [{"file": name, "reason": reason} for name, reason in skipped]
    if not units:
        return []

    try:
        screened = await screen_units(units, decision_model)
    except SupplementScreenError as exc:
        log.warning("supplement screening failed; skipping the supplements", unit=exc.unit_id, error=repr(exc.__cause__))
        annotations["supplement_screen_error"] = [{"unit": exc.unit_id, "error": repr(exc.__cause__)}]
        return []
    annotations["supplement_screen"] = [s.annotation() for s in screened]
    routed = [s for s in screened if s.routed]
    n_one_vs_rest = sum(s.arity == ONE_VS_REST for s in routed)
    log.info("supplements screened", n_units=len(units), n_routed=len(routed), n_one_vs_rest=n_one_vs_rest)

    errors: list[dict[str, str]] = []
    unit_notes: dict[str, ExtractionNotes] = {}
    sem = asyncio.Semaphore(_EXTRACT_CONCURRENCY)

    async def extract(s: ScreenedUnit) -> list[SupplementComparison]:
        async with sem:
            notes = ExtractionNotes()
            try:
                # `Model.complete` is sync; threads keep the loop (and the other units) moving.
                comparisons = await asyncio.to_thread(
                    extract_comparisons,
                    s.unit,
                    model=model,
                    study_title=study_title,
                    one_vs_rest=s.arity == ONE_VS_REST,
                    notes=notes,
                )
                unit_notes[s.unit.id] = notes
                return comparisons
            except _EXPECTED_ERRORS as exc:
                log.warning("supplement extraction failed; skipping the unit", unit=s.unit.id, error=repr(exc))
                errors.append({"unit": s.unit.id, "error": repr(exc)})
                return []

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(extract(s)) for s in routed]
    except ExceptionGroup as group_error:
        raise unwrap_fan_out_failure(group_error, ()) from None

    truncated = [{"unit": unit_id, "cut": cut} for unit_id, notes in unit_notes.items() for cut in notes.cuts]
    if truncated:
        annotations["supplement_truncated"] = truncated
    rejected = [
        {"unit": unit_id, "n_groups": notes.one_vs_rest_rejected_groups}
        for unit_id, notes in unit_notes.items()
        if notes.one_vs_rest_rejected_groups is not None
    ]
    if rejected:
        annotations["supplement_one_vs_rest_rejected"] = rejected

    defaults = main_experiments[0][0] if main_experiments else None
    extracted: list[ExperimentRecord] = []
    inherited: dict[int, dict[str, Any]] = {}  # by id() of the record's fields (kept by dedupe), so only kept experiments report
    for s, task in zip(routed, tasks):
        try:
            unit_records = []
            for c in task.result():
                record = await resolve_comparison(
                    c, s.unit, defaults=defaults, model=model, resolver=resolver, client=client
                )
                unit_records.append(record)
                if names := inherited_field_names(c, defaults):
                    inherited[id(record[0])] = {
                        "source": s.unit.provenance,
                        "group_1_name": c.group_1_name,
                        "fields": names,
                        "from_main_experiment": 0,
                    }
            extracted += unit_records
        except _EXPECTED_ERRORS as exc:
            log.warning("supplement name resolution failed; skipping the unit", unit=s.unit.id, error=repr(exc))
            errors.append({"unit": s.unit.id, "error": repr(exc)})
    if errors:
        annotations["supplement_extract_error"] = errors

    kept, dropped = drop_duplicate_experiments(extracted, main_experiments)
    if kept_inherited := [inherited[id(record[0])] for record in kept if id(record[0]) in inherited]:
        annotations["supplement_inherited_fields"] = kept_inherited
    if dropped:
        annotations["supplement_dropped_duplicates"] = dropped
    log.info(
        "supplement experiments merged",
        n_extracted=len(extracted),
        n_dropped_duplicates=len(dropped),
        n_kept=len(kept),
    )
    return kept
