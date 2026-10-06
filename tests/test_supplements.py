"""Unit tests for `bugsigdb_curation.supplements` -- fully offline.

`parse_supplement_refs`/`unpack_supplement_zip`/`supplement_to_text`/
`supplement_to_model_document` are pure (inline XML fixtures / in-memory
zip+xlsx+docx built inline, no network). `fetch_supplement_zip`/
`fetch_supplements` are covered with `pytest_httpx` mocks -- no live requests.
"""

from __future__ import annotations

import asyncio
import csv
import io
import zipfile
import zlib

import docx
import httpx
import openpyxl
import pytest
from pytest_httpx import HTTPXMock

from bugsigdb_curation.supplements import (
    EUROPEPMC_SUPPLEMENTARY_FILES_URL,
    ZIP_TIMEOUT_SECONDS,
    SupplementFile,
    SupplementRef,
    fetch_supplement_zip,
    fetch_supplements,
    parse_supplement_refs,
    supplement_to_model_document,
    supplement_to_text,
    unpack_supplement_zip,
)

XML_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <body>
    <supplementary-material id="MOESM1">
      <label>Supplementary Material 1</label>
      <caption><p>Uncropped <italic>gel</italic> images.</p></caption>
      <media xlink:href="41598_2021_99379_MOESM1_ESM.pdf" mimetype="application" mime-subtype="pdf"/>
    </supplementary-material>
    <supplementary-material id="MOESM2">
      <label>Supplementary Table S1</label>
      <caption><p>Sample metadata.</p></caption>
      <media xlink:href="41598_2021_99379_MOESM2_ESM.xlsx"/>
    </supplementary-material>
  </body>
</article>
"""

NO_SUPPLEMENTS_XML = "<article><body><p>nothing here</p></body></article>"


# --- parse_supplement_refs ----------------------------------------------------------------


def test_parse_supplement_refs_extracts_id_filename_label_caption():
    refs = parse_supplement_refs(XML_FIXTURE)

    assert refs == [
        SupplementRef(
            supplement_id="MOESM1",
            filename="41598_2021_99379_MOESM1_ESM.pdf",
            label="Supplementary Material 1",
            caption="Uncropped gel images.",
        ),
        SupplementRef(
            supplement_id="MOESM2",
            filename="41598_2021_99379_MOESM2_ESM.xlsx",
            label="Supplementary Table S1",
            caption="Sample metadata.",
        ),
    ]


def test_parse_supplement_refs_returns_empty_list_when_none_present():
    assert parse_supplement_refs(NO_SUPPLEMENTS_XML) == []


# Real EuropePMC shape: caption is nested INSIDE <media> (not a direct child
# of <supplementary-material>), carries the <?suppdata-*?> processing
# instructions, and uses inline markup like <bold>. Verified against live
# PMC8497572 / PMC10590023 fullTextXML. A direct-child `find("caption")`
# returned "" on every real document (ledger: synthetic fixture missed it).
REAL_SHAPE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <body>
    <supplementary-material content-type="local-data" id="MOESM15" position="float">
      <media xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="40168_2023_1671_MOESM15_ESM.xlsx" position="float">
        <?suppdata-name 40168_2023_1671_MOESM15_ESM.xlsx?><?suppdata-size 69304?>
        <caption><p><bold>Additional file 15:</bold> Differential abundance results.</p></caption>
      </media>
    </supplementary-material>
  </body>
</article>
"""


def test_parse_supplement_refs_reads_caption_nested_in_media():
    refs = parse_supplement_refs(REAL_SHAPE_XML)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.supplement_id == "MOESM15"
    assert ref.filename == "40168_2023_1671_MOESM15_ESM.xlsx"
    # inline <bold> flattened; caption recovered despite living under <media>
    assert ref.caption == "Additional file 15: Differential abundance results."


def test_parse_supplement_refs_falls_back_to_synthesized_id_and_none_filename():
    xml = "<article><body><supplementary-material><caption><p>no id, no media</p></caption></supplementary-material></body></article>"
    refs = parse_supplement_refs(xml)
    assert len(refs) == 1
    assert refs[0].supplement_id == "supp-0"
    assert refs[0].filename is None
    assert refs[0].label == ""
    assert refs[0].caption == "no id, no media"


