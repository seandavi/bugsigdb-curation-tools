"""Tests for `bugsigdb review bundle`: one shareable static bundle (index + packets + docs) from a packets dir."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import typer
from review_support import load_annotations, load_draft, sample_evidence
from typer.testing import CliRunner

from bugsigdb_curation.cli import app
from bugsigdb_curation.review.bundle import BundleError, build_bundle, write_bundle_tree, zip_bytes
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
) -> None:
    """Build a real packet + manifest for a variant of the fixture draft, the way `review packet` does."""
    record = load_draft()
    record["pmid"] = int(pmid)
    record["uid"] = pmid
    if title is not None:
        record["title"] = title
    if authors is None:
        record.pop("authors")
    else:
        record["authors"] = authors
    evidence = replace(sample_evidence(license_), authors=evidence_authors)
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
    assert "autosaves" in text and "export early and often" in text.lower()
    assert "nothing is uploaded" in text.lower()
    assert NAME in text and DATE in text and "abc1234" in text and bundle.manifest["content_sha256"] in text


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
    write_packet(directory, "99000001", authors=None)
    assert "authors not stated" in make_bundle(directory).files["index.html"].decode("utf-8")


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


def test_duplicate_pmids_are_refused(packets: Path) -> None:
    (packets / "copy.html").write_bytes((packets / "99000001.html").read_bytes())
    (packets / "copy.manifest.json").write_bytes((packets / "99000001.manifest.json").read_bytes())
    with pytest.raises(BundleError, match=r"duplicate.*99000001"):
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


def test_degraded_and_non_cc_by_packets_warn_but_build(tmp_path: Path) -> None:
    directory = tmp_path / "p"
    write_packet(directory, "99000001")
    write_packet(directory, "99000002", license_="cc by-nc", with_image=False)
    write_packet(directory, "99000003", license_=None, with_image=False)
    bundle = make_bundle(directory)
    assert len(bundle.manifest["packets"]) == 3
    by_pmid = {pmid: [w for w in bundle.warnings if pmid in w] for pmid in ("99000001", "99000002", "99000003")}
    assert by_pmid["99000001"] == []
    assert any("not CC BY/CC0" in w for w in by_pmid["99000002"]) and any(
        "no figure images" in w for w in by_pmid["99000002"]
    )
    assert any("unknown" in w for w in by_pmid["99000003"])


def test_packets_are_never_modified(packets: Path, tmp_path: Path) -> None:
    before = {p.name: p.read_bytes() for p in packets.iterdir()}
    write_bundle_tree(make_bundle(packets), tmp_path / "out")
    assert {p.name: p.read_bytes() for p in packets.iterdir()} == before


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
