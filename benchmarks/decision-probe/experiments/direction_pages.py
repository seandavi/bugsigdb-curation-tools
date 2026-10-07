"""P3 on the L031 failure mode: per-taxon direction from the 34620922 supplement *table pages*.

For each gold signature citing "Supplementary Table N" whose caption ("Table SN") is on a PDF page, ask --
with that page's text as state -- for every gold taxon, "is it more abundant in group_1 than group_0?".
Truth = the signature's gold direction (``increased`` -> yes). Groups are the gold experiment's group
names (oracle-stub: isolates direction from experiment extraction). L031 saw 11.5% direction accuracy
from a generative extractor on these same tables.
"""

from __future__ import annotations

import csv
import re
from typing import Any

from probe_common import (
    PMC_MAP,
    RELATIONAL,
    binary_report,
    bounded_gather,
    coverage_at_confidence,
)

from bugsigdb_curation.decision import DecisionModel, Noul
from bugsigdb_curation.eval.gold import load_gold
from bugsigdb_curation.pdf import open_pdf
from experiments import supp_pages
from experiments.figbench import clean_taxon

_SUPP = re.compile(r"supplementary table\s*s?(\d+)", re.IGNORECASE)
CHUNK = 60


def load_units(*, with_image: bool = False) -> list[dict[str, Any]]:
    names = {int(r["ncbi_id"]): r["taxon_name"] for r in csv.DictReader((RELATIONAL / "taxa.csv").open())}
    study = load_gold(RELATIONAL, PMC_MAP)["34620922"]
    pages_for: dict[str, list[str]] = {}
    first_image: dict[str, bytes] = {}
    with open_pdf(supp_pages.PDF.read_bytes()) as doc:
        for i in range(doc.n_pages):
            text = doc.page_text(i)
            for n in set(re.findall(r"Table S(\d+)", text)):
                pages_for.setdefault(n, []).append(text)
                if with_image and n not in first_image:
                    first_image[n] = supp_pages.render_page(doc, i)
    units = []
    for exp in study.experiments:
        for sig in exp.signatures:
            if sig.direction is None:
                continue
            tables = [n for n in _SUPP.findall(sig.source or "") if n in pages_for]
            taxa = [clean_taxon(names[t]) for t in sorted(sig.taxa) if t in names]
            if not tables or not taxa:
                continue
            page_text = "\n\n".join(pages_for[tables[0]])[:24000]
            units.append(
                {
                    "id": sig.signature_id,
                    "table": tables[0],
                    "group_0": exp.group_0_name,
                    "group_1": exp.group_1_name,
                    "increased": sig.direction == "increased",
                    "taxa": taxa,
                    "page_text": page_text,
                    "image": first_image.get(tables[0]) if with_image else None,
                }
            )
    return units


async def run(model: DecisionModel, *, with_image: bool = False) -> dict[str, Any]:
    async def one(u: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for start in range(0, len(u["taxa"]), CHUNK):
            qs = {
                f"t{i}": Noul(
                    {
                        "question": "In the comparison below, is the taxon more abundant / enriched in group_1 than in group_0 according to this page?",
                        "taxon": taxon,
                        "group_0": u["group_0"],
                        "group_1": u["group_1"],
                    },
                    criteria={"true": "higher in group_1", "false": "higher in group_0"},
                )
                for i, taxon in enumerate(u["taxa"][start : start + CHUNK], start)
            }
            state = {"page": u["table"]} if with_image else {"page_text": u["page_text"]}
            ans = await model.decide(stage="direction_page", state=state, questions=qs, images=[u["image"]] if u["image"] else [])
            rows += [{"taxon": q.instructions["taxon"], "p_yes": ans[k].p_yes} for k, q in qs.items()]  # type: ignore[index,union-attr]
        return {"id": u["id"], "table": u["table"], "increased": u["increased"], "taxa": rows}

    return {"units": await bounded_gather(load_units(with_image=with_image), one, limit=4)}


def score(results: dict[str, Any]) -> dict[str, Any]:
    y, p, sig_ok = [], [], []
    for u in results["units"]:
        ok = []
        for t in u["taxa"]:
            y.append(int(u["increased"]))
            p.append(t["p_yes"])
            ok.append((t["p_yes"] >= 0.5) == u["increased"])
        sig_ok.append(sum(ok) / len(ok) > 0.5)
    correct = [(s >= 0.5) == bool(t) for s, t in zip(p, y)]
    return {
        "n_signatures": len(results["units"]),
        "n_taxa": len(y),
        "taxon_accuracy": sum(correct) / len(correct),
        "signature_majority_correct": sum(sig_ok) / len(sig_ok),
        "binary": binary_report(y, p),
        "coverage_at_confidence": coverage_at_confidence(correct, [abs(s - 0.5) * 2 for s in p], (0.0, 0.5, 0.8, 0.9, 0.95)),
    }