# --- unpack_supplement_zip ----------------------------------------------------------------


def _build_zip(entries: dict[str, bytes], *, dirs: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in dirs:
            zf.writestr(zipfile.ZipInfo(name if name.endswith("/") else name + "/"), "")
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_unpack_supplement_zip_skips_directory_entries_and_maps_media_type():
    zip_bytes = _build_zip(
        {"a.pdf": b"%PDF-1.4 fake", "b.csv": b"x,y\n1,2\n", "sub/c.png": b"fake-png-bytes"},
        dirs=("sub",),
    )
    files = unpack_supplement_zip(zip_bytes)

    by_name = {f.filename: f for f in files}
    assert set(by_name) == {"a.pdf", "b.csv", "c.png"}
    assert by_name["a.pdf"].media_type == "pdf"
    assert by_name["b.csv"].media_type == "csv"
    assert by_name["c.png"].media_type == "image"
    assert by_name["a.pdf"].raw_bytes == b"%PDF-1.4 fake"


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("f.pdf", "pdf"),
        ("f.xlsx", "xlsx"),
        ("f.xls", "xlsx"),
        ("f.csv", "csv"),
        ("f.tsv", "tsv"),
        ("f.docx", "docx"),
        ("f.doc", "docx"),
        ("f.jpg", "image"),
        ("f.png", "image"),
        ("f.txt", "other"),
        ("f", "other"),
    ],
)
def test_unpack_supplement_zip_media_type_by_extension(filename, expected):
    zip_bytes = _build_zip({filename: b"data"})
    files = unpack_supplement_zip(zip_bytes)
    assert files[0].media_type == expected


def test_unpack_skips_videos_eps_nested_zips_and_oversized_members_and_reports_them():
    zip_bytes = _build_zip(
        {
            "movie.MP4": b"v",
            "clip.mov": b"v",
            "fig.eps": b"e",
            "inner.zip": b"z",
            "huge.csv": b"x" * 200,
            "keep.csv": b"a,b\n",
        }
    )
    skipped: list[tuple[str, str]] = []
    files = unpack_supplement_zip(zip_bytes, max_member_bytes=100, skipped=skipped)

    assert [f.filename for f in files] == ["keep.csv"]
    reasons = dict(skipped)
    assert set(reasons) == {"movie.MP4", "clip.mov", "fig.eps", "inner.zip", "huge.csv"}
    assert "larger than" in reasons["huge.csv"]
    assert "not useful" in reasons["movie.MP4"]


def test_unpack_without_a_skipped_collector_still_skips():
    files = unpack_supplement_zip(_build_zip({"a.mp4": b"v", "b.csv": b"x\n"}))
    assert [f.filename for f in files] == ["b.csv"]


@pytest.mark.parametrize("exc", [zlib.error("bad stream"), EOFError("truncated"), NotImplementedError("compression type 99"), RuntimeError("File is encrypted")])
def test_unpack_records_an_unreadable_member_and_keeps_the_others(monkeypatch, exc):
    real_read = zipfile.ZipFile.read

    def read(self, name, *args, **kwargs):
        if name == "bad.csv":
            raise exc
        return real_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    skipped: list[tuple[str, str]] = []
    files = unpack_supplement_zip(_build_zip({"a.csv": b"1\n", "bad.csv": b"2\n", "c.csv": b"3\n"}), skipped=skipped)
    assert [f.filename for f in files] == ["a.csv", "c.csv"]
    ((name, reason),) = skipped
    assert name == "bad.csv" and reason.startswith("unreadable member") and type(exc).__name__ in reason


def test_unpack_survives_a_really_corrupted_deflate_stream():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ok.csv", "a,b\n")
        zf.writestr("bad.csv", "x" * 5000)
    raw = bytearray(buf.getvalue())
    start = raw.index(b"bad.csv") + len(b"bad.csv")
    raw[start + 2 : start + 6] = b"\xff\xff\xff\xff"  # garble the compressed data
    skipped: list[tuple[str, str]] = []
    files = unpack_supplement_zip(bytes(raw), skipped=skipped)
    assert [f.filename for f in files] == ["ok.csv"]
    assert [name for name, _ in skipped] == ["bad.csv"]


