"""Static, self-contained HTML review packets for machine-generated BugSigDB drafts.

A packet is ONE .html file (inline CSS + JS, no external requests) that a BugSigDB curator opens in a
browser, judges against the paper, and exports as a verdict JSON (`review.verdicts`). Reviewers need
no account and no server.

* `build_packet(record, annotations, evidence, meta)` is a pure function (draft in, HTML out), so it is
  testable offline. `evidence` may be None (an offline packet: legends/figures are not shown).
* `fetch_packet_evidence` is the only network code: it gathers the cited tables/figures from
  EuropePMC/PMC and the article licence. Figure IMAGES are embedded as data URLs only when the licence is
  CC BY or CC0 (otherwise the legend and a link are shown) and each image is capped at `MAX_IMAGE_BYTES`.
* `build_manifest` records what a packet was built from (`draft_sha256` in particular), so `review ingest`
  can refuse verdicts that judge a different draft.

The page's behaviour lives in `packet.js` and its look in `packet.css`; both are inlined at build time.
"""

from __future__ import annotations

import base64
import html
import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from bugsigdb_curation.curator.evidence import EvidenceFigure, EvidenceTable, assemble_evidence, fetch_figure_image
from bugsigdb_curation.curator.model import sniff_image_mime
from bugsigdb_curation.curator.resolve import resolve
from bugsigdb_curation.retrieval import PMC_ARTICLE_URL, normalize_source_label
from bugsigdb_curation.review.verdicts import canonical_sha256

#: Embedded figure images larger than this are linked, not embedded (keeps packets emailable).
MAX_IMAGE_BYTES = 1_500_000
#: Table rows shown per evidence panel; longer tables are cut with a note and a link to the paper.
MAX_TABLE_ROWS = 60

EUROPEPMC_CORE_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

_PACKAGE_DIR = Path(__file__).parent
_EMBEDDABLE_LICENSE_RE = re.compile(r"(cc[ -]by|cc0)([ -]\d\.\d)?")
_TABLE_SOURCE_RE = re.compile(r"\s*table\s*#?\s*(\d+)", re.IGNORECASE)
_HEADER_KEYS = ("uid", "pmid", "doi", "title", "authors", "journal", "year", "citation_mode", "experiments")
_EXPERIMENT_FIELDS = (
    ("host_species", "Host species"),
    ("body_site", "Body site"),
    ("condition", "Condition"),
    ("group_0_name", "Group 0 (reference / control)"),
    ("group_1_name", "Group 1 (case)"),
    ("sequencing_type", "Sequencing"),
    ("statistical_test", "Statistical test"),
    ("mht_correction", "Multiple-testing correction"),
)
_VERDICT_OPTIONS = (("ok", "ok"), ("needs_edit", "needs edit"), ("wrong", "wrong"), ("unsure", "unsure"))
_DIRECTION_OPTIONS = (("ok", "ok"), ("flipped", "flipped"), ("unsure", "unsure"))
_TAXON_OPTIONS = (
    ("correct", "correct"),
    ("wrong_taxon", "wrong taxon"),
    ("not_in_source", "not in source"),
    ("unsure", "unsure"),
)


@dataclass(frozen=True, slots=True)
class PacketMeta:
    """What identifies and describes one built packet (also the manifest's core)."""

    packet_id: str
    pmid: str
    draft_sha256: str
    built_at: str
    builder_commit: str | None = None
    model_label: str | None = None
    design_label: str | None = None
    pmcid: str | None = None


@dataclass(frozen=True, slots=True)
class PacketEvidence:
    """Evidence gathered for the artifacts a draft cites.

    `images` maps a figure's provenance (e.g. "Figure 2") to its raw bytes, only for figures actually
    fetched. `license` is the EuropePMC `license` string (e.g. "cc by"); None means unknown.
    `problems` lists what failed while fetching (empty for a complete fetch); it is not cached, since a
    cache entry only ever holds complete evidence.
    """

    pmcid: str | None
    license: str | None
    figures: tuple[EvidenceFigure, ...] = ()
    tables: tuple[EvidenceTable, ...] = ()
    images: dict[str, bytes] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        """True when a fetch step failed, so figures, tables, images or the licence may be missing."""
        return bool(self.problems)


