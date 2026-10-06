"""Print the markdown metric tables for RESULTS.md from the scored summaries.

    uv run python benchmarks/decision-probe/report.py [date]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-10-06"
MODELS = ("clef", "clef-flash")


def load(exp: str, model: str) -> dict:
    return json.loads((HERE / "runs" / f"{DATE}_{model}" / f"{exp}.summary.json").read_text())


def f(x, nd=2):
    return "–" if x is None or x != x else f"{x:.{nd}f}"


def cost(s):
    c = s["cost"]
    return f"{c['input_tokens']:,} tok / {c['mean_latency_ms']:.0f} ms"


def usd(s, model):
    per_m = 0.24 if model == "clef" else 0.09
    return s["cost"]["input_tokens"] * per_m / 1e6


def p1(exps):
    print("| unit | model | n (pos) | AUROC | AUPRC | Brier | P / R @0.5 | R=1.0: τ, P, routed | cost |")
    print("|---|---|---|---|---|---|---|---|---|")
    for exp in exps:
        for m in MODELS:
            s = load(exp, m)
            h = s["has_da_results"]
            r = h.get("at_recall_1.0") or {}
            print(f"| {exp} | {m} | {h['n']} ({h['n_pos']}) | {f(h['auroc'])} | {f(h['auprc'])} | {f(h['brier'],3)} | "
                  f"{f(h['at_0.5']['precision'])} / {f(h['at_0.5']['recall'])} | "
                  f"{f(r.get('tau'),3)}, {f(r.get('precision'))}, {r.get('n_routed','–')}/{h['n']} | {cost(s)} |")


def p4(exps):
    print("| unit | model | n DA units | arity acc | multi recall | two_group recall | one-vs-rest TP / gold / FP | n_groups within ½ level |")
    print("|---|---|---|---|---|---|---|---|")
    for exp in exps:
        for m in MODELS:
            a = load(exp, m).get("arity")
            if not a:
                continue
            o = a["one_vs_rest_detection"]
            ng = load(exp, m).get("n_groups", {})
            print(f"| {exp} | {m} | {a['n']} | {f(a['accuracy'])} | {f(a['multi_group_recall'])} | {f(a['two_group_recall'])} | "
                  f"{o['true_positive']} / {o['gold_positive']} / {o['false_positive']} | {f(ng.get('within_half_level'))} |")


def main() -> None:
    print("### P1 – supplement screening (`has_da_results`)\n"); p1(["supp_files", "supp_sheets", "supp_pages", "supp_pages_txt"])
    print("\n### P4 – arity\n"); p4(["supp_sheets", "supp_pages", "supp_pages_txt", "supp_files"])
    print("\n### P2 – DA-artifact detection vs the S5a regex\n")
    print("| model | AUROC | AUPRC | P / R @0.5 | R=1.0: τ, P | regex P / R | cost |\n|---|---|---|---|---|---|---|")
    for m in MODELS:
        s = load("locate", m); h = s["is_da"]; rg = s["regex_baseline"]["at_0.5"]; r = h["at_recall_1.0"] or {}
        print(f"| {m} | {f(h['auroc'])} | {f(h['auprc'])} | {f(h['at_0.5']['precision'])} / {f(h['at_0.5']['recall'])} | {f(r.get('tau'),3)}, {f(r.get('precision'))} | {f(rg['precision'])} / {f(rg['recall'])} | {cost(s)} |")
    print("\n### P2 – figure type (15 figbench figures)\n\n| model | with image | without image |\n|---|---|---|")
    for m in MODELS:
        print(f"| {m} | {f(load('figbench', m)['figure_type']['accuracy'])} | {f(load('figbench_noimage', m)['figure_type']['accuracy'])} |")
    print("\n### P2 – artifact → experiment assignment (oracle-stub option sets)\n")
    print("| set | model | n | top-1 in gold set | accuracy at conf ≥0.1 (coverage) | options |\n|---|---|---|---|---|---|")
    for m in MODELS:
        a = load("locate", m).get("assignment")
        if a:
            print(f"| main-text artifacts (7) | {m} | {a['n']} | {f(a['top1_in_gold_set'])} | – | ~{a['mean_options']:.0f} |")
    for m in MODELS:
        s = load("locate_pages", m); c = next(c for c in s["coverage_at_confidence"] if c["tau"] == 0.1)
        print(f"| 34620922 DA pages | {m} | {s['n_pages_with_gold']} | {f(s['top1_in_gold_set'])} | {f(c['accuracy'])} ({f(c['coverage'])}) | 48 |")
    print("\n### P3 – direction (per-taxon `increased in group_1?`)\n")
    print("| set | model | taxa | accuracy | AUROC | signature-majority | acc at confidence ≥ 0.5 (coverage) | cost |\n|---|---|---|---|---|---|---|---|")
    for exp, label in (("figbench", "15 figbench figures (image)"), ("figbench_noimage", "same, image withheld (control)"),
                       ("direction_pages", "34620922 table pages, text"), ("direction_pages_img", "34620922 table pages, image")):
        for m in MODELS:
            s = load(exp, m)
            d = s.get("direction") or s
            b = d["binary"]; n = b["n"]
            c = next(c for c in d["coverage_at_confidence"] if c["tau"] == 0.5)
            sm = d.get("signature_majority_correct")
            print(f"| {label} | {m} | {n} | {f(d['taxon_accuracy'])} | {f(b['auroc'])} | {f(sm)} | {f(c['accuracy'])} ({f(c['coverage'])}) | {cost(s)} |")
    print("\n#### P3 by figure type (figbench, with image)\n\n| type | n taxa | clef | clef-flash |\n|---|---|---|---|")
    c, fl = load("figbench", "clef")["direction"]["by_figure_type"], load("figbench", "clef-flash")["direction"]["by_figure_type"]
    for k in c:
        print(f"| {k} | {c[k]['n']} | {f(c[k]['accuracy'])} | {f(fl[k]['accuracy'])} |")
    print("\n### P5 – ontology\n")
    print("| field | model | units | retrieval recall@10 | choice acc (gold present) | exp-weighted acc | none-of-these when absent | acc @conf ≥0.7 (coverage) | cost |\n|---|---|---|---|---|---|---|---|---|")
    for exp in ("ontology_body_site", "ontology_condition"):
        for m in MODELS:
            s = load(exp, m); c = next(c for c in s["coverage_at_confidence"] if c["tau"] == 0.7)
            print(f"| {exp.split('_',1)[1]} | {m} | {s['n_units']} | {f(s['retrieval_recall_at_10'],3)} | {f(s['choice_accuracy_given_present'],3)} | {f(s['choice_accuracy_weighted_by_experiments'],3)} | {f(s['none_of_these_rate_when_absent'])} | {f(c['accuracy'],3)} ({f(c['coverage'])}) | {cost(s)} |")
    print("\n### Cost summary (input tokens × list price; output is 0)\n\n| experiment | clef $ | clef-flash $ |\n|---|---|---|")
    exps = ["figbench", "figbench_noimage", "supp_files", "supp_sheets", "supp_pages", "supp_pages_txt", "locate", "locate_pages", "direction_pages", "direction_pages_img", "ontology_body_site", "ontology_condition"]
    tot = {m: 0.0 for m in MODELS}
    for e in exps:
        v = {m: usd(load(e, m), m) for m in MODELS}
        for m in MODELS: tot[m] += v[m]
        print(f"| {e} | {v['clef']:.4f} | {v['clef-flash']:.4f} |")
    print(f"| **total** | {tot['clef']:.3f} | {tot['clef-flash']:.3f} |")


main()