def test_unpack_stops_at_the_total_uncompressed_budget_and_records_what_it_skipped():
    zip_bytes = _build_zip({"a.csv": b"x" * 60, "b.csv": b"x" * 60, "c.csv": b"x" * 30})
    skipped: list[tuple[str, str]] = []
    files = unpack_supplement_zip(zip_bytes, max_total_bytes=100, skipped=skipped)
    assert [f.filename for f in files] == ["a.csv", "c.csv"]  # b.csv would push the total to 120; c.csv still fits
    ((name, reason),) = skipped
    assert name == "b.csv" and "total" in reason


def test_unpack_reads_at_most_max_members_and_records_the_rest():
    zip_bytes = _build_zip({f"f{i}.csv": b"x\n" for i in range(7)})
    skipped: list[tuple[str, str]] = []
    files = unpack_supplement_zip(zip_bytes, max_members=5, skipped=skipped)
    assert [f.filename for f in files] == [f"f{i}.csv" for i in range(5)]
    ((name, reason),) = skipped
    assert name == "(supplementary files zip members)" and "2 further" in reason and "5" in reason


def test_unpack_default_budgets_are_200mb_and_500_members():
    import inspect

    params = inspect.signature(unpack_supplement_zip).parameters
    assert params["max_total_bytes"].default == 200 * 1024 * 1024
    assert params["max_members"].default == 500


def test_unpack_disambiguates_duplicate_basenames_from_different_folders():
    zip_bytes = _build_zip({"a/S1.xlsx": b"one", "b/S1.xlsx": b"two", "S1.xlsx": b"three"})
    files = unpack_supplement_zip(zip_bytes)
    assert [(f.filename, f.raw_bytes) for f in files] == [
        ("S1.xlsx", b"one"), ("S1 (2).xlsx", b"two"), ("S1 (3).xlsx", b"three"),
    ]
    assert all(f.media_type == "xlsx" for f in files)


# --- fetch_supplement_zip / fetch_supplements (network, mocked) --------------------------


def test_fetch_supplement_zip_returns_bytes_on_200(httpx_mock: HTTPXMock):
    zip_bytes = _build_zip({"a.pdf": b"data"})
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC8497572"),
        content=zip_bytes,
        headers={"Content-Type": "application/zip"},
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC8497572", client=client)

    assert asyncio.run(run()) == zip_bytes


