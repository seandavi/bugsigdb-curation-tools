"""P2 assignment on the 34620922 supplement: which of the 48 gold experiments does a DA page report?

Oracle-stub condition (gold experiments as the option set). A page's gold set = every experiment whose
signature ``source`` is "Supplementary Table N" for an N captioned ("Table SN") on that page. Top-1 is
correct when the pick is in the set. Pages are text state (the extracted page text).
"""

from __future__ import annotations

import re
from typing import Any

from probe_common import PMC_MAP, RELATIONAL, bounded_gather, coverage_at_confidence

from bugsigdb_curation.decision import Choice, DecisionModel
from bugsigdb_curation.eval.gold import load_gold
from bugsigdb_curation.pdf import open_pdf
from experiments import supp_pages

NONE_OPTION = "none"
_SUPP = re.compile(r"supplementary table\s*s?(\d+)", re.IGNORECASE)


def load_units() -> list[dict[str, Any]]:
    study = load_gold(RELATIONAL, PMC_MAP)["34620922"]
    by_table: dict[str, set[str]] = {}
    desc: dict[str, str] = {}
    for exp in study.experiments:
        desc[exp.experiment_id] = f"{exp.group_0_name} vs {exp.group_1_name} · {', '.join(exp.body_site)} · {exp.experiment_name or ''}"[:300]
        for sig in exp.signatures:
            for n in _SUPP.findall(sig.source or ""):
                by_table.setdefault(n, set()).add(exp.experiment_id)
    labels = supp_pages.labels()
    units = []
    with open_pdf(supp_pages.PDF.read_bytes()) as doc:
        for i in range(1, doc.n_pages + 1):
            if not labels[i]["has_da_results"]:
                continue
            text = doc.page_text(i - 1)
            tables = sorted(set(re.findall(r"Table S(\d+)", text)))
            gold = sorted(set().union(*(by_table.get(n, set()) for n in tables)))
            units.append({"id": f"p{i:02d}", "text": text[:12000], "tables": tables, "gold": gold, "options": desc})
    return units


async def run(model: DecisionModel) -> dict[str, Any]:
    units = load_units()

    async def one(u: dict[str, Any]) -> dict[str, Any]:
        crit: dict[str, Any] = dict(u["options"])
        crit[NONE_OPTION] = "this page does not report results for any listed experiment"
        ans = (
            await model.decide(
                stage="locate_page",
                state={"page_text": u["text"]},
                questions={"experiment": Choice("Which experiment (one group comparison in one host species/region) does the first table on this page report results for?", crit)},
            )
        )["experiment"]
        return {"id": u["id"], "tables": u["tables"], "gold": u["gold"], "pick": ans.choice, "conf": ans.confidence}

    return {"units": await bounded_gather(units, one, limit=6)}


def score(results: dict[str, Any]) -> dict[str, Any]:
    units = [u for u in results["units"] if u["gold"]]
    ok = [u["pick"] in u["gold"] for u in units]
    return {
        "n_pages": len(results["units"]),
        "n_pages_with_gold": len(units),
        "top1_in_gold_set": sum(ok) / len(ok) if ok else None,
        "mean_gold_set_size": sum(len(u["gold"]) for u in units) / len(units) if units else None,
        "coverage_at_confidence": coverage_at_confidence(ok, [u["conf"] for u in units], (0.0, 0.1, 0.2, 0.3, 0.5)),
        "none_picked": sum(u["pick"] == NONE_OPTION for u in units),
        "n_options": 48,
    }