def license_allows_embedding(license_: str | None) -> bool:
    """True only for CC BY and CC0 (any version): the licences that let us reproduce a figure in a packet."""
    return bool(license_) and _EMBEDDABLE_LICENSE_RE.fullmatch(" ".join(str(license_).lower().split())) is not None


def study_pmid(record: dict[str, Any]) -> str:
    return str(record.get("pmid") or record.get("uid") or "unknown")


def builder_git_commit() -> str | None:
    """Short commit hash of the code that built the packet, or None outside a git checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PACKAGE_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def make_meta(
    record: dict[str, Any],
    *,
    model_label: str | None = None,
    design_label: str | None = None,
    pmcid: str | None = None,
    built_at: str | None = None,
    builder_commit: str | None = None,
) -> PacketMeta:
    sha = canonical_sha256(record)
    pmid = study_pmid(record)
    return PacketMeta(
        packet_id=f"{pmid}-{sha[:12]}",
        pmid=pmid,
        draft_sha256=sha,
        built_at=built_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        builder_commit=builder_commit,
        model_label=model_label,
        design_label=design_label,
        pmcid=pmcid,
    )


def build_manifest(
    record: dict[str, Any], annotations: dict[str, Any] | None, evidence: PacketEvidence | None, meta: PacketMeta
) -> dict[str, Any]:
    """The `<pmid>.manifest.json` content: identity of the packet and a summary of what it shows."""
    experiments = record.get("experiments") or []
    return {
        "packet_id": meta.packet_id,
        "pmid": meta.pmid,
        "pmcid": meta.pmcid,
        "draft_sha256": meta.draft_sha256,
        "built_at": meta.built_at,
        "builder_commit": meta.builder_commit,
        "model_label": meta.model_label,
        "design_label": meta.design_label,
        "license": evidence.license if evidence else None,
        "n_experiments": len(experiments),
        "n_signatures": sum(len(e.get("signatures") or []) for e in experiments),
        "n_taxa": sum(len(s.get("taxa") or []) for e in experiments for s in e.get("signatures") or []),
        "annotation_errors": sorted(k for k in (annotations or {}) if k.endswith("_error")),
    }


# ---------------------------------------------------------------------------
# evidence fetching (the only network code)
# ---------------------------------------------------------------------------


def cited_artifact(source: str | None) -> tuple[str, str] | None:
    """`("figure", "2")` / `("table", "1")` for a signature `source` like "Figure 2B" / "Table 1"; else None."""
    if not source:
        return None
    figure_number = normalize_source_label(source)
    if figure_number is not None:
        return "figure", figure_number
    table = _TABLE_SOURCE_RE.match(source)
    if table:
        return "table", table.group(1)
    return None


def _cited_artifacts(record: dict[str, Any]) -> set[tuple[str, str]]:
    cited = (
        cited_artifact(sig.get("source"))
        for exp in record.get("experiments") or []
        for sig in exp.get("signatures") or []
    )
    return {c for c in cited if c is not None}


async def fetch_article_license(pmcid: str, *, client: httpx.AsyncClient) -> str | None:
    """The `license` field of the EuropePMC core record for `pmcid` (e.g. "cc by"), or None."""
    response = await client.get(
        EUROPEPMC_CORE_SEARCH_URL, params={"query": f"PMCID:{pmcid}", "resultType": "core", "format": "json"}
    )
    response.raise_for_status()
    results = response.json().get("resultList", {}).get("result", [])
    license_ = results[0].get("license") if results else None
    return str(license_) if license_ else None


async def fetch_packet_evidence(
    record: dict[str, Any], *, client: httpx.AsyncClient, pmcid: str | None = None
) -> PacketEvidence:
    """Fetch the tables/figures a draft cites, plus the article licence, from EuropePMC/PMC.

    `pmcid` is resolved from the PMID when not given. Every step degrades to "less evidence" rather than
    failing the packet: no PMCID -> empty evidence; licence lookup fails -> licence unknown (no images
    embedded); an image fails to download -> that figure is shown as legend + link. Each failure is
    recorded in `problems`, so callers can tell a degraded result from a complete one.
    """
    pmid = study_pmid(record)
    if pmcid is None:
        pmcid = (await resolve(pmid, client=client)).pmcid
    if pmcid is None:
        return PacketEvidence(pmcid=None, license=None)

    problems: list[str] = []
    try:
        license_: str | None = await fetch_article_license(pmcid, client=client)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("licence lookup failed; images will not be embedded", pmcid=pmcid, error=repr(exc))
        problems.append(f"licence lookup failed ({exc!r}); no images are embedded")
        license_ = None

    try:
        bundle = await assemble_evidence(pmid, pmcid, client=client)
    except httpx.HTTPError as exc:
        logger.warning("evidence fetch failed; packet will carry no evidence", pmcid=pmcid, error=repr(exc))
        problems.append(f"could not fetch the article's figures and tables ({exc!r})")
        return PacketEvidence(pmcid=pmcid, license=license_, problems=tuple(problems))

    cited = _cited_artifacts(record)
    figures = tuple(f for f in bundle.figures if f.number is not None and ("figure", f.number) in cited)
    tables = tuple(t for t in bundle.tables if t.number is not None and ("table", t.number) in cited)

    images: dict[str, bytes] = {}
    if license_allows_embedding(license_):
        for figure in figures:
            try:
                data = await fetch_figure_image(figure, client=client)
            except httpx.HTTPError as exc:
                logger.warning("figure image fetch failed", figure=figure.provenance, error=repr(exc))
                problems.append(f"could not download the image for {figure.provenance} ({exc!r})")
                continue
            if data:
                images[figure.provenance] = data
    return PacketEvidence(
        pmcid=pmcid, license=license_, figures=figures, tables=tables, images=images, problems=tuple(problems)
    )


def save_evidence(evidence: PacketEvidence, directory: Path) -> None:
    """Write `evidence` to `directory` (`evidence.json` + one file per fetched image), for `--evidence-dir` reuse."""
    directory.mkdir(parents=True, exist_ok=True)
    image_files = {}
    for provenance, data in evidence.images.items():
        name = re.sub(r"[^A-Za-z0-9]+", "_", provenance).strip("_") + ".img"
        (directory / name).write_bytes(data)
        image_files[provenance] = name
    payload = {
        "pmcid": evidence.pmcid,
        "license": evidence.license,
        "figures": [asdict(f) for f in evidence.figures],
        "tables": [asdict(t) for t in evidence.tables],
        "images": image_files,
    }
    (directory / "evidence.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_evidence(directory: Path) -> PacketEvidence | None:
    """Read evidence written by `save_evidence`; None if `directory` holds none."""
    path = directory / "evidence.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return PacketEvidence(
        pmcid=payload.get("pmcid"),
        license=payload.get("license"),
        figures=tuple(EvidenceFigure(**f) for f in payload.get("figures", [])),
        tables=tuple(
            EvidenceTable(**{**t, "rows": tuple(tuple(r) for r in t["rows"])}) for t in payload.get("tables", [])
        ),
        images={prov: (directory / name).read_bytes() for prov, name in payload.get("images", {}).items()},
    )


# ---------------------------------------------------------------------------
# HTML rendering (pure)
# ---------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _join(value: object) -> str | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def _field_row(label: str, value: object, extra_html: str = "") -> str:
    text = _join(value)
    shown = _e(text) if text is not None else '<span class="na">not stated in the draft</span>'
    return f"<tr><th>{_e(label)}</th><td>{shown}{extra_html}</td></tr>"


def _select(key: str, options: Sequence[tuple[str, str]], label: str, empty: str = "— not reviewed —") -> str:
    opts = [f'<option value="">{_e(empty)}</option>'] + [f'<option value="{v}">{_e(t)}</option>' for v, t in options]
    return f'<select data-key="{_e(key)}" aria-label="{_e(label)}" data-v="">{"".join(opts)}</select>'


def _text_input(key: str, label: str, placeholder: str = "note (optional)") -> str:
    return f'<input type="text" data-key="{_e(key)}" aria-label="{_e(label)}" placeholder="{_e(placeholder)}">'


def _textarea(key: str, label: str, placeholder: str = "") -> str:
    return f'<textarea data-key="{_e(key)}" aria-label="{_e(label)}" placeholder="{_e(placeholder)}"></textarea>'


def _p_da(item: dict[str, Any]) -> str:
    p_da = item.get("p_da")
    return f"{p_da:.2f}" if isinstance(p_da, (int, float)) else ""


def _pmc_url(pmcid: str | None) -> str | None:
    return PMC_ARTICLE_URL.format(pmcid=pmcid) if pmcid else None


class _PacketBuilder:
    """Holds one build's inputs and the images it decided to embed."""

    def __init__(
        self, record: dict[str, Any], annotations: dict[str, Any], evidence: PacketEvidence | None, meta: PacketMeta
    ) -> None:
        self.record = record
        self.annotations = annotations
        self.evidence = evidence
        self.meta = meta
        self.pmcid = (evidence.pmcid if evidence and evidence.pmcid else None) or meta.pmcid
        self.embedded: dict[str, dict[str, str]] = {}

    # --- banner / header -------------------------------------------------------------------

    def banner(self) -> str:
        m = self.meta
        bits = [
            f"Model: {_e(m.model_label)}" if m.model_label else None,
            f"Design: {_e(m.design_label)}" if m.design_label else None,
            f"Built: {_e(m.built_at)}",
            f"Packet: {_e(m.packet_id)}",
            f"Builder commit: {_e(m.builder_commit)}" if m.builder_commit else None,
        ]
        parts = [
            '<header class="banner" role="banner">',
            "<strong>MACHINE-GENERATED DRAFT — UNREVIEWED. Not curated data.</strong>",
            f'<div class="meta">{" · ".join(b for b in bits if b)}</div>',
            "</header>",
        ]
        errors = sorted(k for k in self.annotations if k.endswith("_error"))
        if errors:
            keys = ", ".join(f"<code>{_e(k)}</code>" for k in errors)
            parts.append(
                '<p class="notice"><strong>A decision-model step failed</strong> while this draft was produced '
                f"({keys}); the pipeline fell back to its non-decision-model behaviour for that step, so the "
                "draft may be weaker than usual (for example, the source table or figure may have been chosen "
                "by keyword rather than ranked).</p>"
            )
        return "".join(parts)

    def noscript(self) -> str:
        return (
            '<noscript><p class="notice js-warning"><strong>This review page needs JavaScript.</strong> '
            "Without it you cannot record verdicts or export them. Open this file in a web browser with "
            "JavaScript enabled (not in a mail or file previewer).</p></noscript>"
        )

    def topbar(self) -> str:
        return (
            '<div class="topbar">'
            '<div class="progress-wrap"><progress id="progress-bar" value="0" max="1"></progress>'
            '<span id="progress-text">0 of 0 items reviewed</span></div>'
            '<div class="actions">'
            '<button type="button" data-action="export-json">Export verdicts (JSON)</button>'
            '<button type="button" class="secondary" data-action="export-csv">Export CSV</button>'
            '<button type="button" class="danger" data-action="reset">Reset</button></div>'
            '<span id="reviewer-line"><span id="reviewer-text"></span> '
            '<button type="button" class="link" id="change-reviewer" data-action="change-reviewer" hidden>'
            "change</button></span>"
            '<span id="save-status" role="status">This page needs JavaScript — open the file in a web browser.</span>'
            '<div id="export-error" role="alert"></div></div>'
        )

    def study_section(self, attribution: str) -> str:
        r = self.record
        pmid = study_pmid(r)
        links = (
            [f'<a href="https://pubmed.ncbi.nlm.nih.gov/{_e(pmid)}/">PubMed {_e(pmid)}</a>'] if pmid.isdigit() else []
        )
        if self.pmcid:
            links.append(f'<a href="{_e(_pmc_url(self.pmcid))}">{_e(self.pmcid)} (full text)</a>')
        if r.get("doi"):
            links.append(f'<a href="https://doi.org/{_e(r["doi"])}">DOI {_e(r["doi"])}</a>')
        journal_year = " · ".join(str(x) for x in (r.get("journal"), r.get("year")) if x)
        authors = _join(r.get("authors"))
        license_ = self.evidence.license if self.evidence else None
        rows = [_field_row("Study design", r.get("study_design"))]
        rows += [
            _field_row(k.replace("_", " ").capitalize(), v)
            for k, v in r.items()
            if k not in _HEADER_KEYS and k != "study_design" and not isinstance(v, dict)
        ]
        return (
            '<section class="card study-header" id="study">'
            f"<h1>{_e(r.get('title') or '(no title in the draft)')}</h1>"
            f'<div class="citation">{_e(authors) if authors else "(no authors in the draft)"}'
            f"{' — ' + _e(journal_year) if journal_year else ''}</div>"
            f'<div class="links">{"".join(links)}</div>'
            '<p class="hint">Article license: '
            f"{_e(license_) if license_ else 'unknown / not checked'}.</p>"
            f"{attribution}"
            f'<h2 style="margin-top:1rem">Study-level fields (as the draft states them)</h2>'
            f'<table class="fields">{"".join(rows)}</table>'
            '<div class="verdict-row"><label>Study-level verdict '
            f"{_select('study.verdict', _VERDICT_OPTIONS, 'Study-level verdict')}</label>"
            f"{_text_input('study.note', 'Study-level note')}</div>"
            f"{self.ranking_details()}"
            "</section>"
        )

    def ranking_details(self) -> str:
        ranking = self.annotations.get("artifact_ranking")
        if not isinstance(ranking, list) or not ranking:
            return ""
        rows = "".join(
            f"<tr><td>{_e(item.get('artifact', ''))}</td><td>{_e(item.get('kind', ''))}</td><td>{_p_da(item)}</td></tr>"
            for item in ranking
            if isinstance(item, dict)
        )
        return (
            "<details><summary>How the pipeline chose the source table/figure</summary>"
            '<p class="hint">A decision model ranked each table/figure by the probability that it holds the '
            "differential-abundance results (p(DA)).</p>"
            f'<table class="ranking"><tr><th>Artifact</th><th>Kind</th><th>p(DA)</th></tr>{rows}</table></details>'
        )

    # --- experiments -----------------------------------------------------------------------

    def body_site_suggestions(self, e: int) -> str:
        terms = self.annotations.get("body_site_terms")
        if not isinstance(terms, list):
            return ""
        out = []
        for t in terms:
            if not isinstance(t, dict) or t.get("experiment_index") != e:
                continue
            label = _e(t.get("label", ""))
            status = t.get("status")
            term = f"{_e(t.get('term_label') or '')} ({_e(t.get('term_id') or '')})"
            if status == "mapped":
                out.append(f'<div class="uberon">UBERON: “{label}” → {term}</div>')
            elif status == "low_confidence":
                out.append(
                    f'<div class="uberon">UBERON: “{label}” → {term} '
                    '<span class="badge lowconf">suggestion, low confidence</span></div>'
                )
            else:
                out.append(f'<div class="uberon">UBERON: “{label}” — no ontology term suggested</div>')
        return "".join(out)

    def experiment_section(self, e: int, experiment: dict[str, Any], n_experiments: int) -> str:
        known = {k for k, _ in _EXPERIMENT_FIELDS} | {"signatures"}
        rows = [
            _field_row(label, experiment.get(key), self.body_site_suggestions(e) if key == "body_site" else "")
            for key, label in _EXPERIMENT_FIELDS
        ]
        rows += [_field_row(k.replace("_", " ").capitalize(), v) for k, v in experiment.items() if k not in known]
        signatures = experiment.get("signatures") or []
        groups: dict[str, list[int]] = {}
        for s, sig in enumerate(signatures):
            groups.setdefault((sig.get("source") or "").strip(), []).append(s)
        group_html = "".join(
            self.signature_group(e, experiment, signatures, source, indexes) for source, indexes in groups.items()
        )
        if not signatures:
            group_html = '<p class="na">The draft has no signatures for this experiment.</p>'
        return (
            f'<section class="card experiment" id="exp-{e}">'
            f"<h2>Experiment {e + 1} of {n_experiments}</h2>"
            f'<table class="fields">{"".join(rows)}</table>'
            '<div class="verdict-row"><label>Experiment verdict '
            f"{_select(f'exp.{e}.verdict', _VERDICT_OPTIONS, f'Experiment {e + 1} verdict')}</label>"
            f"{_text_input(f'exp.{e}.note', f'Experiment {e + 1} note')}</div>"
            f"{group_html}"
            f'<h3 style="margin-top:1rem">Missing from the draft</h3>'
            f"{_textarea(f'exp.{e}.missing_note', f'Experiment {e + 1}: taxa or signatures the draft missed', 'Taxa or signatures this experiment should have (optional)')}"
            "</section>"
        )

    def direction_badge(self, experiment: dict[str, Any], direction: str | None) -> str:
        g0 = experiment.get("group_0_name") or "group 0"
        g1 = experiment.get("group_1_name") or "group 1"
        if direction == "increased":
            return f'<span class="badge up">▲ INCREASED in {_e(g1)} (group 1) vs {_e(g0)} (group 0)</span>'
        if direction == "decreased":
            return f'<span class="badge down">▼ DECREASED in {_e(g1)} (group 1) vs {_e(g0)} (group 0)</span>'
        shown = _e(str(direction).upper()) if direction else "NOT STATED"
        return f'<span class="badge unknown">DIRECTION {shown}</span>'

    def signature_group(
        self, e: int, experiment: dict[str, Any], signatures: list[dict[str, Any]], source: str, indexes: list[int]
    ) -> str:
        cards = "".join(self.signature_card(e, s, experiment, signatures[s], len(signatures)) for s in indexes)
        return f'<div class="sig-group"><div class="sigs">{cards}</div>{self.evidence_panel(source)}</div>'

    def signature_card(self, e: int, s: int, experiment: dict[str, Any], sig: dict[str, Any], n_sigs: int) -> str:
        source = sig.get("source")
        rows = []
        for k, taxon in enumerate(sig.get("taxa") or []):
            base = f"exp.{e}.sig.{s}.taxon.{k}"
            ncbi = taxon.get("ncbi_id")
            id_html = (
                f'<span class="taxon-id">NCBI:{_e(ncbi)}</span>'
                if ncbi is not None
                else '<span class="badge unresolved">unresolved id</span>'
            )
            name = taxon.get("taxon_name", "")
            rows.append(
                f'<tr><td><span class="taxon-name">{_e(name)}</span><br>{id_html}</td>'
                f'<td><div class="taxon-controls">{_select(f"{base}.verdict", _TAXON_OPTIONS, f"Verdict for {name}")}'
                f"{_text_input(f'{base}.note', f'Note for {name}', 'note')}</div></td></tr>"
            )
        taxa_html = (
            '<table class="taxa"><tr><th>Taxon (as named in the draft)</th><th>Is it right?</th></tr>'
            f"{''.join(rows)}</table>"
            if rows
            else '<p class="na">This signature lists no taxa.</p>'
        )
        return (
            f'<article class="signature" id="exp-{e}-sig-{s}"><header>'
            f"<h4>Signature {s + 1} of {n_sigs}</h4>{self.direction_badge(experiment, sig.get('abundance_in_group_1'))}"
            f'<span class="source">Source: {_e(source) if source else "none stated"}</span></header>'
            f"{taxa_html}"
            '<div class="sig-controls">'
            f"<label>Direction {_select(f'exp.{e}.sig.{s}.direction', _DIRECTION_OPTIONS, f'Direction verdict, signature {s + 1}')}</label>"
            f'<button type="button" class="secondary small" data-action="mark-taxa-correct" data-exp="{e}" '
            f'data-sig="{s}">Mark remaining taxa correct</button></div></article>'
        )

    # --- evidence --------------------------------------------------------------------------

    def _paper_link(self) -> str:
        url = _pmc_url(self.pmcid)
        if url:
            return f'<a href="{_e(url)}">Open the paper on PMC</a>'
        pmid = study_pmid(self.record)
        if pmid.isdigit():
            return f'<a href="https://pubmed.ncbi.nlm.nih.gov/{_e(pmid)}/">Open the paper on PubMed</a>'
        return ""

    def _no_evidence(self, reason: str) -> str:
        return (
            '<aside class="evidence no-evidence"><h4>Evidence</h4>'
            f"<p>No evidence could be shown here: {_e(reason)}</p><p>{self._paper_link()}</p></aside>"
        )

    def evidence_panel(self, source: str) -> str:
        cited = cited_artifact(source)
        if not source:
            return self._no_evidence("the draft states no source for these signatures.")
        if cited is None:
            return self._no_evidence(f"the cited source “{source}” is not a main-text table or figure.")
        if self.evidence is None:
            return self._no_evidence("this packet was built offline, without fetching the article.")
        kind, number = cited
        if kind == "figure":
            figure = next((f for f in self.evidence.figures if f.number == number), None)
            if figure is None:
                return self._no_evidence(f"“{source}” was not found in the article full text.")
            return self._figure_panel(figure)
        table = next((t for t in self.evidence.tables if t.number == number), None)
        if table is None:
            return self._no_evidence(f"“{source}” was not found in the article full text.")
        return self._table_panel(table)

    def _figure_panel(self, figure: EvidenceFigure) -> str:
        assert self.evidence is not None
        provenance = figure.provenance
        data = self.evidence.images.get(provenance)
        if not license_allows_embedding(self.evidence.license):
            shown = self.evidence.license or "unknown"
            image_html = (
                f'<p class="hint">The image is not reproduced here: the article license ({_e(shown)}) is not '
                f"CC BY or CC0. {self._paper_link()} to see the figure.</p>"
            )
        elif data is None:
            image_html = f'<p class="hint">The image could not be retrieved. {self._paper_link()} to see it.</p>'
        elif len(data) > MAX_IMAGE_BYTES:
            image_html = (
                f'<p class="hint">The image is too large to embed ({len(data) / 1e6:.1f} MB; limit '
                f"{MAX_IMAGE_BYTES / 1e6:.1f} MB). {self._paper_link()} to see it.</p>"
            )
        else:
            self.embedded[provenance] = {
                "type": sniff_image_mime(data),
                "data": base64.b64encode(data).decode("ascii"),
            }
            image_html = (
                f'<img data-image-ref="{_e(provenance)}" alt="{_e(provenance)} from the article" title="Click to zoom">'
            )
        return (
            f'<aside class="evidence"><h4>Evidence: {_e(provenance)}</h4>{image_html}'
            f'<p class="caption"><strong>{_e(figure.label)}</strong> {_e(figure.legend)}</p></aside>'
        )

    def _table_panel(self, table: EvidenceTable) -> str:
        shown_rows = table.rows[:MAX_TABLE_ROWS]
        body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in shown_rows)
        cut = (
            f'<p class="hint">Showing the first {MAX_TABLE_ROWS} of {len(table.rows)} rows. '
            f"{self._paper_link()} for the rest.</p>"
            if len(table.rows) > MAX_TABLE_ROWS
            else ""
        )
        return (
            f'<aside class="evidence"><h4>Evidence: {_e(table.provenance)}</h4>'
            f'<p class="caption"><strong>{_e(table.label)}</strong> {_e(table.caption)}</p>'
            f'<div class="scroll"><table class="evidence-table">{body}</table></div>{cut}</aside>'
        )

    # --- overall + assembly ----------------------------------------------------------------

    def overall_section(self) -> str:
        rating_options = [(str(i), str(i)) for i in range(1, 6)]
        rating_options[0] = ("1", "1 — no time saved")
        rating_options[4] = ("5", "5 — saves most of the time")
        return (
            '<section class="card" id="missing-experiments"><h2>Missing experiments</h2>'
            '<p class="hint">Group comparisons in the paper that the draft has no experiment for.</p>'
            f"{_textarea('missing_experiments_note', 'Experiments the draft missed', 'Describe any experiments the draft missed (optional)')}"
            "</section>"
            '<section class="card" id="overall"><h2>Overall</h2><div class="overall-grid">'
            "<div><label>Compared with curating from scratch, this draft would save me:"
            f"</label>{_select('overall.time_saved_rating', rating_options, 'Time saved, 1 to 5', '— choose —')}</div>"
            "<div><label>Would you publish this after edits?</label>"
            f"{_select('overall.would_publish_after_edits', [('yes', 'yes'), ('no', 'no'), ('unsure', 'unsure')], 'Would publish after edits', '— choose —')}</div>"
            '<div class="wide"><label>Comments</label>'
            f"{_textarea('overall.comment', 'Overall comment')}</div>"
            '<div class="wide"><h3>About you</h3><div class="reviewer-grid">'
            '<label>Name (required) <input type="text" data-key="reviewer.name" autocomplete="name"></label>'
            '<label>Email <input type="email" data-key="reviewer.email" autocomplete="email"></label>'
            '<label>Role (optional) <input type="text" data-key="reviewer.role" placeholder="e.g. curator"></label>'
            '<label>Minutes spent <input type="number" min="0" step="1" data-key="minutes_spent"></label>'
            "</div></div></div>"
            '<p style="margin-top:1rem"><button type="button" data-action="export-json">Export verdicts (JSON)</button> '
            '<button type="button" class="secondary" data-action="export-csv">Export CSV</button></p>'
            '<p class="hint">Exporting saves a file on your computer. Please send it back to whoever sent you this '
            "page. Nothing is uploaded.</p></section>"
        )

    def attribution(self) -> str:
        if not self.embedded or self.evidence is None:
            return ""
        r = self.record
        authors = _join(r.get("authors")) or "authors not stated"
        return (
            '<p class="attribution">Figures shown in this packet are reproduced from: '
            f"{_e(authors)}. {_e(r.get('title') or '')} {_e(r.get('journal') or '')} {_e(r.get('year') or '')}. "
            f"License: {_e(self.evidence.license)}. Original: {self._paper_link()}.</p>"
        )

    def build(self) -> str:
        experiments = self.record.get("experiments") or []
        # Experiments render first: which figures got embedded decides the study header's attribution line.
        body_sections = [self.experiment_section(e, exp, len(experiments)) for e, exp in enumerate(experiments)]
        if not experiments:
            body_sections = ['<section class="card"><p class="na">The draft contains no experiments.</p></section>']
        study = self.study_section(self.attribution())
        payload = {"meta": _meta_dict(self.meta), "record": self.record}
        css = (_PACKAGE_DIR / "packet.css").read_text(encoding="utf-8")
        js = (_PACKAGE_DIR / "packet.js").read_text(encoding="utf-8")
        title = f"Review packet — PMID {self.meta.pmid}"
        return (
            "<!DOCTYPE html>\n"
            '<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(title)}</title><style>\n{css}</style></head><body>\n"
            f"{self.banner()}\n{self.noscript()}\n{self.topbar()}\n<main>\n{study}\n{''.join(body_sections)}\n{self.overall_section()}\n</main>\n"
            f"<footer>Packet {_e(self.meta.packet_id)} · draft sha256 {_e(self.meta.draft_sha256)}</footer>\n"
            f'<script type="application/json" id="packet-data">{_json_for_script(payload)}</script>\n'
            f'<script type="application/json" id="packet-images">{_json_for_script(self.embedded)}</script>\n'
            f"<script>\n{js}</script>\n</body></html>\n"
        )


def _meta_dict(meta: PacketMeta) -> dict[str, Any]:
    return {
        "packet_id": meta.packet_id,
        "pmid": meta.pmid,
        "draft_sha256": meta.draft_sha256,
        "built_at": meta.built_at,
        "model_label": meta.model_label,
        "design_label": meta.design_label,
    }


def _json_for_script(value: Any) -> str:
    """JSON safe to place inside a <script> element (no `</script>` or `<!--` can be formed)."""
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"the draft holds NaN or Infinity, which is not valid JSON: {exc}") from exc
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_packet(
    record: dict[str, Any], annotations: dict[str, Any] | None, evidence: PacketEvidence | None, meta: PacketMeta
) -> str:
    """One self-contained HTML review packet for `record` (the draft), with its annotation sidecar and evidence.

    Pure: no I/O. `annotations` and `evidence` may be None/empty; every sidecar key is optional.
    """
    return _PacketBuilder(record, annotations or {}, evidence, meta).build()
