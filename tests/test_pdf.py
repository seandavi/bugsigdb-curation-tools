"""`bugsigdb_curation.pdf`: the PDF-reading seam (pypdfium2 + Pillow behind four small calls).

Fixtures are committed under ``tests/data/pdf`` (see ``make_fixtures.py`` there).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from bugsigdb_curation.pdf import PdfError, open_pdf

DATA = Path(__file__).parent / "data" / "pdf"


def _fixture(name: str) -> bytes:
    return (DATA / f"{name}.pdf").read_bytes()


def test_page_count_text_and_size_of_a_text_pdf():
    with open_pdf(_fixture("text_pages")) as doc:
        assert doc.n_pages == 3
        assert "Differentially abundant taxa between cases and controls." in doc.page_text(0)
        assert doc.page_text(1) == ""  # a drawing only
        assert doc.page_text(2) == "tiny"
        assert doc.page_size(0) == pytest.approx((595, 842))


def test_page_text_has_plain_newlines_and_no_trailing_whitespace():
    with open_pdf(_fixture("dense_text")) as doc:
        text = doc.page_text(0)
    assert "\r" not in text and text == text.strip()
    assert all(line == line.rstrip() for line in text.splitlines())
    assert len(text) > 10_000


def test_render_jpeg_is_a_jpeg_sized_by_dpi():
    with open_pdf(_fixture("text_pages")) as doc:
        data = doc.render_jpeg(1, 100, 80)
        assert data.startswith(b"\xff\xd8\xff")
        width, height = Image.open(io.BytesIO(data)).size
        assert (width, height) == (pytest.approx(595 * 100 / 72, abs=1), pytest.approx(842 * 100 / 72, abs=1))
        small = Image.open(io.BytesIO(doc.render_jpeg(1, 50, 80))).size
        assert small[0] < width and small[1] < height


def test_render_jpeg_gets_smaller_as_quality_drops():
    with open_pdf(_fixture("noisy")) as doc:
        sizes = [len(doc.render_jpeg(0, 100, quality)) for quality in (80, 65, 50, 35)]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes)


def test_a_huge_page_reports_its_size_in_points():
    with open_pdf(_fixture("poster")) as doc:
        assert doc.page_size(0) == pytest.approx((6000, 3000))


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-garbage",
        b"",
        b"not a pdf at all",
        _fixture("text_pages")[:300],  # truncated
        _fixture("encrypted"),  # needs a password
    ],
    ids=["garbage", "empty", "text", "truncated", "encrypted"],
)
def test_a_file_that_cannot_be_read_raises_pdf_error_on_open(data):
    with pytest.raises(PdfError), open_pdf(data):
        pass


def test_a_bad_page_index_raises_pdf_error():
    with open_pdf(_fixture("text_pages")) as doc:
        for call in (doc.page_text, doc.page_size, lambda i: doc.render_jpeg(i, 100, 80)):
            with pytest.raises(PdfError):
                call(99)
