"""Tests for `curator.supplement_lever`: supplement units, screening (A3), extraction + one-vs-rest expansion (A4),
merge/dedupe, and the pipeline/CLI wiring. Fully offline."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import random
import re
import zipfile

import docx
import httpx
import openpyxl
import pymupdf
import pytest
import test_curator_pipeline_e2e as e2e
from typer.testing import CliRunner

import bugsigdb_curation.cli as cli_module
from bugsigdb_curation.cli import app
from bugsigdb_curation.curator.experiment import ExperimentFields
from bugsigdb_curation.curator.model import MockModel, ModelCallError, ModelError
from bugsigdb_curation.curator.ner import NamedTaxon
from bugsigdb_curation.curator.pipeline import CurationResult, curate_async
from bugsigdb_curation.curator.signature import ExtractedSignature, ExtractedTaxon
from bugsigdb_curation.curator.supplement_lever import (
    MAX_IMAGE_BYTES,
    ONE_VS_REST,
    SCREEN_QUESTIONS,
    SCREEN_THRESHOLD,
    ExtractionNotes,
    ScreenedUnit,
    SupplementComparison,
    SupplementScreenError,
    SupplementUnit,
    build_extract_messages,
    drop_duplicate_experiments,
    expand_one_vs_rest,
    extract_comparisons,
    inherited_field_names,
    resolve_comparison,
    screen_state,
    screen_units,
    supplement_experiments,
    supplement_units,
)
from bugsigdb_curation.curator.taxonomy import NcbiTaxonomyResolver
from bugsigdb_curation.decision import (
    Choice,
    ChoiceAnswer,
    DecisionModelError,
    MockDecisionModel,
    Noul,
    NoulAnswer,
)
from bugsigdb_curation.supplements import EUROPEPMC_SUPPLEMENTARY_FILES_URL, SupplementFile

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


def _noisy_pdf(*, side: int = 400, points: int = 60000, page_size: tuple[float, float] | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=page_size[0], height=page_size[1]) if page_size else doc.new_page()
    rng = random.Random(0)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, side, side), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    for _ in range(points):
        x, y = rng.randrange(side), rng.randrange(side)
        pix.set_pixel(x, y, tuple(rng.randrange(256) for _ in range(3)))
    page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


def _spy_on_rendering(monkeypatch) -> list[int]:
    """Encoded size of every JPEG a page render produces, in order (one entry per `get_pixmap` call)."""
    sizes: list[int] = []
    real_get_pixmap = pymupdf.Page.get_pixmap

    def get_pixmap(self, *args, **kwargs):
        pixmap = real_get_pixmap(self, *args, **kwargs)
        real_tobytes = pixmap.tobytes

        def tobytes(*a, **kw):
            data = real_tobytes(*a, **kw)
            sizes.append(len(data))
            return data

        return type("Spy", (), {"tobytes": staticmethod(tobytes), "__getattr__": lambda _, n: getattr(pixmap, n)})()

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", get_pixmap)
    return sizes


def test_pdf_page_image_is_re_encoded_at_lower_quality_until_it_fits(monkeypatch):
    # a noisy page that does not fit 200 KB at the first JPEG quality
    sizes = _spy_on_rendering(monkeypatch)
    (unit,) = supplement_units([_file("noise.pdf", "pdf", _noisy_pdf())])
    assert unit.image is not None and len(unit.image) <= MAX_IMAGE_BYTES
    assert len(sizes) >= 2 and sizes[0] > MAX_IMAGE_BYTES  # the first attempt was too big, so it was re-encoded
    assert sizes[-1] == len(unit.image) <= MAX_IMAGE_BYTES and all(size > MAX_IMAGE_BYTES for size in sizes[:-1])


def test_a_huge_pdf_page_is_rendered_with_its_longest_side_within_about_2000_px():
    doc = pymupdf.open()
    doc.new_page(width=6000, height=3000).draw_rect(pymupdf.Rect(50, 50, 500, 500), fill=(1, 0, 0))
    (unit,) = supplement_units([_file("poster.pdf", "pdf", doc.tobytes())])
    assert unit.image is not None
    rendered = pymupdf.Pixmap(unit.image)
    assert max(rendered.width, rendered.height) <= 2000 + 1 and rendered.width > rendered.height
    letter = pymupdf.open()
    letter.new_page().draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(1, 0, 0))
    (small,) = supplement_units([_file("a.pdf", "pdf", letter.tobytes())])
    assert small.image is not None and abs(pymupdf.Pixmap(small.image).height - 842 * 100 / 72) <= 2  # 100 dpi unchanged


def test_a_page_image_that_never_fits_is_skipped_with_a_reason_not_sent(monkeypatch):
    monkeypatch.setattr("bugsigdb_curation.curator.supplement_lever.MAX_IMAGE_BYTES", 500)
    skipped: list[tuple[str, str]] = []
    units = supplement_units(
        [_file("noise.pdf", "pdf", _noisy_pdf()), _file("ok.csv", "csv", b"a,b\n1,2\n")], skipped=skipped
    )
    assert [u.id for u in units] == ["ok.csv"]  # nothing oversized goes on to the screen
    ((name, reason),) = skipped
    assert name == "noise.pdf :: page 1" and "page image over 500 bytes" in reason


def test_images_other_legacy_and_unreadable_files_are_skipped_with_a_reason():
    skipped: list[tuple[str, str]] = []
    units = supplement_units(
        [
            _file("fig.png", "image", b"png"),
            _file("data.bin", "other", b"?"),
            _file("legacy.xls", "xlsx", b"\xd0\xcf\x11\xe0 old binary"),
            _file("notes.doc", "docx", b"\xd0\xcf\x11\xe0 old binary"),
            _file("broken.xlsx", "xlsx", b"not really an xlsx"),
            _file("broken.pdf", "pdf", b"%PDF-garbage"),
            _file("ok.csv", "csv", b"a,b\n1,2\n"),
        ],
        skipped=skipped,
    )
    assert [u.id for u in units] == ["ok.csv"]
    assert [name for name, _ in skipped] == ["fig.png", "data.bin", "legacy.xls", "notes.doc", "broken.xlsx", "broken.pdf"]
    reasons = dict(skipped)
    assert "not read" in reasons["fig.png"]
    assert "legacy .xls format not supported" in reasons["legacy.xls"]
    assert "legacy .doc format not supported" in reasons["notes.doc"]
    assert reasons["broken.xlsx"].startswith("unreadable:") and reasons["broken.pdf"].startswith("unreadable:")


def test_a_bug_in_our_own_unit_code_surfaces_instead_of_being_recorded_as_unreadable(monkeypatch):
    def buggy(rows):
        raise ZeroDivisionError("our bug")

    monkeypatch.setattr("bugsigdb_curation.curator.supplement_lever._rows_to_text", buggy)
    with pytest.raises(ZeroDivisionError):
        supplement_units([_file("a.csv", "csv", b"a,b\n1,2\n")])
    with pytest.raises(ZeroDivisionError):
        supplement_units([_file("a.xlsx", "xlsx", _xlsx({"s": [["a"]]}))])


def test_an_unreadable_sheet_or_page_costs_only_that_part(monkeypatch):
    real_get_text = pymupdf.Page.get_text

    def get_text(self, *args, **kwargs):
        if self.number == 1:
            raise RuntimeError("bad page")
        return real_get_text(self, *args, **kwargs)

    monkeypatch.setattr(pymupdf.Page, "get_text", get_text)
    skipped: list[tuple[str, str]] = []
    units = supplement_units([_file("S.pdf", "pdf", _pdf([LONG_TEXT, LONG_TEXT, LONG_TEXT]))], skipped=skipped)
    assert [u.id for u in units] == ["S.pdf::page 1", "S.pdf::page 3"]
    assert [name for name, _ in skipped] == ["S.pdf :: page 2"] and "bad page" in skipped[0][1]


def test_extraction_text_is_capped_in_total_characters_and_flagged_truncated():
    # 400 rows x 40 columns x 200 characters is ~3 MB: far more than a model context should receive
    wide = [["x" * 200] * 40 for _ in range(400)]
    (unit,) = supplement_units([_file("wide.xlsx", "xlsx", _xlsx({"wide": wide}))])
    assert 0 < len(unit.text) <= 60_000 and unit.truncated
    assert unit.text.splitlines()[0].count("\t") == 39


def test_truncated_marks_rows_columns_cells_and_document_characters_that_were_cut():
    def truncated(rows: list[list], name: str = "t") -> bool:
        return supplement_units([_file(f"{name}.xlsx", "xlsx", _xlsx({"s": rows}))])[0].truncated

    assert not truncated([["taxon", "lda"]] + [[f"t{i}", i] for i in range(399)])  # 400 rows fit exactly
    assert truncated([["taxon", "lda"]] + [[f"t{i}", i] for i in range(400)])  # the 401st row is cut
    assert not truncated([["c"] * 40])
    assert truncated([["c"] * 41])  # a 41st column is cut
    assert not truncated([["x" * 200]])
    assert truncated([["x" * 201]])
    csv_rows = "".join(f"t{i},{i}\n" for i in range(500)).encode()
    assert supplement_units([_file("big.csv", "csv", csv_rows)])[0].truncated
    assert not supplement_units([_file("small.csv", "csv", b"a,b\n1,2\n")])[0].truncated
    assert supplement_units([_file("m.docx", "docx", _docx("x" * 20_000))])[0].truncated
    assert not supplement_units([_file("m.docx", "docx", _docx("short"))])[0].truncated
    dense = pymupdf.open()
    dense.new_page().insert_textbox(pymupdf.Rect(20, 20, 580, 820), "word " * 4000, fontsize=3)  # ~20k characters
    long_page, short_page = dense.tobytes(), _pdf([LONG_TEXT])
    assert supplement_units([_file("l.pdf", "pdf", long_page)])[0].truncated
    assert not supplement_units([_file("s.pdf", "pdf", short_page)])[0].truncated


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
        "truncated": False,
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


# --- extraction prompts and parsing -------------------------------------------------------------------


def _prompt(messages) -> str:
    return messages[0]["content"][0]["text"]


def test_two_group_prompt_carries_the_group_convention_and_the_unit_text():
    unit = _unit("a", "taxon\tlda\nBacteroides\t4.1")
    text = _prompt(build_extract_messages(unit, study_title="A gut study", one_vs_rest=False))
    assert "REFERENCE group" in text and "CASE group" in text and "INCREASED means more abundant in Group 1" in text
    assert '"comparisons"' in text and "group_0_name" in text and "do NOT propose" in text
    assert "A gut study" in text and "a.xlsx :: S" in text and "Bacteroides\t4.1" in text
    assert "ENRICHED" not in text


def test_one_vs_rest_prompt_asks_for_each_groups_enriched_taxa():
    text = _prompt(build_extract_messages(_unit("a"), study_title="", one_vs_rest=True))
    assert "ENRICHED in that group versus all other groups" in text and '"groups"' in text
    assert "comparisons" not in text


def test_image_units_send_the_page_image_alongside_the_text():
    jpeg = b"\xff\xd8\xff-fake"
    unit = _unit("p", "caption words", kind="pdf_image", label="page 3", image=jpeg, page=3)
    (message,) = build_extract_messages(unit, study_title="", one_vs_rest=False)
    kinds = [block["type"] for block in message["content"]]
    assert kinds == ["text", "image_url"]
    assert message["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "caption words" in message["content"][0]["text"] and "attached image" in message["content"][0]["text"]
    assert [b["type"] for b in build_extract_messages(_unit("t"), study_title="", one_vs_rest=False)[0]["content"]] == ["text"]


def test_extract_comparisons_parses_groups_sites_taxa_and_calls_the_supplement_extract_stage():
    model = MockModel(
        {
            "supplement_extract": {
                "comparisons": [
                    {
                        "group_0_name": " Healthy ",
                        "group_1_name": "Crohn",
                        "body_site": "Feces",
                        "condition": ["Crohn disease"],
                        "taxa": [
                            {"name": "Bacteroides", "direction": "Decreased"},
                            {"name": "Escherichia coli", "direction": "increased"},
                            {"name": "Bogus", "direction": "up"},
                            {"direction": "increased"},
                            "junk",
                        ],
                    },
                    {"group_0_name": "a", "group_1_name": "b", "taxa": []},
                    "junk",
                ]
            }
        }
    )
    (comparison,) = extract_comparisons(_unit("a"), model=model)
    assert model.calls[0]["stage"] == "supplement_extract"
    assert comparison == SupplementComparison(
        "Healthy", "Crohn", ("Feces",), ("Crohn disease",),
        (NamedTaxon("Bacteroides", "decreased"), NamedTaxon("Escherichia coli", "increased")),
    )


@pytest.mark.parametrize("response", [{}, {"comparisons": None}, {"comparisons": "none"}, {"groups": [{"name": "x", "taxa": ["t"]}]}])
def test_a_two_group_extraction_ignores_missing_or_oddly_shaped_answers(response):
    assert extract_comparisons(_unit("a"), model=MockModel({"supplement_extract": response})) == []


def test_unparseable_generative_response_is_a_model_error():
    def boom(messages):
        raise ModelError("malformed JSON")

    with pytest.raises(ModelError):
        extract_comparisons(_unit("a"), model=MockModel({"supplement_extract": boom}))


# --- A4: one-vs-rest expansion -------------------------------------------------------------------------


def test_expand_one_vs_rest_makes_one_experiment_per_group_with_enriched_taxa_increased():
    comparisons = expand_one_vs_rest([("Cluster A", ["T1", "T2"]), ("Cluster B", ["T3"]), ("Cluster C", [])])
    assert [(c.group_0_name, c.group_1_name) for c in comparisons] == [
        ("all other groups (not Cluster A)", "Cluster A"),
        ("all other groups (not Cluster B)", "Cluster B"),
    ]
    assert [t.name for t in comparisons[0].taxa] == ["T1", "T2"]
    assert all(t.direction == "increased" for c in comparisons for t in c.taxa)


def test_one_vs_rest_extraction_expands_in_code_and_only_when_asked():
    model = MockModel(
        {
            "supplement_extract": {
                "groups": [
                    {"name": "G1", "taxa": [{"name": "A"}, "B"]},
                    {"name": "G2", "taxa": [{"name": "C"}]},
                    {"name": "G3", "taxa": []},
                    {"taxa": []},
                ]
            }
        }
    )
    comparisons = extract_comparisons(_unit("a"), model=model, one_vs_rest=True)
    assert [c.group_1_name for c in comparisons] == ["G1", "G2"]  # G3 has no taxa: nothing to record
    assert [t.name for t in comparisons[0].taxa] == ["A", "B"]
    assert len(model.calls) == 1
    assert extract_comparisons(_unit("a"), model=model, one_vs_rest=False) == []  # never expanded unless the screen said so


def _routing_model(groups: list[dict], comparisons: list[dict] | None = None) -> MockModel:
    """Answers the one-vs-rest prompt with `groups` and the two-group prompt with `comparisons`."""
    return MockModel(
        {
            "supplement_extract": lambda messages: (
                {"groups": groups} if "ENRICHED" in _prompt(messages) else {"comparisons": comparisons or []}
            )
        }
    )


TWO_GROUP_ANSWER = [
    {"group_0_name": "Ctl", "group_1_name": "Case", "taxa": [{"name": "Bacteroides", "direction": "increased"}]}
]


@pytest.mark.parametrize(
    "groups, n_distinct",
    [
        ([{"name": "A", "taxa": ["t1"]}], 1),
        ([{"name": "A", "taxa": ["t1"]}, {"name": "B", "taxa": ["t2"]}], 2),
        ([{"name": "A", "taxa": ["t1"]}, {"name": "B", "taxa": ["t2"]}, {"name": " A ", "taxa": ["t3"]}], 2),
        ([{"name": "A", "taxa": ["t1"]}, {"name": "B", "taxa": ["t2"]}, {"name": "   ", "taxa": ["t3"]}], 2),
    ],
)
def test_one_vs_rest_is_rejected_below_three_distinct_groups_and_the_unit_is_read_as_two_group(groups, n_distinct):
    model = _routing_model(groups, TWO_GROUP_ANSWER)
    notes = ExtractionNotes()
    comparisons = extract_comparisons(_unit("a"), model=model, one_vs_rest=True, notes=notes)
    assert notes.one_vs_rest_rejected_groups == n_distinct
    assert [("ENRICHED" in _prompt(c["messages"])) for c in model.calls] == [True, False]  # fell back to the two-group prompt
    assert [(c.group_0_name, c.group_1_name) for c in comparisons] == [("Ctl", "Case")]  # not "all other groups (not A)"


def test_one_vs_rest_merges_duplicate_group_names_when_three_distinct_remain():
    groups = [
        {"name": "A", "taxa": ["t1", "t2"]},
        {"name": "B", "taxa": ["t3"]},
        {"name": " A ", "taxa": ["t2", "t4"]},
        {"name": "C", "taxa": ["t5"]},
    ]
    notes = ExtractionNotes()
    comparisons = extract_comparisons(_unit("a"), model=_routing_model(groups), one_vs_rest=True, notes=notes)
    assert notes.one_vs_rest_rejected_groups is None
    assert [(c.group_1_name, [t.name for t in c.taxa]) for c in comparisons] == [
        ("A", ["t1", "t2", "t4"]), ("B", ["t3"]), ("C", ["t5"]),
    ]


def test_a_group_with_no_taxa_still_counts_toward_the_three_group_minimum():
    groups = [{"name": "A", "taxa": ["t1"]}, {"name": "B", "taxa": ["t2"]}, {"name": "C", "taxa": []}]
    notes = ExtractionNotes()
    comparisons = extract_comparisons(_unit("a"), model=_routing_model(groups), one_vs_rest=True, notes=notes)
    assert notes.one_vs_rest_rejected_groups is None and [c.group_1_name for c in comparisons] == ["A", "B"]


# --- caps on what the model returns, and whitespace-only names -------------------------------------


def _comparison(i: int, taxa: list[dict] | None = None) -> dict:
    return {
        "group_0_name": f"c{i}", "group_1_name": f"g{i}",
        "taxa": taxa if taxa is not None else [{"name": f"t{i}", "direction": "increased"}],
    }


def test_comparisons_per_unit_are_capped_at_50_and_the_cut_is_noted():
    model = MockModel({"supplement_extract": {"comparisons": [_comparison(i) for i in range(60)]}})
    notes = ExtractionNotes()
    comparisons = extract_comparisons(_unit("a"), model=model, notes=notes)
    assert [c.group_1_name for c in comparisons] == [f"g{i}" for i in range(50)]
    assert notes.cuts == ["10 comparison(s) beyond the first 50 dropped"]


def test_taxa_per_comparison_are_capped_at_500_and_the_cut_is_noted():
    taxa = [{"name": f"t{i}", "direction": "increased"} for i in range(520)]
    model = MockModel({"supplement_extract": {"comparisons": [_comparison(0, taxa)]}})
    notes = ExtractionNotes()
    (comparison,) = extract_comparisons(_unit("a"), model=model, notes=notes)
    assert len(comparison.taxa) == 500 and comparison.taxa[-1].name == "t499"
    assert notes.cuts == ["20 taxon name(s) beyond the first 500 per comparison dropped"]


def test_one_vs_rest_caps_groups_and_taxa_too():
    groups = [{"name": f"G{i}", "taxa": [f"t{i}-{j}" for j in range(501)]} for i in range(52)]
    notes = ExtractionNotes()
    comparisons = extract_comparisons(_unit("a"), model=_routing_model(groups), one_vs_rest=True, notes=notes)
    assert len(comparisons) == 50 and all(len(c.taxa) == 500 for c in comparisons)
    assert notes.cuts == ["2 comparison(s) beyond the first 50 dropped", "50 taxon name(s) beyond the first 500 per comparison dropped"]


def test_long_names_are_cut_to_200_characters_and_noted():
    model = MockModel(
        {
            "supplement_extract": {
                "comparisons": [
                    {
                        "group_0_name": "g" * 300, "group_1_name": "case",
                        "taxa": [{"name": "t" * 250, "direction": "increased"}],
                    }
                ]
            }
        }
    )
    notes = ExtractionNotes()
    (comparison,) = extract_comparisons(_unit("a"), model=model, notes=notes)
    assert comparison.group_0_name == "g" * 200 and comparison.taxa[0].name == "t" * 200
    assert notes.cuts == ["2 name(s) cut to 200 characters"]


def test_whitespace_only_names_are_dropped_before_the_emptiness_test():
    model = MockModel(
        {
            "supplement_extract": {
                "comparisons": [
                    {
                        "group_0_name": "   ", "group_1_name": "\t",
                        "taxa": [{"name": "  ", "direction": "increased"}, {"name": " Bacteroides ", "direction": "increased"}],
                    },
                    {"group_0_name": "a", "group_1_name": "b", "taxa": [{"name": " \n ", "direction": "decreased"}]},
                ]
            }
        }
    )
    (comparison,) = extract_comparisons(_unit("a"), model=model)
    assert comparison.group_0_name is None and comparison.group_1_name is None
    assert [t.name for t in comparison.taxa] == ["Bacteroides"]  # and the all-blank comparison vanished
    groups = [{"name": "  ", "taxa": ["x"]}, {"name": "A", "taxa": [" ", "t1"]}, {"name": "B", "taxa": ["t2"]}, {"name": "C", "taxa": ["t3"]}]
    ovr = extract_comparisons(_unit("a"), model=_routing_model(groups), one_vs_rest=True)
    assert [(c.group_1_name, [t.name for t in c.taxa]) for c in ovr] == [("A", ["t1"]), ("B", ["t2"]), ("C", ["t3"])]


# --- per-comparison metadata ---------------------------------------------------------------------------


def test_the_two_group_prompt_asks_for_host_sequencing_test_and_correction_per_comparison():
    text = _prompt(build_extract_messages(_unit("a"), study_title="", one_vs_rest=False))
    for key in ("host_species", "sequencing_type", "statistical_test", "mht_correction"):
        assert key in text
    assert "16S" in text and "LEfSe" in text  # the closed vocabularies are listed


def test_extraction_parses_stated_metadata_and_normalizes_it_to_the_vocabularies():
    model = MockModel(
        {
            "supplement_extract": {
                "comparisons": [
                    {
                        **_comparison(0), "host_species": " Mus musculus ", "sequencing_type": "16s",
                        "statistical_test": ["lefse", "not a test"], "mht_correction": True,
                    },
                    {**_comparison(1), "host_species": "", "sequencing_type": "pigeon", "statistical_test": "LEfSe", "mht_correction": "yes"},
                    _comparison(2),
                ]
            }
        }
    )
    first, second, third = extract_comparisons(_unit("a"), model=model)
    assert (first.host_species, first.sequencing_type, first.statistical_test, first.mht_correction) == (
        "Mus musculus", "16S", ("LEfSe",), True,
    )
    assert (second.host_species, second.sequencing_type, second.statistical_test, second.mht_correction) == (
        None, None, ("LEfSe",), None,
    )
    assert (third.host_species, third.sequencing_type, third.statistical_test, third.mht_correction) == (None, None, (), None)


# --- resolution and dedupe ---------------------------------------------------------------------------


def _resolver(**cache: int | None) -> NcbiTaxonomyResolver:
    return NcbiTaxonomyResolver(cache={k.replace("_", " "): v for k, v in cache.items()}, cache_path=None, db=None)


def _fields(**kw) -> ExperimentFields:
    base = {
        "host_species": "Homo sapiens", "body_site": ("Feces",), "condition": ("CRC",), "group_0_name": "Control",
        "group_1_name": "Case", "sequencing_type": "16S", "statistical_test": ("LEfSe",), "mht_correction": False,
    }
    return ExperimentFields(**{**base, **kw})


def test_resolve_comparison_resolves_names_and_inherits_only_host_sequencing_and_test_defaults():
    comparison = SupplementComparison(
        "Healthy", "Crohn", ("Ileum",), (), (NamedTaxon("Bacteroides fragilis", "increased"), NamedTaxon("Unknownia", "decreased"))
    )
    unit = _unit("S1", kind="sheet", label="DA")
    fields, signatures, source = asyncio.run(
        resolve_comparison(
            comparison, unit, defaults=_fields(), model=MockModel(),
            resolver=_resolver(bacteroides_fragilis=817, unknownia=None), client=None,  # type: ignore[arg-type]
        )
    )
    assert source == "S1.xlsx :: DA"
    assert (fields.group_0_name, fields.group_1_name, fields.body_site, fields.condition) == ("Healthy", "Crohn", ("Ileum",), ())
    assert (fields.host_species, fields.sequencing_type, fields.statistical_test, fields.mht_correction) == (
        "Homo sapiens", "16S", ("LEfSe",), False,
    )
    by_direction = {sig.direction: [(t.taxon_name, t.ncbi_id) for t in sig.taxa] for sig in signatures}
    assert by_direction == {"increased": [("Bacteroides fragilis", 817)], "decreased": [("Unknownia", None)]}
    bare = asyncio.run(
        resolve_comparison(
            comparison, unit, defaults=None, model=MockModel(),
            resolver=_resolver(bacteroides_fragilis=817, unknownia=None), client=None,  # type: ignore[arg-type]
        )
    )[0]
    assert (bare.host_species, bare.sequencing_type, bare.statistical_test, bare.mht_correction) == (None, None, (), None)


def test_stated_metadata_wins_and_only_absent_fields_are_inherited_from_the_main_experiment():
    unit = _unit("S1", kind="sheet", label="DA")
    taxa = (NamedTaxon("Bacteroides fragilis", "increased"),)
    stated = SupplementComparison(
        "H", "C", (), (), taxa, host_species="Mus musculus", sequencing_type="WMS", mht_correction=False
    )
    fields, _, _ = asyncio.run(
        resolve_comparison(
            stated, unit, defaults=_fields(mht_correction=True), model=MockModel(),
            resolver=_resolver(bacteroides_fragilis=817), client=None,  # type: ignore[arg-type]
        )
    )
    assert (fields.host_species, fields.sequencing_type, fields.statistical_test, fields.mht_correction) == (
        "Mus musculus", "WMS", ("LEfSe",), False,  # a stated False is not "absent"
    )
    assert inherited_field_names(stated, _fields(mht_correction=True)) == ["statistical_test"]
    bare = SupplementComparison("H", "C", (), (), taxa)
    assert inherited_field_names(bare, _fields()) == ["host_species", "sequencing_type", "statistical_test", "mht_correction"]
    assert inherited_field_names(bare, _fields(host_species=None, statistical_test=(), mht_correction=None)) == ["sequencing_type"]
    assert inherited_field_names(bare, None) == []  # nothing to inherit when the main text has no experiment


def _record(direction: str, *taxa: tuple[str, int | None], source: str = "S1.xlsx :: DA"):
    sig = ExtractedSignature(direction, tuple(ExtractedTaxon(n, direction, i) for n, i in taxa))  # type: ignore[arg-type]
    return _fields(), [sig], source


def _taxa(*ids: int) -> tuple[tuple[str, int], ...]:
    return tuple((f"Taxon {i}", i) for i in ids)


def test_dedupe_drops_supplement_experiments_overlapping_a_main_signature_of_the_same_direction():
    main = [_record("increased", ("Escherichia coli", 562), ("Klebsiella", 570), ("Proteus", 584), source="Table 2")]
    dup = _record("increased", ("E. coli", 562), ("Klebsiella", 570), ("Proteus", 584))  # same ids under a synonym
    below_threshold = _record("increased", *_taxa(562, 1, 2, 3, 4))  # 1 of 7 shared
    jaccard_half = _record("increased", *_taxa(562, 570, 584, 1, 2, 3))  # 3/6 = 0.5, inclusive
    other_direction = _record("decreased", *_taxa(562, 570, 584))
    disjoint = _record("increased", ("Bacteroides", 816), ("Prevotella", 838), ("Alistipes", 239759))
    too_small = _record("increased", *_taxa(562, 570))  # identical-looking but only 2 taxa: not enough to call a duplicate
    kept, dropped = drop_duplicate_experiments(
        [dup, below_threshold, jaccard_half, other_direction, disjoint, too_small], main
    )
    assert kept == [below_threshold, other_direction, disjoint, too_small]
    assert dropped == [
        {
            "source": "S1.xlsx :: DA", "group_0_name": "Control", "group_1_name": "Case", "direction": "increased",
            "main_experiment_index": 0, "jaccard": 1.0, "experiment_dropped": True,
        },
        {
            "source": "S1.xlsx :: DA", "group_0_name": "Control", "group_1_name": "Case", "direction": "increased",
            "main_experiment_index": 0, "jaccard": 0.5, "experiment_dropped": True,
        },
    ]
    json.dumps(dropped)


def test_dedupe_is_per_signature_and_drops_the_experiment_only_when_every_signature_is_a_duplicate():
    main = [_record("increased", *_taxa(1, 2, 3, 4))]
    new_decreased = ExtractedSignature("decreased", tuple(ExtractedTaxon(n, "decreased", i) for n, i in _taxa(7, 8, 9, 10)))
    dup_increased = _record("increased", *_taxa(1, 2, 3, 4))[1][0]
    fields = _fields()
    mixed = (fields, [dup_increased, new_decreased], "S3.xlsx :: DA")
    all_dup = (_fields(group_1_name="Dup"), [dup_increased], "S4.xlsx :: DA")
    kept, dropped = drop_duplicate_experiments([mixed, all_dup], main)
    assert kept == [(fields, [new_decreased], "S3.xlsx :: DA")]  # the new decreased set survives; the duplicate signature is gone
    assert [(d["source"], d["direction"], d["experiment_dropped"]) for d in dropped] == [
        ("S3.xlsx :: DA", "increased", False), ("S4.xlsx :: DA", "increased", True),
    ]


def test_dedupe_also_compares_supplement_experiments_against_each_other():
    pdf = _record("increased", *_taxa(1, 2, 3, 4), source="S2.pdf :: page 3")
    xlsx = _record("increased", *_taxa(1, 2, 3, 4), source="S2.xlsx :: Table 3")
    distinct = _record("increased", *_taxa(20, 21, 22), source="S2.xlsx :: Table 4")
    kept, dropped = drop_duplicate_experiments([pdf, xlsx, distinct], [])
    assert kept == [pdf, distinct]
    assert dropped == [
        {
            "source": "S2.xlsx :: Table 3", "group_0_name": "Control", "group_1_name": "Case", "direction": "increased",
            "main_experiment_index": None, "jaccard": 1.0, "experiment_dropped": True,
            "matched_supplement": {"source": "S2.pdf :: page 3", "group_0_name": "Control", "group_1_name": "Case"},
        }
    ]


def test_distinct_comparisons_within_one_file_are_never_duplicates_of_each_other():
    # Regression (34620922): pages of ONE PDF whose comparisons share most taxa (SI vs colon, SI vs rectum;
    # the same species pair in different gut regions) were dropped as "duplicates" at Jaccard 0.5-0.75.
    p28 = _record("increased", *_taxa(1, 2, 3, 4, 5), source="S.pdf :: page 28")
    p29 = _record("increased", *_taxa(1, 2, 3, 4, 6), source="S.pdf :: page 29")  # Jaccard 4/6
    p45 = _record("increased", *_taxa(1, 2, 3, 4, 5), source="S.pdf :: page 45")  # identical taxa AND identical groups
    kept, dropped = drop_duplicate_experiments([p28, p29, p45], [])
    assert kept == [p28, p29, p45] and dropped == []


def test_cross_file_dedupe_also_needs_the_same_two_groups():
    pdf = (_fields(), _record("increased", *_taxa(1, 2, 3, 4))[1], "S2.pdf :: page 3")
    other_groups = (_fields(group_0_name="Young", group_1_name="Old"), _record("increased", *_taxa(1, 2, 3, 4))[1], "S2.xlsx :: Table 9")
    swapped = (_fields(group_0_name="case", group_1_name="CONTROL"), _record("increased", *_taxa(1, 2, 3, 4))[1], "S2.xlsx :: Table 3")
    kept, dropped = drop_duplicate_experiments([pdf, other_groups, swapped], [])
    assert kept == [pdf, other_groups]  # different groups survive; the same pair (any case/order) is the duplicate
    assert [d["source"] for d in dropped] == ["S2.xlsx :: Table 3"]


def test_a_signature_dropped_as_a_duplicate_does_not_join_the_comparison_pool():
    main = [_record("increased", *_taxa(1, 2, 3, 4))]
    first = _record("increased", *_taxa(1, 2, 3, 4), source="a")  # dropped against the main text
    second = _record("increased", *_taxa(1, 2, 3, 4), source="b")  # also dropped, but against the main text, not "a"
    _, dropped = drop_duplicate_experiments([first, second], main)
    assert [d["main_experiment_index"] for d in dropped] == [0, 0] and all("matched_supplement" not in d for d in dropped)


def test_dedupe_compares_unresolved_taxa_by_normalized_name_and_ignores_empty_sets():
    names = ("Escherichia  coli", "Klebsiella pneumoniae", "Proteus mirabilis")
    main = [_record("increased", *((n, None) for n in names))]
    same = _record("increased", ("escherichia coli", None), ("KLEBSIELLA pneumoniae", None), ("proteus  mirabilis", None))
    kept, dropped = drop_duplicate_experiments([same], main)
    assert kept == [] and [d["jaccard"] for d in dropped] == [1.0]
    empty = _record("increased")
    kept, dropped = drop_duplicate_experiments([empty], [_record("increased")])
    assert kept == [empty] and dropped == []  # empty vs empty: not a match


# --- the lever end to end (offline) ---------------------------------------------------------------------

DA_TAXA = [
    {  # new experiment: kept
        "group_0_name": "Control", "group_1_name": "Crohn", "body_site": ["Feces"], "condition": ["Crohn disease"],
        "taxa": [{"name": "Bacteroides fragilis", "direction": "increased"}, {"name": "Prevotella copri", "direction": "decreased"}],
    },
    {  # kept: the main text's signatures are single taxa, too small to call a duplicate
        "group_0_name": "Control", "group_1_name": "Treated",
        "taxa": [{"name": "Escherichia coli", "direction": "increased"}, {"name": "Klebsiella pneumoniae", "direction": "increased"}],
    },
    {  # kept
        "group_0_name": "Control", "group_1_name": "Other",
        "taxa": [
            {"name": "Escherichia coli", "direction": "increased"},
            {"name": "Klebsiella pneumoniae", "direction": "increased"},
            {"name": "Proteus mirabilis", "direction": "increased"},
        ],
    },
]
OVR_GROUPS = [
    {"name": "Cluster1", "taxa": [{"name": "Roseburia hominis"}, {"name": "Blautia obeum"}]},
    {"name": "Cluster2", "taxa": [{"name": "Akkermansia muciniphila"}]},
    {"name": "Cluster3", "taxa": [{"name": "Dorea longicatena"}]},
]
SUPPLEMENT_TAXA_IDS = {
    "bacteroides fragilis": 817, "prevotella copri": 165179, "klebsiella pneumoniae": 573, "proteus mirabilis": 584,
    "roseburia hominis": 301301, "blautia obeum": 40520, "akkermansia muciniphila": 239935, "dorea longicatena": 88431,
}
PAGE_TEXT = "Differentially abundant taxa between ileal and colonic samples, LEfSe. " * 5


def _supplement_zip() -> bytes:
    xlsx = _xlsx(
        {
            "DA": [["taxon", "lda", "group"], ["Bacteroides fragilis", 4.2, "Crohn"]],
            "Meta": [["sample", "age"], ["s1", 5]],
            "OVR": [["cluster", "taxon"], ["Cluster1", "Roseburia hominis"]],
        }
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("S1.xlsx", xlsx)
        zf.writestr("S2.pdf", _pdf([PAGE_TEXT, None]))
        zf.writestr("tiny.csv", "a,b\n1,2\n")
        zf.writestr("fig.png", b"png")
        zf.writestr("movie.mp4", b"video")
    return buf.getvalue()


def _screen_answers(state, questions):
    key = (state["file"], state.get("sheet") or state.get("page"))
    p, arity = {
        ("S1.xlsx", "DA"): (0.9, "two_group"),
        ("S1.xlsx", "Meta"): (0.1, "not_a_comparison"),
        ("S1.xlsx", "OVR"): (0.8, ONE_VS_REST),
        ("S2.pdf", 1): (0.7, "two_group"),
        ("S2.pdf", 2): (0.2, "not_a_comparison"),
        ("tiny.csv", None): (0.3, "not_a_comparison"),
    }[key]
    return _answers(p, arity=arity)


def _extract_answers(messages):
    text = _prompt(messages)
    if "S1.xlsx :: OVR" in text:
        assert "ENRICHED" in text
        return {"groups": OVR_GROUPS}
    assert "ENRICHED" not in text
    if "S1.xlsx :: DA" in text:
        return {"comparisons": DA_TAXA}
    assert "S2.pdf :: page 1" in text  # nothing else is ever extracted
    return {
        "comparisons": [
            {"group_0_name": "Colon", "group_1_name": "Ileum", "taxa": [{"name": "Prevotella copri", "direction": "decreased"}]},
            DA_TAXA[2],  # the sheet's third table again (same two groups), shipped as a PDF page: a cross-file duplicate
        ]
    }


def _mock_zip(httpx_mock, **kw):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid=e2e.PMCID),
        headers={"Content-Type": "application/zip"},
        **({"content": _supplement_zip()} | kw),
    )


def _curate(httpx_mock, tmp_path, *, tag, decision=None, model=None, supplements=False, zip_mock=True, **zip_kw):
    e2e._mock_idconv(httpx_mock)
    e2e._mock_fulltext(httpx_mock)
    e2e._mock_taxonomy(httpx_mock)
    if decision is not None:
        _mock_ols(httpx_mock)
    if supplements and zip_mock:
        _mock_zip(httpx_mock, **zip_kw)

    async def run():
        async with httpx.AsyncClient() as client:
            return await curate_async(
                e2e.PMID,
                model=model or MockModel({"supplement_extract": _extract_answers}),
                client=client,
                decision_model=decision,
                supplements=supplements,
                resolver=NcbiTaxonomyResolver(cache=dict(SUPPLEMENT_TAXA_IDS), cache_path=None, db=None),
                taxonomy_cache_path=tmp_path / f"tax-{tag}.json",
                ols_cache_path=tmp_path / f"ols-{tag}.json",
                html_cache_dir=tmp_path / f"html-{tag}",  # a fresh PMC-HTML cache per run, so each run requests it
            )

    return asyncio.run(run())


def _mock_ols(httpx_mock) -> None:
    httpx_mock.add_response(
        url=httpx.URL("https://www.ebi.ac.uk/ols4/api/search").copy_merge_params(
            {"q": "Feces", "ontology": "uberon", "rows": "10", "type": "class", "queryFields": "label,synonym,short_form,obo_id"}
        ),
        json={"response": {"docs": [{"obo_id": "UBERON:0001988", "label": "feces"}]}},
    )


def _decision(**stages) -> MockDecisionModel:
    return MockDecisionModel(
        {
            "s5a_locate": {"is_da_artifact": NoulAnswer(0.5)},
            "s4_ontology": {"term": ChoiceAnswer("UBERON:0001988", {"UBERON:0001988": 0.9, "none_of_these": 0.1}, 0.9)},
            "s1b_screen": _screen_answers,
            **stages,
        }
    )


def test_e2e_routes_extracts_expands_and_dedupes(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, tag="base")
    decision = _decision()
    model = MockModel({"supplement_extract": _extract_answers})
    result = _curate(httpx_mock, tmp_path, tag="supp", decision=decision, model=model, supplements=True)

    assert result.valid, result.problems
    main_experiments = baseline.record["experiments"]
    experiments = result.record["experiments"]
    assert experiments[: len(main_experiments)] == main_experiments  # main text first and untouched
    supplement = experiments[len(main_experiments) :]

    # routed vs unrouted: screening saw every readable unit once; only the routed ones were extracted
    screen = {u["id"]: u for u in result.annotations["supplement_screen"]}
    assert {k: u["routed"] for k, u in screen.items()} == {
        "S1.xlsx::DA": True, "S1.xlsx::Meta": False, "S1.xlsx::OVR": True,
        "S2.pdf::page 1": True, "S2.pdf::page 2": False, "tiny.csv": False,
    }
    assert screen["S1.xlsx::OVR"]["arity"] == ONE_VS_REST and screen["S1.xlsx::DA"]["content_kind"] == "da_taxa_table"
    assert not any(u["truncated"] for u in screen.values())
    assert sum(1 for c in decision.calls if c["stage"] == "s1b_screen") == 6
    assert [c["images"] != [] for c in decision.calls if c["stage"] == "s1b_screen" and c["state"].get("page") == 2] == [True]
    assert sum(1 for c in model.calls if c["stage"] == "supplement_extract") == 3

    # DA unit: 3 comparisons; OVR unit: 3 groups -> 3 experiments; PDF page 1: 2 comparisons, one of which repeats
    # the DA sheet's third table (same groups, different file) and is dropped as a duplicate of that supplement experiment
    assert [e["group_1_name"] for e in supplement] == [
        "Crohn", "Treated", "Other", "Cluster1", "Cluster2", "Cluster3", "Ileum",
    ]
    assert [e["signatures"][0]["source"] for e in supplement] == [
        "S1.xlsx :: DA", "S1.xlsx :: DA", "S1.xlsx :: DA", "S1.xlsx :: OVR", "S1.xlsx :: OVR", "S1.xlsx :: OVR",
        "S2.pdf :: page 1",
    ]
    ovr = supplement[3:6]
    assert [e["group_0_name"] for e in ovr] == [f"all other groups (not Cluster{i})" for i in (1, 2, 3)]
    assert all([s["abundance_in_group_1"] for s in e["signatures"]] == ["increased"] for e in ovr)
    assert [t["ncbi_id"] for t in ovr[0]["signatures"][0]["taxa"]] == [301301, 40520]  # ids from the authority path
    assert supplement[0]["host_species"] == "Homo sapiens" and supplement[0]["sequencing_type"] == "16S"  # S4 exp 0 defaults
    # the first comparison states its own site; every other one inherits it because the paper's only main-text
    # experiment (hence "every main-text experiment") names one site -- and says so in the annotation
    assert supplement[0]["body_site"] == ["Feces"] and supplement[-1]["body_site"] == ["Feces"]
    all_four = ["host_species", "sequencing_type", "statistical_test", "mht_correction"]
    assert result.annotations["supplement_inherited_fields"] == [
        {
            "source": source, "group_1_name": group_1, "from_main_experiment": 0,
            "fields": all_four if group_1 == "Crohn" else [*all_four, "body_site"],
        }
        for source, group_1 in [
            ("S1.xlsx :: DA", "Crohn"), ("S1.xlsx :: DA", "Treated"), ("S1.xlsx :: DA", "Other"),
            ("S1.xlsx :: OVR", "Cluster1"), ("S1.xlsx :: OVR", "Cluster2"), ("S1.xlsx :: OVR", "Cluster3"),
            ("S2.pdf :: page 1", "Ileum"),
        ]
    ]  # the dropped cross-file duplicate (the sheet's "Other" table, repeated on the PDF page) is not listed

    assert result.annotations["supplement_dropped_duplicates"] == [
        {
            "source": "S2.pdf :: page 1", "group_0_name": "Control", "group_1_name": "Other", "direction": "increased",
            "main_experiment_index": None, "jaccard": 1.0, "experiment_dropped": True,
            "matched_supplement": {"source": "S1.xlsx :: DA", "group_0_name": "Control", "group_1_name": "Other"},
        }
    ]
    skipped = {s["file"]: s["reason"] for s in result.annotations["supplement_skipped"]}
    assert set(skipped) == {"fig.png", "movie.mp4"}
    assert not {"supplement_screen_error", "supplement_extract_error"} & set(result.annotations)
    json.dumps(result.annotations)


def test_cpu_bound_unit_parsing_runs_off_the_event_loop(httpx_mock, tmp_path, monkeypatch):
    import threading

    import bugsigdb_curation.curator.supplement_lever as lever

    threads: list[threading.Thread] = []
    real = lever.supplement_units

    def spy(files, *, skipped=None):
        threads.append(threading.current_thread())
        return real(files, skipped=skipped)

    monkeypatch.setattr(lever, "supplement_units", spy)
    _curate(httpx_mock, tmp_path, tag="thread", decision=_decision(), supplements=True)
    assert threads and all(t is not threading.main_thread() for t in threads)


def test_with_the_flag_off_the_record_is_byte_identical_and_nothing_is_fetched(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, tag="base")
    off = _curate(httpx_mock, tmp_path, tag="off", decision=_decision(), supplements=False)
    assert json.dumps(off.record, sort_keys=True) == json.dumps(baseline.record, sort_keys=True)
    assert not any(k.startswith("supplement") for k in off.annotations)
    assert all("supplementaryFiles" not in str(r.url) for r in httpx_mock.get_requests())


def test_supplements_without_a_decision_model_is_rejected():
    with pytest.raises(ValueError, match="decision_model"):
        asyncio.run(curate_async(e2e.PMID, model=MockModel(), supplements=True))


def test_no_supplementary_files_is_recorded_and_the_record_is_the_main_text_one(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, tag="base")
    result = _curate(httpx_mock, tmp_path, tag="none", decision=_decision(), supplements=True, status_code=404, content=b"")
    assert result.record == baseline.record and result.valid
    assert [s["file"] for s in result.annotations["supplement_skipped"]] == ["(supplementary files zip)"]
    assert "supplement_screen" not in result.annotations


def test_a_screen_failure_is_recorded_and_the_study_still_succeeds(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, tag="base")

    def down(state, questions):
        raise DecisionModelError("401")

    result = _curate(httpx_mock, tmp_path, tag="down", decision=_decision(s1b_screen=down), supplements=True)
    assert result.valid and result.record == baseline.record
    (failure,) = result.annotations["supplement_screen_error"]
    assert failure["unit"].startswith(("S1.xlsx::", "S2.pdf::", "tiny.csv")) and "401" in failure["error"]
    assert "supplement_screen" not in result.annotations
    json.dumps(result.annotations)


def test_a_failed_extraction_skips_only_that_unit(httpx_mock, tmp_path):
    def flaky(messages):
        if "S1.xlsx :: DA" in _prompt(messages):
            raise ModelError("malformed JSON")
        return _extract_answers(messages)

    result = _curate(
        httpx_mock, tmp_path, tag="flaky", decision=_decision(), model=MockModel({"supplement_extract": flaky}), supplements=True
    )
    assert result.valid
    (failure,) = result.annotations["supplement_extract_error"]
    assert failure["unit"] == "S1.xlsx::DA" and "malformed JSON" in failure["error"]
    groups = [e.get("group_1_name") for e in result.record["experiments"][1:]]
    assert groups == ["Cluster1", "Cluster2", "Cluster3", "Ileum", "Other"]  # the other routed units landed


def test_a_model_transport_error_in_extraction_skips_only_that_unit_and_keeps_the_main_text(httpx_mock, tmp_path):
    baseline = _curate(httpx_mock, tmp_path, tag="base")

    def rate_limited(messages):
        if "S1.xlsx :: DA" in _prompt(messages):
            raise ModelCallError("RateLimitError: slow down")
        return _extract_answers(messages)

    result = _curate(
        httpx_mock, tmp_path, tag="rl", decision=_decision(), model=MockModel({"supplement_extract": rate_limited}),
        supplements=True,
    )
    assert result.valid
    (failure,) = result.annotations["supplement_extract_error"]
    assert failure["unit"] == "S1.xlsx::DA" and "RateLimitError" in failure["error"]
    experiments = result.record["experiments"]
    main_experiments = baseline.record["experiments"]
    assert experiments[: len(main_experiments)] == main_experiments  # the main-text record survives untouched
    assert [e.get("group_1_name") for e in experiments[len(main_experiments) :]] == [
        "Cluster1", "Cluster2", "Cluster3", "Ileum", "Other",  # (its duplicate source, the DA unit, failed)
    ]  # and so do the other routed units


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_a_model_call_error_in_a_main_text_stage_still_fails_the_study(httpx_mock, tmp_path):
    class Down(MockModel):
        def complete(self, *, stage, messages):
            raise ModelCallError("RateLimitError: slow down")

    with pytest.raises(ModelCallError):
        _curate(httpx_mock, tmp_path, tag="main-down", model=Down(), supplements=False)


def test_a_bug_in_extraction_is_not_swallowed(httpx_mock, tmp_path):
    def buggy(messages):
        raise KeyError("bug")

    with pytest.raises(KeyError):
        _curate(httpx_mock, tmp_path, tag="bug", decision=_decision(), model=MockModel({"supplement_extract": buggy}), supplements=True)


def test_a_corrupt_zip_is_recorded_not_raised(httpx_mock):
    _mock_zip(httpx_mock, content=b"PK\x03\x04 definitely not a zip")
    annotations: dict = {}

    async def run():
        async with httpx.AsyncClient() as client:
            return await supplement_experiments(
                e2e.PMCID, client=client, decision_model=_decision(), model=MockModel(), resolver=_resolver(),
                study_title="", main_experiments=[], annotations=annotations,
            )

    assert asyncio.run(run()) == []
    assert "corrupt zip" in annotations["supplement_skipped"][0]["reason"]


# --- lever-level guards, annotations and error paths (offline) ----------------------------------------


def _csv_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _run_lever(httpx_mock, files: dict[str, str], *, decision, model, resolver, main_experiments=(), **kwargs):
    _mock_zip(httpx_mock, content=_csv_zip(files))
    annotations: dict = {}

    async def run():
        async with httpx.AsyncClient() as client:
            return await supplement_experiments(
                e2e.PMCID, client=client, decision_model=decision, model=model, resolver=resolver, study_title="",
                main_experiments=list(main_experiments), annotations=annotations, **kwargs,
            )

    return asyncio.run(run()), annotations


def _cached(**ids: int | None) -> NcbiTaxonomyResolver:
    return NcbiTaxonomyResolver(cache={k.replace("_", " "): v for k, v in ids.items()}, cache_path=None, db=None)


def test_at_most_400_units_are_screened_and_the_rest_are_recorded_as_skipped(httpx_mock):
    decision = MockDecisionModel({"s1b_screen": lambda s, q: _answers(0.1)})
    files = {f"f{i:03d}.csv": "a,b\n1,2\n" for i in range(405)}
    kept, annotations = _run_lever(httpx_mock, files, decision=decision, model=MockModel(), resolver=_cached())
    assert kept == [] and len(decision.calls) == 400 and len(annotations["supplement_screen"]) == 400
    assert annotations["supplement_screen"][-1]["id"] == "f399.csv"
    assert annotations["supplement_skipped"] == [
        {"file": f"f{i:03d}.csv", "reason": "1 unit(s) not screened (limit 400 units per study)"} for i in range(400, 405)
    ]


def test_cuts_and_a_rejected_one_vs_rest_label_are_recorded_in_the_annotations(httpx_mock):
    def screen(state, questions):
        return _answers(0.9, arity=ONE_VS_REST if state["file"] == "two_groups.csv" else "two_group")

    def extract(messages):
        text = _prompt(messages)
        if "two_groups.csv" in text and "ENRICHED" in text:
            return {"groups": [{"name": "A", "taxa": ["t0"]}, {"name": "B", "taxa": ["t1"]}]}
        if "two_groups.csv" in text:
            return {"comparisons": [_comparison(0)]}
        return {"comparisons": [_comparison(i) for i in range(60)]}

    resolver = _cached(**{f"t{i}": 1000 + i for i in range(60)})
    kept, annotations = _run_lever(
        httpx_mock, {"two_groups.csv": "a,b\n1,2\n", "many.csv": "a,b\n1,2\n"},
        decision=MockDecisionModel({"s1b_screen": screen}), model=MockModel({"supplement_extract": extract}),
        resolver=resolver,
    )
    assert annotations["supplement_one_vs_rest_rejected"] == [{"unit": "two_groups.csv", "n_groups": 2}]
    assert annotations["supplement_truncated"] == [{"unit": "many.csv", "cut": "10 comparison(s) beyond the first 50 dropped"}]
    assert [fields.group_0_name for fields, _, _ in kept[:1]] == ["c0"]  # two_groups.csv read as a two-group table
    assert len(kept) == 51  # 1 + the first 50 of many.csv, no duplicates among them (distinct taxa)
    json.dumps(annotations)


def test_the_lever_dedupes_against_the_main_text_and_annotates_what_it_dropped(httpx_mock):
    main = [(_fields(), _record("increased", ("Bacteroides", 816), ("Prevotella", 838), ("Alistipes", 239759))[1], "Table 2")]
    names = ["Bacteroides", "Prevotella", "Alistipes"]

    def extract(messages):
        taxa = [{"name": n, "direction": "increased"} for n in names] + [
            {"name": "Roseburia", "direction": "decreased"}, {"name": "Blautia", "direction": "decreased"},
            {"name": "Dorea", "direction": "decreased"},
        ]
        return {"comparisons": [{"group_0_name": "Ctl", "group_1_name": "Case", "taxa": taxa}]}

    resolver = _cached(bacteroides=816, prevotella=838, alistipes=239759, roseburia=841, blautia=572511, dorea=189330)
    kept, annotations = _run_lever(
        httpx_mock, {"t.csv": "a,b\n1,2\n"}, decision=MockDecisionModel({"s1b_screen": lambda s, q: _answers(0.9)}),
        model=MockModel({"supplement_extract": extract}), resolver=resolver, main_experiments=main,
    )
    ((_, signatures, source),) = kept  # the increased signature repeats the main text; the new decreased one survives
    assert source == "t.csv" and [(sig.direction, [t.ncbi_id for t in sig.taxa]) for sig in signatures] == [
        ("decreased", [841, 572511, 189330])
    ]
    (entry,) = annotations["supplement_dropped_duplicates"]
    assert (entry["direction"], entry["main_experiment_index"], entry["jaccard"], entry["experiment_dropped"]) == (
        "increased", 0, 1.0, False,
    )


def test_a_name_resolution_failure_skips_only_that_unit_and_is_recorded(httpx_mock):
    class Offline(NcbiTaxonomyResolver):
        async def resolve_name(self, name, *, client):
            raise httpx.ConnectError("no route")

    resolver = Offline(cache={"bacteroides": 816}, cache_path=None, db=None)

    def extract(messages):
        taxon = "Bacteroides" if "ok.csv" in _prompt(messages) else "Uncached taxon"
        return {"comparisons": [{"group_0_name": "c", "group_1_name": "g", "taxa": [{"name": taxon, "direction": "increased"}]}]}

    kept, annotations = _run_lever(
        httpx_mock, {"ok.csv": "a,b\n1,2\n", "net.csv": "a,b\n1,2\n"},
        decision=MockDecisionModel({"s1b_screen": lambda s, q: _answers(0.9)}),
        model=MockModel({"supplement_extract": extract}), resolver=resolver,
    )
    assert [source for _, _, source in kept] == ["ok.csv"]
    (failure,) = annotations["supplement_extract_error"]
    assert failure["unit"] == "net.csv" and "ConnectError" in failure["error"]


def test_a_fetch_failure_reason_reaches_the_skipped_annotation(httpx_mock):
    _mock_zip(httpx_mock, status_code=503, content=b"")
    annotations: dict = {}

    async def run():
        async with httpx.AsyncClient() as client:
            return await supplement_experiments(
                e2e.PMCID, client=client, decision_model=_decision(), model=MockModel(), resolver=_resolver(),
                study_title="", main_experiments=[], annotations=annotations,
            )

    assert asyncio.run(run()) == []
    assert annotations["supplement_skipped"] == [{"file": "(supplementary files zip)", "reason": "fetch failed: HTTP 503"}]


# --- CLI ----------------------------------------------------------------------------------------------

#: Rich wraps/styles console output per terminal; pin a wide, colourless one and normalise what is left.
_PLAIN_TERMINAL = {"COLUMNS": "250", "TERMINAL_WIDTH": "250", "NO_COLOR": "1", "TERM": "dumb"}  # typer/rich read TERMINAL_WIDTH


def _invoke(*args: str):
    result = CliRunner().invoke(app, list(args), env=_PLAIN_TERMINAL)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output).replace("│", " ")  # rich's help-box borders
    return result, " ".join(plain.split())


def _stub_curate(monkeypatch, annotations_for=lambda pmid: {}):
    seen: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_open(name, archive=None, **_):
        yield object()

    async def fake_curate_async(pmid, **kwargs):
        seen.append({"pmid": pmid, **kwargs})
        return CurationResult(
            pmid=pmid, pmcid=None, has_pmc=False, record={"uid": pmid}, valid=True, problems=(),
            annotations=annotations_for(pmid),
        )

    monkeypatch.setattr(cli_module, "open_decision_model", fake_open)
    monkeypatch.setattr(cli_module, "curate_async", fake_curate_async)
    monkeypatch.setattr(cli_module, "require_credentials", lambda: None)
    monkeypatch.setattr(cli_module, "_build_model", lambda mock, name: MockModel())
    return seen


def test_cli_supplements_requires_a_decision_model(monkeypatch, tmp_path):
    seen = _stub_curate(monkeypatch)
    result, output = _invoke("curate", "--pmid", "1", "--supplements", "--out", str(tmp_path / "o.json"))
    assert result.exit_code == 2 and "--supplements needs a decision model" in output and "--decision-model" in output
    mocked, mocked_output = _invoke(
        "curate", "--pmid", "1", "--mock", "--decision-model", "clef", "--supplements", "--out", str(tmp_path / "o.json")
    )
    assert mocked.exit_code == 2 and "--supplements needs a decision model" in mocked_output
    assert seen == []  # nothing ran


def test_cli_single_pmid_threads_supplements_to_curate_async(monkeypatch, tmp_path):
    seen = _stub_curate(monkeypatch)
    base = ["curate", "--pmid", "1", "--decision-model", "clef", "--out", str(tmp_path / "o.json")]
    base += ["--taxonomy-cache", str(tmp_path / "tax.json"), "--ols-cache", str(tmp_path / "ols.json")]
    for extra in (["--supplements"], [], ["--no-supplements"]):
        result, _ = _invoke(*base, *extra)
        assert result.exit_code == 0
    assert [kw["supplements"] for kw in seen] == [True, False, False]


def test_cli_smoke_threads_supplements_and_counts_screen_failures(monkeypatch, tmp_path):
    seen = _stub_curate(
        monkeypatch,
        lambda pmid: {"supplement_screen_error": [{"unit": "S1.xlsx::DA", "error": "boom"}]} if pmid == "A" else {},
    )
    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: ["A", "B", "C"])
    caches = ["--taxonomy-cache", str(tmp_path / "tax.json"), "--ols-cache", str(tmp_path / "ols.json")]
    result, output = _invoke(
        "curate", "--smoke", "--decision-model", "clef", "--supplements", "--out", str(tmp_path / "s"), *caches
    )
    assert result.exit_code == 0, output
    assert [kw["supplements"] for kw in seen] == [True, True, True]
    assert "1 study(ies) have no supplement experiments because the supplement screening call failed" in output
    smoke_off, _ = _invoke("curate", "--smoke", "--decision-model", "clef", "--out", str(tmp_path / "s2"), *caches)
    assert smoke_off.exit_code == 0 and [kw["supplements"] for kw in seen[3:]] == [False, False, False]


def test_cli_smoke_summary_also_counts_extraction_errors_and_fetch_failures(monkeypatch, tmp_path):
    zip_skip = "(supplementary files zip)"
    annotations = {
        "A": {"supplement_extract_error": [{"unit": "S1.xlsx::DA", "error": "ModelCallError('rate limit')"}]},
        "B": {"supplement_skipped": [{"file": zip_skip, "reason": "download timed out after 240 s"}]},
        "C": {"supplement_skipped": [{"file": zip_skip, "reason": "download timed out after 240 s"}]},
        "D": {"supplement_skipped": [{"file": zip_skip, "reason": "no supplementary files (HTTP 404)"}]},  # normal
        "E": {"supplement_skipped": [{"file": "fig.png", "reason": "image file not read"}]},  # not a fetch failure
        "F": {"supplement_skipped": [{"file": zip_skip, "reason": "fetch failed: HTTP 503"}]},
    }
    _stub_curate(monkeypatch, lambda pmid: annotations[pmid])
    monkeypatch.setattr(cli_module, "smoke_study_ids", lambda: list(annotations))
    caches = ["--taxonomy-cache", str(tmp_path / "tax.json"), "--ols-cache", str(tmp_path / "ols.json")]
    result, output = _invoke(
        "curate", "--smoke", "--decision-model", "clef", "--supplements", "--out", str(tmp_path / "s"), *caches
    )
    assert result.exit_code == 0, output
    assert "1 study(ies) lost some supplement units because extraction or name resolution failed" in output
    assert "see supplement_extract_error" in output
    assert "3 study(ies) lost their supplements at the fetch" in output
    assert "2x download timed out after 240 s" in output and "1x fetch failed: HTTP 503" in output
    assert "no supplementary files" not in output  # a plain 404 is not a failure
    assert "screening call failed" not in output


def _curate_params() -> dict[str, object]:
    """The `curate` command's declared options by parameter name (independent of rendered help width)."""
    import typer.main

    return {p.name: p for p in typer.main.get_command(app).commands["curate"].params}


