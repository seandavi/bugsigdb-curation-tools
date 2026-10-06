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
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from bugsigdb_curation.curator.routing import DECISION_CALL_ERRORS, unwrap_fan_out_failure
from bugsigdb_curation.decision import Choice, ChoiceAnswer, DecisionModel, Noul, NoulAnswer
from bugsigdb_curation.supplements import SupplementFile, supplement_to_text

SCREEN_STAGE = "s1b_screen"
EXTRACT_STAGE = "supplement_extract"

#: A unit is routed to the strong model iff p(has_da_results) is at least this (probe: recall 1.0 at precision
#: 0.77-1.0 on pages/sheets with clef -- cheap to over-include, costly to miss).
SCREEN_THRESHOLD = 0.5
#: The only arity answer that switches a unit to the one-vs-rest prompt + deterministic expansion
#: (probe: 3/3 detected, 0 false positives -- but only 3 positives, so every use is logged).
ONE_VS_REST = "multi_group_one_vs_rest"

#: Concurrent decision calls while screening / generative calls while extracting.
_SCREEN_CONCURRENCY = 6
_EXTRACT_CONCURRENCY = 4

#: Screening state (probe setting): the header plus the first rows, cells cut to 60 chars, first 15 columns.
_SCREEN_ROWS = 30
_SCREEN_CELL_CHARS = 60
_SCREEN_COLUMNS = 15
#: Extraction text for a sheet/csv: up to this many rows, cells cut to 200 chars, first 40 columns.
_EXTRACT_ROWS = 400
_EXTRACT_CELL_CHARS = 200
_EXTRACT_COLUMNS = 40
#: Characters of docx / PDF-page text sent to a model.
_TEXT_CHARS = 12_000
#: A PDF page with at least this much extractable text is read as text; otherwise it is rendered.
_PDF_TEXT_MIN_CHARS = 200
#: Pages read per PDF (guards a pathological several-hundred-page file; the rest are recorded as skipped).
_MAX_PDF_PAGES = 200
#: The Workers AI pre-flight token estimate scales with the *encoded* image size (probe: a 0.8 MB PNG page was
#: rejected as ~274k tokens against a 65k context; <= 200 KB JPEGs averaged ~1.6k), so pages are JPEGs under this.
MAX_IMAGE_BYTES = 200_000
#: (dpi, JPEG quality) attempts in order: the probe's 100 dpi at decreasing quality, then a lower resolution for
#: a pathological (photographic / noisy) page that still does not fit.
_RENDER_ATTEMPTS = ((100, 80), (100, 65), (100, 50), (100, 35), (72, 35), (50, 35))

UnitKind = Literal["sheet", "delimited", "docx", "pdf_text", "pdf_image"]


@dataclass(frozen=True, slots=True)
class SupplementUnit:
    """One screenable, extractable piece of a supplementary file.

    `label` is the sheet name / ``"page N"`` (empty for a whole-file csv/docx); `text` is the extraction text
    (tab-joined rows for tables, plain text for documents, the sparse page text for an image page);
    `image` is the rendered JPEG of a text-free PDF page.
    """

    id: str
    filename: str
    label: str
    kind: UnitKind
    text: str
    image: bytes | None
    page: int | None = None

    @property
    def provenance(self) -> str:
        """``"<filename> :: <sheet or page N>"`` -- the signature ``source`` of everything extracted from it."""
        return f"{self.filename} :: {self.label}" if self.label else self.filename


# --- units ---------------------------------------------------------------------------------------------


