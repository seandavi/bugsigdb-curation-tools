"""Generate the tiny PDF fixtures in this directory (run once; the outputs are committed).

    uv run --no-project --with pymupdf python tests/data/pdf/make_fixtures.py

pymupdf (AGPL-3.0) is used here ONLY to author the fixtures; it is not a dependency of the project, and nothing
that ships or runs in the test suite imports it.
"""

from __future__ import annotations

import random
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
LONG_TEXT = "Differentially abundant taxa between cases and controls. " * 12  # > 200 chars


def text_pages() -> bytes:
    """Three A4 pages: > 200 chars of text, a text-free drawing, and a page with only the word 'tiny'."""
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 550, 800), LONG_TEXT, fontsize=9)
    doc.new_page().draw_rect(pymupdf.Rect(50, 50, 300, 300), color=(0, 0, 1), fill=(1, 0, 0))
    doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 550, 800), "tiny", fontsize=9)
    return doc.tobytes()


def dense_text() -> bytes:
    """One A4 page with ~20k characters of 3 pt text (longer than the lever's per-page text cap)."""
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(20, 20, 580, 820), "word " * 4000, fontsize=3)
    return doc.tobytes()


def poster() -> bytes:
    """One 6000 x 3000 pt page with a red square: far larger than the render's pixel budget."""
    doc = pymupdf.open()
    doc.new_page(width=6000, height=3000).draw_rect(pymupdf.Rect(50, 50, 500, 500), fill=(1, 0, 0))
    return doc.tobytes()


def noisy() -> bytes:
    """One A4 page covered by a random-noise image: its JPEG exceeds 200 KB at 100 dpi even at quality 35 (so the lever must also drop the dpi)."""
    side = 150
    doc = pymupdf.open()
    page = doc.new_page()
    rng = random.Random(0)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, side, side), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    for _ in range(12000):
        pix.set_pixel(rng.randrange(side), rng.randrange(side), tuple(rng.randrange(256) for _ in range(3)))
    page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


def encrypted() -> bytes:
    """A one-page PDF that needs a password to open (user password 'secret')."""
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 550, 800), LONG_TEXT, fontsize=9)
    return doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner", garbage=3, deflate=True
    )


if __name__ == "__main__":
    for name, build in {
        "text_pages": text_pages,
        "dense_text": dense_text,
        "poster": poster,
        "noisy": noisy,
        "encrypted": encrypted,
    }.items():
        data = build()
        (HERE / f"{name}.pdf").write_bytes(data)
        print(f"{name}.pdf  {len(data):>8,} bytes")