def test_cli_declares_the_supplements_flag_and_describes_it():
    param = _curate_params()["supplements"]
    assert "--supplements" in param.opts and "--no-supplements" in param.secondary_opts
    assert param.default is False and "supplementary files" in param.help


# --- round 2: rank labels, cohort-level body site, supplement-vs-supplement threshold --------------------------


def test_rank_labels_are_stripped_from_extracted_taxon_names_but_not_from_real_names():
    from collections import Counter

    from bugsigdb_curation.curator.supplement_lever import _taxon_name

    cuts: Counter[str] = Counter()
    assert [_taxon_name(v, cuts) for v in ("Genus: Streptococcus", "Family: Veillonellaceae", "species - Prevotella copri",
                                           "  Order:Lactobacillales ", "Streptococcus", "Classical Bacteroides", None)] == [
        "Streptococcus", "Veillonellaceae", "Prevotella copri", "Lactobacillales", "Streptococcus", "Classical Bacteroides", "",
    ]


def test_shared_body_site_only_when_every_main_experiment_agrees():
    from bugsigdb_curation.curator.supplement_lever import shared_body_site

    def record(*site):
        return _fields(body_site=tuple(site)), [], "x"

    assert shared_body_site([record("Feces"), record("Feces")]) == ("Feces",)
    assert shared_body_site([record("Feces"), record("Cecum")]) == ()  # a multi-site paper: no cohort-level site
    assert shared_body_site([record("Feces"), record()]) == ()  # one main experiment with no site: don't guess
    assert shared_body_site([]) == ()


