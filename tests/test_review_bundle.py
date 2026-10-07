"""Tests for `bugsigdb review bundle`: one shareable static bundle (index + packets + docs) from a packets dir."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import typer
from review_support import load_annotations, load_draft, sample_evidence
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.review import bundle as bundle_module
from bugsigdb_curation.review.bundle import (
    BundleError,
    build_bundle,
    refuse_existing_outputs,
    write_bundle_tree,
    write_bundle_zip,
    zip_bytes,
)
from bugsigdb_curation.review.packet import build_manifest, build_packet, make_meta

runner = CliRunner()

DATE = "2026-10-07"
NAME = f"bugsigdb-review-{DATE}"
CONTACT = "Sean Davis <sean@example.org>"
HOSTILE_TITLE = 'Evil <script>alert("x")</script> & "quotes" \'single\''
HOSTILE_AUTHOR = '<img src=x onerror=alert(1)> O\'Brien "B"'

_PLAIN_ENV = {"COLUMNS": "300", "NO_COLOR": "1", "TERM": "dumb"}


def write_packet(
    directory: Path,
    pmid: str,
    *,
    title: str | None = None,
    authors: list[str] | None = ["Doe J", "Roe R", "Poe P"],  # noqa: B006 - never mutated; None drops the field
    license_: str | None = "cc by",
    with_image: bool = True,
    builder_commit: str | None = "abc1234",
    evidence_authors: tuple[str, ...] = (),
    problems: tuple[str, ...] = (),
    table_only: bool = False,
    edit: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Build a real packet + manifest for a variant of the fixture draft, the way `review packet` does."""
    record = load_draft()
    record["pmid"] = int(pmid)
    record["uid"] = pmid
    if title is not None:
        record["title"] = title
    if edit is not None:
        edit(record)
    if table_only:
        for experiment in record["experiments"]:
            for signature in experiment["signatures"]:
                signature["source"] = "Table 1"
    if authors is None:
        record.pop("authors")
    else:
        record["authors"] = authors
    evidence = replace(sample_evidence(license_), authors=evidence_authors, problems=problems)
    if not with_image:
        evidence = replace(evidence, images={})
    meta = make_meta(record, built_at="2026-10-06T12:00:00Z", builder_commit=builder_commit, pmcid="PMC9000001")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pmid}.html").write_text(build_packet(record, load_annotations(), evidence, meta), encoding="utf-8")
    manifest = build_manifest(record, load_annotations(), evidence, meta)
    (directory / f"{pmid}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def packets(tmp_path: Path) -> Path:
    directory = tmp_path / "packets"
    write_packet(directory, "99000002", title="Second study", authors=["Zed Z", "Yu Y"])
    write_packet(directory, "99000001")
    write_packet(directory, "99000003", title=HOSTILE_TITLE, authors=[HOSTILE_AUTHOR, "Roe R"])
    return directory


def make_bundle(packets_dir: Path) -> Any:
    return build_bundle(packets_dir, name=NAME, date=DATE, contact=CONTACT)


class _Refs(HTMLParser):
    """Every tag and every URL-bearing attribute of a page, plus its <style> text."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []  # (tag, attribute, value)
        self.tags: list[str] = []
        self.style = ""
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self._in_style = tag == "style"
        self.refs += [(tag, k, v) for k, v in attrs if k in ("src", "href", "action", "data", "srcset") and v]

    def handle_endtag(self, tag: str) -> None:
        self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.style += data


def parse_index(bundle: Any) -> _Refs:
    parser = _Refs()
    parser.feed(bundle.files["index.html"].decode("utf-8"))
    return parser


def index_text(bundle: Any) -> str:
    """The index page as whitespace-normalised visible-ish text (tags removed, entities kept)."""
    return " ".join(re.sub(r"<[^>]+>", " ", bundle.files["index.html"].decode("utf-8")).split())


# --- index.html ---------------------------------------------------------------------------------------------


def test_index_has_one_card_per_study_sorted_by_pmid(packets: Path) -> None:
    html = make_bundle(packets).files["index.html"].decode("utf-8")
    assert html.count('<article class="card">') == 3
    positions = [html.index(f'href="packets/{pmid}.html"') for pmid in ("99000001", "99000002", "99000003")]
    assert positions == sorted(positions)


def test_index_carries_the_required_notices_and_instructions(packets: Path) -> None:
    bundle = make_bundle(packets)
    text = index_text(bundle)
    assert "MACHINE-GENERATED" in text and "UNREVIEWED" in text and "not curated data" in text
    assert "Extract the zip first" in text and "email attachment viewer" in text
    assert "Chrome, Firefox or Safari" in text
    assert "unsure" in text and "never guess" in text
    assert "Export verdicts (JSON)" in text
    assert "Sean Davis &lt;sean@example.org&gt;" in text
    assert "autosaves" in text
    assert "nothing is uploaded" in text.lower()
    assert NAME in text and DATE in text and "abc1234" in text and bundle.manifest["content_sha256"] in text


def _normalised(text: str) -> str:
    return " ".join(text.split())


def test_reviewer_instructions_are_accurate_about_the_export(packets: Path) -> None:
    bundle = make_bundle(packets)
    texts = {
        "README.txt": _normalised(bundle.files["README.txt"].decode("utf-8")),
        "index.html": index_text(bundle),
    }
    for where, text in texts.items():
        assert "verdicts_<pmid>_<your-name>.json" in text.replace("&lt;", "<").replace("&gt;", ">"), where
        assert "timestamp" not in text, where
        assert "Downloads folder" in text, where
        assert "NOT the CSV" in text, where
        assert "once per packet when you finish" in text and "if you stop early" in text, where
        assert "extract the zip somewhere else" in text.lower(), where


def test_the_export_file_name_in_the_instructions_matches_packet_js() -> None:
    script = (Path(bundle_module.__file__).parent / "packet.js").read_text(encoding="utf-8")
    assert '"verdicts_" + verdicts.pmid + "_" + slug + "." + extension' in script


def test_identity_wording_is_exact_and_points_to_manifest_hash(packets: Path) -> None:
    bundle = make_bundle(packets)
    readme = _normalised(bundle.files["README.txt"].decode("utf-8"))
    assert "sha256 of manifest.json" in readme
    assert (
        "content_sha256" in readme
        and "packets/*.html" in readme
        and "does not cover index.html, README.txt or ATTRIBUTION.txt" in readme
    )
    assert "same Python" in _normalised(bundle_module.__doc__ or "") and "manifest.json" in (
        bundle_module.__doc__ or ""
    )
    text = index_text(bundle)
    assert "Content hash (sha256 of the packets)" not in text and "Packets hash" in text


def test_index_pluralises_counts(packets: Path) -> None:
    assert "3 studies to review" in index_text(make_bundle(packets))


def test_index_without_contact_says_who_sent_it(packets: Path) -> None:
    bundle = build_bundle(packets, name=NAME, date=DATE, contact=None)
    assert "whoever sent you this bundle" in bundle.files["index.html"].decode("utf-8")


def test_index_escapes_hostile_titles_and_authors(packets: Path) -> None:
    bundle = make_bundle(packets)
    html = bundle.files["index.html"].decode("utf-8")
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt; O&#x27;Brien &quot;B&quot;" in html
    assert {"script", "img"}.isdisjoint(parse_index(bundle).tags)


def test_index_makes_no_external_requests(packets: Path) -> None:
    bundle = make_bundle(packets)
    html = bundle.files["index.html"].decode("utf-8")
    parser = parse_index(bundle)
    assert {"script", "img", "link", "iframe", "object", "embed", "form", "video", "audio", "source"}.isdisjoint(
        parser.tags
    )
    for tag, attribute, value in parser.refs:
        assert (tag, attribute) == ("a", "href"), f"only plain links are allowed, found <{tag} {attribute}>"
        assert value.startswith(("https://", "packets/")), value
    assert "@import" not in html and "url(" not in html and "http://" not in html
    assert not re.search(r"https?://", parser.style)


def test_index_relative_links_resolve_to_existing_files(packets: Path, tmp_path: Path) -> None:
    bundle = make_bundle(packets)
    root = write_bundle_tree(bundle, tmp_path / "out")
    relative = [value for _tag, _attr, value in parse_index(bundle).refs if not value.startswith("https://")]
    assert sorted(relative) == [f"packets/{pmid}.html" for pmid in ("99000001", "99000002", "99000003")]
    for value in relative:
        assert (root / value).is_file(), value


def test_index_counts_and_evidence_match_the_embedded_records(packets: Path) -> None:
    draft = load_draft()
    n_exp = len(draft["experiments"])
    n_sig = sum(len(e["signatures"]) for e in draft["experiments"])
    n_taxa = sum(len(s["taxa"]) for e in draft["experiments"] for s in e["signatures"])
    text = index_text(make_bundle(packets))
    assert text.count(f"{n_exp} experiments · {n_sig} signatures · {n_taxa} taxa") == 3
    assert text.count("Evidence cited: Figure 2, Table 1") == 3  # the fixture's signatures cite these two


def test_index_card_links_licence_size_and_packet_id(packets: Path) -> None:
    bundle = make_bundle(packets)
    html = bundle.files["index.html"].decode("utf-8")
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/99000001/"' in html
    assert 'href="https://pmc.ncbi.nlm.nih.gov/articles/PMC9000001/"' in html
    assert 'href="https://doi.org/10.1000/review.fixture.1"' in html
    assert "Licence: cc by" in html and "Open packet →" in html
    entry = next(p for p in bundle.manifest["packets"] if p["pmid"] == "99000001")
    assert entry["packet_id"] in html
    size = len(bundle.files["packets/99000001.html"])
    assert (f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB") in html


_SAFE_HREF = re.compile(
    r"https://pubmed\.ncbi\.nlm\.nih\.gov/\d+/"
    r"|https://pmc\.ncbi\.nlm\.nih\.gov/articles/PMC\d+/"
    r"|https://doi\.org/10\.\d{4,9}/[^\s]+"
    r"|packets/\d+\.html"
)


def _assert_links_are_safe(bundle: Any) -> list[str]:
    hrefs = [value for _tag, _attr, value in parse_index(bundle).refs]
    for href in hrefs:
        assert _SAFE_HREF.fullmatch(href), href
        parsed = urlparse(href)
        assert parsed.scheme in ("https", "") and not parsed.query and not parsed.fragment, href
        assert ".." not in parsed.path.split("/") and "." not in parsed.path.split("/"), href
    return hrefs


@pytest.mark.parametrize(
    "doi",
    [
        "javascript:alert(1)",
        "../..",
        "10.1000/../../x",
        "10.1000/x?y#z",
        "//evil.example/10.1000/x",
        'https://evil.example/"><svg onload=alert(1)>',
        "10.1000/a b\nc",
    ],
)
def test_hostile_doi_never_changes_the_link_scheme_or_path(tmp_path: Path, doi: str) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", with_image=False, edit=lambda record: record.update(doi=doi))
    _assert_links_are_safe(make_bundle(directory))


@pytest.mark.parametrize("pmcid", ["../x", "PMC1/../../x", "javascript:alert(1)", "PMC1?x#y", "pmc1", "PMC\u00b2"])
def test_hostile_pmcid_never_changes_the_link_scheme_or_path(packets: Path, pmcid: str) -> None:
    _edit_manifest(packets, "99000001", pmcid=pmcid)
    hrefs = _assert_links_are_safe(make_bundle(packets))
    assert not any("pmc.ncbi" in h and "99000001" in h for h in hrefs)


def test_well_formed_doi_with_reserved_characters_is_encoded_not_dropped(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", with_image=False, edit=lambda record: record.update(doi="10.1000/a(b)?c#d"))
    hrefs = _assert_links_are_safe(make_bundle(directory))
    assert "https://doi.org/10.1000/a(b)%3Fc%23d" in hrefs


def test_index_truncates_long_author_lists(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=[f"Author{i} A" for i in range(12)])
    html = make_bundle(directory).files["index.html"].decode("utf-8")
    assert "Author0 A" in html and "Author5 A" in html and "Author6 A" not in html
    assert "et al." in html


def test_authors_fall_back_to_the_articles_own_when_the_draft_has_none(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=None, evidence_authors=("Jats J", "Xml X"))
    bundle = make_bundle(directory)
    assert "Jats J; Xml X" in bundle.files["index.html"].decode("utf-8")
    assert "Jats J; Xml X" in bundle.files["ATTRIBUTION.txt"].decode("utf-8")


def test_authors_are_marked_when_nobody_states_them(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=None, with_image=False)
    bundle = make_bundle(directory)
    assert "authors not stated" in bundle.files["index.html"].decode("utf-8")
    assert "Authors: not stated" in bundle.files["ATTRIBUTION.txt"].decode("utf-8")


def test_authors_come_from_the_meta_the_packet_recorded_not_the_page_markup(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=None, evidence_authors=("Jats J", "Xml X"))
    page = (directory / "99000001.html").read_text(encoding="utf-8")
    assert page.count('<div class="citation">') == 1
    (directory / "99000001.html").write_text(
        page.replace('<div class="citation">', '<div class="cite">'), encoding="utf-8"
    )
    bundle = make_bundle(directory)
    assert "Jats J; Xml X" in bundle.files["ATTRIBUTION.txt"].decode("utf-8")
    assert not any("predates" in w for w in bundle.warnings)


def _strip_attribution_authors(directory: Path, stem: str) -> None:
    path = directory / f"{stem}.html"
    page = path.read_text(encoding="utf-8")
    stripped = re.sub(r',\s*"attribution_authors":\s*\[[^\]]*\]', "", page)
    assert stripped != page
    path.write_text(stripped, encoding="utf-8")


def test_older_packets_without_recorded_authors_fall_back_to_the_page_and_say_so(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=None, evidence_authors=("Jats J", "Xml X"))
    write_packet(directory, "99000002")
    _strip_attribution_authors(directory, "99000001")
    _strip_attribution_authors(directory, "99000002")
    bundle = make_bundle(directory)
    attribution = bundle.files["ATTRIBUTION.txt"].decode("utf-8")
    assert "Jats J; Xml X" in attribution and "Doe J; Roe R; Poe P" in attribution
    predates = [w for w in bundle.warnings if "predates" in w]
    assert len(predates) == 2 and "99000001" in predates[0] and "attribution_authors" in predates[0]


def test_cc_by_packet_with_figures_but_no_resolvable_authors_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=None, evidence_authors=())
    with pytest.raises(BundleError, match=r"99000001\.html.*figure image.*no authors.*credit"):
        make_bundle(directory)


def test_packet_text_cannot_make_authors_look_resolved_when_meta_says_none(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", authors=None, evidence_authors=())
    page = (directory / "99000001.html").read_text(encoding="utf-8")
    page = page.replace("(no authors in the draft)", "Fake F; Fake G")
    (directory / "99000001.html").write_text(page, encoding="utf-8")
    with pytest.raises(BundleError, match="no authors"):
        make_bundle(directory)


def _inject_image(directory: Path, stem: str) -> None:
    path = directory / f"{stem}.html"
    page = path.read_text(encoding="utf-8")
    block = '<script type="application/json" id="packet-images">'
    assert f"{block}{{}}</script>" in page
    image = json.dumps({"Figure 2": {"type": "image/png", "data": "AAAA"}})
    path.write_text(page.replace(f"{block}{{}}</script>", f"{block}{image}</script>"), encoding="utf-8")


@pytest.mark.parametrize("license_", ["cc by-nc", "cc by-nd", "all rights reserved", None])
def test_packet_that_embeds_images_without_an_embedding_licence_is_refused(
    tmp_path: Path, license_: str | None
) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001")
    write_packet(directory, "99000002", license_=license_, with_image=False)
    _inject_image(directory, "99000002")
    with pytest.raises(
        BundleError, match=r"99000002\.html: embeds 1 figure image\(s\) but the licence is .*may not be"
    ) as e:
        make_bundle(directory)
    assert len(e.value.problems) == 1  # the clean packet is not blamed
    assert str(license_ if license_ else "unknown") in e.value.problems[0]


# --- README / ATTRIBUTION / manifest ------------------------------------------------------------------------


def test_readme_has_howto_legend_and_return_path(packets: Path) -> None:
    text = make_bundle(packets).files["README.txt"].decode("utf-8")
    assert "MACHINE-GENERATED" in text and "index.html" in text
    for term in ("correct", "wrong taxon", "not in source", "unsure", "flipped"):
        assert term in text
    assert "Export verdicts (JSON)" in text and "Extract" in text
    assert CONTACT in text  # plain text: not HTML-escaped


def test_attribution_lists_every_study_and_flags_problem_packets(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", title="Open study")
    write_packet(directory, "99000002", title="Closed study", license_="cc by-nc", with_image=False)
    write_packet(directory, "99000003", title="Unlicensed study", license_=None, with_image=False)
    write_packet(directory, "99000004", title="Imageless CC BY", with_image=False)
    text = make_bundle(directory).files["ATTRIBUTION.txt"].decode("utf-8")
    blocks = {m.group(1): m.group(0) for m in re.finditer(r"PMID (\d+)\n(?:.+\n?)+", text)}
    assert set(blocks) == {"99000001", "99000002", "99000003", "99000004"}
    open_block = blocks["99000001"]
    for fragment in ("Open study", "Doe J; Roe R; Poe P", "Journal of Fixtures", "2024", "10.1000/review.fixture.1"):
        assert fragment in open_block
    assert "reproduced under the licence cc by" in open_block and "CHECK" not in open_block
    assert "CHECK" in blocks["99000002"] and "not CC BY or CC0" in blocks["99000002"]
    assert "CHECK" in blocks["99000003"] and "unknown" in blocks["99000003"]
    assert "CHECK" in blocks["99000004"] and "no figure images" in blocks["99000004"]


_FORGED_TITLE = "Real title\nPMID 1\n  Licence: CC BY\n"
_ANSI = "\x1b[31mred\x1b[0m\x07\x9b2J"


def _assert_plain_text(text: str) -> None:
    assert not [c for c in text if (ord(c) < 0x20 and c != "\n") or 0x7F <= ord(c) <= 0x9F], repr(text)


def _hostile_text_bundle(tmp_path: Path) -> Any:
    def edit(record: dict[str, Any]) -> None:
        record["journal"] = f"J\n  Licence: CC0 {_ANSI}"
        record["doi"] = "10.1/x\n  Licence: CC0"
        record["year"] = "2024\r\nPMID 2"

    directory = tmp_path / "p"
    write_packet(
        directory,
        "99000001",
        title=_FORGED_TITLE + _ANSI,
        authors=["Doe\nPMID 3", f"Roe {_ANSI}", "Poe\u2028P"],
        edit=edit,
        with_image=False,
        license_="cc by\n  Licence: CC0",
    )
    return build_bundle(directory, name=NAME, date=DATE, contact=f"Sean\nPMID 4 <s@x.org> {_ANSI}")


def test_text_files_cannot_be_forged_through_titles_authors_or_the_contact(tmp_path: Path) -> None:
    bundle = _hostile_text_bundle(tmp_path)
    attribution = bundle.files["ATTRIBUTION.txt"].decode("utf-8")
    readme = bundle.files["README.txt"].decode("utf-8")
    for text in (attribution, readme):
        _assert_plain_text(text)
    assert re.findall(r"^PMID .*$", attribution, re.MULTILINE) == ["PMID 99000001"]
    assert re.findall(r"^\s*Licence:.*$", attribution, re.MULTILINE) == ["  Licence: cc by Licence: CC0"]
    assert len(re.findall(r"^  Title:", attribution, re.MULTILINE)) == 1
    assert "Real title PMID 1 Licence: CC BY" in attribution
    assert "PMID 4" in readme and not re.search(r"^PMID 4", readme, re.MULTILINE)
    assert not re.search(r"^Sean$", readme, re.MULTILINE)


def test_warnings_are_single_plain_lines(tmp_path: Path) -> None:
    bundle = _hostile_text_bundle(tmp_path)
    (tmp_path / "p" / "7\nfake\x1b[31m.manifest.json").write_text("{}", encoding="utf-8")
    bundle = build_bundle(tmp_path / "p", name=NAME, date=DATE, contact=None)
    assert any("manifest without a packet" in w for w in bundle.warnings)
    assert bundle.warnings
    for warning in bundle.warnings:
        assert "\n" not in warning
        _assert_plain_text(warning)


def test_manifest_lists_files_with_verifiable_hashes(packets: Path) -> None:
    bundle = make_bundle(packets)
    manifest = bundle.manifest
    assert manifest["name"] == NAME and manifest["built_at"] == DATE and manifest["builder_commit"] == "abc1234"
    paths = [f["path"] for f in manifest["files"]]
    assert paths == sorted(paths)
    assert set(paths) == set(bundle.files) - {"manifest.json"}
    for entry in manifest["files"]:
        data = bundle.files[entry["path"]]
        assert entry["sha256"] == hashlib.sha256(data).hexdigest() and entry["bytes"] == len(data)
    assert [p["pmid"] for p in manifest["packets"]] == ["99000001", "99000002", "99000003"]
    first = manifest["packets"][0]
    assert first["file"] == "packets/99000001.html" and first["packet_id"].startswith("99000001-")
    assert first["draft_sha256"] == json.loads(bundle.files["packets/99000001.manifest.json"])["draft_sha256"]
    assert json.loads(bundle.files["manifest.json"]) == manifest


def test_builder_commit_is_null_unless_uniform(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", builder_commit="aaa1111")
    write_packet(directory, "99000002", builder_commit="bbb2222")
    assert make_bundle(directory).manifest["builder_commit"] is None


def test_packets_are_copied_byte_for_byte(packets: Path) -> None:
    bundle = make_bundle(packets)
    for pmid in ("99000001", "99000002", "99000003"):
        for suffix in ("html", "manifest.json"):
            assert bundle.files[f"packets/{pmid}.{suffix}"] == (packets / f"{pmid}.{suffix}").read_bytes()


def test_content_hash_changes_with_the_packets(packets: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    write_packet(other, "99000001", title="Different")
    assert make_bundle(other).manifest["content_sha256"] != make_bundle(packets).manifest["content_sha256"]


# --- zip ----------------------------------------------------------------------------------------------------


def test_zip_is_deterministic(packets: Path) -> None:
    assert (
        hashlib.sha256(zip_bytes(make_bundle(packets))).digest()
        == hashlib.sha256(zip_bytes(make_bundle(packets))).digest()
    )


_EXPECTED_MEMBERS = [
    f"{NAME}/{path}"
    for path in (
        "ATTRIBUTION.txt",
        "README.txt",
        "index.html",
        "manifest.json",
        "packets/99000001.html",
        "packets/99000001.manifest.json",
        "packets/99000002.html",
        "packets/99000002.manifest.json",
        "packets/99000003.html",
        "packets/99000003.manifest.json",
    )
]


def test_zip_members_are_explicitly_ordered_and_stamped(packets: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes(make_bundle(packets)))) as zf:
        infos = zf.infolist()
    assert [i.filename for i in infos] == _EXPECTED_MEMBERS
    for info in infos:
        assert info.create_system == 3
        assert info.external_attr == 0o100644 << 16
        assert info.date_time == (2000, 1, 1, 0, 0, 0)
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert info.extra == b"" and info.comment == b"" and info.flag_bits == 0


def test_zip_is_identical_across_processes_and_hash_seeds(packets: Path) -> None:
    script = (
        "import hashlib, sys\n"
        "from pathlib import Path\n"
        "from bugsigdb_curation.review.bundle import build_bundle, zip_bytes\n"
        f"b = build_bundle(Path(sys.argv[1]), name={NAME!r}, date={DATE!r}, contact={CONTACT!r})\n"
        "print(hashlib.sha256(zip_bytes(b)).hexdigest())\n"
    )

    def digest(seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run(
            [sys.executable, "-c", script, str(packets)], env=env, capture_output=True, text=True, check=False
        )
        assert run.returncode == 0, run.stderr
        return run.stdout.strip()

    expected = hashlib.sha256(zip_bytes(make_bundle(packets))).hexdigest()
    assert digest("1") == digest("2") == expected


def test_zip_layout_single_top_folder_and_safe_members(packets: Path, tmp_path: Path) -> None:
    bundle = make_bundle(packets)
    path = tmp_path / "b.zip"
    path.write_bytes(zip_bytes(bundle))
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        infos = zf.infolist()
        names = [i.filename for i in infos]
        assert names == sorted(names)
        assert set(names) == {f"{NAME}/{p}" for p in bundle.files}
        for info in infos:
            assert not info.filename.startswith("/") and ".." not in info.filename.split("/")
            assert info.filename.split("/")[0] == NAME
            assert info.date_time == (2000, 1, 1, 0, 0, 0)
            assert (info.external_attr >> 16) == 0o100644
            assert zf.read(info) == bundle.files[info.filename.removeprefix(f"{NAME}/")]


def test_zip_manifest_hashes_verify_after_extraction(packets: Path, tmp_path: Path) -> None:
    path = tmp_path / "b.zip"
    path.write_bytes(zip_bytes(make_bundle(packets)))
    with zipfile.ZipFile(path) as zf:
        zf.extractall(tmp_path / "x")
    root = tmp_path / "x" / NAME
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        assert hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


# --- validation ---------------------------------------------------------------------------------------------


def test_empty_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(BundleError, match="no review packets"):
        make_bundle(tmp_path / "empty")


def test_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="not a directory"):
        make_bundle(tmp_path / "nope")


def test_missing_manifest_is_refused(packets: Path) -> None:
    (packets / "99000002.manifest.json").unlink()
    with pytest.raises(BundleError, match=r"99000002\.html.*no manifest"):
        make_bundle(packets)


def _edit_manifest(packets: Path, stem: str, **changes: Any) -> None:
    path = packets / f"{stem}.manifest.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), **changes}), encoding="utf-8")


def test_mismatched_packet_id_is_refused(packets: Path) -> None:
    _edit_manifest(packets, "99000002", packet_id="99000002-000000000000")
    with pytest.raises(BundleError, match=r"99000002\.html.*packet_id"):
        make_bundle(packets)


def test_manifest_pmid_must_match_file_name(packets: Path) -> None:
    _edit_manifest(packets, "99000002", pmid="12345")
    with pytest.raises(BundleError, match=r"99000002\.html.*pmid"):
        make_bundle(packets)


def test_packet_copied_under_another_name_is_refused(packets: Path) -> None:
    (packets / "99000009.html").write_bytes((packets / "99000001.html").read_bytes())
    (packets / "99000009.manifest.json").write_bytes((packets / "99000001.manifest.json").read_bytes())
    with pytest.raises(BundleError, match=r"99000009\.html.*pmid"):
        make_bundle(packets)


@pytest.mark.parametrize("stem", ["..\\x", "\u00b2", ".hidden", "a b", "unknown", "12a", "\u0663"])
def test_file_names_that_are_not_numeric_pmids_are_refused(packets: Path, stem: str) -> None:
    (packets / f"{stem}.html").write_bytes((packets / "99000001.html").read_bytes())
    (packets / f"{stem}.manifest.json").write_bytes((packets / "99000001.manifest.json").read_bytes())
    with pytest.raises(BundleError, match="not a numeric PMID") as excinfo:
        make_bundle(packets)
    assert any(stem in problem for problem in excinfo.value.problems)


def _edit_html(packets: Path, stem: str, old: str, new: str) -> None:
    path = packets / f"{stem}.html"
    page = path.read_text(encoding="utf-8")
    assert old in page, old
    path.write_text(page.replace(old, new), encoding="utf-8")


def test_manifest_packet_id_must_be_pmid_and_record_hash(packets: Path) -> None:
    fake = "99000002-000000000000"
    real = json.loads((packets / "99000002.manifest.json").read_text(encoding="utf-8"))["packet_id"]
    _edit_manifest(packets, "99000002", packet_id=fake)
    _edit_html(packets, "99000002", real, fake)  # manifest and page agree with each other, but not with the record
    with pytest.raises(BundleError, match=r"99000002\.html.*packet_id.*pmid and draft hash"):
        make_bundle(packets)


def test_embedded_meta_pmid_must_match_the_file_name(packets: Path) -> None:
    _edit_html(packets, "99000002", '"pmid": "99000002"', '"pmid": "99000007"')
    with pytest.raises(BundleError, match=r"99000002\.html.*embedded meta pmid"):
        make_bundle(packets)


def test_embedded_record_pmid_must_match_the_file_name(packets: Path) -> None:
    _edit_html(packets, "99000002", '"pmid": 99000002', '"pmid": 99000007')
    with pytest.raises(BundleError, match=r"99000002\.html.*embedded record's pmid"):
        make_bundle(packets)


def test_embedded_meta_draft_sha256_must_match_the_record(packets: Path) -> None:
    sha = json.loads((packets / "99000002.manifest.json").read_text(encoding="utf-8"))["draft_sha256"]
    page = (packets / "99000002.html").read_text(encoding="utf-8")
    meta_block = re.search(r'"meta": \{.*?\}', page, re.DOTALL).group(0)  # type: ignore[union-attr]
    _edit_html(packets, "99000002", meta_block, meta_block.replace(sha, "0" * 64))
    with pytest.raises(BundleError, match=r"99000002\.html.*embedded meta draft_sha256"):
        make_bundle(packets)


@pytest.mark.parametrize("manifest_sha", [None, "absent"])
def test_record_that_cannot_be_hashed_is_refused_not_matched_against_none(packets: Path, manifest_sha: Any) -> None:
    page = (packets / "99000002.html").read_text(encoding="utf-8")
    assert '"year": 2024' in page
    _edit_html(packets, "99000002", '"year": 2024', '"year": NaN')
    path = packets / "99000002.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest_sha == "absent":
        del manifest["draft_sha256"]
    else:
        manifest["draft_sha256"] = manifest_sha
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match=r"99000002\.html.*(NaN|cannot be hashed)"):
        make_bundle(packets)


def test_packet_without_embedded_record_is_refused(packets: Path) -> None:
    path = packets / "99000002.html"
    path.write_text(path.read_text(encoding="utf-8").replace('id="packet-data"', 'id="other"'), encoding="utf-8")
    with pytest.raises(BundleError, match=r"99000002\.html.*embedded record"):
        make_bundle(packets)


def test_record_that_does_not_match_the_manifest_hash_is_refused(packets: Path) -> None:
    _edit_manifest(packets, "99000002", draft_sha256="0" * 64)
    with pytest.raises(BundleError, match=r"99000002\.html.*draft_sha256"):
        make_bundle(packets)


def test_all_problems_are_reported_together(packets: Path) -> None:
    (packets / "99000001.manifest.json").unlink()
    (packets / "99000002.manifest.json").unlink()
    with pytest.raises(BundleError) as excinfo:
        make_bundle(packets)
    assert len(excinfo.value.problems) == 2


def test_non_cc_by_packets_warn_once_each_but_build(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001")
    write_packet(directory, "99000002", license_="cc by-nc", with_image=False)
    write_packet(directory, "99000003", license_=None, with_image=False)
    bundle = make_bundle(directory)
    assert len(bundle.manifest["packets"]) == 3
    by_pmid = {pmid: [w for w in bundle.warnings if pmid in w] for pmid in ("99000001", "99000002", "99000003")}
    assert by_pmid["99000001"] == []
    assert len(by_pmid["99000002"]) == 1 and "not CC BY/CC0" in by_pmid["99000002"][0]
    assert len(by_pmid["99000003"]) == 1 and "unknown" in by_pmid["99000003"][0]


def test_cc_by_packet_citing_figures_without_images_warns(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", with_image=False)
    warnings = make_bundle(directory).warnings
    assert len(warnings) == 1 and "99000001" in warnings[0] and "cites figures" in warnings[0]
    assert "degraded" not in warnings[0]


def test_table_only_cc_by_packet_does_not_warn_about_missing_images(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", with_image=False, table_only=True)
    assert make_bundle(directory).warnings == []


def test_evidence_problems_from_the_manifest_are_warned_about(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", problems=("could not download the image for Figure 2 (boom)",))
    warnings = make_bundle(directory).warnings
    assert len(warnings) == 1
    assert "99000001" in warnings[0] and "incomplete" in warnings[0] and "could not download the image" in warnings[0]


def test_packets_are_never_modified(packets: Path, tmp_path: Path) -> None:
    before = {p.name: p.read_bytes() for p in packets.iterdir()}
    write_bundle_tree(make_bundle(packets), tmp_path / "out")
    assert {p.name: p.read_bytes() for p in packets.iterdir()} == before


# --- writing: atomic, never overwriting ---------------------------------------------------------------------


def test_tree_is_staged_inside_out_and_renamed_into_place(packets: Path, tmp_path: Path, monkeypatch: Any) -> None:
    out = tmp_path / "out"
    renames: list[tuple[Path, Path]] = []
    real_replace = bundle_module.os.replace

    def spy(src: Any, dst: Any) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(bundle_module.os, "replace", spy)
    root = write_bundle_tree(make_bundle(packets), out)
    assert root == out / NAME and (root / "index.html").is_file()
    assert [(src.parent, dst) for src, dst in renames] == [(out, out / NAME)]
    assert [p.name for p in out.iterdir()] == [NAME]


def test_failed_tree_write_leaves_no_partial_folder_or_temp(packets: Path, tmp_path: Path, monkeypatch: Any) -> None:
    out = tmp_path / "out"

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bundle_module.os, "replace", boom)
    with pytest.raises(BundleError, match="No space left"):
        write_bundle_tree(make_bundle(packets), out)
    assert list(out.iterdir()) == []


def test_zip_is_written_via_a_temp_file_and_refuses_an_existing_zip(packets: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    bundle = make_bundle(packets)
    path = write_bundle_zip(bundle, out)
    assert path == out / f"{NAME}.zip" and path.read_bytes() == zip_bytes(bundle)
    assert [p.name for p in out.iterdir()] == [path.name]
    with pytest.raises(BundleError, match=r"\.zip already exists"):
        write_bundle_zip(bundle, out)
    with pytest.raises(BundleError, match=r"\.zip already exists"):
        refuse_existing_outputs(bundle, out, with_zip=True)
    refuse_existing_outputs(bundle, out, with_zip=False)


def test_failed_zip_write_leaves_no_temp_and_no_zip(packets: Path, tmp_path: Path, monkeypatch: Any) -> None:
    out = tmp_path / "out"
    monkeypatch.setattr(
        bundle_module.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError(13, "Permission denied"))
    )
    with pytest.raises(BundleError, match="Permission denied"):
        write_bundle_zip(make_bundle(packets), out)
    assert list(out.iterdir()) == []


# --- CLI ----------------------------------------------------------------------------------------------------


def _invoke(*args: str, **kwargs: Any) -> Any:
    return runner.invoke(app, ["review", "bundle", *args], env=_PLAIN_ENV, **kwargs)


def test_bundle_command_options_are_wired() -> None:
    command = typer.main.get_command(app).commands["review"].commands["bundle"]  # type: ignore[attr-defined]
    params = {p.name: p for p in command.params}
    assert set(params) == {"packets", "out", "name", "contact", "zip_", "date"}
    assert params["packets"].required and params["out"].required
    assert params["zip_"].default is True and params["zip_"].opts == ["--zip"]
    assert params["zip_"].secondary_opts == ["--no-zip"]


def test_cli_writes_tree_and_a_reproducible_zip(packets: Path, tmp_path: Path) -> None:
    args = ("--packets", str(packets), "--date", DATE, "--contact", "A <a@b.c>")
    result = _invoke(*args, "--out", str(tmp_path / "out"))
    assert result.exit_code == 0, result.output
    out = tmp_path / "out"
    assert (out / NAME / "index.html").is_file() and (out / NAME / "packets" / "99000001.html").is_file()
    assert (out / f"{NAME}.zip").is_file()
    assert _invoke(*args, "--out", str(tmp_path / "out2")).exit_code == 0
    assert (tmp_path / "out2" / f"{NAME}.zip").read_bytes() == (out / f"{NAME}.zip").read_bytes()


def test_cli_no_zip_writes_only_the_tree(packets: Path, tmp_path: Path) -> None:
    result = _invoke("--packets", str(packets), "--out", str(tmp_path / "out"), "--date", DATE, "--no-zip")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / NAME).is_dir() and not list((tmp_path / "out").glob("*.zip"))


def test_cli_name_defaults_to_the_date(packets: Path, tmp_path: Path) -> None:
    result = _invoke("--packets", str(packets), "--out", str(tmp_path / "out"), "--date", "2031-01-02", "--no-zip")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "bugsigdb-review-2031-01-02" / "index.html").is_file()


def test_cli_validation_error_exits_2_and_writes_nothing(packets: Path, tmp_path: Path) -> None:
    (packets / "99000002.manifest.json").unlink()
    result = _invoke("--packets", str(packets), "--out", str(tmp_path / "out"), "--date", DATE)
    assert result.exit_code == 2
    assert "99000002.html" in " ".join(result.output.split())
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("extra", [["--date", "07/10/2026"], ["--name", "../evil"], ["--name", "a b"]])
def test_cli_rejects_bad_date_and_name(packets: Path, tmp_path: Path, extra: list[str]) -> None:
    result = _invoke("--packets", str(packets), "--out", str(tmp_path / "o"), *extra)
    assert result.exit_code == 2
    assert not (tmp_path / "o").exists()


def test_cli_refuses_to_overwrite_an_existing_bundle_folder(packets: Path, tmp_path: Path) -> None:
    args = ("--packets", str(packets), "--out", str(tmp_path / "out"), "--date", DATE, "--no-zip")
    assert _invoke(*args).exit_code == 0
    assert _invoke(*args).exit_code == 2


def test_cli_prints_warnings(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001", license_="cc by-nc", with_image=False)
    result = _invoke("--packets", str(directory), "--out", str(tmp_path / "o"), "--date", DATE, "--no-zip")
    assert result.exit_code == 0, result.output
    assert "99000001" in " ".join(result.output.split()) and "not CC BY/CC0" in " ".join(result.output.split())


def test_cli_refuses_an_existing_zip_before_writing_the_folder(packets: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / f"{NAME}.zip").write_bytes(b"precious")
    result = _invoke("--packets", str(packets), "--out", str(out), "--date", DATE)
    assert result.exit_code == 2
    assert "already exists" in " ".join(result.output.split())
    assert (out / f"{NAME}.zip").read_bytes() == b"precious" and not (out / NAME).exists()


def test_cli_reports_unwritable_output_cleanly(packets: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    result = _invoke("--packets", str(packets), "--out", str(blocker / "out"), "--date", DATE)
    assert result.exit_code == 2
    assert "Traceback" not in result.output and "Wrote" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
