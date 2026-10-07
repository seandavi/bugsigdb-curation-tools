"""Turn a directory of review packets into ONE shareable static bundle (`bugsigdb review bundle`).

A bundle is `index.html` (a card per study, linking the packets), the packets themselves, `README.txt`,
`ATTRIBUTION.txt` and `manifest.json`, optionally zipped. Everything here is pure and offline: the same
inputs (packets, name, date, contact) give byte-identical files. The zip is byte-identical only on the same
Python and zlib build (deflate output can differ between zlib versions), so across machines compare the
files, not the zip: `manifest.json` lists every file with its sha256, and the sha256 of `manifest.json` itself
identifies the bundle. `content_sha256` is narrower: the sha256 of the sorted `path<TAB>sha256` lines of
`packets/*.html` and `packets/*.manifest.json` only, so it does not cover `index.html`, `README.txt` or
`ATTRIBUTION.txt` (the contact address is among what it leaves out). The packets are copied verbatim, never
modified.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date as date_type
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bugsigdb_curation.review.packet import _DOI_RE, cited_artifact, license_allows_embedding, study_pmid
from bugsigdb_curation.review.verdicts import canonical_sha256

#: Authors shown on an index card before "et al." (ATTRIBUTION.txt always lists every author).
MAX_CARD_AUTHORS = 6

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PMCID_RE = re.compile(r"PMC[0-9]+")
_PMID_RE = re.compile(r"[0-9]+")  # ASCII digits only: the file stem becomes zip member names and hrefs
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PACKET_DATA_RE = re.compile(r'<script type="application/json" id="packet-data">(.*?)</script>', re.DOTALL)
_PACKET_IMAGES_RE = re.compile(r'<script type="application/json" id="packet-images">(.*?)</script>', re.DOTALL)
_CITATION_RE = re.compile(r'<div class="citation">(.*?)</div>', re.DOTALL)
_NO_AUTHORS_IN_PACKET = "(no authors in the draft)"
_ZIP_TIMESTAMP = (2000, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644


class BundleError(Exception):
    """The packets directory cannot be bundled; `problems` lists everything that is wrong, one line each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Bundle:
    """A built bundle: every file's bytes keyed by its path inside the bundle folder, plus what to warn about."""

    name: str
    files: dict[str, bytes]
    manifest: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Study:
    """What the bundle shows about one packet, read from its manifest and its embedded record."""

    pmid: str
    packet_id: str
    draft_sha256: str
    html_bytes: bytes
    manifest_bytes: bytes
    title: str
    authors: tuple[str, ...]
    journal: str
    year: str
    doi: str
    pmcid: str | None
    license: str | None
    n_experiments: int
    n_signatures: int
    n_taxa: int
    evidence_cited: tuple[str, ...]
    n_images: int
    builder_commit: str | None
    evidence_problems: tuple[str, ...]
    authors_from_page: bool

    @property
    def licence_ok(self) -> bool:
        return license_allows_embedding(self.license)

    @property
    def cites_figures(self) -> bool:
        return any(label.startswith("Figure ") for label in self.evidence_cited)


# ---------------------------------------------------------------------------
# reading and validating packets
# ---------------------------------------------------------------------------


def _embedded_json(page: str, pattern: re.Pattern[str]) -> Any:
    match = pattern.search(page)
    if match is None:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _page_authors(page: str, record: dict[str, Any]) -> tuple[str, ...]:
    """Authors for a packet that predates `meta.attribution_authors`: the draft's, else the page's citation line."""
    authors = record.get("authors")
    if isinstance(authors, list) and authors:
        return tuple(str(a) for a in authors)
    match = _CITATION_RE.search(page)
    if match is None:
        return ()
    text = html.unescape(match.group(1)).strip()
    journal_year = " · ".join(str(x) for x in (record.get("journal"), record.get("year")) if x)
    if journal_year and text.endswith(f" — {journal_year}"):
        text = text.removesuffix(f" — {journal_year}")
    if not text or text == _NO_AUTHORS_IN_PACKET:
        return ()
    return tuple(part.strip() for part in text.split(";") if part.strip())