def test_fetch_supplement_zip_returns_none_on_404(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC0000000"), status_code=404
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC0000000", client=client)

    assert asyncio.run(run()) is None


def test_fetch_supplement_zip_returns_none_on_non_zip_content_type(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC1111111"),
        content=b"<html>not a zip</html>",
        headers={"Content-Type": "text/html"},
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC1111111", client=client)

    assert asyncio.run(run()) is None


def test_fetch_supplement_zip_returns_none_on_non_404_http_error(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC2222222"), status_code=500
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC2222222", client=client)

    assert asyncio.run(run()) is None


def test_fetch_supplement_zip_aborts_when_the_body_exceeds_max_bytes(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC3333333"),
        content=b"x" * 5000,
        headers={"Content-Type": "application/zip"},
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC3333333", client=client, max_bytes=1000)

    assert asyncio.run(run()) is None


def test_fetch_supplement_zip_refuses_up_front_on_a_declared_oversize_length(httpx_mock: HTTPXMock):
    read: list[bool] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(True)
            yield b"x"

    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC3333334"),
        stream=Body(),
        headers={"Content-Type": "application/zip", "Content-Length": "262000000"},
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC3333334", client=client)

    assert asyncio.run(run()) is None
    assert read == []  # refused from the headers alone, nothing downloaded


def test_fetch_supplement_zip_aborts_when_the_total_time_exceeds_timeout(httpx_mock: HTTPXMock):
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200, content=b"zip", headers={"Content-Type": "application/zip"})

    httpx_mock.add_callback(slow, url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC3333335"))

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip("PMC3333335", client=client, timeout=0.05)

    assert asyncio.run(run()) is None


def test_fetch_supplement_zip_lets_the_overall_deadline_govern_not_the_clients_shorter_per_operation_timeout(
    httpx_mock: HTTPXMock,
):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC3333336"),
        content=b"zip",
        headers={"Content-Type": "application/zip"},
    )

    async def run() -> bytes | None:
        async with httpx.AsyncClient(timeout=1.0) as client:
            return await fetch_supplement_zip("PMC3333336", client=client, timeout=75.0)

    assert asyncio.run(run()) == b"zip"
    assert httpx_mock.get_requests()[0].extensions["timeout"]["read"] == 75.0


def test_fetch_supplement_zip_default_guards_are_60mb_and_240s():
    import inspect

    params = inspect.signature(fetch_supplement_zip).parameters
    assert params["max_bytes"].default == 60 * 1024 * 1024
    assert params["timeout"].default == 240.0 == ZIP_TIMEOUT_SECONDS


def _fetch_reason(httpx_mock: HTTPXMock, pmcid: str, **kwargs) -> str:
    skipped: list[tuple[str, str]] = []

    async def run() -> bytes | None:
        async with httpx.AsyncClient() as client:
            return await fetch_supplement_zip(pmcid, client=client, skipped=skipped, **kwargs)

    assert asyncio.run(run()) is None
    ((name, reason),) = skipped
    assert name == "(supplementary files zip)"
    return reason


def test_fetch_supplement_zip_says_why_nothing_came_back(httpx_mock: HTTPXMock):
    url = EUROPEPMC_SUPPLEMENTARY_FILES_URL.format

    httpx_mock.add_response(url=url(pmcid="PMC1"), status_code=404)
    assert _fetch_reason(httpx_mock, "PMC1") == "no supplementary files (HTTP 404)"

    httpx_mock.add_response(url=url(pmcid="PMC2"), status_code=503)
    assert _fetch_reason(httpx_mock, "PMC2") == "fetch failed: HTTP 503"

    httpx_mock.add_response(url=url(pmcid="PMC3"), content=b"<html>", headers={"Content-Type": "text/html"})
    assert _fetch_reason(httpx_mock, "PMC3") == "response was not a zip (content-type 'text/html')"

    httpx_mock.add_response(
        url=url(pmcid="PMC4"), content=b"x" * 5000, headers={"Content-Type": "application/zip"}
    )
    assert "too large" in _fetch_reason(httpx_mock, "PMC4", max_bytes=1000)

    httpx_mock.add_response(
        url=url(pmcid="PMC5"), content=b"x", headers={"Content-Type": "application/zip", "Content-Length": "999999"}
    )
    assert "too large" in _fetch_reason(httpx_mock, "PMC5", max_bytes=1000)


def test_fetch_supplement_zip_reports_a_timeout_and_a_transport_error(httpx_mock: HTTPXMock):
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200, content=b"zip", headers={"Content-Type": "application/zip"})

    httpx_mock.add_callback(slow, url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC6"))
    assert _fetch_reason(httpx_mock, "PMC6", timeout=0.05) == "download timed out after 0.05 s"

    httpx_mock.add_exception(httpx.ConnectError("no route"), url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC7"))
    assert _fetch_reason(httpx_mock, "PMC7").startswith("fetch failed: ConnectError")


def test_fetch_supplements_passes_the_failure_reason_through_to_skipped(httpx_mock: HTTPXMock):
    httpx_mock.add_response(url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC8"), status_code=404)
    skipped: list[tuple[str, str]] = []

    async def run() -> list[SupplementFile]:
        async with httpx.AsyncClient() as client:
            return await fetch_supplements("PMC8", client=client, skipped=skipped)

    assert asyncio.run(run()) == []
    assert skipped == [("(supplementary files zip)", "no supplementary files (HTTP 404)")]


def test_fetch_supplements_returns_unpacked_files(httpx_mock: HTTPXMock):
    zip_bytes = _build_zip({"tiny.csv": b"a,b\n1,2\n"})
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC8497572"),
        content=zip_bytes,
        headers={"Content-Type": "application/zip"},
    )

    async def run() -> list[SupplementFile]:
        async with httpx.AsyncClient() as client:
            return await fetch_supplements("PMC8497572", client=client)

    files = asyncio.run(run())
    assert len(files) == 1
    assert files[0].filename == "tiny.csv"
    assert files[0].media_type == "csv"


def test_fetch_supplements_returns_empty_list_on_404(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url=EUROPEPMC_SUPPLEMENTARY_FILES_URL.format(pmcid="PMC0000000"), status_code=404
    )

    async def run() -> list[SupplementFile]:
        async with httpx.AsyncClient() as client:
            return await fetch_supplements("PMC0000000", client=client)

    assert asyncio.run(run()) == []


# --- supplement_to_text -------------------------------------------------------------------


def _tiny_xlsx_bytes() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Taxon", "LDA score"])
    ws.append(["Bacteroides", 3.2])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _tiny_docx_bytes() -> bytes:
    document = docx.Document()
    document.add_paragraph("Uncropped gel images for Figure 2.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Sample"
    table.rows[0].cells[1].text = "Value"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_supplement_to_text_renders_xlsx_sheet_grid():
    f = SupplementFile(filename="s.xlsx", media_type="xlsx", raw_bytes=_tiny_xlsx_bytes())
    text = supplement_to_text(f)
    assert text is not None
    assert "Sheet1" in text
    assert "Bacteroides" in text
    assert "3.2" in text


def test_supplement_to_text_renders_csv_grid():
    raw = b"Taxon,LDA score\nBacteroides,3.2\n"
    f = SupplementFile(filename="s.csv", media_type="csv", raw_bytes=raw)
    text = supplement_to_text(f)
    assert text == "Taxon\tLDA score\nBacteroides\t3.2"


def test_supplement_to_text_renders_tsv_grid():
    raw = b"Taxon\tLDA score\nBacteroides\t3.2\n"
    f = SupplementFile(filename="s.tsv", media_type="tsv", raw_bytes=raw)
    text = supplement_to_text(f)
    assert text == "Taxon\tLDA score\nBacteroides\t3.2"


def test_supplement_to_text_renders_docx_paragraphs_and_tables():
    f = SupplementFile(filename="s.docx", media_type="docx", raw_bytes=_tiny_docx_bytes())
    text = supplement_to_text(f)
    assert text is not None
    assert "Uncropped gel images for Figure 2." in text
    assert "Sample\tValue" in text


def test_supplement_to_text_returns_none_for_pdf_image_other():
    for media_type in ("pdf", "image", "other"):
        f = SupplementFile(filename=f"s.{media_type}", media_type=media_type, raw_bytes=b"data")
        assert supplement_to_text(f) is None


def test_supplement_to_text_returns_none_for_malformed_xlsx_without_raising():
    f = SupplementFile(filename="bad.xlsx", media_type="xlsx", raw_bytes=b"not an xlsx file")
    assert supplement_to_text(f) is None


def test_supplement_to_text_returns_none_for_malformed_docx_without_raising():
    f = SupplementFile(filename="bad.docx", media_type="docx", raw_bytes=b"not a docx file")
    assert supplement_to_text(f) is None


# --- supplement_to_model_document ---------------------------------------------------------


def test_supplement_to_model_document_pdf_returns_document_block():
    f = SupplementFile(filename="s.pdf", media_type="pdf", raw_bytes=b"%PDF-1.4 fake bytes")
    block = supplement_to_model_document(f)
    assert block is not None
    assert block["type"] == "file"
    assert block["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_supplement_to_model_document_non_pdf_returns_none():
    for media_type in ("xlsx", "csv", "tsv", "docx", "image", "other"):
        f = SupplementFile(filename=f"s.{media_type}", media_type=media_type, raw_bytes=b"data")
        assert supplement_to_model_document(f) is None
