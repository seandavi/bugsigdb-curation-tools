"""Generate the README figures as standalone SVG (stdlib only).

    python docs/figures/make_figures.py

Writes, next to this file, a light and a dark variant (transparent background)
of each figure:

    workflow-{light,dark}.svg     end-to-end workflow, ingest -> curate -> evaluate
    data-model-{light,dark}.svg   the LinkML data model (schema/bugsigdb.yaml)

Colour roles are shared by both figures: violet = data artifact, green = CLI
command / pipeline stage, blue = shared service, amber = external source or
ontology, coral = held-out gold. Every figure carries its own legend.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).parent

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# role -> (fill, stroke, text)
THEMES: dict[str, dict] = {
    "light": {
        "ink": "#1f2937",
        "muted": "#6b7280",
        "line": "#6b7280",
        "panel": "#f9fafb",
        "violet": ("#ede9fe", "#7c3aed", "#3b1d8a"),
        "green": ("#dcfce7", "#16a34a", "#14532d"),
        "blue": ("#dbeafe", "#2563eb", "#1e3a8a"),
        "amber": ("#fef3c7", "#d97706", "#78350f"),
        "coral": ("#ffe4de", "#e5533d", "#7f1d1d"),
        "gray": ("#f3f4f6", "#9ca3af", "#374151"),
    },
    "dark": {
        "ink": "#e5e7eb",
        "muted": "#9ca3af",
        "line": "#9ca3af",
        "panel": "#1f2937",
        "violet": ("#2e1f5e", "#a78bfa", "#ddd6fe"),
        "green": ("#12351f", "#4ade80", "#bbf7d0"),
        "blue": ("#14305c", "#60a5fa", "#bfdbfe"),
        "amber": ("#3d2c0a", "#fbbf24", "#fde68a"),
        "coral": ("#47201a", "#fb7a63", "#fecaca"),
        "gray": ("#2b3341", "#6b7280", "#d1d5db"),
    },
}


class Svg:
    """A tiny SVG builder bound to one theme."""

    def __init__(self, w: int, h: int, theme: str, title: str, desc: str) -> None:
        self.w, self.h, self.t = w, h, THEMES[theme]
        self.parts: list[str] = []
        self.title, self.desc = title, desc

    # -- primitives -------------------------------------------------------
    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, *, size=12, weight="normal", fill=None, anchor="start", mono=False, italic=False):
        fam = MONO if mono else SANS
        style = ' font-style="italic"' if italic else ""
        self.add(
            f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" font-weight="{weight}"'
            f' fill="{fill or self.t["ink"]}" text-anchor="{anchor}"{style}>{escape(s)}</text>'
        )

    def box(self, x, y, w, h, role, lines, *, dashed=False, rx=8, title_size=12.5, sub_size=11, round_=False):
        fill, stroke, ink = self.t[role]
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        rx = h / 2 if round_ else rx
        self.add(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"'
            f' stroke="{stroke}" stroke-width="1.5"{dash}/>'
        )
        n = len(lines)
        line_h = 15
        top = y + h / 2 - (n - 1) * line_h / 2 + 4
        for i, ln in enumerate(lines):
            first = i == 0
            self.text(
                x + w / 2, top + i * line_h, ln,
                size=title_size if first else sub_size, weight="600" if first else "normal",
                fill=ink if first else self.t["muted"] if self.t is THEMES["light"] else ink, anchor="middle",
            )

    def arrow(self, pts, *, dashed=False, color=None, label=None, label_at=None, head=True):
        color = color or self.t["line"]
        d = "M" + " L".join(f"{x},{y}" for x, y in pts)
        dash = ' stroke-dasharray="4 4"' if dashed else ""
        marker = ' marker-end="url(#arrowhead)"' if head else ""
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5"{dash}{marker}/>')
        if label:
            lx, ly = label_at or pts[0]
            self.text(lx, ly, label, size=10.5, fill=self.t["muted"], anchor="middle", italic=True)

    def render(self) -> str:
        t = self.t
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}"'
            f' width="{self.w}" height="{self.h}" role="img" aria-labelledby="t d">\n'
            f"<title id=\"t\">{escape(self.title)}</title><desc id=\"d\">{escape(self.desc)}</desc>\n"
            f'<defs><marker id="arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7"'
            f' orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{t["line"]}"/></marker></defs>\n'
        )
        return head + "\n".join(self.parts) + "\n</svg>\n"


# ---------------------------------------------------------------------------
# Legend (shared)
# ---------------------------------------------------------------------------
def legend(s: Svg, y: int, items: list[tuple[str, str]], extra: list[tuple[str, str]], note: str | None = None) -> None:
    t = s.t
    h = 98 if note else 78
    s.add(f'<rect x="20" y="{y}" width="{s.w - 40}" height="{h}" rx="8" fill="{t["panel"]}" stroke="{t["gray"][1]}"/>')
    s.text(34, y + 20, "Legend", size=12, weight="600")
    x = 34
    for role, label in items:
        fill, stroke, _ = t[role]
        s.add(f'<rect x="{x}" y="{y + 30}" width="22" height="14" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
        s.text(x + 30, y + 42, label, size=11)
        x += 30 + int(len(label) * 6.1) + 24
    x = 34
    for kind, label in extra:
        if kind == "dashed-box":
            s.add(f'<rect x="{x}" y="{y + 52}" width="22" height="14" rx="4" fill="none" stroke="{t["line"]}" stroke-dasharray="4 3" stroke-width="1.5"/>')
        elif kind == "arrow":
            s.arrow([(x, y + 59), (x + 24, y + 59)])
        elif kind == "dashed-arrow":
            s.arrow([(x, y + 59), (x + 24, y + 59)], dashed=True)
        elif kind == "fw":
            s.add(f'<line x1="{x + 11}" y1="{y + 50}" x2="{x + 11}" y2="{y + 68}" stroke="{t["coral"][1]}" stroke-width="2.5" stroke-dasharray="5 3"/>')
        s.text(x + 32, y + 63, label, size=11)
        x += 32 + int(len(label) * 6.1) + 24
    if note:
        s.text(34, y + 88, note, size=11, fill=t["muted"])


# ---------------------------------------------------------------------------
# Figure 1: workflow
# ---------------------------------------------------------------------------
def workflow(theme: str) -> str:
    s = Svg(
        1200, 930, theme,
        "BugSigDB automated curation: end-to-end workflow",
        "Gold-building commands feed only the scorer. The curator takes a PMID, runs stages S0 to S9 "
        "(with optional supplement and verifier stages), and emits a prediction record that the scorer "
        "and human review packets consume. A data firewall separates the curator from the gold.",
    )
    t = s.t
    s.text(24, 32, "BugSigDB automated curation — end-to-end workflow", size=17, weight="700")

    # Lane A: build the gold ------------------------------------------------
    s.text(30, 68, "A · INGEST — build the held-out gold", size=11.5, weight="700", fill=t["muted"])
    s.box(30, 80, 170, 56, "amber", ["waldronlab/", "bugsigdbexports"])
    s.box(235, 80, 110, 56, "green", ["export"])
    s.box(380, 80, 130, 56, "violet", ["full_dump.csv", "one row / signature"], sub_size=10.5)
    s.box(545, 80, 100, 56, "green", ["split"])
    s.box(680, 80, 230, 56, "coral", ["relational CSVs", "studies · experiments · signatures", ], sub_size=10.5)
    s.arrow([(200, 108), (235, 108)])
    s.arrow([(345, 108), (380, 108)])
    s.arrow([(510, 108), (545, 108)])
    s.arrow([(645, 108), (680, 108)])
    s.box(380, 170, 130, 52, "green", ["load", "→ nested records"], sub_size=10)
    s.box(545, 170, 100, 52, "green", ["validate", "vs schema"], sub_size=10)
    s.arrow([(445, 136), (445, 170)])
    s.arrow([(510, 196), (545, 196)])
    s.box(690, 170, 100, 52, "green", ["pmc-map", "PMID → PMCID"], sub_size=10)
    s.box(825, 170, 215, 52, "coral", ["pmid_pmcid_map.csv", "NCBI idconv; ~84% have a PMCID"], sub_size=10)
    s.arrow([(795, 136), (795, 170)])
    s.arrow([(790, 196), (825, 196)])

    # Firewall divider ------------------------------------------------------
    s.add(f'<line x1="815" y1="262" x2="815" y2="808" stroke="{t["coral"][1]}" stroke-width="2.5" stroke-dasharray="7 5"/>')
    fw_fill = t["panel"]
    s.add(f'<rect x="745" y="250" width="140" height="24" rx="12" fill="{fw_fill}" stroke="{t["coral"][1]}" stroke-width="1.5"/>')
    s.text(815, 266, "DATA FIREWALL", size=11, weight="700", fill=t["coral"][1], anchor="middle")

    # Curator side ----------------------------------------------------------
    s.text(30, 296, "B · CURATE — sees a PMID only (never any gold field)", size=11.5, weight="700", fill=t["muted"])
    xs = [32, 233, 434, 635]
    w, h = 165, 72
    r1, r2, r3, r4 = 312, 430, 548, 690
    s.box(xs[0], r1, w, h, "violet", ["PMID"], round_=True)
    s.box(xs[1], r1, w, h, "green", ["S0 · resolve", "NCBI idconv → PMCID"], sub_size=10.5)
    s.box(xs[2], r1, w, h, "green", ["S1 · evidence", "text · tables · figures", "EuropePMC + PMC"], sub_size=10.5)
    s.box(xs[3], r1, w, h, "green", ["S2 · study metadata", "title · design · …"], sub_size=10.5)
    s.box(xs[0], r2, w, h, "green", ["S3 · segment", "one stub per 2-group", "comparison"], sub_size=10.5)
    s.box(xs[1], r2, w, h, "green", ["S4 · experiment", "groups · body site ·", "condition · methods"], sub_size=10.5)
    s.box(xs[2], r2, w, h, "green", ["S5a · locate", "rank tables / figures", "by DA likelihood"], sub_size=10)
    s.box(xs[3], r2, w, h, "green", ["S5b/S6 · extract", "taxa · direction ·", "NCBI id (verified)"], sub_size=10.5)
    s.box(xs[0], r3, w, h, "green", ["S10 · verify / panel", "split designs only"], dashed=True, sub_size=10.5)
    s.box(xs[1], r3, w, h, "green", ["S1b · supplements", "screen, extract, append", "after main-text results"], dashed=True, sub_size=10)
    s.box(xs[2], r3, w, h, "green", ["S8 · assemble", "nested-dict record"], sub_size=10.5)
    s.box(xs[3], r3, w, h, "green", ["S9 · validate", "LinkML schema + CURIEs"], sub_size=10.5)
    s.box(xs[3], r4, w, 64, "violet", ["prediction record", "Study → Exp → Sig"], sub_size=10.5)

    # row 1 flow
    for a, b in zip(xs[:3], xs[1:]):
        s.arrow([(a + w, r1 + h / 2), (b, r1 + h / 2)])
    # row 1 -> row 2 (elbow)
    s.arrow([(xs[3] + w / 2, r1 + h), (xs[3] + w / 2, r1 + h + 24), (xs[0] + w / 2, r1 + h + 24), (xs[0] + w / 2, r2)])
    for a, b in zip(xs[:3], xs[1:]):
        s.arrow([(a + w, r2 + h / 2), (b, r2 + h / 2)])
    s.arrow([(xs[3] + w / 2, r2 + h), (xs[3] + w / 2, r2 + h + 24), (xs[0] + w / 2, r2 + h + 24), (xs[0] + w / 2, r3)])
    # per-experiment bracket label
    s.text(xs[1] + 4, r2 - 8, "↻ repeated for every experiment stub", size=10.5, fill=t["muted"], italic=True)
    for a, b in zip(xs[:3], xs[1:]):
        s.arrow([(a + w, r3 + h / 2), (b, r3 + h / 2)])
    s.arrow([(xs[3] + w / 2, r3 + h), (xs[3] + w / 2, r4)])

    # Shared services (row 4)
    s.text(30, r4 - 10, "Shared services", size=11, weight="700", fill=t["muted"])
    s.box(xs[0], r4, w, 64, "blue", ["LLM · LiteLLM", "Gemini", "used by S2–S5b, S10"], sub_size=10.5)
    s.box(xs[1], r4, w, 64, "blue", ["TaxonomyDB", "DuckDB ← NCBI taxdump", "used by S6, S10, scorer"], sub_size=10)
    s.box(xs[2], r4, w, 64, "blue", ["Decision model", "optional Clef; used by", "S5a, S1b, S4 body site"], dashed=True, sub_size=10)

    # Scorer side -----------------------------------------------------------
    s.text(850, 296, "C · EVALUATE — the only reader of gold", size=11.5, weight="700", fill=t["muted"])
    sx, sw = 855, 315
    s.box(sx, 312, sw, 92, "green", ["eval score", "Hungarian experiment match; taxa as NCBI", "taxid sets → P / R / F1 by gold source type"], sub_size=10.5)
    s.box(sx, 436, sw, 60, "violet", ["scores.jsonl · report.md · report.html", "cross-tab by gold source type"], sub_size=10)
    s.box(sx, 548, sw, 72, "green", ["review packet", "self-contained HTML; curators judge the", "draft against the paper → verdict JSON"], sub_size=10.5)
    s.box(sx, 652, sw, 60, "violet", ["review ingest / report", "verdicts filed under data/reviews/<pmid>/"], sub_size=10)
    s.arrow([(sx + sw / 2, 404), (sx + sw / 2, 436)])
    s.arrow([(sx + sw / 2, 620), (sx + sw / 2, 652)])
    # gold -> scorer (above the divider, down the right edge)
    s.arrow([(910, 108), (1130, 108), (1130, 312)], color=t["coral"][1])
    s.arrow([(1000, 222), (1000, 312)], color=t["coral"][1])
    s.text(1085, 296, "gold", size=10.5, fill=t["coral"][1], italic=True, anchor="middle")
    # prediction -> scorer / packet
    s.arrow([(xs[3] + w, r4 + 32), (835, r4 + 32), (835, 358), (sx, 358)])
    s.arrow([(835, 584), (sx, 584)])
    s.text(sx, 772, "Prediction records cross the firewall one way:", size=10.5, fill=t["muted"], italic=True)
    s.text(sx, 786, "curator → scorer. Gold never flows the other way.", size=10.5, fill=t["muted"], italic=True)
    s.text(sx, 730, "Names → taxids via TaxonomyDB; retired ids", size=10.5, fill=t["muted"], italic=True)
    s.text(sx, 744, "canonicalised through merged.dmp on both sides.", size=10.5, fill=t["muted"], italic=True)

    legend(
        s, 828,
        [("violet", "data artifact"), ("green", "CLI command / pipeline stage"), ("blue", "shared service"),
         ("amber", "external source"), ("coral", "held-out gold (scorer only)")],
        [("dashed-box", "optional stage"), ("arrow", "data flow"),
         ("fw", "data firewall: curator never reads gold")],
    )
    return s.render()


# ---------------------------------------------------------------------------
# Figure 2: data model
# ---------------------------------------------------------------------------
ROW = 17


def class_box(
    s: Svg, x: int, y: int, w: int, name: str, rows: list, *, badge: str | None = None,
    role: str = "violet", dashed: bool = False,
) -> int:
    """Draw a class card. rows: (slot, type, flags, tone) or ('#', 'Group heading'). Returns bottom y."""
    t = s.t
    fill, stroke, ink = t[role]
    height = 30 + sum(ROW + 3 if r[0] == "#" else ROW for r in rows) + 8
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    s.add(f'<rect x="{x}" y="{y}" width="{w}" height="{height}" rx="8" fill="{t["panel"]}" stroke="{stroke}" stroke-width="1.5"{dash}/>')
    s.add(f'<path d="M{x},{y + 30} L{x},{y + 8} Q{x},{y} {x + 8},{y} L{x + w - 8},{y} Q{x + w},{y} {x + w},{y + 8} L{x + w},{y + 30} Z" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')
    s.text(x + 12, y + 20, name, size=14, weight="700", fill=ink)
    if badge:
        s.text(x + w - 12, y + 20, badge, size=10, fill=ink, anchor="end", italic=True)
    cy = y + 30 + 4
    tone_color = {"enum": t["violet"][1], "onto": t["amber"][1], "ref": t["green"][1], "": t["muted"]}
    for r in rows:
        if r[0] == "#":
            cy += 3
            s.text(x + 12, cy + 12, r[1].upper(), size=9.5, weight="700", fill=t["muted"])
            cy += ROW
            continue
        slot, typ, flags, tone = r
        s.text(x + 12, cy + 12, slot, size=11.5, mono=True)
        if flags:
            fx = x + 12 + len(slot) * 6.95 + 3
            s.text(fx, cy + 12, flags, size=11, mono=True, weight="700", fill=t["coral"][1])
        s.text(x + w - 12, cy + 12, typ, size=11, anchor="end", fill=tone_color[tone], weight="600" if tone else "normal")
        cy += ROW
    return y + height


def datamodel(theme: str) -> str:
    s = Svg(
        1200, 940, theme,
        "BugSigDB LinkML data model",
        "Study contains Experiments, each Experiment contains Signatures, each Signature lists Taxa. "
        "Study, Experiment and Signature mix in CurationProvenance, which links to Review records. "
        "Condition, body site, host species and taxa are bound to EFO, UBERON and NCBI Taxonomy.",
    )
    t = s.t
    s.text(24, 32, "BugSigDB data model — schema/bugsigdb.yaml", size=17, weight="700")
    s.text(24, 52, "6 classes · 63 slots · 12 enums. Shown: the slots curators and the agent fill in; one card per class.", size=11.5, fill=t["muted"])

    study = [
        ("uid", "string", "◆", ""), ("pmid", "integer", "", ""), ("doi", "string", "", ""), ("uri", "string", "", ""),
        ("citation_mode", "CitationMode", "*", "enum"), ("title", "string", "", ""), ("authors", "string[]", "", ""),
        ("journal", "string", "", ""), ("year", "integer", "", ""), ("pages", "string", "", ""),
        ("first_page", "integer", "", ""), ("keywords", "string[]", "", ""), ("abstract", "string", "", ""),
        ("study_design", "StudyDesign[]", "", "enum"), ("experiments", "Experiment[]", "", "ref"),
    ]
    exp = [
        ("#", "Subjects and groups"),
        ("location_of_subjects", "string[]", "", ""), ("host_species", "NCBITaxon", "*", "onto"),
        ("body_site", "UBERON[]", "", "onto"), ("condition", "EFO / MONDO[]", "", "onto"),
        ("group_{0,1}_name", "string", "", ""), ("group_{0,1}_definition", "string", "", ""),
        ("group_{0,1}_sample_size", "integer", "", ""), ("antibiotics_exclusion", "string", "", ""),
        ("matched_on", "string[]", "", ""), ("confounders_controlled_for", "string[]", "", ""),
        ("#", "Sequencing"),
        ("sequencing_type", "SequencingType", "", "enum"), ("variable_region_{lower,upper}_bound", "SixteenSRegion", "", "enum"),
        ("sequencing_platform", "SequencingPlatform[]", "", "enum"),
        ("#", "Statistics"),
        ("data_transformation", "DataTransformation", "", "enum"), ("statistical_test", "StatisticalTest[]", "", "enum"),
        ("significance_threshold", "float", "", ""), ("mht_correction", "boolean", "", ""), ("lda_score_above", "float", "", ""),
        ("#", "Alpha diversity (AlphaDivChange enum)"),
        ("pielou · shannon · chao1 · simpson", "AlphaDivChange", "", "enum"),
        ("inverse_simpson · richness · faith", "AlphaDivChange", "", "enum"),
        ("#", "Relationships"),
        ("study", "Study", "", "ref"), ("signatures", "Signature[]", "", "ref"),
    ]
    sig = [
        ("source", "string", "", ""), ("description", "string", "", ""),
        ("abundance_in_group_1", "AbundanceDirection", "", "enum"), ("taxa", "Taxon[]", "", "ref"),
    ]
    tax = [
        ("ncbi_id", "NCBITaxon", "◆", "onto"), ("taxon_name", "string", "", ""),
        ("taxonomic_rank", "string", "", ""), ("lineage", "string[]", "", ""),
    ]
    rev = [
        ("reviewer", "string", "", ""), ("review_date", "date", "", ""), ("review_state", "ReviewState", "*", "enum"),
        ("quality_control_tags", "QualityControlTag[]", "", "enum"), ("review_subject", "string", "", ""),
    ]
    prov = [
        ("curation_state", "CurationState", "", "enum"), ("curator", "string[]", "", ""),
        ("curated_date", "date", "", ""), ("revision_editor", "string[]", "", ""), ("reviews", "Review[]", "", "ref"),
    ]

    top = 76
    b_study = class_box(s, 30, top, 300, "Study", study, badge="one publication  ·  + provenance")
    b_exp = class_box(s, 405, top, 390, "Experiment", exp, badge="one two-group comparison  ·  + provenance")
    b_sig = class_box(s, 870, top, 300, "Signature", sig, badge="taxa moving one way  ·  + provenance")
    tax_top = b_sig + 52
    b_tax = class_box(s, 870, tax_top, 300, "Taxon", tax, badge="NCBI Taxonomy node", role="amber")
    prov_top = b_study + 50
    b_prov = class_box(s, 30, prov_top, 300, "CurationProvenance", prov, badge="mixin", role="gray", dashed=True)
    rev_top = b_tax + 50
    b_rev = class_box(s, 870, rev_top, 300, "Review", rev, badge="review workflow", role="green")

    # relationship arrows (a filled diamond marks the containing class)
    def hdiamond(x, y):
        s.add(f'<polygon points="{x},{y} {x + 7},{y - 5} {x + 14},{y} {x + 7},{y + 5}" fill="{t["line"]}"/>')

    def vdiamond(x, y):
        s.add(f'<polygon points="{x},{y} {x + 5},{y + 7} {x},{y + 14} {x - 5},{y + 7}" fill="{t["line"]}"/>')

    ya = top + 34 + 14 * ROW + 8  # Study.experiments row
    hdiamond(330, ya)
    s.arrow([(344, ya), (405, ya)])
    s.text(375, ya - 6, "0..*", size=10.5, fill=t["muted"], anchor="middle", italic=True)
    yb = b_exp - 8 - ROW / 2  # Experiment.signatures row (last)
    hdiamond(795, yb)
    s.arrow([(809, yb), (835, yb), (835, top + 15), (870, top + 15)])
    s.text(822, yb - 6, "0..*", size=10.5, fill=t["muted"], anchor="middle", italic=True)
    vdiamond(1020, b_sig)
    s.arrow([(1020, b_sig + 14), (1020, tax_top)])
    s.text(1052, b_sig + 32, "taxa 0..*", size=10.5, fill=t["muted"], anchor="middle", italic=True)
    ry = b_prov - 8 - ROW / 2  # CurationProvenance.reviews row (last)
    route = max(b_exp, b_prov) + 22
    s.arrow([(330, ry), (360, ry), (360, route), (1020, route), (1020, b_rev)], dashed=True)
    s.text(690, route - 6, "reviews 0..*", size=10.5, fill=t["muted"], anchor="middle", italic=True)
    s.text(30, b_prov + 18, "Mixed into Study, Experiment and Signature.", size=10.5, fill=t["muted"], italic=True)
    s.text(30, b_prov + 32, "Slot names with {…} or · stand for several slots.", size=10.5, fill=t["muted"], italic=True)

    legend(
        s, route + 24,
        [("violet", "class card"), ("amber", "NCBI Taxonomy class"), ("green", "review workflow"), ("gray", "mixin")],
        [("arrow", "contains (◆ at the container)"), ("dashed-arrow", "mixin slot → Review")],
        note="Slot notation:  * required  ·  [] multivalued  ·  ◆ identifier.   Type colour: violet = enum, amber = ontology / NCBI-bound, green = class reference.",
    )
    s.h = route + 24 + 98 + 14
    return s.render()


def main() -> None:
    for theme in THEMES:
        (OUT / f"workflow-{theme}.svg").write_text(workflow(theme))
        (OUT / f"data-model-{theme}.svg").write_text(datamodel(theme))
        print(f"wrote {theme} variants")


if __name__ == "__main__":
    main()
