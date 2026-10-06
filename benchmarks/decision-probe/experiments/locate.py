"""P2: is this table/figure a DA artifact, and which experiment does it belong to?

Unit: every table and figure in the cached evidence bundles of the smoke papers (legend / caption text;
the figure image is not used here because the HTML blob URL is unresolved for many bundles --
figure *type* from images is covered by ``figbench``).

Gold: an artifact is a DA artifact iff some gold signature's free-text ``source`` cites it
("Figure 2", "Fig. 1C", "Table 2"); supplementary citations are ignored (not in the bundle).
Gold-derived negatives are noisy: an artifact the curator did not cite might still hold DA results.

``belongs_to_experiment`` uses the **gold experiments as the option set** (oracle-stub condition: it
isolates the assignment judgment from S3's segmentation error) for papers with >= 2 gold experiments.
The gold set for an artifact is every experiment with a signature citing it; top-1 is correct when
the pick is in that set.

Baseline: the S5a regex (``curator.locate._DA_SIGNAL_RE``) as a binary predictor.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from probe_common import (
    PMC_MAP,
    RELATIONAL,
    REPO,
    binary_report,
    bounded_gather,
    coverage_at_confidence,
)

from bugsigdb_curation.curator.locate import _DA_SIGNAL_RE
from bugsigdb_curation.curator.smoke import smoke_study_ids
from bugsigdb_curation.decision import Choice, DecisionModel, Noul
from bugsigdb_curation.eval.gold import load_gold

CACHE = REPO / "data" / "decision-probe"
_SPLIT = re.compile(r"\s*(?:,|;|&|\band\b|\+)\s*", re.IGNORECASE)
_FIG = re.compile(r"\bfig(?:ure)?s?\.?\s*(\d+)", re.IGNORECASE)
_TAB = re.compile(r"\btable\s+(\d+)\b", re.IGNORECASE)
NONE_OPTION = "none"


def cited_artifacts(source: str | None) -> set[tuple[str, str]]:
    """{("figure"|"table", number)} for the *main-text* artifacts a source string cites."""
    cites: set[tuple[str, str]] = set()
    for piece in _SPLIT.split(source or ""):
        if re.search(r"supp|additional|\bS\d", piece, re.IGNORECASE):
            continue
        cites.update(("figure", n) for n in _FIG.findall(piece))
        cites.update(("table", n) for n in _TAB.findall(piece))
    return cites


def load_units() -> list[dict[str, Any]]:
    gold = load_gold(RELATIONAL, PMC_MAP)
    units = []
    for pmid in smoke_study_ids():
        bundle_path = CACHE / pmid / "bundle.json"
        if not bundle_path.exists() or pmid not in gold:
            continue
        bundle = json.loads(bundle_path.read_text())
        study = gold[pmid]
        cited_by: dict[tuple[str, str], set[str]] = defaultdict(set)
        for exp in study.experiments:
            for sig in exp.signatures:
                for key in cited_artifacts(sig.source):
                    cited_by[key].add(exp.experiment_id)
        exp_desc = {
            e.experiment_id: f"{e.group_0_name} vs {e.group_1_name} · {', '.join(e.body_site)} · {', '.join(e.condition)}"
            for e in study.experiments
        }
        arts = [("table", t["number"], t["label"], t["caption"], [" | ".join(r) for r in t["rows"][:8]]) for t in bundle["tables"]]
        arts += [("figure", f["number"], f["label"], f["legend"], []) for f in bundle["figures"]]
        for kind, number, label, text, rows in arts:
            if not number:
                continue
            gold_exps = sorted(cited_by.get((kind, number), set()))
            units.append(
                {
                    "id": f"{pmid}:{kind}{number}",
                    "pmid": pmid,
                    "kind": kind,
                    "title": bundle["title"],
                    "label": label,
                    "text": text[:3000],
                    "rows": rows,
                    "is_da": bool(gold_exps),
                    "gold_experiments": gold_exps,
                    "experiment_options": {k: v[:300] for k, v in exp_desc.items()} if len(exp_desc) >= 2 else {},
                    "regex_hit": bool(_DA_SIGNAL_RE.search(text or "") or _DA_SIGNAL_RE.search(label or "")),
                }
            )
    return units


async def run(model: DecisionModel) -> dict[str, Any]:
    units = load_units()

    async def one(u: dict[str, Any]) -> dict[str, Any]:
        state = {"paper": u["title"], "artifact": u["label"], "kind": u["kind"], "caption_or_legend": u["text"]}
        if u["rows"]:
            state["first_rows"] = u["rows"]
        qs: dict[str, Any] = {
            "is_da_artifact": Noul(
                "Does this table or figure report per-taxon differential-abundance results (taxa shown as higher or lower between groups)?",
                criteria={"true": "taxa are reported as differentially abundant between groups", "false": "no per-taxon differential-abundance result (e.g. diversity, composition, metadata)"},
            )
        }
        opts = u["experiment_options"]
        if opts and u["is_da"]:
            crit = dict(opts)
            crit[NONE_OPTION] = "this artifact does not belong to any listed experiment"
            qs["belongs_to_experiment"] = Choice("Which experiment (group comparison) does this artifact report results for?", crit)
        ans = await model.decide(stage=f"locate:{u['pmid']}", state=state, questions=qs)
        out = {k: u[k] for k in ("id", "pmid", "kind", "is_da", "regex_hit", "gold_experiments")} | {"p_da": ans["is_da_artifact"].p_yes}
        if "belongs_to_experiment" in ans:
            a = ans["belongs_to_experiment"]
            out |= {"pick": a.choice, "pick_conf": a.confidence, "n_options": len(opts)}
        return out

    return {"units": await bounded_gather(units, one, limit=6)}


def score(results: dict[str, Any]) -> dict[str, Any]:
    units = results["units"]
    y = [int(u["is_da"]) for u in units]
    p = [u["p_da"] for u in units]
    regex = [float(u["regex_hit"]) for u in units]
    out: dict[str, Any] = {"is_da": binary_report(y, p), "regex_baseline": binary_report(y, regex)}
    out["by_kind"] = {k: binary_report([int(u["is_da"]) for u in units if u["kind"] == k], [u["p_da"] for u in units if u["kind"] == k]) for k in ("table", "figure")}
    assign = [u for u in units if "pick" in u]
    if assign:
        ok = [u["pick"] in u["gold_experiments"] for u in assign]
        conf = [u["pick_conf"] for u in assign]
        single = [(o, u) for o, u in zip(ok, assign) if len(u["gold_experiments"]) == 1]
        out["assignment"] = {
            "n": len(assign),
            "top1_in_gold_set": sum(ok) / len(ok),
            "n_single_gold_experiment": len(single),
            "top1_accuracy_single": (sum(o for o, _ in single) / len(single)) if single else None,
            "mean_options": sum(u["n_options"] for u in assign) / len(assign),
            "coverage_at_confidence": coverage_at_confidence(ok, conf, (0.0, 0.5, 0.7, 0.9)),
            "none_picked": sum(u["pick"] == NONE_OPTION for u in assign),
        }
    return out
