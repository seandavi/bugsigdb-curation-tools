"""P3 (orientation) + the figure-type half of P2, on the 15 figure-benchmark figures.

Unit: one benchmark figure (image + legend). Per figure, per gold signature
(a taxon set sharing a direction and a group pair):

* P3: one ``noul`` per gold taxon -- "increased in group_1 vs group_0?" --
  truth = gold ``direction == "increased"``; plus one per-figure ``orientation``
  choice.
* P2: one ``figure_type`` choice per figure; truth = manifest ``figure_type``.

The gold groups/taxa are the oracle-stub condition: this measures the
direction judgment in isolation from taxon/experiment extraction errors.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from probe_common import (
    MANIFEST_PATH,
    binary_report,
    bounded_gather,
    coverage_at_confidence,
)
from probe_retrieve import figbench_image_path

from bugsigdb_curation.decision import Choice, DecisionModel, Noul

FIGURE_TYPES = {
    "lefse_lda_bar": "ranked bar chart of an effect size (LDA score, log fold change) with bars signed/coloured by group",
    "cladogram": "circular taxonomic tree with coloured clades marking differentially abundant taxa",
    "heatmap": "taxa x samples/groups heatmap of abundance or fold change",
    "box_or_violin": "box / violin / median-IQR plots of abundance per taxon, optionally with significance marks",
    "volcano": "volcano or MA plot of effect size against significance",
    "stacked_bar_composition": "stacked bars of relative abundance per sample or group",
    "network": "co-occurrence or correlation network",
    "other": "any other analysis figure",
    "not_a_figure": "not a data figure (photo, schematic, flow chart)",
}
#: manifest label -> probe label
FIGURE_TYPE_MAP = {
    "lefse_bar_LDA": "lefse_lda_bar",
    "cladogram": "cladogram",
    "heatmap": "heatmap",
    "box_or_violin_with_stats": "box_or_violin",
    "stacked_bar_composition": "stacked_bar_composition",
    "other": "other",
}
ORIENTATION = {
    "positive_or_enriched_means_group_0": "the figure shows enrichment/positive effect for group_0 when a taxon is marked as higher in group_0",
    "positive_or_enriched_means_group_1": "the figure shows enrichment/positive effect for group_1 when a taxon is marked as higher in group_1",
    "unclear": "the figure does not make clear which group a bar/colour belongs to",
}
_RANK_PREFIX = re.compile(r"^[a-z]__")
CHUNK = 60


def clean_taxon(name: str) -> str:
    return _RANK_PREFIX.sub("", name).replace("_", " ").strip()


def load_items() -> list[dict[str, Any]]:
    return json.loads(MANIFEST_PATH.read_text())


def taxon_questions(sig: dict[str, Any]) -> list[tuple[dict[str, Noul], dict[str, bool]]]:
    """Chunked (questions, truth) for one gold signature."""
    taxa = [clean_taxon(t["name"]) for t in sig["taxa"]]
    truth = sig["direction"] == "increased"
    out = []
    for start in range(0, len(taxa), CHUNK):
        qs: dict[str, Noul] = {}
        tr: dict[str, bool] = {}
        for i, taxon in enumerate(taxa[start : start + CHUNK], start):
            qid = f"t{i}"
            qs[qid] = Noul(
                {
                    "question": "According to this figure, is the taxon more abundant / enriched in group_1 than in group_0?",
                    "taxon": taxon,
                    "group_0": sig["group_0_name"],
                    "group_1": sig["group_1_name"],
                },
                criteria={"true": "higher in group_1 than group_0", "false": "higher in group_0 than group_1"},
            )
            tr[qid] = truth
        out.append((qs, tr))
    return out


async def run(model: DecisionModel) -> dict[str, Any]:
    items = load_items()

    async def one(entry: dict[str, Any]) -> dict[str, Any]:
        image = figbench_image_path(entry).read_bytes()
        state = {"figure_legend": entry["legend"], "study": entry["study_title"]}
        res: dict[str, Any] = {"pmid": entry["pmid"], "figure_type_gold": FIGURE_TYPE_MAP[entry["figure_type"]], "taxa": []}

        ans = await model.decide(
            stage=f"figtype:{entry['pmid']}",
            state=state,
            questions={"figure_type": Choice("What kind of figure is this?", FIGURE_TYPES)},
            images=[image],
        )
        a = ans["figure_type"]
        res["figure_type"] = {"choice": a.choice, "confidence": a.confidence, "probabilities": a.probabilities}

        for sig in entry["gold"]:
            ostate = {**state, "group_0": sig["group_0_name"], "group_1": sig["group_1_name"]}
            oans = await model.decide(
                stage=f"orientation:{entry['pmid']}",
                state=ostate,
                questions={
                    "orientation": Choice(
                        {
                            "question": "In this figure, what does a taxon being marked enriched / positive / the bar colour mean?",
                            "group_0": sig["group_0_name"],
                            "group_1": sig["group_1_name"],
                        },
                        ORIENTATION,
                    )
                },
                images=[image],
            )
            o = oans["orientation"]
            res.setdefault("orientation", []).append({"choice": o.choice, "confidence": o.confidence})
            for qs, truth in taxon_questions(sig):
                tans = await model.decide(stage=f"direction:{entry['pmid']}", state=ostate, questions=qs, images=[image])
                for qid, q in qs.items():
                    res["taxa"].append(
                        {
                            "taxon": q.instructions["taxon"],  # type: ignore[index]
                            "signature_id": sig["signature_id"],
                            "gold_increased": truth[qid],
                            "p_yes": tans[qid].p_yes,  # type: ignore[union-attr]
                        }
                    )
        return res

    results = await bounded_gather(items, one, limit=4)
    return {"items": results}


def score(results: dict[str, Any]) -> dict[str, Any]:
    items = results["items"]
    figtypes = {i["pmid"]: i for i in items}
    ft_correct = [i["figure_type"]["choice"] == i["figure_type_gold"] for i in items]
    ft_conf = [i["figure_type"]["confidence"] for i in items]

    y: list[int] = []
    p: list[float] = []
    by_type: dict[str, list[tuple[int, float]]] = defaultdict(list)
    per_sig: dict[str, list[bool]] = defaultdict(list)
    per_fig: list[dict[str, Any]] = []
    for it in items:
        ok = []
        for t in it["taxa"]:
            y.append(int(t["gold_increased"]))
            p.append(t["p_yes"])
            by_type[it["figure_type_gold"]].append((int(t["gold_increased"]), t["p_yes"]))
            correct = (t["p_yes"] >= 0.5) == t["gold_increased"]
            ok.append(correct)
            per_sig[t["signature_id"]].append(correct)
        per_fig.append({"pmid": it["pmid"], "type": it["figure_type_gold"], "taxon_accuracy": sum(ok) / len(ok) if ok else None, "n": len(ok)})
    taxon_correct = [(s >= 0.5) == bool(t) for s, t in zip(p, y)]
    conf = [abs(s - 0.5) * 2 for s in p]
    return {
        "figure_type": {
            "accuracy": sum(ft_correct) / len(ft_correct),
            "n": len(ft_correct),
            "confusions": [(i["figure_type_gold"], i["figure_type"]["choice"]) for i in items if i["figure_type"]["choice"] != i["figure_type_gold"]],
            "coverage_at_confidence": coverage_at_confidence(ft_correct, ft_conf, (0.5, 0.7, 0.9)),
        },
        "direction": {
            "taxon_accuracy": sum(taxon_correct) / len(taxon_correct),
            "binary": binary_report(y, p),
            "by_figure_type": {
                k: {"n": len(v), "accuracy": sum((s >= 0.5) == bool(t) for t, s in v) / len(v)} for k, v in sorted(by_type.items())
            },
            "per_figure": per_fig,
            "signature_majority_correct": sum(sum(v) / len(v) > 0.5 for v in per_sig.values()) / len(per_sig),
            "coverage_at_confidence": coverage_at_confidence(taxon_correct, conf, (0.0, 0.5, 0.8, 0.9, 0.95)),
        },
        "orientation_answers": [
            {"pmid": i["pmid"], "answers": [o["choice"] for o in i.get("orientation", [])]} for i in items
        ],
    }
