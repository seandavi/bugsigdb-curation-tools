"""Tests for `curator.supplement_lever`: supplement units, screening (A3), extraction + one-vs-rest expansion (A4),
merge/dedupe, and the pipeline/CLI wiring. Fully offline."""

from __future__ import annotations

import asyncio
import io
import random

import docx
import httpx
import openpyxl
import pymupdf
import pytest

from bugsigdb_curation.curator.supplement_lever import (
    MAX_IMAGE_BYTES,
    ONE_VS_REST,
    SCREEN_QUESTIONS,
    SCREEN_THRESHOLD,
    ScreenedUnit,
    SupplementScreenError,
    SupplementUnit,
    screen_state,
    screen_units,
    supplement_units,
)
from bugsigdb_curation.decision import (
    Choice,
    ChoiceAnswer,
    DecisionModelError,
    MockDecisionModel,
    Noul,
    NoulAnswer,
)
from bugsigdb_curation.supplements import SupplementFile

# --- fixtures: tiny in-memory supplement files --------------------------------------------------------


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx(*paragraphs: str) -> bytes:
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _pdf(pages: list[str | None]) -> bytes:
    """A PDF with one page per entry: text pages carry that text, `None` makes a text-free page with a drawing."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:
            page.draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 1), fill=(1, 0, 0))
        else:
            page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text, fontsize=9)
    return doc.tobytes()


def _file(name: str, media_type: str, data: bytes) -> SupplementFile:
    return SupplementFile(filename=name, media_type=media_type, raw_bytes=data)


LONG_TEXT = "Differentially abundant taxa between cases and controls. " * 12  # > 200 chars


# --- units --------------------------------------------------------------------------------------------


def test_xlsx_gives_one_unit_per_sheet_with_provenance():
    data = _xlsx({"DA": [["taxon", "lda"], ["Bacteroides", 4.123456]], "Meta": [["sample", "age"], ["s1", 5]]})
    units = supplement_units([_file("S2.xlsx", "xlsx", data)])
    assert [(u.id, u.label, u.kind, u.provenance) for u in units] == [
        ("S2.xlsx::DA", "DA", "sheet", "S2.xlsx :: DA"),
        ("S2.xlsx::Meta", "Meta", "sheet", "S2.xlsx :: Meta"),
    ]
    assert all(u.image is None and u.filename == "S2.xlsx" for u in units)
    assert units[0].text.splitlines() == ["taxon\tlda", "Bacteroides\t4.123"]


def test_xlsx_extraction_text_is_capped_at_about_400_rows_and_blank_sheets_are_skipped():
    rows = [["taxon", "lda"]] + [[f"t{i}", i] for i in range(1000)]
    skipped: list[tuple[str, str]] = []
    units = supplement_units([_file("big.xlsx", "xlsx", _xlsx({"big": rows, "empty": []}))], skipped=skipped)
    assert [u.label for u in units] == ["big"]
    assert len(units[0].text.splitlines()) == 400
    assert skipped == [("big.xlsx :: empty", "empty sheet")]


def test_csv_and_tsv_are_one_unit_each():
    units = supplement_units(
        [_file("a.csv", "csv", b"taxon,lda\nBacteroides,4.1\n"), _file("b.tsv", "tsv", b"taxon\tlda\nPrevotella\t3\n")]
    )
    assert [(u.id, u.kind, u.label) for u in units] == [("a.csv", "delimited", ""), ("b.tsv", "delimited", "")]
    assert units[0].provenance == "a.csv"
    assert units[0].text.splitlines() == ["taxon\tlda", "Bacteroides\t4.1"]
    assert units[1].text.splitlines() == ["taxon\tlda", "Prevotella\t3"]


def test_docx_is_one_unit_truncated_to_about_12k_chars():
    (unit,) = supplement_units([_file("m.docx", "docx", _docx("x" * 20_000))])
    assert unit.kind == "docx" and unit.id == "m.docx" and len(unit.text) == 12_000


def test_pdf_pages_with_text_are_text_units_and_text_free_pages_are_size_capped_jpegs():
    pdf = _pdf([LONG_TEXT, None, "tiny"])
    units = supplement_units([_file("S1.pdf", "pdf", pdf)])
    assert [(u.id, u.kind, u.page) for u in units] == [
        ("S1.pdf::page 1", "pdf_text", 1),
        ("S1.pdf::page 2", "pdf_image", 2),
        ("S1.pdf::page 3", "pdf_image", 3),  # < 200 chars of text: not enough to read
    ]
    assert units[0].image is None and "Differentially abundant" in units[0].text
    for unit in units[1:]:
        assert unit.image is not None and unit.image.startswith(b"\xff\xd8\xff")  # JPEG
        assert len(unit.image) <= MAX_IMAGE_BYTES
    assert units[2].text.strip() == "tiny"  # the sparse text stays available to the prompt


def test_pdf_page_image_is_re_encoded_at_lower_quality_until_it_fits():
    # a noisy page that does not fit 200 KB at the first JPEG quality
    doc = pymupdf.open()
    page = doc.new_page()
    rng = random.Random(0)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 400), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    for _ in range(60000):
        x, y = rng.randrange(400), rng.randrange(400)
        pix.set_pixel(x, y, tuple(rng.randrange(256) for _ in range(3)))
    page.insert_image(page.rect, pixmap=pix)
    (unit,) = supplement_units([_file("noise.pdf", "pdf", doc.tobytes())])
    assert unit.image is not None and len(unit.image) <= MAX_IMAGE_BYTES


def test_images_other_and_unreadable_files_are_skipped_with_a_reason():
    skipped: list[tuple[str, str]] = []
    units = supplement_units(
        [
            _file("fig.png", "image", b"png"),
            _file("data.bin", "other", b"?"),
            _file("legacy.xls", "xlsx", b"not really an xlsx"),
            _file("broken.pdf", "pdf", b"%PDF-garbage"),
            _file("ok.csv", "csv", b"a,b\n1,2\n"),
        ],
        skipped=skipped,
    )
    assert [u.id for u in units] == ["ok.csv"]
    assert [name for name, _ in skipped] == ["fig.png", "data.bin", "legacy.xls", "broken.pdf"]
    reasons = dict(skipped)
    assert "not read" in reasons["fig.png"] and "unreadable" in reasons["legacy.xls"]


def test_unit_is_a_frozen_dataclass():
    unit = SupplementUnit(id="a", filename="a.csv", label="", kind="delimited", text="x", image=None)
    try:
        unit.text = "y"  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("SupplementUnit must be frozen")


# --- screening (A3) -----------------------------------------------------------------------------------

def _unit(uid: str, text: str = "a\tb", kind="sheet", label="S", image=None, page=None) -> SupplementUnit:
    return SupplementUnit(uid, f"{uid}.xlsx", label, kind, text, image, page)


def _answers(p_da: float, kind: str = "da_taxa_table", arity: str = "two_group"):
    return {
        "has_da_results": NoulAnswer(p_da),
        "content_kind": ChoiceAnswer(kind, {kind: 0.9}, 0.9),
        "arity": ChoiceAnswer(arity, {arity: 0.9}, 0.9),
    }


def test_the_three_probe_questions_are_asked_with_the_probe_options():
    assert set(SCREEN_QUESTIONS) == {"has_da_results", "content_kind", "arity"}
    assert isinstance(SCREEN_QUESTIONS["has_da_results"], Noul)
    assert set(SCREEN_QUESTIONS["content_kind"].criteria) == {
        "da_taxa_table", "abundance_matrix", "diversity_or_ordination_stats", "sample_metadata",
        "methods_text", "raw_or_sequence", "figure_or_image", "other",
    }
    assert isinstance(SCREEN_QUESTIONS["arity"], Choice)
    assert set(SCREEN_QUESTIONS["arity"].criteria) == {
        "two_group", "multi_group_one_vs_rest", "multi_group_all_pairwise", "not_a_comparison",
    }
    assert ONE_VS_REST == "multi_group_one_vs_rest" and SCREEN_THRESHOLD == 0.5


def test_screen_routes_iff_p_da_reaches_the_threshold_and_keeps_input_order():
    units = [_unit("lo"), _unit("edge"), _unit("hi", ), _unit("ovr")]
    p = {"lo.xlsx": 0.49, "edge.xlsx": 0.5, "hi.xlsx": 0.97, "ovr.xlsx": 0.9}
    mock = MockDecisionModel(
        {"s1b_screen": lambda state, qs: _answers(p[state["file"]], arity=ONE_VS_REST if state["file"] == "ovr.xlsx" else "two_group")}
    )
    screened = asyncio.run(screen_units(units, mock))
    assert [(s.unit.id, s.routed) for s in screened] == [("lo", False), ("edge", True), ("hi", True), ("ovr", True)]
    assert screened[3].arity == ONE_VS_REST and screened[3].content_kind == "da_taxa_table"
    assert screened[0].annotation() == {
        "id": "lo", "p_da": 0.49, "content_kind": "da_taxa_table", "arity": "two_group", "routed": False,
    }
    assert isinstance(screened[0], ScreenedUnit)


def test_screen_state_is_header_plus_30_rows_with_cells_cut_to_60_chars_and_15_columns():
    rows = ["\t".join(["x" * 100] * 20)] + [f"r{i}\t1" for i in range(100)]
    state = screen_state(_unit("a", "\n".join(rows)))
    assert state["file"] == "a.xlsx" and state["sheet"] == "S"
    assert len(state["first_rows"]) == 30
    assert len(state["first_rows"][0]) == 15 and all(len(c) == 60 for c in state["first_rows"][0])
    assert screen_state(_unit("c", "a\tb", kind="delimited", label="")).keys() == {"file", "first_rows"}


def test_screen_state_for_pages_and_documents():
    text_page = screen_state(_unit("p", "hello", kind="pdf_text", label="page 4", page=4))
    assert text_page == {"file": "p.xlsx", "page": 4, "page_text": "hello"}
    image_page = screen_state(_unit("p", "", kind="pdf_image", label="page 5", image=b"\xff\xd8\xff", page=5))
    assert image_page == {"file": "p.xlsx", "page": 5}
    assert screen_state(_unit("d", "words", kind="docx", label=""))["text"] == "words"


def test_image_units_are_sent_with_their_jpeg_and_text_units_without():
    mock = MockDecisionModel({"s1b_screen": lambda s, q: _answers(0.1)})
    jpeg = b"\xff\xd8\xff-fake"
    asyncio.run(screen_units([_unit("t"), _unit("i", "", kind="pdf_image", label="page 1", image=jpeg, page=1)], mock))
    by_file = {c["state"]["file"]: c for c in mock.calls}
    assert by_file["t.xlsx"]["images"] == [] and by_file["i.xlsx"]["images"] == [jpeg]
    assert all(c["stage"] == "s1b_screen" for c in mock.calls)


def test_screen_concurrency_is_bounded_at_six():
    live = peak = 0

    class Slow:
        async def decide(self, **_):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return _answers(0.1)

    asyncio.run(screen_units([_unit(f"u{i}") for i in range(20)], Slow()))  # type: ignore[arg-type]
    assert peak == 6


def test_screen_failure_names_the_unit_keeps_the_cause_and_cancels_siblings():
    cancelled: list[str] = []

    class Mixed:
        async def decide(self, *, state, **_):
            if state["file"] == "bad.xlsx":
                raise DecisionModelError("boom")
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(state["file"])
                raise
            return _answers(0.1)

    with pytest.raises(SupplementScreenError) as info:
        asyncio.run(screen_units([_unit("bad"), _unit("x"), _unit("y")], Mixed()))  # type: ignore[arg-type]
    assert info.value.unit_id == "bad" and isinstance(info.value.__cause__, DecisionModelError)
    assert sorted(cancelled) == ["x.xlsx", "y.xlsx"]


def test_screen_transport_errors_are_absorbable_too():
    class Down:
        async def decide(self, **_):
            raise httpx.ConnectError("no route")

    with pytest.raises(SupplementScreenError) as info:
        asyncio.run(screen_units([_unit("a")], Down()))  # type: ignore[arg-type]
    assert "ConnectError" in repr(info.value.__cause__)


def test_screen_bug_is_not_masked_by_a_sibling_routine_failure():
    class Mixed:
        async def decide(self, *, state, **_):
            if state["file"] == "bad.xlsx":
                raise DecisionModelError("boom")
            return {}  # missing answers -> KeyError: a bug

    with pytest.raises(KeyError):
        asyncio.run(screen_units([_unit("bad"), _unit("x")], Mixed()))  # type: ignore[arg-type]


def test_screen_of_no_units_makes_no_calls():
    mock = MockDecisionModel()
    assert asyncio.run(screen_units([], mock)) == []
    assert mock.calls == []