def test_inherited_field_names_includes_body_site_only_when_a_site_is_offered_and_none_stated():
    stated = SupplementComparison("A", "B", ("Cecum",), (), (NamedTaxon("x", "increased"),))
    bare = SupplementComparison("A", "B", (), (), (NamedTaxon("x", "increased"),))
    assert "body_site" not in inherited_field_names(stated, _fields(), ("Feces",))  # it stated its own
    assert "body_site" in inherited_field_names(bare, _fields(), ("Feces",))
    assert "body_site" not in inherited_field_names(bare, _fields(), ())  # no shared site: stays empty
    assert inherited_field_names(bare, None, ("Feces",)) == ["body_site"]


def test_supplement_vs_supplement_needs_a_near_identical_set_but_main_text_keeps_the_looser_threshold():
    # Different files, same groups, Jaccard 0.6: different tables of one paper (another rank or method), kept
    a = _record("increased", *_taxa(1, 2, 3, 4, 5), source="S1.xlsx :: T1")
    b = _record("increased", *_taxa(1, 2, 3, 6), source="S2.xlsx :: T2")  # 3/6 = 0.5
    c = _record("increased", *_taxa(1, 2, 3, 4, 5, 6), source="S3.xlsx :: T3")  # vs a: 5/6 = 0.83 -> a copy
    kept, dropped = drop_duplicate_experiments([a, b, c], [])
    assert kept == [a, b] and [d["source"] for d in dropped] == ["S3.xlsx :: T3"]
    # the same 0.5 overlap against the MAIN TEXT is still a duplicate
    main = [_record("increased", *_taxa(1, 2, 3), source="Table 2")]
    kept, dropped = drop_duplicate_experiments([_record("increased", *_taxa(1, 2, 3, 4, 5, 6))], main)  # 3/6
    assert kept == [] and dropped[0]["jaccard"] == 0.5
