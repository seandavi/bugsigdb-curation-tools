"""Read a PDF: page count, page text, page size and a page rendered to JPEG.

The one place that knows which PDF library is in use (pypdfium2, Apache-2.0/BSD, with Pillow to encode the
rendered bitmap), so callers -- the supplement lever and the decision-probe benchmarks -- depend only on this
small surface. Every way reading can fail (corrupt or encrypted file, a bad page, a render or encode error)
surfaces as :class:`PdfError`, so a caller guards one exception type, not a library's.

pdfium is not thread-safe; a module-wide lock serialises every call into it, so documents may be read from
worker threads.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import ParamSpec, TypeVar

import pypdfium2

_P = ParamSpec("_P")
_T = TypeVar("_T")

_PDFIUM_LOCK = threading.RLock()
_POINTS_PER_INCH = 72


class PdfError(Exception):
    """A PDF could not be opened or read (corrupt, encrypted, a bad page, a failed render)."""


def _guarded(fn: Callable[_P, _T]) -> Callable[_P, _T]:
    """Run `fn` under the pdfium lock; anything it raises becomes :class:`PdfError`."""

    def run(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        with _PDFIUM_LOCK:
            try:
                return fn(*args, **kwargs)
            except PdfError:
                raise
            except Exception as exc:  # a malformed third-party file must not leak library types
                raise PdfError(f"{type(exc).__name__}: {exc}") from exc

    run.__doc__ = fn.__doc__
    run.__name__ = fn.__name__
    return run


def _normalise_text(raw: str) -> str:
    """pdfium's text uses CRLF line ends and keeps trailing spaces; return plain ``\\n``-separated lines."""
    lines = (line.rstrip() for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


class PdfDoc:
    """An open PDF. Pages are 0-based. Use through :func:`open_pdf`."""

    def __init__(self, document: pypdfium2.PdfDocument) -> None:
        self._document = document

    @property
    @_guarded
    def n_pages(self) -> int:
        return len(self._document)

    @_guarded
    def page_text(self, index: int) -> str:
        """The page's extractable text: ``\\n``-separated lines, no trailing whitespace; empty for an image-only page."""
        page = self._document[index]
        try:
            textpage = page.get_textpage()
            try:
                return _normalise_text(textpage.get_text_range())
            finally:
                textpage.close()
        finally:
            page.close()

    @_guarded
    def page_size(self, index: int) -> tuple[float, float]:
        """``(width, height)`` of the page in points (1/72 inch)."""
        page = self._document[index]
        try:
            width, height = page.get_size()
            return float(width), float(height)
        finally:
            page.close()

    @_guarded
    def render_jpeg(self, index: int, dpi: int, quality: int) -> bytes:
        """The page rendered at `dpi` and JPEG-encoded at `quality` (1-95)."""
        page = self._document[index]
        try:
            image = page.render(scale=dpi / _POINTS_PER_INCH).to_pil().convert("RGB")
        finally:
            page.close()
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality)
        return buffer.getvalue()

    def close(self) -> None:
        with _PDFIUM_LOCK:
            self._document.close()


@contextmanager
def open_pdf(data: bytes) -> Iterator[PdfDoc]:
    """Open the PDF in `data`; raises :class:`PdfError` if it cannot be read (corrupt, or needs a password)."""
    document = _guarded(pypdfium2.PdfDocument)(data)
    doc = PdfDoc(document)
    try:
        yield doc
    finally:
        doc.close()
