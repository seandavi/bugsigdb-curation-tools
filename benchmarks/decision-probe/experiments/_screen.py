"""Shared P1/P4 machinery: screen a list of labelled units with the same three questions.

A *unit* is ``{"id", "state", "images", "label"}`` where ``label`` is the hand
label dict (``has_da_results`` bool, ``content_kind`` str, optional ``arity``
and ``n_groups``). Per unit we ask:

* ``has_da_results`` (noul) -- contains a per-taxon differential-abundance result;
* ``content_kind`` (choice);
* ``arity`` (choice) and ``n_groups`` (score) -- the P4 questions, asked for every
  unit but scored only on units whose gold label is a DA table.
"""

from __future__ import annotations

from typing import Any

from probe_common import binary_report, bounded_gather, coverage_at_confidence

from bugsigdb_curation.decision import Choice, DecisionModel, Noul, Score

CONTENT_KINDS = {
    "da_taxa_table": "a table of taxa with differential-abundance statistics (LDA score, fold change, p/q values)",
    "abundance_matrix": "a taxa x samples abundance / count matrix with no per-taxon test result",
    "diversity_or_ordination_stats": "alpha/beta diversity, ordination or PERMANOVA / mixed-model statistics",
    "sample_metadata": "sample, subject or site information",
    "methods_text": "prose: methods, results discussion, legends",
    "raw_or_sequence": "raw sequence data, variants, genotypes",
    "figure_or_image": "a figure or image",
    "other": "anything else, including summary counts of significant taxa",
}
ARITY = {
    "two_group": "each result table compares exactly two groups",
    "multi_group_one_vs_rest": "three or more groups compared at once; each group's enriched taxa are listed against all the others",
    "multi_group_all_pairwise": "three or more groups with every pairwise comparison reported separately",
    "not_a_comparison": "no group comparison is reported",
}
N_GROUPS = ("2 groups", "3 groups", "4 groups", "5 or more groups")

HAS_DA_Q = Noul(
    "Does this content contain a per-taxon differential-abundance result (for example LEfSe/LDA, fold change, "
    "or p/q-values for individual taxa)?",
    criteria={
        "true": "individual taxa are listed with a statistic showing they differ between groups",
        "false": "no per-taxon differential-abundance result",
    },
)


def questions() -> dict[str, Any]:
    return {
        "has_da_results": HAS_DA_Q,
        "content_kind": Choice("What kind of content is this?", CONTENT_KINDS),
        "arity": Choice("How many groups does the comparison in this content involve?", ARITY),
        "n_groups": Score("How many groups are being compared?", N_GROUPS),
    }


async def screen(model: DecisionModel, units: list[dict[str, Any]], *, stage: str, limit: int = 6) -> dict[str, Any]:
    async def one(unit: dict[str, Any]) -> dict[str, Any]:
        ans = await model.decide(stage=f"{stage}:{unit['id']}", state=unit["state"], questions=questions(), images=unit.get("images", ()))
        return {
            "id": unit["id"],
            "label": unit["label"],
            "p_da": ans["has_da_results"].p_yes,
            "content_kind": ans["content_kind"].choice,
            "content_kind_conf": ans["content_kind"].confidence,
            "arity": ans["arity"].choice,
            "arity_conf": ans["arity"].confidence,
            "n_groups": ans["n_groups"].score,
        }

    return {"units": await bounded_gather(units, one, limit=limit)}


def score(results: dict[str, Any]) -> dict[str, Any]:
    units = results["units"]
    y = [int(u["label"]["has_da_results"]) for u in units]
    p = [u["p_da"] for u in units]
    out: dict[str, Any] = {"has_da_results": binary_report(y, p)}
    # Pages/units the strong model would still read at the deployable threshold.
    deploy = out["has_da_results"].get("at_recall_0.95")
    if deploy:
        out["routed_at_recall_0.95"] = {"routed": deploy["n_routed"], "total": len(units)}
    kind_ok = [u["content_kind"] == u["label"]["content_kind"] for u in units]
    out["content_kind"] = {
        "accuracy": sum(kind_ok) / len(kind_ok),
        "n": len(kind_ok),
        "da_vs_rest_agreement": sum((u["content_kind"] == "da_taxa_table") == bool(u["label"]["has_da_results"]) for u in units) / len(units),
        "confusions": sorted({(u["label"]["content_kind"], u["content_kind"]) for u in units if u["content_kind"] != u["label"]["content_kind"]}),
    }
    da_units = [u for u in units if u["label"]["has_da_results"] and "arity" in u["label"]]
    if da_units:
        ok = [u["arity"] == u["label"]["arity"] for u in da_units]
        conf = [u["arity_conf"] for u in da_units]
        multi = [u for u in da_units if u["label"]["arity"].startswith("multi")]
        two = [u for u in da_units if u["label"]["arity"] == "two_group"]
        out["arity"] = {
            "n": len(da_units),
            "accuracy": sum(ok) / len(ok),
            "multi_group_recall": (sum(u["arity"].startswith("multi") for u in multi) / len(multi)) if multi else None,
            "two_group_recall": (sum(u["arity"] == "two_group" for u in two) / len(two)) if two else None,
            "coverage_at_confidence": coverage_at_confidence(ok, conf, (0.0, 0.5, 0.7, 0.9)),
            "confusions": sorted({(u["label"]["arity"], u["arity"]) for u in da_units if u["arity"] != u["label"]["arity"]}),
        }
        # The decision the pipeline actually needs: does this table need a one-vs-rest decomposition?
        gold_ovr = [u["label"]["arity"] == "multi_group_one_vs_rest" for u in da_units]
        pred_ovr = [u["arity"] == "multi_group_one_vs_rest" for u in da_units]
        tp = sum(g and q for g, q in zip(gold_ovr, pred_ovr))
        out["arity"]["one_vs_rest_detection"] = {
            "gold_positive": sum(gold_ovr),
            "true_positive": tp,
            "false_positive": sum(q and not g for g, q in zip(gold_ovr, pred_ovr)),
        }
        with_n = [u for u in da_units if u["label"].get("n_groups")]
        if with_n:
            err = [abs(u["n_groups"] - (min(u["label"]["n_groups"], 5) - 2)) for u in with_n]
            out["n_groups"] = {"n": len(with_n), "mean_abs_level_error": sum(err) / len(err), "within_half_level": sum(e <= 0.5 for e in err) / len(err)}
    return out
