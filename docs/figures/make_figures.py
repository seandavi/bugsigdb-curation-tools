"""Generate the README figures as standalone SVG (stdlib only).

    python docs/figures/make_figures.py

Writes, next to this file, a light and a dark variant (transparent background)
of each figure:

    logical-{light,dark}.svg      Figure 1: the logical view (what each part does and may read)
    commands-{light,dark}.svg     Figure 2: the command workflow (`bugsigdb ...`, in the order you run it)
    data-model-{light,dark}.svg   Figure 3: the LinkML data model (schema/bugsigdb.yaml)

Colour roles are shared by all figures: violet = data artifact, green = CLI
command / pipeline stage, blue = shared service, amber = external source or
people, coral = held-out gold. Every figure carries its own legend.
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

    def box(
        self, x, y, w, h, role, lines, *, dashed=False, rx=8, title_size=12.5, sub_size=11, round_=False,
        mono_first=False,
    ):
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
                mono=mono_first and first,
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
# Figure 1: logical view
# ---------------------------------------------------------------------------
def logical(theme: str) -> str:
    s = Svg(
        1200, 948, theme,
        "BugSigDB automated curation: logical view",
        "A held-out gold is built from the public BugSigDB export. The curator takes a PMID, resolves the paper, "
        "reads its text, tables, figures and optionally supplements, segments it into experiments, finds the "
        "evidence for each, extracts and resolves taxa, and emits a validated prediction record with a sidecar. "
        "The scorer is the only reader of gold; human review sees drafts only. A data firewall separates the "
        "curator from the gold.",
    )
    t = s.t
    s.text(24, 32, "BugSigDB automated curation — logical view", size=17, weight="700")
    s.text(
        24, 52,
        "What each part does and what it may read. Stage codes (S0–S10) are those of Table 2; Figure 2 maps the parts to commands.",
        size=11.5, fill=t["muted"],
    )

    # A: corpus -> held-out gold --------------------------------------------
    s.text(30, 86, "A · CORPUS — build the held-out gold", size=11.5, weight="700", fill=t["muted"])
    s.box(30, 98, 200, 62, "amber", ["BugSigDB public export", "waldronlab/bugsigdbexports"], sub_size=10.5)
    s.box(
        270, 98, 520, 62, "coral",
        ["Held-out gold", "studies · experiments · signatures · taxa", "+ PMID → PMCID map (84% of studies have PMC full text)"],
        sub_size=10.5,
    )
    s.arrow([(230, 129), (270, 129)])
    T = 236  # top of the curator and right-hand columns
    s.arrow([(790, 129), (1140, 129), (1140, T)], color=t["coral"][1])
    s.text(1150, 190, "gold", size=10.5, fill=t["coral"][1], italic=True)

    # B: curate ------------------------------------------------------------------
    s.text(30, T - 16, "B · CURATE — input: a PMID, and nothing else", size=11.5, weight="700", fill=t["muted"])
    cx, cw, h = [32, 297, 562], 235, 66
    r1 = T
    r2 = r1 + h + 30
    s.box(cx[0], r1, cw, h, "violet", ["PMID"], round_=True)
    s.box(cx[1], r1, cw, h, "green", ["S0 · resolve", "PMID → PMCID and DOI", "NCBI idconv; Europe PMC if throttled"], sub_size=10.5)
    s.box(cx[2], r1, cw, h, "green", ["S1 · evidence", "text · tables · figure images", "Europe PMC + PMC page (cached)"], sub_size=10.5)
    s.box(cx[0], r2, cw, h, "green", ["S2 · study metadata", "title · authors · journal · design"], sub_size=10.5)
    s.box(cx[1], r2, cw, h, "green", ["S3 · segment", "one stub per two-group comparison"], sub_size=10.5)
    s.box(
        cx[2], r2, cw, h, "green",
        ["S5a · rank evidence", "tables and figures by p(DA), once per", "study; one regex pick without Clef"],
        sub_size=10.5,
    )
    for i in range(2):
        s.arrow([(cx[i] + cw, r1 + h / 2), (cx[i + 1], r1 + h / 2)])
        s.arrow([(cx[i] + cw, r2 + h / 2), (cx[i + 1], r2 + h / 2)])
    s.arrow([(cx[2] + cw / 2, r1 + h), (cx[2] + cw / 2, r1 + h + 15), (cx[0] + cw / 2, r1 + h + 15), (cx[0] + cw / 2, r2)])

    # per-experiment loop
    py = r2 + h + 28  # panel top
    by, bh = py + 28, 78
    s.add(
        f'<rect x="22" y="{py}" width="782" height="{bh + 40}" rx="10" fill="none" stroke="{t["line"]}"'
        f' stroke-width="1.5" stroke-dasharray="6 4"/>'
    )
    s.text(792, py + 18, "↻ for each experiment stub (with Clef, up to 3 ranked artifacts are tried in turn)", size=10.5, fill=t["muted"], anchor="end", italic=True)
    s.box(cx[0], by, cw, bh, "green", ["S4 · experiment", "groups · host · body site · condition", "sequencing · statistics"], sub_size=10.5)
    s.box(
        cx[1], by, cw, bh, "green",
        ["S5b/S6 · extract + resolve", "taxa and direction from the first", "artifact that reports the comparison;", "ids verified, unresolved names grounded"],
        sub_size=10,
    )
    s.box(cx[2], by, cw, bh, "green", ["S10 · verify / panel", "split designs only: check each taxon", "and direction; repair ≤ 2 rounds"], dashed=True, sub_size=10)
    for i in range(2):
        s.arrow([(cx[i] + cw, by + bh / 2), (cx[i + 1], by + bh / 2)])
    # S5a (col 3, row 2) -> S4 (col 1, loop)
    s.arrow([(cx[2] + cw / 2, r2 + h), (cx[2] + cw / 2, py - 13), (cx[0] + cw / 2, py - 13), (cx[0] + cw / 2, by)])

    r4 = py + bh + 40 + 28
    s.box(cx[0], r4, cw, h, "green", ["S1b · supplements", "screen sheets and pages, extract,", "one-vs-rest expand, append"], dashed=True, sub_size=10.5)
    s.box(cx[1], r4, cw, h, "green", ["S8 · assemble", "nested Study → Experiment → Signature"], sub_size=10.5)
    s.box(cx[2], r4, cw, h, "green", ["S9 · validate", "LinkML schema: types, enums,", "required fields; failures recorded"], sub_size=10.5)
    for i in range(2):
        s.arrow([(cx[i] + cw, r4 + h / 2), (cx[i + 1], r4 + h / 2)])
    s.arrow([(cx[2] + cw / 2, by + bh), (cx[2] + cw / 2, py + bh + 40 + 14), (cx[0] + cw / 2, py + bh + 40 + 14), (cx[0] + cw / 2, r4)])
    r5 = r4 + h + 26
    s.box(
        cx[2], r5, cw, h, "violet",
        ["prediction record + sidecar", "record: Study → Experiment → Signature", "sidecar: rankings, skips, fallbacks"], sub_size=10.5,
    )
    s.arrow([(cx[2] + cw / 2, r4 + h), (cx[2] + cw / 2, r5)])

    # shared services and sources
    sy = r5 + h + 22
    s.text(30, sy, "Services and sources the stages draw on", size=11, weight="700", fill=t["muted"])
    sx4, sw4 = [32, 226, 420, 614], 180
    s.box(sx4[0], sy + 10, sw4, 66, "amber", ["Paper sources", "Europe PMC · PMC · suppl. ZIP", "used by S0, S1, S1b"], sub_size=10.5)
    s.box(sx4[1], sy + 10, sw4, 66, "blue", ["TaxonomyDB (local)", "NCBI taxdump, live for gaps", "used by S5b/S6, S10, scorer"], sub_size=10.5)
    s.box(sx4[2], sy + 10, sw4, 66, "blue", ["LLM · LiteLLM", "Gemini, generative calls", "used by S2–S5b, S1b, S10"], sub_size=10.5)
    s.box(sx4[3], sy + 10, sw4, 66, "blue", ["Decision model", "Cloudflare Clef + OLS4 terms", "S5a · S1b screen · S4 body site"], dashed=True, sub_size=10)

    # C: evaluate -----------------------------------------------------------------
    ex, ew = 855, 315
    s.text(ex, T - 16, "C · EVALUATE — the only reader of gold", size=11.5, weight="700", fill=t["muted"])
    s.box(ex, T, ew, 84, "green", ["eval score", "match experiments (Hungarian), pair", "signatures by taxa overlap, compare", "taxa as NCBI id sets"], sub_size=10.5)
    s.box(ex, T + 106, ew, 78, "violet", ["scores", "P / R / F1 · direction accuracy ·", "over- and under-segmentation,", "all cut by gold source type"], sub_size=10.5)
    s.arrow([(ex + ew / 2, T + 84), (ex + ew / 2, T + 106)])

    # D: human review ------------------------------------------------------------
    s.text(ex, T + 220, "D · HUMAN REVIEW — a draft, never gold", size=11.5, weight="700", fill=t["muted"])
    s.box(ex, T + 238, ew, 66, "green", ["review packet + bundle", "one HTML page per draft, with its sidecar,", "zipped for curators"], sub_size=10.5)
    s.box(ex, T + 330, ew, 66, "amber", ["Curators (people, outside the repo)", "judge each taxon, direction, experiment", "against the paper → verdict JSON"], dashed=True, sub_size=10.5)
    s.box(ex, T + 422, ew, 66, "violet", ["verdict report", "taxa precision · direction flips ·", "time saved · reviewer notes"], sub_size=10.5)
    s.arrow([(ex + ew / 2, T + 304), (ex + ew / 2, T + 330)])
    s.arrow([(ex + ew / 2, T + 396), (ex + ew / 2, T + 422)])

    # prediction record crosses the firewall one way, to the scorer and to review
    s.arrow([(cx[2] + cw, r5 + h / 2), (835, r5 + h / 2), (835, T + 42), (ex, T + 42)])
    s.arrow([(835, T + 271), (ex, T + 271)])
    s.text(ex, T + 514, "Names → taxids through the same TaxonomyDB;", size=10.5, fill=t["muted"], italic=True)
    s.text(ex, T + 528, "retired ids are canonicalised on both sides.", size=10.5, fill=t["muted"], italic=True)
    s.text(ex, T + 554, "Records cross the firewall one way: curator → scorer", size=10.5, fill=t["muted"], italic=True)
    s.text(ex, T + 568, "and curator → reviewers. Gold never flows back.", size=10.5, fill=t["muted"], italic=True)

    # Data firewall: between the curator and everything on the right ------------------
    fy = sy + 10 + 66 + 8
    s.add(f'<line x1="815" y1="{T - 36}" x2="815" y2="{fy}" stroke="{t["coral"][1]}" stroke-width="2.5" stroke-dasharray="7 5"/>')
    s.add(f'<rect x="745" y="{T - 62}" width="140" height="24" rx="12" fill="{t["panel"]}" stroke="{t["coral"][1]}" stroke-width="1.5"/>')
    s.text(815, T - 46, "DATA FIREWALL", size=11, weight="700", fill=t["coral"][1], anchor="middle")

    legend(
        s, fy + 16,
        [("violet", "data artifact"), ("green", "pipeline stage / command"), ("blue", "shared service"),
         ("amber", "external source or people"), ("coral", "held-out gold (scorer only)")],
        [("dashed-box", "optional stage"), ("arrow", "data flow"),
         ("fw", "data firewall: the curator never reads gold")],
    )
    s.h = fy + 16 + 78 + 14
    return s.render()


# ---------------------------------------------------------------------------
# Figure 2: command workflow
# ---------------------------------------------------------------------------
RX, RW = 70, 240  # reads column
CX, CW = 342, 448  # command column
WX, WW = 822, 348  # writes column


def _artifacts(s: Svg, x: int, w: int, y: int, h: int, items: list) -> None:
    """One artifact box, or two stacked single-line ones, in a column."""
    if len(items) == 1:
        role, lines = items[0]
        s.box(x, y, w, h, role, lines, sub_size=10.5)
        return
    each = (h - 8) / len(items)
    for i, (role, lines) in enumerate(items):
        s.box(x, y + i * (each + 8), w, each, role, lines[:1], sub_size=10.5, title_size=12)


def cmd_row(s: Svg, y: int, n: int, reads: list, cmd: list, writes: list, *, h: int = 52, human: bool = False) -> None:
    t = s.t
    s.add(f'<circle cx="46" cy="{y + h / 2}" r="12" fill="{t["panel"]}" stroke="{t["ink"]}" stroke-width="1.5"/>')
    s.text(46, y + h / 2 + 4, str(n), size=12, weight="700", anchor="middle")
    _artifacts(s, RX, RW, y, h, reads)
    if human:
        s.box(CX, y, CW, h, "amber", cmd, dashed=True, sub_size=10.5)
    else:
        s.box(CX, y, CW, h, "green", cmd, sub_size=10.5, mono_first=True, title_size=11.5)
    _artifacts(s, WX, WW, y, h, writes)
    s.arrow([(RX + RW, y + h / 2), (CX, y + h / 2)])
    s.arrow([(CX + CW, y + h / 2), (WX, y + h / 2)])


def phase(s: Svg, y: int, h: int, title: str, note: str) -> None:
    t = s.t
    s.add(f'<rect x="20" y="{y}" width="1160" height="{h}" rx="10" fill="{t["panel"]}" stroke="{t["gray"][1]}"/>')
    s.text(34, y + 20, title, size=12, weight="700", fill=t["muted"])
    s.text(1166, y + 20, note, size=10.5, fill=t["muted"], italic=True, anchor="end")


def commands(theme: str) -> str:
    s = Svg(
        1200, 1160, theme,
        "BugSigDB automated curation: command workflow",
        "The bugsigdb commands in the order they are run. Build the gold and the taxonomy database once "
        "(export, split, pmc-map, taxonomy build); curate PMIDs into prediction records; score them against "
        "the gold; and route drafts to curators with review packet, bundle, ingest and report.",
    )
    t = s.t
    V, C, A = "violet", "coral", "amber"
    s.text(24, 32, "BugSigDB automated curation — the command workflow", size=17, weight="700")
    s.text(
        24, 52,
        "Commands in the order you run them. Each reads the artifact on its left and writes the one on its right; names match across rows.",
        size=11.5, fill=t["muted"],
    )

    gap, hh, hd = 8, 52, 68
    y = 68
    # Phase 1 ---------------------------------------------------------------
    ph = 30 + 4 * hh + 3 * gap + 12
    phase(s, y, ph, "1 · BUILD THE GOLD AND THE TAXONOMY", "once; needs the network")
    ry = y + 30
    cmd_row(s, ry, 1, [(A, ["waldronlab/bugsigdbexports", "GitHub, public"])],
            ["bugsigdb export", "download the merged CSV dump (--select gmt for GMT sets)"],
            [(V, ["data/exports/full_dump.csv", "one row per signature"])])
    ry += hh + gap
    cmd_row(s, ry, 2, [(V, ["data/exports/full_dump.csv"])],
            ["bugsigdb split", "flat dump → relational tables"],
            [(C, ["data/exports/relational/*.csv", "studies, experiments, signatures, taxa"])])
    ry += hh + gap
    cmd_row(s, ry, 3, [(C, ["relational/studies.csv"])],
            ["bugsigdb pmc-map", "PMID → PMCID by NCBI idconv (about 84% have one)"],
            [(C, ["data/eval/pmid_pmcid_map.csv"])])
    ry += hh + gap
    cmd_row(s, ry, 4, [(A, ["NCBI taxdump", "pinned release"])],
            ["bugsigdb taxonomy build --download --release 2026-07-01", "build the local, offline taxonomy database"],
            [(V, ["ncbi-taxdump-<release>.duckdb", "XDG cache, or BUGSIGDB_TAXONOMY_DB"])])
    y += ph + 14

    # Phase 2 ---------------------------------------------------------------
    ph = 30 + hd + 12
    phase(s, y, ph, "2 · CURATE", "never takes a gold path")
    cmd_row(s, y + 30, 5, [(V, ["a PMID, or the --smoke set"]), (V, ["taxonomy DB from step 4"])],
            ["bugsigdb curate --smoke -o preds/",
             "needs model keys in .env (Gemini; Cloudflare for Clef)",
             "levers: --decision-model clef · --supplements · --design"],
            [(V, ["preds/<pmid>.json · prediction record"]), (V, ["preds/_annotations/<pmid>.json · sidecar"])], h=hd)
    y += ph + 14

    # Phase 3 ---------------------------------------------------------------
    ph = 30 + hd + 12
    phase(s, y, ph, "3 · SCORE", "reads the gold to score the predictions")
    cmd_row(s, y + 30, 6, [(V, ["preds/ (step 5)"]), (C, ["gold tables + PMC map (steps 2–3)"])],
            ["bugsigdb eval score --pred preds/ --out report/ --smoke",
             "match experiments, pair signatures, compare taxa as taxid sets",
             "uses the taxonomy DB from step 4"],
            [(V, ["report/", "scores.jsonl · report.md · report.html"])], h=hd)
    y += ph + 14

    # Phase 4 ---------------------------------------------------------------
    ph = 30 + 5 * hh + 4 * gap + 12
    phase(s, y, ph, "4 · REVIEW", "drafts only; no gold")
    ry = y + 30
    cmd_row(s, ry, 7, [(V, ["preds/<pmid>.json + sidecar"])],
            ["bugsigdb review packet --pred preds/<pmid>.json --out packets/",
             "one HTML page per draft; pass --annotations for --smoke sidecars"],
            [(V, ["packets/<pmid>.html", "+ <pmid>.manifest.json, pinning the draft hash"])])
    ry += hh + gap
    cmd_row(s, ry, 8, [(V, ["packets/"])],
            ["bugsigdb review bundle --packets packets/ --out share/",
             "index + packets + README, zipped; --contact says where verdicts go"],
            [(V, ["share/bugsigdb-review-<date>.zip"])])
    ry += hh + gap
    cmd_row(s, ry, 9, [(V, ["the zip, sent to curators"])],
            ["Curators, in a browser (no command)", "open a packet, judge it against the paper,", "press “Export verdicts (JSON)”"],
            [(V, ["verdicts_<pmid>_<time>.json"])], human=True)
    ry += hh + gap
    cmd_row(s, ry, 10, [(V, ["verdicts_*.json"])],
            ["bugsigdb review ingest verdicts_*.json --manifests packets/",
             "validate against the schema and the draft hash; file by study"],
            [(V, ["data/reviews/<pmid>/", "<reviewer>_<time>.json"])])
    ry += hh + gap
    cmd_row(s, ry, 11, [(V, ["data/reviews/"])],
            ["bugsigdb review report --reviews data/reviews --out report.md",
             "pool verdicts per study and overall"],
            [(V, ["report.md", "taxa precision · direction flips · time saved"])])
    y += ph + 14

    # Other commands -----------------------------------------------------------
    ph = 30 + hh + 12
    phase(s, y, ph, "OTHER COMMANDS", "inspection and utilities")
    chips = [
        ("bugsigdb load FILE.csv", "dump → nested Study records"),
        ("bugsigdb validate FILE", "check records against the schema"),
        ("bugsigdb eval gold --smoke", "dump gold in the prediction shape"),
        ("bugsigdb supplements --pmid N", "list a paper's supplement files"),
        ("bugsigdb taxonomy lookup NAME", "name ↔ NCBI taxid, offline"),
    ]
    for i, (c, d) in enumerate(chips):
        s.box(32 + i * 226, y + 30, 212, hh, "green", [c, d], sub_size=10, mono_first=True, title_size=10.5)
    y += ph + 14

    legend(
        s, y,
        [("violet", "data artifact"), ("green", "bugsigdb command"), ("amber", "external source or people"),
         ("coral", "gold artifact (never given to curate)")],
        [("arrow", "reads → command → writes"), ("dashed-box", "manual step, no command")],
    )
    s.h = y + 78 + 14
    return s.render()


# ---------------------------------------------------------------------------
# Figure 3: data model
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
    s.text(24, 52, "6 classes · 64 slots · 12 enums. Every slot is shown, one card per class; related slots (group fields, alpha diversity) share a row.", size=11.5, fill=t["muted"])

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
        (OUT / f"logical-{theme}.svg").write_text(logical(theme))
        (OUT / f"commands-{theme}.svg").write_text(commands(theme))
        (OUT / f"data-model-{theme}.svg").write_text(datamodel(theme))
        print(f"wrote {theme} variants")


if __name__ == "__main__":
    main()