def _cell(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = f"{value:.4g}" if isinstance(value, float) else str(value)
    return re.sub(r"[\t\r\n]+", " ", text).strip()[:limit]


def _rows_to_text(rows: Any) -> str:
    """Tab-joined, blank-row-free text of up to :data:`_EXTRACT_ROWS` rows."""
    lines: list[str] = []
    for row in rows:
        cells = [_cell(c, _EXTRACT_CELL_CHARS) for c in list(row)[:_EXTRACT_COLUMNS]]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        lines.append("\t".join(cells))
        if len(lines) >= _EXTRACT_ROWS:
            break
    return "\n".join(lines)


def _xlsx_units(f: SupplementFile, skipped: list[tuple[str, str]]) -> list[SupplementUnit]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(f.raw_bytes), read_only=True, data_only=True)
    try:
        units = []
        for sheet in workbook.worksheets:
            text = _rows_to_text(sheet.iter_rows(values_only=True, max_row=_EXTRACT_ROWS * 4))
            if not text:
                skipped.append((f"{f.filename} :: {sheet.title}", "empty sheet"))
                continue
            units.append(
                SupplementUnit(f"{f.filename}::{sheet.title}", f.filename, sheet.title, "sheet", text, None)
            )
        return units
    finally:
        workbook.close()


def _delimited_unit(f: SupplementFile) -> list[SupplementUnit]:
    decoded = f.raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(decoded), delimiter="\t" if f.media_type == "tsv" else ",")
    text = _rows_to_text(reader)
    return [SupplementUnit(f.filename, f.filename, "", "delimited", text, None)] if text else []


def _render_page(page: Any) -> bytes:
    for dpi, quality in _RENDER_ATTEMPTS:
        data = page.get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=quality)
        if len(data) <= MAX_IMAGE_BYTES:
            return data
    return data


def _pdf_units(f: SupplementFile, skipped: list[tuple[str, str]]) -> list[SupplementUnit]:
    import pymupdf

    units = []
    with pymupdf.open(stream=f.raw_bytes, filetype="pdf") as doc:
        if doc.page_count > _MAX_PDF_PAGES:
            skipped.append((f.filename, f"pages beyond {_MAX_PDF_PAGES} not read ({doc.page_count} pages)"))
        for number, page in enumerate(doc, 1):
            if number > _MAX_PDF_PAGES:
                break
            label, unit_id = f"page {number}", f"{f.filename}::page {number}"
            text = page.get_text().strip()
            if len(text) >= _PDF_TEXT_MIN_CHARS:
                units.append(SupplementUnit(unit_id, f.filename, label, "pdf_text", text[:_TEXT_CHARS], None, number))
            else:
                units.append(SupplementUnit(unit_id, f.filename, label, "pdf_image", text, _render_page(page), number))
    return units


def supplement_units(
    files: Sequence[SupplementFile], *, skipped: list[tuple[str, str]] | None = None
) -> list[SupplementUnit]:
    """Split unpacked supplementary files into units: xlsx -> one per sheet, csv/tsv and docx -> one, PDF -> one per page.

    A PDF page with at least 200 characters of extractable text is a text unit; a text-free page is rendered to a
    JPEG of at most :data:`MAX_IMAGE_BYTES`. Images and other file types are not read; an unreadable or empty
    file/sheet is skipped too. Every skip is appended to `skipped` (when given) as ``(file, reason)``.
    """
    notes: list[tuple[str, str]] = skipped if skipped is not None else []
    units: list[SupplementUnit] = []
    for f in files:
        try:
            if f.media_type == "xlsx":
                units += _xlsx_units(f, notes)
            elif f.media_type in ("csv", "tsv"):
                units += _delimited_unit(f)
            elif f.media_type == "docx":
                text = supplement_to_text(f)
                if text is None:
                    notes.append((f.filename, "unreadable docx"))
                elif text.strip():
                    units.append(SupplementUnit(f.filename, f.filename, "", "docx", text.strip()[:_TEXT_CHARS], None))
            elif f.media_type == "pdf":
                units += _pdf_units(f, notes)
            else:
                notes.append((f.filename, f"{f.media_type} file not read"))
        except Exception as exc:  # noqa: BLE001 -- a malformed third-party file must not abort the study
            logger.bind(stage="S1b").warning("unreadable supplement file", filename=f.filename, error=repr(exc))
            notes.append((f.filename, f"unreadable: {exc!r}"))
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