def _credited_authors(page: str, record: dict[str, Any], meta: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    """The authors the packet credits, and whether they had to be scraped (an older packet without the meta field)."""
    recorded = meta.get("attribution_authors")
    if isinstance(recorded, list):
        return tuple(str(a) for a in recorded), False
    return _page_authors(page, record), True


def _line(value: object) -> str:
    """`value` as one line of plain text: whitespace collapsed, C0/C1 controls (newlines, ANSI escapes) removed."""
    return _CONTROL_RE.sub("", " ".join(str(value).split()))


def _is_pmid(text: str) -> bool:
    return _PMID_RE.fullmatch(text) is not None


def _pmid_key(study: _Study) -> tuple[bool, int, str]:
    return not _is_pmid(study.pmid), int(study.pmid) if _is_pmid(study.pmid) else 0, study.pmid


def _evidence_label(kind: str, number: str) -> str:
    return f"{kind.capitalize()} {number}"


def _natural_key(item: tuple[str, str]) -> tuple[str, int, str]:
    kind, number = item
    digits = re.match(r"\d+", number)
    return kind, int(digits.group()) if digits else 0, number


def _read_study(html_path: Path, problems: list[str]) -> _Study | None:
    """One packet's `_Study`, or None after appending to `problems` what is wrong with it."""
    stem = html_path.name.removesuffix(".html")
    manifest_path = html_path.with_name(f"{stem}.manifest.json")
    start = len(problems)

    def refuse(message: str) -> None:
        problems.append(f"{_line(html_path.name)}: {message}")

    if not _is_pmid(stem):
        refuse("the file name is not a numeric PMID (expected <digits>.html)")
        return None
    if not manifest_path.is_file():
        refuse(f"no manifest ({manifest_path.name} not found beside it)")
        return None
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or not all(isinstance(manifest.get(k), str) for k in ("packet_id", "pmid")):
        refuse(f"{manifest_path.name} is not a packet manifest (needs string packet_id and pmid)")
        return None

    html_bytes = html_path.read_bytes()
    page = html_bytes.decode("utf-8", errors="replace")
    data = _embedded_json(page, _PACKET_DATA_RE)
    record = data.get("record") if isinstance(data, dict) else None
    if not isinstance(record, dict):
        refuse('no embedded record (the <script id="packet-data"> block is missing or unreadable)')
        return None
    try:
        record_sha = canonical_sha256(record)
    except ValueError:
        refuse("the embedded record holds NaN or Infinity and cannot be hashed")
        return None

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if manifest["packet_id"] != meta.get("packet_id"):
        refuse(
            f"packet_id in {manifest_path.name} ({manifest['packet_id']}) is not the packet's ({meta.get('packet_id')})"
        )
    if manifest["packet_id"] != f"{stem}-{record_sha[:12]}":
        refuse(f"packet_id in {manifest_path.name} ({manifest['packet_id']}) is not the file's pmid and draft hash")
    if manifest["pmid"] != stem:
        refuse(f"pmid in {manifest_path.name} ({manifest['pmid']}) does not match the file name")
    if meta.get("pmid") != stem:
        refuse(f"embedded meta pmid ({meta.get('pmid')}) does not match the file name")
    if study_pmid(record) != stem:
        refuse(f"embedded record's pmid ({study_pmid(record)}) does not match the file name")
    if manifest.get("draft_sha256") != record_sha:
        refuse(f"draft_sha256 in {manifest_path.name} does not match the packet's embedded record")
    if meta.get("draft_sha256") != record_sha:
        refuse(f"embedded meta draft_sha256 ({meta.get('draft_sha256')}) does not match the packet's embedded record")
    if len(problems) > start:
        return None

    experiments = [e for e in record.get("experiments") or [] if isinstance(e, dict)]
    signatures = [s for e in experiments for s in e.get("signatures") or [] if isinstance(s, dict)]
    cited = {c for s in signatures if (c := cited_artifact(s.get("source"))) is not None}
    images = _embedded_json(page, _PACKET_IMAGES_RE)
    n_images = len(images) if isinstance(images, dict) else 0
    license_ = manifest.get("license") if isinstance(manifest.get("license"), str) else None
    if n_images and not license_allows_embedding(license_):
        refuse(
            f"embeds {n_images} figure image(s) but the licence is {_line(license_ or 'unknown')}; "
            "figures may not be redistributed"
        )
    authors, authors_from_page = _credited_authors(page, record, meta)
    if n_images and not authors and license_allows_embedding(license_):
        refuse(
            f"embeds {n_images} figure image(s) but names no authors; a CC BY figure must credit the authors "
            "(rebuild the packet from a draft or article that states them)"
        )
    if len(problems) > start:
        return None
    problems_field = manifest.get("evidence_problems")
    return _Study(
        pmid=stem,
        packet_id=manifest["packet_id"],
        draft_sha256=manifest["draft_sha256"],
        html_bytes=html_bytes,
        manifest_bytes=manifest_bytes,
        title=str(record.get("title") or ""),
        authors=authors,
        journal=str(record.get("journal") or ""),
        year=str(record.get("year") or ""),
        doi=str(record.get("doi") or ""),
        pmcid=manifest.get("pmcid") if isinstance(manifest.get("pmcid"), str) else None,
        license=license_ or None,
        n_experiments=len(experiments),
        n_signatures=len(signatures),
        n_taxa=sum(len(s.get("taxa") or []) for s in signatures),
        evidence_cited=tuple(_evidence_label(k, n) for k, n in sorted(cited, key=_natural_key)),
        n_images=n_images,
        builder_commit=manifest.get("builder_commit") if isinstance(manifest.get("builder_commit"), str) else None,
        evidence_problems=tuple(str(x) for x in problems_field) if isinstance(problems_field, list) else (),
        authors_from_page=authors_from_page,
    )


def _read_studies(packets_dir: Path) -> tuple[list[_Study], list[str]]:
    """Every packet in `packets_dir` (sorted by PMID) and the warnings; raises BundleError if any is unusable."""
    if not packets_dir.is_dir():
        raise BundleError([f"{packets_dir} is not a directory"])
    html_paths = sorted(p for p in packets_dir.iterdir() if p.is_file() and p.suffix == ".html")
    if not html_paths:
        raise BundleError([f"no review packets (*.html) found in {packets_dir}"])

    problems: list[str] = []
    studies = [s for p in html_paths if (s := _read_study(p, problems)) is not None]
    if problems:
        raise BundleError(problems)

    warnings = [
        f"{_line(m.name)}: manifest without a packet (not bundled)"
        for m in sorted(packets_dir.glob("*.manifest.json"))
        if not m.with_name(m.name.removesuffix(".manifest.json") + ".html").is_file()
    ]
    for study in studies:
        if study.license is None:
            warnings.append(f"PMID {study.pmid}: licence unknown; figures were not embedded")
        elif not study.licence_ok:
            warnings.append(f"PMID {study.pmid}: licence '{_line(study.license)}' is not CC BY/CC0")
        elif study.n_images == 0 and study.cites_figures:
            warnings.append(
                f"PMID {study.pmid}: cites figures but embeds no figure images (reviewers get legends and links only)"
            )
        if study.evidence_problems:
            warnings.append(
                f"PMID {study.pmid}: the packet may be incomplete; evidence fetch problems: "
                + "; ".join(_line(p) for p in study.evidence_problems)
            )
        if study.authors_from_page:
            warnings.append(
                f"PMID {study.pmid}: packet predates meta.attribution_authors; "
                "authors were read from the packet's visible citation line"
            )
    return sorted(studies, key=_pmid_key), warnings


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _size(n_bytes: int) -> str:
    return f"{n_bytes / 1024:.0f} KB" if n_bytes < 1024 * 1024 else f"{n_bytes / 1024 / 1024:.1f} MB"


def _count(n: int, noun: str) -> str:
    if n == 1:
        return f"{n} {noun}"
    return f"{n} {noun.removesuffix('y')}ies" if noun.endswith("y") else f"{n} {noun}s"


def _card_authors(authors: tuple[str, ...]) -> str:
    if not authors:
        return "authors not stated"
    shown = "; ".join(authors[:MAX_CARD_AUTHORS])
    return f"{shown}; et al." if len(authors) > MAX_CARD_AUTHORS else shown


def _licence_text(study: _Study) -> str:
    return study.license or "unknown"


_INDEX_CSS = """
*{box-sizing:border-box}
body{margin:0;overflow-wrap:break-word;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1c2330;background:#f4f6f9}
main,header.top,footer{max-width:60rem;margin:0 auto;padding:0 1rem}
header.top{padding-top:1.5rem}
h1{margin:0 0 .25rem;font-size:1.6rem}
.lede{margin:0 0 1rem;color:#4a5568}
.warning{background:#fff4e5;border:2px solid #d9822b;border-radius:6px;padding:.75rem 1rem;margin:1rem 0;font-size:1.05rem}
.panel{background:#fff;border:1px solid #d5dbe3;border-radius:6px;padding:.25rem 1rem .75rem;margin:1rem 0}
.panel h2{font-size:1.15rem;margin:.75rem 0 .25rem}
.panel ol{margin:.25rem 0 .25rem 1.25rem;padding:0}
.panel li{margin:.3rem 0}
.panel p{margin:.5rem 0}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,26rem),1fr));gap:1rem;margin:1rem 0}
.card{background:#fff;border:1px solid #d5dbe3;border-radius:6px;padding:.75rem 1rem;display:flex;flex-direction:column;gap:.35rem;min-width:0}
.card h3{margin:0;font-size:1.05rem;line-height:1.3;overflow-wrap:anywhere}
.card p{margin:0;overflow-wrap:anywhere}
.authors{color:#4a5568;font-size:.92rem}
.venue{font-size:.92rem}
.links{font-size:.92rem}
.links a{margin-right:.75rem;white-space:nowrap}
.stats{font-weight:600}
.meta{font-size:.88rem;color:#4a5568}
.flag{color:#a4400b;font-weight:600}
.open{margin-top:auto!important;padding-top:.4rem;display:flex;gap:.75rem;align-items:center;flex-wrap:wrap}
.open a{background:#1a56a0;color:#fff;text-decoration:none;padding:.4rem .9rem;border-radius:4px;font-weight:600}
.open a:hover,.open a:focus{background:#123f78}
.pid{font-size:.78rem;color:#6b7686;font-family:ui-monospace,Menlo,Consolas,monospace}
a{color:#1a56a0}
footer{padding-bottom:2rem;font-size:.82rem;color:#4a5568;overflow-wrap:anywhere}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.88em}
""".strip()


def _contact_html(contact: str | None) -> str:
    return _e(contact) if contact else "whoever sent you this bundle"


def _card(study: _Study) -> str:
    links = []
    if _is_pmid(study.pmid):
        links.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{_e(study.pmid)}/">PMID {_e(study.pmid)}</a>')
    if study.pmcid and _PMCID_RE.fullmatch(study.pmcid):
        links.append(f'<a href="https://pmc.ncbi.nlm.nih.gov/articles/{_e(quote(study.pmcid))}/">{_e(study.pmcid)}</a>')
    if _DOI_RE.fullmatch(study.doi) and not {".", ".."} & set(study.doi.split("/")):
        links.append(f'<a href="https://doi.org/{_e(quote(study.doi, safe="/()"))}">DOI</a>')
    venue = " · ".join(x for x in (study.journal, study.year) if x)
    licence_class = "" if study.licence_ok else ' class="flag"'
    images = f"{_count(study.n_images, 'figure image')} embedded" if study.n_images else "no figure images embedded"
    evidence = ", ".join(study.evidence_cited) if study.evidence_cited else "none identified"
    return (
        '<article class="card">'
        f"<h3>{_e(study.title or '(no title in the draft)')}</h3>"
        f'<p class="authors">{_e(_card_authors(study.authors))}</p>'
        f'<p class="venue">{_e(venue) if venue else "journal and year not stated"}</p>'
        f'<p class="links">{" ".join(links)}</p>'
        f'<p class="stats">{_count(study.n_experiments, "experiment")} · {_count(study.n_signatures, "signature")} · '
        f"{study.n_taxa} {'taxon' if study.n_taxa == 1 else 'taxa'}</p>"
        f'<p class="meta">Evidence cited: {_e(evidence)}</p>'
        f'<p class="meta"><span{licence_class}>Licence: {_e(_licence_text(study))}</span> · {_e(images)}</p>'
        f'<p class="open"><a href="packets/{_e(quote(study.pmid))}.html">Open packet →</a>'
        f'<span class="meta">{_size(len(study.html_bytes))}</span></p>'
        f'<p class="pid">Packet {_e(study.packet_id)}</p>'
        "</article>"
    )


def _render_index(
    studies: list[_Study], *, name: str, date: str, contact: str | None, builder_commit: str | None, content_sha256: str
) -> str:
    cards = "\n".join(_card(s) for s in studies)
    contact_html = _contact_html(contact)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>BugSigDB draft review — {_e(name)}</title>
<style>
{_INDEX_CSS}
</style></head><body>
<header class="top">
<h1>BugSigDB draft review</h1>
<p class="lede">{_count(len(studies), "study")} to review — bundle {_e(name)}, built {_e(date)}.</p>
<div class="warning" role="note"><strong>MACHINE-GENERATED, UNREVIEWED drafts for evaluation — not curated data.</strong>
They were produced by software from the published papers and have not been checked by a person. Your judgement is the evaluation.</div>
</header>
<main>
<section class="panel">
<h2>How to review</h2>
<ol>
<li><strong>Extract the zip first.</strong> Don't open files from inside a zip preview or an email attachment viewer — the review pages need to run in a browser from a normal folder.</li>
<li>Open a packet (below) in Chrome, Firefox or Safari.</li>
<li>Judge each experiment, signature and taxon against the evidence panel next to it. If something is unknown or unclear, choose <em>unsure</em> — never guess.</li>
<li>Enter your name in the <em>Overall</em> section.</li>
<li>Click <strong>Export verdicts (JSON)</strong>. The file is named <code>verdicts_&lt;pmid&gt;_&lt;your-name&gt;.json</code> and lands in your browser's Downloads folder.</li>
<li>Send the JSON file(s) to {contact_html} &mdash; the JSON files, NOT the CSV (the CSV is only a convenience copy).</li>
</ol>
<p>Export once per packet when you finish it, and export if you stop early so nothing is lost.</p>
<p>Your progress autosaves in that browser, on that computer, for that file. Don't move or rename the packet file while you work; if you extract the zip somewhere else, your progress may not carry over, so export first.</p>
<p>Privacy: nothing is uploaded. Everything stays on your computer until you send the exported file yourself.</p>
</section>
<section class="cards" aria-label="Studies to review">
{cards}
</section>
</main>
<footer>
<p>Bundle {_e(name)} · built {_e(date)} · builder commit {_e(builder_commit or "unknown")}<br>
Packets hash (content_sha256 in <code>manifest.json</code>; covers the packet files only): <code>{_e(content_sha256)}</code></p>
<p>Figures are reproduced from open-access papers under their stated licences; see <code>ATTRIBUTION.txt</code>.</p>
</footer>
</body></html>
"""


def _render_readme(studies: list[_Study], *, name: str, date: str, contact: str | None) -> str:
    to = _line(contact) if contact and _line(contact) else "whoever sent you this bundle"
    return f"""BugSigDB draft review — {name}
Built {date}; {_count(len(studies), "study")}.

THESE ARE MACHINE-GENERATED, UNREVIEWED DRAFTS FOR EVALUATION. THEY ARE NOT CURATED DATA.
Your judgement of them is the evaluation.

HOW TO REVIEW
1. Extract this zip first. Do not open files from inside a zip preview or an email attachment viewer.
2. Open index.html, then open a packet from the packets/ folder, in Chrome, Firefox or Safari.
3. Judge each experiment, signature and taxon against the evidence panel beside it.
   If something is unknown or unclear, choose "unsure". Never guess.
4. Enter your name in the Overall section.
5. Click "Export verdicts (JSON)". The file is named verdicts_<pmid>_<your-name>.json and lands in your
   browser's Downloads folder.
6. Send the JSON file(s) to {to}: the JSON files, NOT the CSV (the CSV is only a convenience copy).

Export once per packet when you finish it, and export if you stop early so nothing is lost.
Progress autosaves in that browser, on that computer, for that file. Do not move or rename the packet
file while you work. If you extract the zip somewhere else, your progress may not carry over, so export first.

PRIVACY
Nothing is uploaded. Everything stays on your computer until you send the exported file yourself.

WHAT THE VERDICTS MEAN
Taxon:       correct        the taxon is right, as the evidence shows
             wrong taxon    the paper reports a different taxon (or a different rank) than the draft names
             not in source  the paper does not report this taxon in the cited figure or table
             unsure         you cannot tell
Direction:   ok             increased/decreased is right for the signature
             flipped        the draft has the direction the wrong way round
             unsure         you cannot tell
Experiment / study: ok, needs edit, wrong, unsure

HOW THE FILE COMES BACK
The exported file is named verdicts_<pmid>_<your-name>.json (one per packet you finish).
Email it, or put it in the shared folder, as {to} asks.

FILES
index.html        the list of studies, with a link to each packet
packets/          one self-contained review page per study (and its manifest)
ATTRIBUTION.txt   authors, journal and licence for the figures shown
manifest.json     file list with sha256 checksums

CHECKING A COPY
manifest.json lists every file in the bundle with its sha256, so the sha256 of manifest.json identifies the
whole bundle. Its content_sha256 field is narrower: the sha256 of the sorted "path<TAB>sha256" lines of
packets/*.html and packets/*.manifest.json only. It does not cover index.html, README.txt or ATTRIBUTION.txt
(the contact address is among what it leaves out), so equal content_sha256 values do not mean the same bundle.
The zip is byte-identical only when built with the same Python and zlib; compare manifest.json instead.
"""


def _render_attribution(studies: list[_Study], *, name: str) -> str:
    blocks = [
        (
            f"Attribution for {name}.\n"
            "Figures in the review packets are reproduced from open-access articles under the licence stated for each.\n"
            "CC BY requires crediting the authors; the credit is below."
        )
    ]
    for study in studies:
        licence = _line(_licence_text(study))
        lines = [
            f"PMID {study.pmid}",
            f"  Title:   {_line(study.title) or '(not stated)'}",
            f"  Authors: {_line('; '.join(study.authors)) if study.authors else 'not stated'}",
            f"  Journal: {_line(study.journal) or '(not stated)'}",
            f"  Year:    {_line(study.year) or '(not stated)'}",
            f"  DOI:     {_line(study.doi) or '(not stated)'}",
            f"  Licence: {licence}",
        ]
        if study.licence_ok and study.n_images:
            lines.append(
                f"  Figures in this packet are reproduced under the licence {licence}; credit: the authors above."
            )
        else:
            lines.append("  No figures are reproduced in this packet.")
        if study.license is None:
            lines.append("  CHECK: the licence is unknown, so figures were not embedded.")
        elif not study.licence_ok:
            lines.append(f"  CHECK: the licence ({licence}) is not CC BY or CC0, so figures were not embedded.")
        elif not study.n_images and study.cites_figures:
            lines.append(
                "  CHECK: no figure images were embedded in this packet (reviewers see legends and links only)."
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# assembling, writing, zipping
# ---------------------------------------------------------------------------


def _file_entries(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [
        {"path": path, "sha256": hashlib.sha256(files[path]).hexdigest(), "bytes": len(files[path])}
        for path in sorted(files)
    ]


def build_bundle(packets_dir: Path, *, name: str, date: str, contact: str | None) -> Bundle:
    """The bundle for every packet in `packets_dir`. Raises `BundleError` if any packet is unusable.

    `date` (YYYY-MM-DD) is recorded as the build date; nothing here reads the clock.
    """
    if not _NAME_RE.fullmatch(name):
        raise BundleError([f"invalid --name {name!r}: use letters, digits, '.', '_' and '-' only"])
    try:
        if not _DATE_RE.fullmatch(date):
            raise ValueError(date)
        date_type.fromisoformat(date)
    except ValueError:
        raise BundleError([f"invalid --date {date!r}: expected YYYY-MM-DD"]) from None

    studies, warnings = _read_studies(packets_dir)
    commits = {s.builder_commit for s in studies}
    builder_commit = next(iter(commits)) if len(commits) == 1 else None

    files: dict[str, bytes] = {}
    for study in studies:
        files[f"packets/{study.pmid}.html"] = study.html_bytes
        files[f"packets/{study.pmid}.manifest.json"] = study.manifest_bytes
    content_sha256 = hashlib.sha256(
        "".join(f"{e['path']}\t{e['sha256']}\n" for e in _file_entries(files)).encode("utf-8")
    ).hexdigest()
    files["index.html"] = _render_index(
        studies, name=name, date=date, contact=contact, builder_commit=builder_commit, content_sha256=content_sha256
    ).encode("utf-8")
    files["README.txt"] = _render_readme(studies, name=name, date=date, contact=contact).encode("utf-8")
    files["ATTRIBUTION.txt"] = _render_attribution(studies, name=name).encode("utf-8")

    manifest = {
        "name": name,
        "built_at": date,
        "builder_commit": builder_commit,
        "content_sha256": content_sha256,
        "files": _file_entries(files),
        "packets": [
            {
                "pmid": s.pmid,
                "packet_id": s.packet_id,
                "draft_sha256": s.draft_sha256,
                "file": f"packets/{s.pmid}.html",
            }
            for s in studies
        ],
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return Bundle(name=name, files=files, manifest=manifest, warnings=warnings)


def refuse_existing_outputs(bundle: Bundle, out_dir: Path, *, with_zip: bool) -> None:
    """Raise `BundleError` if the folder (or, with `with_zip`, the zip) the bundle would be written to exists."""
    taken = [
        p for p in (out_dir / bundle.name, out_dir / f"{bundle.name}.zip" if with_zip else None) if p and p.exists()
    ]
    if taken:
        raise BundleError([f"{p} already exists; remove it or choose another --name" for p in taken])


def write_bundle_tree(bundle: Bundle, out_dir: Path) -> Path:
    """Write the bundle folder `out_dir/<name>/`; refuses to touch one that already exists.

    The files go into a temporary folder inside `out_dir` that is renamed into place at the end, so a failure
    never leaves a half-written bundle folder behind.
    """
    root = out_dir / bundle.name
    refuse_existing_outputs(bundle, out_dir, with_zip=False)
    staging: Path | None = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{bundle.name}.", suffix=".tmp", dir=out_dir))
        for path, data in bundle.files.items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        os.replace(staging, root)
    except OSError as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise BundleError([f"could not write {root}: {exc.strerror or exc}"]) from exc
    return root


def write_bundle_zip(bundle: Bundle, out_dir: Path) -> Path:
    """Write `out_dir/<name>.zip` (via a temporary file renamed into place); refuses to touch an existing one."""
    zip_path = out_dir / f"{bundle.name}.zip"
    if zip_path.exists():
        raise BundleError([f"{zip_path} already exists; remove it or choose another --name"])
    staging = out_dir / f".{bundle.name}.zip.tmp"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(zip_bytes(bundle))
        os.replace(staging, zip_path)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise BundleError([f"could not write {zip_path}: {exc.strerror or exc}"]) from exc
    return zip_path


def zip_bytes(bundle: Bundle) -> bytes:
    """The bundle as a zip rooted at `<name>/`: sorted members, fixed timestamps and permissions, so it is reproducible."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(bundle.files):
            info = zipfile.ZipInfo(f"{bundle.name}/{path}", date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = _ZIP_FILE_MODE << 16
            archive.writestr(info, bundle.files[path], compresslevel=9)
    return buffer.getvalue()
